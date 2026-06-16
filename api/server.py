"""
api/server.py
-------------
Lightweight FastAPI backend for ECO Ready imaging system.

Endpoints:
  GET  /                        → serves the web UI (index.html)
  GET  /api/cameras             → list of configured cameras
  GET  /api/dates               → list of dates with captures
  GET  /api/images              → list images, filter by date + camera_id
  GET  /api/images/latest       → latest image per camera
  GET  /image/{date}/{filename} → serve raw image file
  GET  /api/export              → ZIP download with manifest.csv
  GET  /api/status              → service health / storage stats
  POST /api/capture/{camera_id} → trigger immediate capture for one camera
  GET  /api/schedule            → get current cron schedule
  POST /api/schedule            → update cron schedule + reschedule live jobs
"""

import csv
import io
import json
import os
import zipfile
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import threading

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def get_base_path(cfg: dict) -> Path:
    return Path(os.environ.get("CAPTURE_BASE_PATH", cfg["storage"]["base_path"]))


class ScheduleUpdate(BaseModel):
    cron: str  # standard 5-field cron: "minute hour dom month dow"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ECO Ready Imaging API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_dates(base_path: Path) -> list[str]:
    if not base_path.exists():
        return []
    dates = []
    for d in sorted(base_path.iterdir(), reverse=True):
        if d.is_dir():
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                dates.append(d.name)
            except ValueError:
                pass
    return dates


def list_images(base_path: Path, for_date: str, camera_id: Optional[str] = None) -> list[dict]:
    date_dir = base_path / for_date
    if not date_dir.exists():
        return []
    images = sorted(date_dir.glob("*.jpg"))
    if camera_id:
        images = [p for p in images if p.stem.startswith(camera_id)]
    result = []
    for img in images:
        meta = load_sidecar(img)
        result.append({
            "filename": img.name,
            "date": for_date,
            "camera_id": meta.get("camera_id", ""),
            "camera_label": meta.get("camera_label", ""),
            "captured_at": meta.get("captured_at", ""),
            "size_kb": round(img.stat().st_size / 1024, 1),
            "url": f"/image/{for_date}/{img.name}",
        })
    return result


def load_sidecar(image_path: Path) -> dict:
    meta_path = image_path.with_suffix(".json")
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    # Fallback: parse from filename
    stem = image_path.stem
    return {
        "camera_id": stem.rsplit("_", 2)[0] if stem.count("_") >= 2 else stem,
        "camera_label": stem.rsplit("_", 2)[0] if stem.count("_") >= 2 else stem,
        "captured_at": None,
        "file": image_path.name,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status():
    cfg = load_config()
    base_path = get_base_path(cfg)
    dates = list_dates(base_path)

    total_images = 0
    total_bytes = 0
    if base_path.exists():
        for d in base_path.iterdir():
            if d.is_dir():
                for f in d.glob("*.jpg"):
                    total_images += 1
                    total_bytes += f.stat().st_size

    return {
        "status": "ok",
        "total_images": total_images,
        "storage_mb": round(total_bytes / (1024 * 1024), 1),
        "days_with_data": len(dates),
        "capture_path": str(base_path),
    }


@app.get("/api/cameras")
def cameras():
    cfg = load_config()
    return [
        {"id": c["id"], "label": c.get("label", c["id"]), "active": c.get("active", True)}
        for c in cfg["cameras"]
    ]


@app.get("/api/dates")
def dates():
    cfg = load_config()
    base_path = get_base_path(cfg)
    return list_dates(base_path)


@app.get("/api/images")
def images(
    date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    camera_id: Optional[str] = Query(None),
):
    cfg = load_config()
    base_path = get_base_path(cfg)

    if date:
        return list_images(base_path, date, camera_id)

    # No date specified — return today and yesterday
    results = []
    for d in list_dates(base_path)[:2]:
        results.extend(list_images(base_path, d, camera_id))
    return results


@app.get("/api/images/latest")
def images_latest():
    """Return the most recent image for each active camera."""
    cfg = load_config()
    base_path = get_base_path(cfg)
    all_dates = list_dates(base_path)
    cameras = [c for c in cfg["cameras"] if c.get("active", True)]

    result = []
    for cam in cameras:
        found = None
        for d in all_dates:
            imgs = list_images(base_path, d, cam["id"])
            if imgs:
                found = imgs[-1]
                break
        result.append({
            "camera_id": cam["id"],
            "camera_label": cam.get("label", cam["id"]),
            "latest": found,
        })
    return result


@app.get("/image/{date}/{filename}")
def serve_image(date: str, filename: str):
    cfg = load_config()
    base_path = get_base_path(cfg)
    image_path = base_path / date / filename

    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    if not image_path.suffix.lower() == ".jpg":
        raise HTTPException(status_code=400, detail="Only JPEG images supported")

    return FileResponse(image_path, media_type="image/jpeg")


@app.get("/api/export")
def export(
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    camera_id: Optional[str] = Query(None),
):
    cfg = load_config()
    base_path = get_base_path(cfg)

    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    if start > end:
        raise HTTPException(status_code=400, detail="date_from must be before date_to")

    # Collect images in range
    all_images = []
    d = start
    while d <= end:
        all_images.extend(list_images(base_path, d.strftime("%Y-%m-%d"), camera_id))
        d += timedelta(days=1)

    if not all_images:
        raise HTTPException(status_code=404, detail="No images found for this range")

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for img_meta in all_images:
            img_path = base_path / img_meta["date"] / img_meta["filename"]
            if img_path.exists():
                zf.write(img_path, img_meta["filename"])

        # CSV manifest
        csv_buf = io.StringIO()
        writer = csv.DictWriter(
            csv_buf,
            fieldnames=["filename", "camera_id", "camera_label", "captured_at", "size_kb"],
        )
        writer.writeheader()
        writer.writerows(all_images)
        zf.writestr("manifest.csv", csv_buf.getvalue())

    buf.seek(0)
    filename = f"ecoready_{date_from}_{date_to}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/capture/{camera_id}")
def trigger_capture(camera_id: str):
    """
    Trigger an immediate capture for one camera.
    Runs in a background thread so the request returns quickly.
    """
    cfg = load_config()
    camera = next((c for c in cfg["cameras"] if c["id"] == camera_id), None)
    if camera is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
    if not camera.get("active", True):
        raise HTTPException(status_code=400, detail=f"Camera '{camera_id}' is inactive")

    # Import here to avoid circular dependency if API and capture run in same process
    from capture.service import capture_camera

    thread = threading.Thread(target=capture_camera, args=(camera_id,), daemon=True)
    thread.start()

    return {"status": "triggered", "camera_id": camera_id, "label": camera.get("label", camera_id)}


@app.get("/api/schedule")
def get_schedule():
    cfg = load_config()
    cron = cfg["schedule"]["cron"]
    parts = cron.split()
    return {
        "cron": cron,
        "fields": {
            "minute": parts[0],
            "hour": parts[1],
            "dom": parts[2],
            "month": parts[3],
            "dow": parts[4],
        },
    }


@app.post("/api/schedule")
def update_schedule(body: ScheduleUpdate):
    """
    Update the cron schedule. Persists to config.yaml and attempts to
    reschedule live APScheduler jobs if the scheduler is reachable.
    """
    # Validate: must be 5 fields
    parts = body.cron.strip().split()
    if len(parts) != 5:
        raise HTTPException(status_code=400, detail="Cron must have exactly 5 fields: minute hour dom month dow")

    # Persist to config
    cfg = load_config()
    old_cron = cfg["schedule"]["cron"]
    cfg["schedule"]["cron"] = body.cron.strip()
    save_config(cfg)

    # Attempt live reschedule via APScheduler jobstore
    rescheduled = []
    skipped = []
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

        JOBSTORE_PATH = Path(os.environ.get("JOBSTORE_PATH", "data/scheduler.db"))
        if JOBSTORE_PATH.exists():
            jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{JOBSTORE_PATH}")}
            sched = BackgroundScheduler(jobstores=jobstores, timezone="UTC")
            sched.start()

            minute, hour, dom, month, dow = parts
            for cam in cfg["cameras"]:
                if not cam.get("active", True):
                    continue
                job_id = f"capture_{cam['id']}"
                job = sched.get_job(job_id)
                if job:
                    sched.reschedule_job(
                        job_id,
                        trigger="cron",
                        minute=minute, hour=hour,
                        day=dom, month=month, day_of_week=dow,
                    )
                    rescheduled.append(cam["id"])
                else:
                    skipped.append(cam["id"])

            sched.shutdown(wait=False)
    except Exception as e:
        # Non-fatal — config is already saved, capture service will pick it up on restart
        return {
            "status": "saved",
            "cron": body.cron.strip(),
            "warning": f"Config saved but live reschedule failed: {str(e)}. Restart capture service to apply.",
            "rescheduled": [],
        }

    return {
        "status": "ok",
        "cron": body.cron.strip(),
        "previous_cron": old_cron,
        "rescheduled": rescheduled,
        "skipped_not_found": skipped,
    }


# ---------------------------------------------------------------------------
# Serve frontend — must be last
# ---------------------------------------------------------------------------

UI_PATH = Path(os.environ.get("UI_PATH", "ui/static"))

if UI_PATH.exists():
    app.mount("/", StaticFiles(directory=UI_PATH, html=True), name="static")
else:
    @app.get("/")
    def root():
        return JSONResponse({"message": "ECO Ready API running. UI not found — set UI_PATH."})

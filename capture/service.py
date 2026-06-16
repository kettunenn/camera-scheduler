"""
capture/service.py
------------------
Capture scheduler for ECO Ready imaging pipeline.

Responsibilities:
  - Hourly (configurable) frame grab from each active TAPO camera via ffmpeg/RTSP
  - Writes images to disk: base_path/YYYY-MM-DD/<camera_id>_<timestamp>.jpg
  - Writes a sidecar metadata JSON per image
  - Retries on failure, sends email alert if all retries exhausted
  - Persists schedule across reboots via APScheduler SQLite jobstore
"""

import json
import logging
import os
import smtplib
import subprocess
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH = Path(os.environ.get("LOG_PATH", "logs/capture.log"))
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ecoready.capture")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Alert state (in-memory cooldown tracking)
# ---------------------------------------------------------------------------

_last_alert: dict[str, datetime] = {}


def _should_alert(camera_id: str, cooldown_minutes: int) -> bool:
    now = datetime.now(timezone.utc)
    last = _last_alert.get(camera_id)
    if last is None:
        return True
    elapsed = (now - last).total_seconds() / 60
    return elapsed >= cooldown_minutes


def send_alert(cfg: dict, camera_id: str, camera_label: str, reason: str) -> None:
    alert_cfg = cfg.get("alerts", {})
    if not alert_cfg.get("enabled", False):
        return

    cooldown = alert_cfg.get("alert_cooldown_minutes", 60)
    if not _should_alert(camera_id, cooldown):
        log.debug(f"Alert suppressed for {camera_id} (within cooldown)")
        return

    try:
        msg = EmailMessage()
        msg["Subject"] = f"[ECO Ready] Camera failure: {camera_label}"
        msg["From"] = alert_cfg["from_addr"]
        msg["To"] = ", ".join(alert_cfg["to_addrs"])
        msg.set_content(
            f"Camera capture failed.\n\n"
            f"Camera ID : {camera_id}\n"
            f"Label     : {camera_label}\n"
            f"Time      : {datetime.now().isoformat()}\n"
            f"Reason    : {reason}\n\n"
            f"Check the capture log for details: {LOG_PATH}"
        )

        with smtplib.SMTP(alert_cfg["smtp_host"], alert_cfg["smtp_port"]) as smtp:
            smtp.starttls()
            smtp.login(alert_cfg["smtp_user"], alert_cfg["smtp_password"])
            smtp.send_message(msg)

        _last_alert[camera_id] = datetime.now(timezone.utc)
        log.info(f"Alert sent for camera {camera_id}")

    except Exception as e:
        log.error(f"Failed to send alert for {camera_id}: {e}")


# ---------------------------------------------------------------------------
# Frame capture
# ---------------------------------------------------------------------------

def build_rtsp_url(camera: dict, stream: str) -> str:
    return (
        f"rtsp://{camera['user']}:{camera['password']}"
        f"@{camera['ip']}:554/{stream}"
    )


def capture_frame(camera: dict, output_path: Path, capture_cfg: dict) -> bool:
    """
    Grab a single frame from the camera's RTSP stream using ffmpeg.
    Returns True on success, False on failure.

    Uses TCP transport to avoid UDP packet loss / corruption issues
    common with TAPO cameras on busy WiFi networks.
    """
    rtsp_url = build_rtsp_url(camera, capture_cfg.get("stream", "stream1"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", str(capture_cfg.get("jpeg_quality", 2)),
        str(output_path),
    ]

    timeout = capture_cfg.get("timeout_seconds", 15)

    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning(
                f"ffmpeg exited {result.returncode} for {camera['id']}: "
                f"{result.stderr.strip()}"
            )
            return False
        if not output_path.exists() or output_path.stat().st_size == 0:
            log.warning(f"ffmpeg produced empty/no output for {camera['id']}")
            return False
        return True

    except subprocess.TimeoutExpired:
        log.warning(f"ffmpeg timed out ({timeout}s) for {camera['id']}")
        return False
    except FileNotFoundError:
        log.error("ffmpeg not found — install ffmpeg on this system")
        return False


def write_metadata(image_path: Path, camera: dict, captured_at: datetime) -> None:
    """Write a sidecar .json file next to the image with capture metadata."""
    meta = {
        "camera_id": camera["id"],
        "camera_label": camera.get("label", camera["id"]),
        "captured_at": captured_at.isoformat(),
        "file": image_path.name,
    }
    meta_path = image_path.with_suffix(".json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)


# ---------------------------------------------------------------------------
# Per-camera capture job
# ---------------------------------------------------------------------------

def capture_camera(camera_id: str) -> None:
    """
    Entry point called by APScheduler for each camera.
    Reloads config on each invocation so changes take effect without restart.
    """
    cfg = load_config()
    camera = next((c for c in cfg["cameras"] if c["id"] == camera_id), None)

    if camera is None:
        log.error(f"Camera {camera_id} not found in config")
        return
    if not camera.get("active", True):
        log.debug(f"Camera {camera_id} is inactive, skipping")
        return

    capture_cfg = cfg.get("capture", {})
    base_path = Path(os.environ.get("CAPTURE_BASE_PATH", cfg["storage"]["base_path"]))
    retries = capture_cfg.get("retries", 2)
    retry_delay = capture_cfg.get("retry_delay_seconds", 10)

    captured_at = datetime.now()
    date_str = captured_at.strftime("%Y-%m-%d")
    ts_str = captured_at.strftime("%Y%m%d_%H%M%S")
    output_path = base_path / date_str / f"{camera_id}_{ts_str}.jpg"

    log.info(f"Capturing {camera['label']} → {output_path}")

    success = False
    last_error = "unknown"

    for attempt in range(1, retries + 2):  # +2: retries=2 means 3 total attempts
        success = capture_frame(camera, output_path, capture_cfg)
        if success:
            write_metadata(output_path, camera, captured_at)
            log.info(f"✓ {camera['label']} captured ({output_path.stat().st_size // 1024} KB)")
            break
        last_error = f"All ffmpeg attempts failed (attempt {attempt})"
        if attempt <= retries:
            log.info(f"Retrying {camera['label']} in {retry_delay}s (attempt {attempt}/{retries + 1})")
            time.sleep(retry_delay)

    if not success:
        log.error(f"✗ {camera['label']} failed after {retries + 1} attempts")
        send_alert(cfg, camera_id, camera.get("label", camera_id), last_error)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

JOBSTORE_PATH = Path(os.environ.get("JOBSTORE_PATH", "data/scheduler.db"))


def build_scheduler(cfg: dict) -> BlockingScheduler:
    JOBSTORE_PATH.parent.mkdir(parents=True, exist_ok=True)

    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{JOBSTORE_PATH}")
    }
    scheduler = BlockingScheduler(jobstores=jobstores, timezone="UTC")

    cron_expr = cfg["schedule"]["cron"]
    # Parse "minute hour dom month dow" into APScheduler kwargs
    minute, hour, dom, month, dow = cron_expr.split()

    for camera in cfg["cameras"]:
        if not camera.get("active", True):
            continue

        job_id = f"capture_{camera['id']}"

        # Remove existing job so config changes (e.g. new cron) take effect
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        scheduler.add_job(
            capture_camera,
            trigger="cron",
            id=job_id,
            name=f"Capture — {camera.get('label', camera['id'])}",
            args=[camera["id"]],
            minute=minute,
            hour=hour,
            day=dom,
            month=month,
            day_of_week=dow,
            misfire_grace_time=300,   # 5 min: run even if node was briefly down
            coalesce=True,            # don't pile up if multiple misfires
            replace_existing=True,
        )
        log.info(f"Scheduled {camera['label']} — cron: {cron_expr}")

    return scheduler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("ECO Ready capture service starting")
    cfg = load_config()
    scheduler = build_scheduler(cfg)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Capture service stopped")


if __name__ == "__main__":
    main()

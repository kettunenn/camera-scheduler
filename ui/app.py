"""
ui/app.py
---------
Streamlit dashboard for ECO Ready image data.

Features:
  - Browse captures by date and camera
  - View latest capture per camera
  - Download individual images or bulk export (ZIP + CSV manifest)
  - Simple, non-technical UI
"""

import csv
import io
import json
import os
import zipfile
from datetime import datetime, date, timedelta
from pathlib import Path

import streamlit as st
import yaml
from PIL import Image

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))


@st.cache_resource
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_base_path(cfg: dict) -> Path:
    return Path(os.environ.get("CAPTURE_BASE_PATH", cfg["storage"]["base_path"]))


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def list_dates(base_path: Path) -> list[date]:
    """Return all dates that have capture data, newest first."""
    dates = []
    for d in sorted(base_path.iterdir(), reverse=True):
        if d.is_dir():
            try:
                dates.append(datetime.strptime(d.name, "%Y-%m-%d").date())
            except ValueError:
                pass
    return dates


def list_captures(base_path: Path, for_date: date, camera_id: str | None = None) -> list[Path]:
    """Return sorted list of JPEG paths for a given date, optionally filtered by camera."""
    date_dir = base_path / for_date.strftime("%Y-%m-%d")
    if not date_dir.exists():
        return []
    images = sorted(date_dir.glob("*.jpg"))
    if camera_id:
        images = [p for p in images if p.stem.startswith(camera_id)]
    return images


def load_metadata(image_path: Path) -> dict:
    meta_path = image_path.with_suffix(".json")
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    # Fallback: parse from filename
    stem = image_path.stem  # e.g. cam_growbed_a1_20240315_143000
    parts = stem.rsplit("_", 2)
    return {
        "camera_id": parts[0] if len(parts) >= 3 else stem,
        "camera_label": parts[0] if len(parts) >= 3 else stem,
        "captured_at": None,
        "file": image_path.name,
    }


def latest_capture_per_camera(base_path: Path, cameras: list[dict]) -> dict[str, Path | None]:
    """Find the most recent image for each camera across all dates."""
    result = {}
    all_dates = list_dates(base_path)
    for cam in cameras:
        found = None
        for d in all_dates:
            images = list_captures(base_path, d, cam["id"])
            if images:
                found = images[-1]
                break
        result[cam["id"]] = found
    return result


def build_zip_export(images: list[Path]) -> tuple[bytes, list[dict]]:
    """Build a ZIP of images + CSV manifest, return as bytes."""
    buf = io.BytesIO()
    manifest_rows = []

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for img_path in images:
            meta = load_metadata(img_path)
            zf.write(img_path, img_path.name)
            manifest_rows.append({
                "file": img_path.name,
                "camera_id": meta.get("camera_id", ""),
                "camera_label": meta.get("camera_label", ""),
                "captured_at": meta.get("captured_at", ""),
            })

        # Write CSV manifest inside the ZIP
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["file", "camera_id", "camera_label", "captured_at"])
        writer.writeheader()
        writer.writerows(manifest_rows)
        zf.writestr("manifest.csv", csv_buf.getvalue())

    return buf.getvalue(), manifest_rows


# ---------------------------------------------------------------------------
# Page: Overview (latest per camera)
# ---------------------------------------------------------------------------

def page_overview(cfg: dict, base_path: Path) -> None:
    st.header("Latest Captures")
    st.caption("Most recent image from each active camera.")

    cameras = [c for c in cfg["cameras"] if c.get("active", True)]
    latest = latest_capture_per_camera(base_path, cameras)

    cols = st.columns(min(len(cameras), 3))

    for i, cam in enumerate(cameras):
        img_path = latest.get(cam["id"])
        with cols[i % 3]:
            st.subheader(cam["label"])
            if img_path:
                meta = load_metadata(img_path)
                captured_at = meta.get("captured_at", "")
                if captured_at:
                    ts = datetime.fromisoformat(captured_at)
                    st.caption(ts.strftime("%d %b %Y, %H:%M"))
                else:
                    st.caption(img_path.stem)
                st.image(str(img_path), use_container_width=True)
                with open(img_path, "rb") as f:
                    st.download_button(
                        "⬇ Download",
                        f.read(),
                        file_name=img_path.name,
                        mime="image/jpeg",
                        key=f"dl_latest_{cam['id']}",
                    )
            else:
                st.info("No captures yet")


# ---------------------------------------------------------------------------
# Page: Browse by date
# ---------------------------------------------------------------------------

def page_browse(cfg: dict, base_path: Path) -> None:
    st.header("Browse Captures")

    cameras = [c for c in cfg["cameras"] if c.get("active", True)]
    all_dates = list_dates(base_path)

    if not all_dates:
        st.info("No captures found. The service may not have run yet.")
        return

    col1, col2 = st.columns(2)
    with col1:
        selected_date = st.selectbox(
            "Date",
            options=all_dates,
            format_func=lambda d: d.strftime("%d %b %Y"),
        )
    with col2:
        cam_options = ["All cameras"] + [c["label"] for c in cameras]
        selected_cam_label = st.selectbox("Camera", options=cam_options)

    selected_cam_id = None
    if selected_cam_label != "All cameras":
        selected_cam_id = next(
            (c["id"] for c in cameras if c["label"] == selected_cam_label), None
        )

    images = list_captures(base_path, selected_date, selected_cam_id)

    if not images:
        st.info("No captures for this selection.")
        return

    st.caption(f"{len(images)} image(s) found")

    # Bulk export
    if st.button(f"⬇ Export all {len(images)} images as ZIP"):
        with st.spinner("Building export..."):
            zip_bytes, manifest = build_zip_export(images)
        label = selected_cam_label.replace(" ", "_").lower()
        filename = f"ecoready_{selected_date}_{label}.zip"
        st.download_button(
            "Download ZIP",
            zip_bytes,
            file_name=filename,
            mime="application/zip",
        )
        st.success(f"ZIP ready — {len(manifest)} images + manifest.csv")

    st.divider()

    # Image grid
    cols = st.columns(3)
    for i, img_path in enumerate(images):
        meta = load_metadata(img_path)
        with cols[i % 3]:
            captured_at = meta.get("captured_at", "")
            if captured_at:
                ts = datetime.fromisoformat(captured_at)
                label = f"{meta.get('camera_label', '')} · {ts.strftime('%H:%M')}"
            else:
                label = img_path.stem
            st.caption(label)
            st.image(str(img_path), use_container_width=True)
            with open(img_path, "rb") as f:
                st.download_button(
                    "⬇",
                    f.read(),
                    file_name=img_path.name,
                    mime="image/jpeg",
                    key=f"dl_{img_path.stem}",
                )


# ---------------------------------------------------------------------------
# Page: Export
# ---------------------------------------------------------------------------

def page_export(cfg: dict, base_path: Path) -> None:
    st.header("Export Data")
    st.write("Download images and a CSV manifest for a date range.")

    all_dates = list_dates(base_path)
    if not all_dates:
        st.info("No data available yet.")
        return

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From", value=all_dates[-1])
    with col2:
        end_date = st.date_input("To", value=all_dates[0])

    cameras = [c for c in cfg["cameras"] if c.get("active", True)]
    cam_options = ["All cameras"] + [c["label"] for c in cameras]
    selected_cam_label = st.selectbox("Camera", options=cam_options)
    selected_cam_id = None
    if selected_cam_label != "All cameras":
        selected_cam_id = next(
            (c["id"] for c in cameras if c["label"] == selected_cam_label), None
        )

    if start_date > end_date:
        st.error("Start date must be before end date.")
        return

    # Collect all images in range
    all_images = []
    d = start_date
    while d <= end_date:
        all_images.extend(list_captures(base_path, d, selected_cam_id))
        d += timedelta(days=1)

    st.info(f"{len(all_images)} images in selected range.")

    if all_images and st.button("Build export ZIP"):
        with st.spinner(f"Packaging {len(all_images)} images..."):
            zip_bytes, manifest = build_zip_export(all_images)
        filename = f"ecoready_export_{start_date}_{end_date}.zip"
        st.download_button(
            "⬇ Download ZIP",
            zip_bytes,
            file_name=filename,
            mime="application/zip",
        )


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    base_path = get_base_path(cfg)

    st.set_page_config(
        page_title=cfg["ui"].get("title", "ECO Ready"),
        page_icon="🌱",
        layout="wide",
    )

    st.title("🌱 " + cfg["ui"].get("title", "ECO Ready — Imaging Dashboard"))

    # Storage stats in sidebar
    with st.sidebar:
        st.header("System")
        if base_path.exists():
            all_dates = list_dates(base_path)
            total_images = sum(
                len(list(d.glob("*.jpg")))
                for d in base_path.iterdir()
                if d.is_dir()
            )
            total_mb = sum(
                f.stat().st_size
                for d in base_path.iterdir()
                if d.is_dir()
                for f in d.glob("*.jpg")
            ) / (1024 * 1024)
            st.metric("Total captures", total_images)
            st.metric("Storage used", f"{total_mb:.1f} MB")
            st.metric("Days with data", len(all_dates))
        else:
            st.warning(f"Capture path not found:\n`{base_path}`")

        st.divider()
        active_cams = [c for c in cfg["cameras"] if c.get("active", True)]
        st.caption(f"**{len(active_cams)} active camera(s)**")
        for cam in active_cams:
            st.caption(f"• {cam['label']}")

    # Navigation
    page = st.radio(
        "Navigate",
        ["Latest", "Browse", "Export"],
        horizontal=True,
        label_visibility="collapsed",
    )

    st.divider()

    if page == "Latest":
        page_overview(cfg, base_path)
    elif page == "Browse":
        page_browse(cfg, base_path)
    elif page == "Export":
        page_export(cfg, base_path)


if __name__ == "__main__":
    main()

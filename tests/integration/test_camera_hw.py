"""Integration tests against real camera hardware.

Run:  pytest tests/integration -v

Every attached camera is detected and tested; the suite fails if no camera is attached.
`config/config.toml` (or $WATERBUOY_CONFIG) is also checked: every camera it lists must work.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from waterbuoy.app import BuoyApp
from waterbuoy.camera import Picamera2Camera, RpicamStillCamera, UsbCamera, make_camera, make_cameras
from waterbuoy.config import Config, StorageConfig, load_config
from waterbuoy.storage import ImageStore

from .conftest import camera_config, csi_config, usb_config

CONFIG = Path(os.environ.get("WATERBUOY_CONFIG", Path(__file__).parents[2] / "config" / "config.toml"))


def assert_valid_jpeg(path, size=None):
    assert path.exists(), f"{path} was not written"
    assert path.stat().st_size > 5_000, "image suspiciously small"
    with Image.open(path) as img:
        assert img.format == "JPEG"
        if size:
            assert img.size == size
        img.verify()  # detects truncated/corrupt data


def make_app(cams, tmp_path):
    cfg = Config(cameras=[c.cfg for c in cams], storage=StorageConfig(base_dir=tmp_path, min_free_mb=0))
    return BuoyApp(cfg, cameras=cams, store=ImageStore(tmp_path, min_free_mb=0))


# =========================================================================== any camera

def test_at_least_one_camera_attached(attached_cameras):
    assert attached_cameras


def test_capture_from_every_attached_camera(cam_info, tmp_path):
    cam = make_camera(camera_config(cam_info, "cam"))
    try:
        out = tmp_path / "shot.jpg"
        cam.capture(out)
    finally:
        cam.close()
    assert_valid_jpeg(out)
    with Image.open(out) as img:
        stddev = ImageStat.Stat(img.convert("L")).stddev[0]
    assert stddev > 2, f"image is nearly uniform (stddev={stddev:.2f}): lens cap or dead stream?"


def test_configured_cameras_work(attached_cameras, tmp_path):
    """Every camera in the deployment config must be present and produce an image."""
    if not CONFIG.exists():
        pytest.skip(f"{CONFIG} not found (set WATERBUOY_CONFIG)")
    cfg = load_config(CONFIG)
    cams = make_cameras([replace(c, warmup_frames=min(c.warmup_frames, 10)) for c in cfg.cameras])
    app = make_app(cams, tmp_path)
    try:
        app.capture_once(trigger="test")
    finally:
        app.shutdown()
    failed = {n: s["last_error"] for n, s in app.camera_stats.items() if s["errors"]}
    assert not failed, f"configured cameras failed: {failed}"
    assert len(list(tmp_path.rglob("*.jpg"))) == len(cfg.cameras)


# =========================================================================== multiple cameras

def test_all_cameras_in_one_app(attached_cameras, tmp_path):
    """Every attached camera, plus the first one a second time under another name (a USB
    camera is released between shots, so this exercises multi-camera handling even on a
    buoy with a single physical camera)."""
    cfgs = [camera_config(c, f"cam{i}") for i, c in enumerate(attached_cameras)]
    first_usb = next((c for c in attached_cameras if c["backend"] == "usb"), None)
    if first_usb:
        cfgs.append(usb_config(first_usb, "again"))
    app = make_app(make_cameras(cfgs), tmp_path)
    before = datetime.now(timezone.utc)
    try:
        app.capture_once(trigger="test")
        app.capture_once(trigger="test")
    finally:
        app.shutdown()

    assert app.error_count == 0, {n: s["last_error"] for n, s in app.camera_stats.items()}
    assert app.capture_count == 2 * len(cfgs)
    images = sorted(tmp_path.rglob("*.jpg"))
    assert len(images) == 2 * len(cfgs)
    assert not list(tmp_path.rglob("*.part")), "temporary file left behind"
    assert {p.parent for p in images} == {tmp_path / f"{before:%Y}" / f"{before:%m}" / f"{before:%d}"}
    for c in cfgs:
        mine = [p for p in images if p.stem.split("_")[1] == c.name]
        assert len(mine) == 2, f"camera {c.name}: {mine}"
    for img in images:
        assert_valid_jpeg(img)


def test_unplugged_camera_does_not_stop_the_others(usb_info, tmp_path):
    good = UsbCamera(usb_config(usb_info, "good"))
    gone = UsbCamera(usb_config(usb_info, "gone"))
    gone.cfg = replace(gone.cfg, device="/dev/video-unplugged")  # vanished after start-up
    app = make_app([gone, good], tmp_path)
    app.capture_once()
    app.shutdown()
    assert app.camera_stats["gone"]["errors"] == 1
    assert "USB capture failed" in app.camera_stats["gone"]["last_error"]
    assert app.camera_stats["good"]["captures"] == 1
    assert [p.stem.split("_")[1] for p in tmp_path.rglob("*.jpg*")] == ["good"]


def test_missing_configured_camera_stops_startup(usb_info):
    with pytest.raises(RuntimeError, match="no device at /dev/video-missing"):
        make_cameras([usb_config(usb_info, "ok"), usb_config(usb_info, "missing", device="/dev/video-missing")])


# =========================================================================== USB webcams

def test_usb_every_format(usb_format, tmp_path):
    """Each pixel format the camera offers (MJPEG, raw YUYV, ...) produces a correct image."""
    info, fmt = usb_format
    sizes = info["formats"][fmt]
    w, h = min(sizes, key=lambda s: abs(s[0] * s[1] - 640 * 480)) if sizes else (640, 480)
    cam = UsbCamera(usb_config(info, input_format=fmt, width=w, height=h))
    assert cam.pix_fmt == fmt
    out = tmp_path / "shot.jpg"
    cam.capture(out)
    assert_valid_jpeg(out, (w, h))


def test_usb_auto_picks_largest_resolution(usb_info, tmp_path):
    cam = UsbCamera(usb_config(usb_info, width=0, height=0))
    largest = max(((w, h) for s in usb_info["formats"].values() for w, h in s or []), key=lambda s: s[0] * s[1])
    assert (cam.width, cam.height) == largest
    out = tmp_path / "max.jpg"
    cam.capture(out)
    assert_valid_jpeg(out, largest)


def test_usb_consecutive_captures(usb_info, tmp_path):
    """The device is released after each shot, so it can be reopened immediately."""
    cam = UsbCamera(usb_config(usb_info))
    paths = [tmp_path / f"shot{i}.jpg" for i in range(3)]
    t0 = time.monotonic()
    for p in paths:
        cam.capture(p)
    elapsed = time.monotonic() - t0
    for p in paths:
        assert_valid_jpeg(p, (cam.width, cam.height))
    assert elapsed < 45, f"3 captures took {elapsed:.1f}s"


def test_usb_respects_jpeg_quality(usb_info, tmp_path):
    sizes = {}
    for q in (30, 95):
        out = tmp_path / f"q{q}.jpg"
        UsbCamera(usb_config(usb_info, jpeg_quality=q)).capture(out)
        sizes[q] = out.stat().st_size
    assert sizes[95] > sizes[30]


def test_usb_unsupported_resolution_rejected_at_startup(usb_info):
    with pytest.raises(RuntimeError, match="does not support 4321x1234"):
        UsbCamera(usb_config(usb_info, width=4321, height=1234))


def test_usb_metadata_node_rejected(usb_info):
    """UVC cameras also expose a metadata node (index 1) that cannot capture images."""
    node = Path("/sys/class/video4linux") / Path(usb_info["node"]).name
    meta = [d for d in node.parent.glob("video*")
            if (d / "device").resolve() == (node / "device").resolve() and (d / "index").read_text().strip() != "0"]
    if not meta:
        pytest.skip("camera has no metadata node")
    with pytest.raises(RuntimeError, match="not a usable V4L2 capture device"):
        UsbCamera(usb_config(usb_info, device=f"/dev/{meta[0].name}"))


# =========================================================================== Pi ribbon cameras

def test_picamera2_capture(csi_info, tmp_path):
    cam = Picamera2Camera(csi_config(csi_info, "picamera2"))
    try:
        cam.capture(tmp_path / "shot.jpg")
    finally:
        cam.close()
    assert_valid_jpeg(tmp_path / "shot.jpg", (1280, 720))


def test_picamera2_select_by_index(csi_info, tmp_path):
    cam = Picamera2Camera(csi_config(csi_info, "picamera2", device="", index=csi_info["index"]))
    try:
        assert cam.csi["device"] == csi_info["device"]
    finally:
        cam.close()


@pytest.mark.skipif(shutil.which("rpicam-still") is None, reason="rpicam-still not installed")
def test_rpicam_still_capture(csi_info, tmp_path):
    out = tmp_path / "shot.jpg"
    RpicamStillCamera(csi_config(csi_info, "rpicam-still")).capture(out)
    assert_valid_jpeg(out, (1280, 720))


def test_ribbon_camera_index_out_of_range():
    from waterbuoy.config import CameraConfig

    with pytest.raises(RuntimeError, match="no Raspberry Pi camera with index 9"):
        Picamera2Camera(CameraConfig(name="x", backend="picamera2", index=9))

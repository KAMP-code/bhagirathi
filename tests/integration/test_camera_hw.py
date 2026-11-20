"""Integration tests against real camera hardware.

Run:   pytest tests/integration -v
Force: WATERBUOY_REQUIRE_CAMERA=1 pytest tests/integration   (fail if camera missing)
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from PIL import Image

from waterbuoy.app import BuoyApp
from waterbuoy.camera import Picamera2Camera, RpicamStillCamera, make_camera
from waterbuoy.config import Config, StorageConfig
from waterbuoy.storage import ImageStore

from .conftest import TEST_HEIGHT, TEST_WIDTH


def assert_valid_jpeg(path, width=TEST_WIDTH, height=TEST_HEIGHT):
    assert path.exists(), f"{path} was not written"
    assert path.stat().st_size > 10_000, "image suspiciously small"
    with Image.open(path) as img:
        assert img.format == "JPEG"
        assert img.size == (width, height)
        img.verify()  # detects truncated/corrupt data


# --------------------------------------------------------------------------- picamera2

@pytest.fixture
def picam(capture_cfg):
    cam = Picamera2Camera(replace(capture_cfg, backend="picamera2"))
    yield cam
    cam.close()


@pytest.mark.camera
def test_picamera2_single_capture(picam, tmp_path):
    out = tmp_path / "shot.jpg"
    picam.capture(out)
    assert_valid_jpeg(out)


@pytest.mark.camera
def test_picamera2_consecutive_captures_reuse_open_camera(picam, tmp_path):
    paths = [tmp_path / f"shot{i}.jpg" for i in range(3)]
    t0 = time.monotonic()
    for p in paths:
        picam.capture(p)
    elapsed = time.monotonic() - t0
    for p in paths:
        assert_valid_jpeg(p)
    # Camera stays open between shots, so repeated captures should be quick.
    assert elapsed < 15, f"3 captures took {elapsed:.1f}s"


@pytest.mark.camera
def test_picamera2_respects_jpeg_quality(capture_cfg, tmp_path):
    sizes = {}
    for q in (30, 95):
        cam = Picamera2Camera(replace(capture_cfg, backend="picamera2", jpeg_quality=q))
        try:
            out = tmp_path / f"q{q}.jpg"
            cam.capture(out)
            assert_valid_jpeg(out)
            sizes[q] = out.stat().st_size
        finally:
            cam.close()
    assert sizes[95] > sizes[30]


@pytest.mark.camera
def test_picamera2_can_reopen_after_close(capture_cfg, tmp_path):
    cfg = replace(capture_cfg, backend="picamera2")
    for i in range(2):
        cam = Picamera2Camera(cfg)
        try:
            cam.capture(tmp_path / f"r{i}.jpg")
        finally:
            cam.close()
        assert_valid_jpeg(tmp_path / f"r{i}.jpg")


# --------------------------------------------------------------------------- rpicam-still

@pytest.mark.camera
@pytest.mark.rpicam
def test_rpicam_still_single_capture(capture_cfg, tmp_path):
    cam = RpicamStillCamera(replace(capture_cfg, backend="rpicam-still"))
    out = tmp_path / "shot.jpg"
    cam.capture(out)
    assert_valid_jpeg(out)


# --------------------------------------------------------------------------- full pipeline

@pytest.mark.camera
@pytest.mark.parametrize("backend", ["picamera2", "rpicam-still"])
def test_store_and_app_pipeline_with_real_camera(backend, capture_cfg, tmp_path):
    """Real camera -> ImageStore (atomic write, dated path) -> BuoyApp bookkeeping."""
    cfg = Config(
        capture=replace(capture_cfg, backend=backend),
        storage=StorageConfig(base_dir=tmp_path, min_free_mb=0),
    )
    camera = make_camera(cfg.capture)
    app = BuoyApp(cfg, camera=camera, store=ImageStore(tmp_path, min_free_mb=0))
    try:
        before = datetime.now(timezone.utc)
        app.capture_once(trigger="test")
        app.capture_once(trigger="test")
    finally:
        app.shutdown()

    assert app.error_count == 0
    assert app.capture_count == 2
    images = sorted(tmp_path.rglob("*.jpg"))
    assert len(images) == 2
    assert not list(tmp_path.rglob("*.part")), "temporary file left behind"
    day_dir = tmp_path / f"{before:%Y}" / f"{before:%m}" / f"{before:%d}"
    assert images[0].parent == day_dir
    for img in images:
        assert_valid_jpeg(img)


# --------------------------------------------------------------------------- no camera attached

@pytest.mark.no_camera
def test_picamera2_raises_clearly_without_camera(capture_cfg):
    with pytest.raises((RuntimeError, IndexError)):
        Picamera2Camera(replace(capture_cfg, backend="picamera2"))


@pytest.mark.no_camera
@pytest.mark.rpicam
def test_rpicam_still_raises_without_camera(capture_cfg, tmp_path):
    cam = RpicamStillCamera(replace(capture_cfg, backend="rpicam-still"))
    out = tmp_path / "shot.jpg"
    with pytest.raises(RuntimeError, match="rpicam-still failed"):
        cam.capture(out)
    assert not out.exists()


@pytest.mark.no_camera
def test_app_survives_camera_failure(capture_cfg, tmp_path):
    """A failing capture is counted and reported, and leaves no partial files."""
    cfg = Config(
        capture=replace(capture_cfg, backend="rpicam-still"),
        storage=StorageConfig(base_dir=tmp_path, min_free_mb=0),
    )
    app = BuoyApp(cfg, camera=RpicamStillCamera(cfg.capture), store=ImageStore(tmp_path, min_free_mb=0))
    app.capture_once()
    assert app.error_count == 1 and app.capture_count == 0
    assert not list(tmp_path.rglob("*.jpg*"))

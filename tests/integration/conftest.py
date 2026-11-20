"""Fixtures for camera hardware integration tests.

Tests marked ``camera`` need a real Raspberry Pi camera. They are skipped when none is
detected, unless WATERBUOY_REQUIRE_CAMERA=1 is set, in which case they fail instead
(use that on a deployed buoy so a missing camera is not silently ignored).

Tests marked ``no_camera`` check error handling and only run when NO camera is attached.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache

import pytest

from waterbuoy.config import CaptureConfig

REQUIRE_CAMERA = os.environ.get("WATERBUOY_REQUIRE_CAMERA") == "1"

# Small resolution keeps the tests fast; still exercises the full pipeline.
TEST_WIDTH, TEST_HEIGHT = 1280, 720


@lru_cache(maxsize=1)
def camera_count() -> int:
    try:
        from picamera2 import Picamera2
    except ImportError:
        return 0
    try:
        return len(Picamera2.global_camera_info())
    except Exception:  # noqa: BLE001 - libcamera unavailable
        return 0


@pytest.fixture(autouse=True)
def _camera_gate(request):
    present = camera_count() > 0
    if request.node.get_closest_marker("camera") and not present:
        msg = "no Raspberry Pi camera detected (Picamera2.global_camera_info() is empty)"
        if REQUIRE_CAMERA:
            pytest.fail(f"WATERBUOY_REQUIRE_CAMERA=1 but {msg}")
        pytest.skip(msg)
    if request.node.get_closest_marker("no_camera") and present:
        pytest.skip("camera is attached; missing-camera behaviour cannot be tested")
    if request.node.get_closest_marker("rpicam") and shutil.which("rpicam-still") is None:
        pytest.skip("rpicam-still not installed")


@pytest.fixture
def capture_cfg() -> CaptureConfig:
    return CaptureConfig(width=TEST_WIDTH, height=TEST_HEIGHT, jpeg_quality=85, align_to_clock=False)

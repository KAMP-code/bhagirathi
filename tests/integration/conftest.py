"""Fixtures for camera hardware integration tests.

A camera is always required: the tests detect every attached camera (USB webcams and
Raspberry Pi ribbon cameras) and run against each one. With no camera attached they FAIL.

Tests that only apply to one kind of camera (e.g. ribbon-camera tests on a buoy that only
has USB webcams) are reported as skipped with "got empty parameter set".
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import pytest

from waterbuoy.camera import list_csi_cameras, list_usb_cameras
from waterbuoy.config import CameraConfig

# Small resolutions keep the tests fast; tried in this order, else the camera's largest.
PREFERRED_TEST_SIZES = [(1280, 720), (640, 480)]


@lru_cache(maxsize=1)
def usb_cameras() -> tuple[dict, ...]:
    return tuple(list_usb_cameras())


@lru_cache(maxsize=1)
def csi_cameras() -> tuple[dict, ...]:
    return tuple(list_csi_cameras())


def all_cameras() -> tuple[dict, ...]:
    return usb_cameras() + csi_cameras()


def cam_id(info: dict) -> str:
    if info["backend"] == "usb":
        return f"usb:{info['node']}"
    return f"csi:{info['index']}"


def usb_test_size(info: dict) -> tuple[int, int]:
    for size in PREFERRED_TEST_SIZES:
        if any(sizes is None or size in sizes for sizes in info["formats"].values()):
            return size
    return 0, 0


def usb_config(info: dict, name: str = "usb", **kw) -> CameraConfig:
    w, h = usb_test_size(info)
    cfg = CameraConfig(name=name, backend="usb", device=info["device"], width=w, height=h,
                       jpeg_quality=85, warmup_frames=10)  # fewer warm-up frames: quicker tests
    return replace(cfg, **kw)


def csi_config(info: dict, backend: str, name: str = "csi", **kw) -> CameraConfig:
    cfg = CameraConfig(name=name, backend=backend, device=info["device"], width=1280, height=720,
                       jpeg_quality=85)
    return replace(cfg, **kw)


def camera_config(info: dict, name: str) -> CameraConfig:
    return usb_config(info, name) if info["backend"] == "usb" else csi_config(info, "picamera2", name)


def pytest_generate_tests(metafunc):
    if "usb_info" in metafunc.fixturenames:
        metafunc.parametrize("usb_info", usb_cameras(), ids=cam_id)
    if "csi_info" in metafunc.fixturenames:
        metafunc.parametrize("csi_info", csi_cameras(), ids=cam_id)
    if "usb_format" in metafunc.fixturenames:
        pairs = [(c, f) for c in usb_cameras() for f in c["formats"]]
        metafunc.parametrize("usb_format", pairs, ids=lambda p: f"{cam_id(p[0])}-{p[1]}")
    if "cam_info" in metafunc.fixturenames:
        metafunc.parametrize("cam_info", all_cameras(), ids=cam_id)


@pytest.fixture(scope="session")
def attached_cameras() -> tuple[dict, ...]:
    cams = all_cameras()
    if not cams:
        pytest.fail("no camera detected: plug in a USB webcam or a Raspberry Pi camera "
                    "(check `python -m waterbuoy --list-cameras`)")
    return cams

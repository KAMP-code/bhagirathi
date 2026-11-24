"""Integration tests for the water classifier (YOLOv8) and the dirty-water servo demo.

Run:  pytest tests/integration/test_water_classifier_hw.py -v

Needs the ultralytics package (the yolov8s-cls.pt weights are downloaded on first use),
the test images in testimages/ (named clean-* / dirty-*), the webcam, and the servo on
GPIO16 (the demo test moves it to 180 degrees and back).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("ultralytics", reason="pip install -e .[vision]")

from waterbuoy.water_classifier import LABELS, WaterClassifier  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
IMAGES = sorted(p for p in (ROOT / "testimages").glob("*")
                if p.suffix.lower() in {".png", ".jpg", ".jpeg"} and p.name.split("-")[0] in ("clean", "dirty"))
EXPECTED = {"clean": "clean_water", "dirty": "dirty_water"}


@pytest.fixture(scope="module")
def clf():
    return WaterClassifier()


@pytest.mark.parametrize("path", IMAGES, ids=lambda p: p.name)
def test_test_images_are_classified_correctly(clf, path):
    expected = EXPECTED[path.name.split("-")[0]]
    r = clf.classify(path)
    assert r.label == expected, f"{path.name}: {r} top={r.top_classes}"


@pytest.mark.parametrize("rgb", [(128, 128, 128), (240, 240, 235), (20, 20, 20)])
def test_plain_images_are_not_water(clf, rgb):
    r = clf.classify(np.full((480, 640, 3), rgb, np.uint8))
    assert r.label == "no_water", str(r)


def test_classifies_a_webcam_frame_quickly(clf):
    cv2 = pytest.importorskip("cv2")
    sys.path.insert(0, str(ROOT / "scripts"))
    from water_demo import default_camera_device

    cap = cv2.VideoCapture(default_camera_device(), cv2.CAP_V4L2)
    try:
        assert cap.isOpened(), "webcam not available"
        for _ in range(10):              # let exposure settle
            ok, frame = cap.read()
        assert ok
    finally:
        cap.release()
    clf.classify(frame[..., ::-1])       # warm-up
    t = time.monotonic()
    r = clf.classify(frame[..., ::-1])
    assert time.monotonic() - t < 1.0
    assert r.label in LABELS


def test_demo_turns_servo_for_dirty_water_only():
    dirty = [p for p in IMAGES if p.name.startswith("dirty")][:1]
    clean = [p for p in IMAGES if p.name.startswith("clean")][:1]
    assert dirty and clean
    t = time.monotonic()
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "water_demo.py"), "--hold", "1",
         "--images", str(clean[0]), str(dirty[0])],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("DIRTY WATER") == 1, out.stdout
    assert "clean_water" in out.stdout and "servo -> 0 deg" in out.stdout
    assert time.monotonic() - t >= 1

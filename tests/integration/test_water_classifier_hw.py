"""Integration tests for the water classifier (YOLOv8).

Run:  pytest tests/integration/test_water_classifier_hw.py -v

Needs the ultralytics package (the yolov8s-cls.pt weights are downloaded on first use),
the test images in testimages/ (named clean-* / dirty-*), and the webcam.
"""

from __future__ import annotations

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
    from check_water import default_camera_device

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

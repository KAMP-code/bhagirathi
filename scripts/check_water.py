#!/usr/bin/env python3
"""Dirty-water check: every 5 seconds take a photo, classify it, signal with the servo.

Runs until Ctrl+C. Each round takes one picture from the webcam and classifies it as
clean water, dirty water or no water. If it is dirty water the servo turns to 180 degrees,
holds for 5 seconds and turns back to 0; the next photo is taken after that.

    .venv/bin/python scripts/check_water.py                    # photo every 5 s
    .venv/bin/python scripts/check_water.py --interval 10      # photo every 10 s
    .venv/bin/python scripts/check_water.py --save last.jpg    # keep the latest photo

Ctrl+C stops; the servo returns to 0 and is released.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run without installing

from waterbuoy.servo import Servo  # noqa: E402
from waterbuoy.water_classifier import WaterClassifier, to_rgb_image  # noqa: E402

REST_ANGLE, ALARM_ANGLE = 0, 180


def default_camera_device() -> str:
    """The first USB camera in config/config.toml, else /dev/video0."""
    try:
        from waterbuoy.config import load_config

        for cam in load_config(ROOT / "config" / "config.toml").cameras:
            if cam.backend == "usb" and cam.device:
                return cam.device
    except Exception:
        pass
    return "/dev/video0"


def take_photo(device: str, warmup: int = 10):
    """One RGB frame (numpy array) from the webcam."""
    import cv2

    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera {device}")
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, frame = False, None
        for _ in range(warmup):          # let exposure and white balance settle
            ok, frame = cap.read()
        if not ok:
            raise SystemExit(f"camera {device} returned no image")
    finally:
        cap.release()
    return frame[..., ::-1].copy()       # OpenCV gives BGR


def signal_dirty(servo: Servo, hold_s: float) -> None:
    """Servo to 180 degrees, hold, back to 0, release."""
    try:
        print(f"  servo -> {ALARM_ANGLE} deg, holding {hold_s:g} s", flush=True)
        servo.angle = ALARM_ANGLE
        time.sleep(hold_s)
    finally:                             # back to rest even on Ctrl+C
        print(f"  servo -> {REST_ANGLE} deg", flush=True)
        servo.angle = REST_ANGLE
        time.sleep(0.5)                  # let it get there before releasing
        servo.detach()                   # no holding current while idle


def check_once(clf: WaterClassifier, servo: Servo, device: str, hold_s: float, save: str | None) -> None:
    image = to_rgb_image(take_photo(device))
    if save:
        image.save(save)
    r = clf.classify(image)
    top = ", ".join(f"{n} {p:.2f}" for n, p in r.top_classes[:3])
    print(f"{time.strftime('%H:%M:%S')} {r}  [{top}]", flush=True)
    if r.label == "dirty_water":
        print("  DIRTY WATER", flush=True)
        signal_dirty(servo, hold_s)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between photos (default 5)")
    p.add_argument("--camera", default=None, help="webcam device (default: first USB camera in config.toml)")
    p.add_argument("--save", metavar="FILE", help="save each photo to this file (overwritten)")
    p.add_argument("--servo-pin", type=int, default=16, help="servo BCM GPIO (default 16)")
    p.add_argument("--hold", type=float, default=5.0, help="seconds at 180 deg (default 5)")
    args = p.parse_args()

    print("loading YOLOv8 classifier...", flush=True)
    clf = WaterClassifier()
    device = args.camera or default_camera_device()

    with Servo(args.servo_pin) as servo:
        servo.angle = REST_ANGLE
        time.sleep(0.5)
        servo.detach()
        print(f"camera {device}: photo every {args.interval:g} s. Ctrl+C to stop.\n", flush=True)
        try:
            while True:
                started = time.monotonic()
                check_once(clf, servo, device, args.hold, args.save)
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print("\nstopping")
    print("done, servo released")
    return 0


if __name__ == "__main__":
    sys.exit(main())

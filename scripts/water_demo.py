#!/usr/bin/env python3
"""Dirty-water alarm demo: camera -> water classifier -> servo.

Each webcam frame is classified as clean water, dirty water or no water. When dirty
water (algae bloom or red tide) is seen (in 2 frames in a row) the servo turns to 180 degrees, waits 5 seconds
and returns to 0. It triggers again only after the camera has seen something that is
not dirty water, so a dirty-water picture held in front of the camera fires once.

Show water pictures to the webcam (on a phone, a screen or printed).

    .venv/bin/python scripts/water_demo.py                     # live webcam
    .venv/bin/python scripts/water_demo.py --no-show           # no preview window
    .venv/bin/python scripts/water_demo.py --images testimages/*   # classify files instead

Ctrl+C (or q in the preview window) stops; the servo returns to 0 and is released.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run without installing

from waterbuoy.servo import Servo  # noqa: E402
from waterbuoy.water_classifier import WaterClassifier, WaterResult  # noqa: E402

REST_ANGLE, ALARM_ANGLE = 0, 180
COLOURS = {"dirty_water": (0, 0, 230), "clean_water": (255, 160, 0), "no_water": (160, 160, 160)}  # BGR


class DirtyWaterAlarm:
    """Turns the servo when dirty water is confirmed in `confirm` consecutive results."""

    def __init__(self, servo: Servo, hold_s: float = 5.0, confirm: int = 2):
        self.servo, self.hold_s, self.confirm = servo, hold_s, confirm
        self.streak = 0
        self.armed = True
        self.servo.angle = REST_ANGLE
        time.sleep(0.5)
        self.servo.detach()

    def update(self, label: str) -> bool:
        """Feed one classification; returns True if the servo was cycled."""
        if label != "dirty_water":
            self.streak = 0
            self.armed = True
            return False
        self.streak += 1
        if not (self.armed and self.streak >= self.confirm):
            return False
        self.armed = False
        self.trigger()
        return True

    def trigger(self) -> None:
        """Servo to the alarm angle, hold, and back to rest."""
        print(f"  >>> DIRTY WATER: servo -> {ALARM_ANGLE} deg, holding {self.hold_s:g} s", flush=True)
        self.servo.angle = ALARM_ANGLE
        time.sleep(self.hold_s)
        print(f"  <<< servo -> {REST_ANGLE} deg", flush=True)
        self.servo.angle = REST_ANGLE
        time.sleep(0.5)
        self.servo.detach()  # no holding current while idle


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


def describe(r: WaterResult) -> str:
    top = ", ".join(f"{n} {p:.2f}" for n, p in r.top_classes[:3])
    return f"{r.label:<12} water={r.water_score:.2f} green={r.green_fraction:.2f} red={r.red_fraction:.2f}  [{top}]"


def run_images(clf: WaterClassifier, alarm: DirtyWaterAlarm, paths: list[str]) -> None:
    for p in paths:
        r = clf.classify(p)
        print(f"{Path(p).name:<28} {describe(r)}", flush=True)
        if r.label == "dirty_water":      # each file is independent: no confirmation
            alarm.trigger()


def run_camera(clf: WaterClassifier, alarm: DirtyWaterAlarm, device: str, show: bool) -> None:
    import cv2

    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera {device}")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    print(f"camera {device}: show water pictures to it. Ctrl+C{' or q' if show else ''} to stop.\n")
    last = None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                raise SystemExit("camera stopped delivering frames")
            r = clf.classify(frame[..., ::-1])  # OpenCV gives BGR
            if r.label != last:
                print(time.strftime("%H:%M:%S"), describe(r), flush=True)
                last = r.label
            if show:
                txt = f"{r.label}  water {r.water_score:.2f}  green {r.green_fraction:.2f}  red {r.red_fraction:.2f}"
                cv2.rectangle(frame, (0, 0), (frame.shape[1], 36), (0, 0, 0), -1)
                cv2.putText(frame, txt, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, COLOURS[r.label], 2)
                cv2.imshow("water demo", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            if alarm.update(r.label):
                for _ in range(5):        # drop frames buffered while the servo was out
                    cap.grab()
    finally:
        cap.release()
        if show:
            cv2.destroyAllWindows()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--images", nargs="+", metavar="FILE", help="classify these files instead of the webcam")
    p.add_argument("--camera", default=None, help="webcam device (default: first USB camera in config.toml)")
    p.add_argument("--no-show", action="store_true", help="no preview window")
    p.add_argument("--servo-pin", type=int, default=16, help="servo BCM GPIO (default 16)")
    p.add_argument("--hold", type=float, default=5.0, help="seconds at 180 deg (default 5)")
    args = p.parse_args()

    print("loading YOLOv8 classifier...", flush=True)
    clf = WaterClassifier()
    with Servo(args.servo_pin) as servo:
        alarm = DirtyWaterAlarm(servo, hold_s=args.hold)
        try:
            if args.images:
                run_images(clf, alarm, args.images)
            else:
                show = not args.no_show and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                run_camera(clf, alarm, args.camera or default_camera_device(), show)
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            if not servo._closed:
                servo.angle = REST_ANGLE
                time.sleep(0.5)
    print("done, servo released")


if __name__ == "__main__":
    main()

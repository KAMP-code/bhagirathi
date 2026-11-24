#!/usr/bin/env python3
"""Proximity alarm demo: distance sensor -> servo.

The ultrasonic sensor (Trig GPIO23, Echo GPIO24) is read about 5 times a second. When
something is closer than 15 cm (in 2 readings in a row) the servo on GPIO16 turns to
180 degrees, waits 5 seconds and returns to 0. It triggers again only after the object
has moved away (15 cm or more, or no echo), so a hand held over the sensor fires once.

    .venv/bin/python scripts/water_demo.py                 # trigger below 15 cm
    .venv/bin/python scripts/water_demo.py --threshold 10  # trigger below 10 cm
    .venv/bin/python scripts/water_demo.py --hold 2        # 2 s at 180 deg

Ctrl+C stops; the servo returns to 0 and both are released.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run without installing

from waterbuoy.distance_sensor import DistanceSensor  # noqa: E402
from waterbuoy.servo import Servo  # noqa: E402

REST_ANGLE, ALARM_ANGLE = 0, 180


class ProximityAlarm:
    """Turns the servo when an object is closer than `threshold_cm` in `confirm`
    consecutive readings; re-arms once the object moves away."""

    def __init__(self, servo: Servo, threshold_cm: float = 15.0, hold_s: float = 5.0, confirm: int = 2):
        self.servo, self.threshold_cm, self.hold_s, self.confirm = servo, threshold_cm, hold_s, confirm
        self.streak = 0
        self.armed = True
        self.servo.angle = REST_ANGLE
        time.sleep(0.5)
        self.servo.detach()

    def is_close(self, cm: float | None) -> bool:
        return cm is not None and cm < self.threshold_cm

    def update(self, cm: float | None) -> bool:
        """Feed one distance reading (cm, or None for no echo); returns True if the
        servo was cycled."""
        if not self.is_close(cm):
            self.streak = 0
            self.armed = True
            return False
        self.streak += 1
        if not (self.armed and self.streak >= self.confirm):
            return False
        self.armed = False
        self.trigger(cm)
        return True

    def trigger(self, cm: float) -> None:
        """Servo to the alarm angle, hold, and back to rest."""
        print(f"\n  >>> OBJECT AT {cm:.1f} cm: servo -> {ALARM_ANGLE} deg, holding {self.hold_s:g} s", flush=True)
        self.servo.angle = ALARM_ANGLE
        time.sleep(self.hold_s)
        print(f"  <<< servo -> {REST_ANGLE} deg", flush=True)
        self.servo.angle = REST_ANGLE
        time.sleep(0.5)
        self.servo.detach()  # no holding current while idle


def describe(cm: float | None, threshold_cm: float) -> str:
    if cm is None:
        return "  --.- cm  no echo"
    return f"{cm:7.1f} cm{'  CLOSE' if cm < threshold_cm else ''}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--threshold", type=float, default=15.0, help="trigger distance in cm (default 15)")
    p.add_argument("--trig", type=int, default=23, help="sensor Trig BCM GPIO (default 23)")
    p.add_argument("--echo", type=int, default=24, help="sensor Echo BCM GPIO (default 24)")
    p.add_argument("--servo-pin", type=int, default=16, help="servo BCM GPIO (default 16)")
    p.add_argument("--hold", type=float, default=5.0, help="seconds at 180 deg (default 5)")
    p.add_argument("--temp", type=float, default=20.0, help="air temperature, C (default 20)")
    args = p.parse_args()

    with DistanceSensor(args.trig, args.echo, temperature_c=args.temp) as sensor, Servo(args.servo_pin) as servo:
        alarm = ProximityAlarm(servo, threshold_cm=args.threshold, hold_s=args.hold)
        print(f"distance sensor on Trig GPIO{args.trig} / Echo GPIO{args.echo}, servo on GPIO{args.servo_pin}. "
              f"Bring something closer than {args.threshold:g} cm. Ctrl+C to stop.\n")
        try:
            while True:
                d = sensor.read(samples=3)
                cm = None if d is None else d * 100
                print("\r" + describe(cm, args.threshold), end="      ", flush=True)
                alarm.update(cm)
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            if not servo._closed:
                servo.angle = REST_ANGLE
                time.sleep(0.5)
    print("done, servo and sensor released")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Servo demo for the TowerPro SG90 on GPIO16.

    .venv/bin/python scripts/servo_demo.py              # full demo
    .venv/bin/python scripts/servo_demo.py --angle 45   # go to 45 degrees and hold
    .venv/bin/python scripts/servo_demo.py --pin 16 --min-pulse 600 --max-pulse 2300

Wiring: brown = GND, red = +5 V, orange = signal -> GPIO16 (physical pin 36).
Ctrl+C at any time centres the servo and releases the pin.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without installing

from waterbuoy.servo import Servo  # noqa: E402


def step(servo: Servo, title: str) -> None:
    print(f"  {title:<34} angle={servo.angle!s:>6}  pulse={servo.pulse_for(servo.angle) if servo.attached else '-'} us")


def demo(servo: Servo) -> None:
    print("1. centre")
    servo.center()
    step(servo, "centre")
    time.sleep(1)

    print("2. jump between positions")
    for a in (0, 45, 90, 135, 180, 90):
        servo.angle = a
        step(servo, f"jump to {a}")
        time.sleep(0.8)

    print("3. smooth sweeps at increasing speed")
    for speed in (45, 90, 180):
        servo.move_to(0, speed=speed)
        servo.move_to(180, speed=speed)
        step(servo, f"0 -> 180 at {speed} deg/s")
    servo.move_to(90, speed=90)

    print("4. wave (small fast moves)")
    for _ in range(3):
        servo.move_to(60, speed=240)
        servo.move_to(120, speed=240)
    servo.move_to(90, speed=240)
    step(servo, "back to centre")

    print("5. detach (servo goes limp, stops humming)")
    time.sleep(0.5)
    servo.detach()
    step(servo, "detached")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pin", type=int, default=16, help="BCM GPIO number (default 16)")
    p.add_argument("--min-pulse", type=int, default=500, help="pulse width at 0 degrees, us (default 500)")
    p.add_argument("--max-pulse", type=int, default=2400, help="pulse width at 180 degrees, us (default 2400)")
    p.add_argument("--angle", type=float, help="just move to this angle and hold until Ctrl+C")
    args = p.parse_args()

    with Servo(args.pin, min_pulse_us=args.min_pulse, max_pulse_us=args.max_pulse) as servo:
        print(f"Servo on GPIO{args.pin}, {args.min_pulse}-{args.max_pulse} us = 0-180 deg")
        try:
            if args.angle is not None:
                servo.move_to(args.angle)
                step(servo, f"holding at {args.angle:g} (Ctrl+C to stop)")
                while True:
                    time.sleep(1)
            else:
                demo(servo)
        except KeyboardInterrupt:
            print("\ninterrupted: centring")
            servo.center()
            time.sleep(0.5)
    print("done, GPIO released")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Live distance demo for the ultrasonic sensor (Trig GPIO23, Echo GPIO24).

Hold your hand over the sensor and move it up and down: the distance and a bar update
about 5 times a second. Ctrl+C to stop.

    .venv/bin/python scripts/distance_demo.py
    .venv/bin/python scripts/distance_demo.py --max 50     # bar scale 0-50 cm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without installing

from waterbuoy.distance_sensor import DistanceSensor  # noqa: E402

BAR_WIDTH = 40


def line(cm: float | None, scale_cm: float) -> str:
    if cm is None:
        return f"{'  --.- cm':>9}  |{' ' * BAR_WIDTH}|  no echo (nothing in range?)"
    n = min(BAR_WIDTH, round(cm / scale_cm * BAR_WIDTH))
    label = "  close!" if cm < 10 else ""
    return f"{cm:7.1f} cm  |{'#' * n}{' ' * (BAR_WIDTH - n)}|{label}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trig", type=int, default=23, help="Trig BCM GPIO (default 23)")
    p.add_argument("--echo", type=int, default=24, help="Echo BCM GPIO (default 24)")
    p.add_argument("--max", type=float, default=100, help="bar scale in cm (default 100)")
    p.add_argument("--temp", type=float, default=20.0, help="air temperature, C (default 20)")
    args = p.parse_args()

    with DistanceSensor(args.trig, args.echo, temperature_c=args.temp) as sensor:
        print(f"Distance sensor on Trig GPIO{args.trig} / Echo GPIO{args.echo}. "
              "Hold your hand over it. Ctrl+C to stop.\n")
        try:
            while True:
                d = sensor.read(samples=3)
                print("\r" + line(None if d is None else d * 100, args.max), end="  ", flush=True)
        except KeyboardInterrupt:
            print("\nstopped, GPIO released")


if __name__ == "__main__":
    main()

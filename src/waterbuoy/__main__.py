"""Entry point: python -m waterbuoy -c config/config.toml"""

from __future__ import annotations

import argparse
import logging
import signal

from .app import BuoyApp
from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(description="WaterBuoy camera service")
    p.add_argument("-c", "--config", default="config/config.toml")
    p.add_argument("--once", action="store_true", help="take one picture per camera and exit")
    p.add_argument("--camera", help="with --once: only this camera")
    p.add_argument("--list-cameras", action="store_true",
                   help="show attached cameras with a [[camera]] config snippet, and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.list_cameras:
        print_cameras()
        return
    cfg = load_config(args.config)
    if args.once:
        cfg.iot.enabled = False
    app = BuoyApp(cfg)

    if args.once:
        try:
            app.capture_once(trigger="manual", camera=args.camera)
        finally:
            app.shutdown()
        if app.error_count:
            raise SystemExit(1)
        return

    signal.signal(signal.SIGTERM, lambda *_: app.stop())
    signal.signal(signal.SIGINT, lambda *_: app.stop())
    app.run()


def print_cameras() -> None:
    from .camera import discover_cameras

    cams = discover_cameras()
    if not cams:
        print("No cameras detected.")
        raise SystemExit(1)
    for i, c in enumerate(cams):
        print(f"# {c['model']}")
        if c["backend"] == "usb":
            print(f"#   node {c['node']}")
            for fmt, sizes in c["formats"].items():
                listed = " ".join(f"{w}x{h}" for w, h in sizes) if sizes is not None else "any size"
                print(f"#   {fmt}: {listed}")
        print("[[camera]]")
        print(f'name = "cam{i}"')
        print(f'backend = "{c["backend"]}"')
        print(f'device = "{c["device"]}"')
        print("width = 0            # 0 = largest supported")
        print("height = 0")
        print()


if __name__ == "__main__":
    main()

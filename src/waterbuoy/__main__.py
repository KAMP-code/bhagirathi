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
    p.add_argument("--once", action="store_true", help="take a single picture and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    if args.once:
        cfg.iot.enabled = False
    app = BuoyApp(cfg)

    if args.once:
        try:
            app.capture_once(trigger="manual")
        finally:
            app.shutdown()
        return

    signal.signal(signal.SIGTERM, lambda *_: app.stop())
    signal.signal(signal.SIGINT, lambda *_: app.stop())
    app.run()


if __name__ == "__main__":
    main()

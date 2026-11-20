"""Camera backends. Each backend writes a JPEG to a given path."""

from __future__ import annotations

import logging
import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .config import CaptureConfig

log = logging.getLogger(__name__)


class Camera(ABC):
    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self._lock = threading.Lock()  # captures may come from scheduler or IoT command

    def capture(self, dest: Path) -> None:
        with self._lock:
            self._capture(dest)

    @abstractmethod
    def _capture(self, dest: Path) -> None: ...

    def close(self) -> None:
        pass


class Picamera2Camera(Camera):
    """Uses the libcamera Python bindings; keeps the camera open between shots."""

    def __init__(self, cfg: CaptureConfig):
        super().__init__(cfg)
        from picamera2 import Picamera2  # system package (python3-picamera2)

        self._cam = Picamera2()
        still = self._cam.create_still_configuration(main={"size": (cfg.width, cfg.height)})
        self._cam.configure(still)
        self._cam.options["quality"] = cfg.jpeg_quality
        self._cam.start()
        log.info("picamera2 started at %dx%d", cfg.width, cfg.height)

    def _capture(self, dest: Path) -> None:
        self._cam.capture_file(str(dest), format="jpeg")

    def close(self) -> None:
        try:
            self._cam.stop()
            self._cam.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing camera")


class RpicamStillCamera(Camera):
    """Shells out to rpicam-still for each shot (camera released between shots)."""

    def _capture(self, dest: Path) -> None:
        cmd = [
            "rpicam-still", "-n", "--immediate",
            "--width", str(self.cfg.width), "--height", str(self.cfg.height),
            "-q", str(self.cfg.jpeg_quality),
            "-e", "jpg",
            "-o", str(dest),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"rpicam-still failed ({res.returncode}): {res.stderr.strip()[-500:]}")


class MockCamera(Camera):
    """Generates a synthetic JPEG; for development without camera hardware."""

    def _capture(self, dest: Path) -> None:
        from datetime import datetime, timezone

        from PIL import Image, ImageDraw

        img = Image.new("RGB", (self.cfg.width, self.cfg.height), (20, 60, 110))
        ImageDraw.Draw(img).text((20, 20), datetime.now(timezone.utc).isoformat(), fill=(255, 255, 255))
        img.save(dest, format="JPEG", quality=self.cfg.jpeg_quality)


def make_camera(cfg: CaptureConfig) -> Camera:
    return {
        "picamera2": Picamera2Camera,
        "rpicam-still": RpicamStillCamera,
        "mock": MockCamera,
    }[cfg.backend](cfg)

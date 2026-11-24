"""Main service: scheduled capture loop + IoT command handling."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from .camera import Camera, make_cameras
from .config import Config
from .storage import ImageStore

log = logging.getLogger(__name__)


def seconds_until_next(interval: int, align: bool, now: float | None = None) -> float:
    if not align:
        return float(interval)
    now = time.time() if now is None else now
    return interval - (now % interval)


class BuoyApp:
    def __init__(self, cfg: Config, cameras: list[Camera] | None = None, store: ImageStore | None = None,
                 iot=None):
        self.cfg = cfg
        self.interval = cfg.capture.interval_seconds
        # Every configured camera must open; a missing one stops start-up (systemd retries).
        self.cameras = cameras if cameras is not None else make_cameras(cfg.cameras)
        if not self.cameras:
            raise ValueError("at least one camera is required")
        names = [c.name for c in self.cameras]
        if len(set(names)) != len(names):
            raise ValueError(f"camera names must be unique: {names}")
        self.store = store or ImageStore(cfg.storage.base_dir, cfg.storage.min_free_mb)
        self.iot = iot
        self._stop = threading.Event()
        self._wake = threading.Event()        # interrupts the sleep (interval changed / manual capture)
        self._capture_now = threading.Event()
        self._capture_only: str | None = None   # camera name requested by a capture command
        self.capture_count = 0
        self.error_count = 0
        self.camera_stats = {n: {"captures": 0, "errors": 0, "last_image": None, "last_error": None}
                             for n in names}
        self.last_algae: dict | None = None

        if self.iot is None and cfg.iot.enabled:
            from .iot import IotClient
            self.iot = IotClient(cfg, self.handle_command)

    # --- capture ---------------------------------------------------------
    def capture_once(self, trigger: str = "schedule", camera: str | None = None) -> None:
        """Take one picture with every camera (or only `camera`). A failing camera never
        stops the others."""
        cams = self.cameras if camera is None else [c for c in self.cameras if c.name == camera]
        if not cams:
            raise ValueError(f"unknown camera {camera!r}; have {[c.name for c in self.cameras]}")
        for cam in cams:
            self._capture_camera(cam, trigger)

    def _capture_camera(self, cam: Camera, trigger: str) -> None:
        stats = self.camera_stats[cam.name]
        ts = datetime.now(timezone.utc)
        t0 = time.monotonic()
        try:
            path = self.store.save(ts, cam.capture, camera=cam.name)
        except Exception as e:  # noqa: BLE001 - keep the service running
            self.error_count += 1
            stats["errors"] += 1
            stats["last_error"] = str(e)[:500]
            log.exception("capture failed on camera %s", cam.name)
            if self.iot:
                self.iot.publish_status("error", camera=cam.name, error=str(e)[:500], trigger=trigger)
            return
        self.capture_count += 1
        stats["captures"] += 1
        stats["last_image"] = str(path)
        size = path.stat().st_size
        log.info("%s: saved %s (%d bytes, %.2fs, %s)", cam.name, path, size, time.monotonic() - t0, trigger)
        extra = {}
        if self.cfg.algae.enabled:
            extra["algae"] = self._analyze_algae(path)
        if self.iot:
            self.iot.publish_capture(
                camera=cam.name, file=str(path), size_bytes=size, captured_at=ts.isoformat(), trigger=trigger,
                **extra, **self.store.stats(),
            )

    @property
    def last_image(self) -> str | None:
        """Most recent image from any camera."""
        imgs = [s["last_image"] for s in self.camera_stats.values() if s["last_image"]]
        return max(imgs, key=lambda p: Path(p).name) if imgs else None

    def _analyze_algae(self, path) -> dict:
        # Analysis must never break capture: report the error in the payload instead.
        try:
            from .algae import analyze_image
            res = analyze_image(path, self.cfg.algae).to_dict()
        except Exception as e:  # noqa: BLE001
            log.exception("algae analysis failed for %s", path)
            return {"label": "error", "error": str(e)[:200]}
        log.info("algae: %s (bloom %.1f%% of valid pixels)", res["label"], res["bloom_fraction"] * 100)
        self.last_algae = res
        return res

    # --- commands --------------------------------------------------------
    def handle_command(self, cmd: dict) -> None:
        action = cmd.get("action")
        if action == "capture":
            cam = cmd.get("camera")
            if cam is not None and cam not in self.camera_stats:
                raise ValueError(f"unknown camera {cam!r}")
            self._capture_only = cam
            self._capture_now.set()
            self._wake.set()
        elif action == "set_interval":
            seconds = int(cmd["seconds"])
            if seconds < 10:
                raise ValueError("interval must be >= 10 seconds")
            self.interval = seconds
            log.info("interval set to %ds", seconds)
            self._wake.set()
            self._publish_state()
        elif action == "status":
            self._publish_state()
        else:
            log.warning("unknown command: %r", action)

    def _publish_state(self) -> None:
        if self.iot:
            self.iot.publish_status(
                "online", interval_seconds=self.interval, captures=self.capture_count,
                errors=self.error_count, last_image=self.last_image, last_algae=self.last_algae,
                cameras={c.name: {**c.info(), **self.camera_stats[c.name]} for c in self.cameras},
                **self.store.stats(),
            )

    # --- main loop -------------------------------------------------------
    def run(self) -> None:
        if self.iot:
            self._connect_iot()
        log.info("capturing every %ds from %s into %s", self.interval,
                 ", ".join(c.name for c in self.cameras), self.store.base_dir)
        try:
            while not self._stop.is_set():
                delay = seconds_until_next(self.interval, self.cfg.capture.align_to_clock)
                deadline = time.monotonic() + delay
                fired = True
                while (remaining := deadline - time.monotonic()) > 0:
                    if self._stop.is_set():
                        return
                    if self._wake.wait(remaining):
                        self._wake.clear()
                        if self._capture_now.is_set():
                            self._capture_now.clear()
                            self.capture_once(trigger="command", camera=self._capture_only)
                        else:  # interval changed: recompute schedule
                            fired = False
                            break
                if fired and not self._stop.is_set():
                    self.capture_once()
        finally:
            self.shutdown()

    def _connect_iot(self) -> None:
        # The SDK reconnects automatically after the first successful connect;
        # failing the initial connect must not prevent local capture.
        try:
            self.iot.connect()
        except Exception:  # noqa: BLE001
            log.exception("initial IoT connect failed; retrying in background")
            threading.Thread(target=self._retry_connect, daemon=True).start()

    def _retry_connect(self) -> None:
        delay = 10
        while not self._stop.wait(delay):
            try:
                self.iot.connect()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("IoT connect retry failed: %s", e)
                delay = min(delay * 2, 600)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def shutdown(self) -> None:
        if self.iot:
            self.iot.disconnect()
        for cam in self.cameras:
            cam.close()
        log.info("stopped")

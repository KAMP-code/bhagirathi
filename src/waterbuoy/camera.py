"""Camera backends and discovery. Each backend writes a JPEG to a given path.

Backends:
  usb           any UVC webcam via V4L2 + ffmpeg (MJPEG or raw YUV, probed at start-up)
  picamera2     Raspberry Pi ribbon (CSI) camera, kept open between shots
  rpicam-still  Raspberry Pi ribbon camera, one rpicam-still process per shot
  mock          synthetic image, for development
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .config import CameraConfig

log = logging.getLogger(__name__)


class Camera(ABC):
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.name = cfg.name
        self._lock = threading.Lock()  # captures may come from scheduler or IoT command

    def capture(self, dest: Path) -> None:
        with self._lock:
            self._capture(dest)

    @abstractmethod
    def _capture(self, dest: Path) -> None: ...

    def info(self) -> dict:
        return {"name": self.name, "backend": self.cfg.backend}

    def close(self) -> None:
        pass


# =========================================================================== USB / V4L2

# Pixel formats in order of preference when input_format = "auto". MJPEG gives the highest
# resolutions over USB 2; raw formats are the fallback for cameras without MJPEG.
PREFERRED_FORMATS = ("mjpeg", "yuyv422", "nv12", "yuv420p", "uyvy422", "rgb24", "bgr24", "gray")
# Compressed formats we can't take a single clean frame from (need a keyframe/decoder state).
UNSUPPORTED_FORMATS = {"h264", "hevc", "h265", "vp8"}

_FMT_LINE = re.compile(r"\]\s*(Compressed|Raw)\s*:\s*(\S+)\s*:\s*(.*?)\s*:\s*(.*)$")
_SIZE = re.compile(r"(\d+)x(\d+)")

# format -> list of discrete (w, h) sizes, or None for a stepwise/continuous range
Formats = dict[str, list[tuple[int, int]] | None]


def parse_ffmpeg_formats(text: str) -> Formats:
    """Parse the stderr of `ffmpeg -f v4l2 -list_formats all -i <dev>`."""
    formats: Formats = {}
    for line in text.splitlines():
        m = _FMT_LINE.search(line)
        if not m:
            continue
        kind, fmt, _desc, sizes = m.groups()
        if fmt in UNSUPPORTED_FORMATS or (kind == "Compressed" and fmt != "mjpeg"):
            continue
        if "{" in sizes:  # stepwise, e.g. {32-2592, 2}x{32-1944, 2}
            formats[fmt] = None
        else:
            formats[fmt] = [(int(w), int(h)) for w, h in _SIZE.findall(sizes)]
    return formats


def probe_v4l2(device: str) -> Formats:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-f", "v4l2", "-list_formats", "all", "-i", device],
        capture_output=True, text=True, timeout=20,
    )  # always exits non-zero ("Immediate exit requested"); the listing is on stderr
    formats = parse_ffmpeg_formats(res.stderr)
    if not formats:
        raise RuntimeError(
            f"{device} is not a usable V4L2 capture device: {res.stderr.strip()[-300:]}"
        )
    return formats


def _rank(fmt: str) -> int:
    return PREFERRED_FORMATS.index(fmt) if fmt in PREFERRED_FORMATS else len(PREFERRED_FORMATS)


def choose_mode(formats: Formats, width: int, height: int, input_format: str = "auto") -> tuple[str, int, int]:
    """Pick (pixel format, width, height). width/height 0 = largest available."""
    if input_format != "auto":
        if input_format not in formats:
            raise RuntimeError(f"camera does not offer format {input_format!r}; it offers {sorted(formats)}")
        candidates = {input_format: formats[input_format]}
    else:
        candidates = formats

    def describe() -> str:
        return "; ".join(
            f"{f}: " + (" ".join(f"{w}x{h}" for w, h in s) if s is not None else "any size")
            for f, s in candidates.items()
        )

    if width == 0:
        best = None
        for fmt, sizes in candidates.items():
            for w, h in sizes or []:
                key = (w * h, -_rank(fmt))
                if best is None or key > best[0]:
                    best = (key, fmt, w, h)
        if best is None:
            raise RuntimeError(f"cannot pick a resolution automatically; set width/height ({describe()})")
        return best[1], best[2], best[3]

    ok = [f for f, sizes in candidates.items() if sizes is None or (width, height) in sizes]
    if not ok:
        raise RuntimeError(f"camera does not support {width}x{height} (supported: {describe()})")
    return min(ok, key=_rank), width, height


class UsbCamera(Camera):
    """USB (UVC) webcam via V4L2, captured with ffmpeg; the device is released between shots.

    At start-up the camera's formats are probed and the mode is chosen (see `choose_mode`).
    Each shot discards `warmup_frames` frames so auto-exposure / white balance can settle,
    decodes one frame losslessly (PNG) and Pillow encodes it at `jpeg_quality`.
    """

    def __init__(self, cfg: CameraConfig):
        super().__init__(cfg)
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found; install it with: sudo apt install ffmpeg")
        if not os.path.exists(cfg.device):
            raise RuntimeError(f"camera {cfg.name}: no device at {cfg.device} (is the camera plugged in?)")
        if not os.access(cfg.device, os.R_OK | os.W_OK):
            raise RuntimeError(f"camera {cfg.name}: no permission for {cfg.device} (add the user to group 'video')")
        self.formats = probe_v4l2(cfg.device)
        try:
            self.pix_fmt, self.width, self.height = choose_mode(
                self.formats, cfg.width, cfg.height, cfg.input_format)
        except RuntimeError as e:
            raise RuntimeError(f"camera {cfg.name} ({cfg.device}): {e}") from None
        log.info("camera %s: USB %s, %s %dx%d", cfg.name, cfg.device, self.pix_fmt, self.width, self.height)

    def info(self) -> dict:
        return {**super().info(), "device": self.cfg.device, "format": self.pix_fmt,
                "width": self.width, "height": self.height}

    def _capture(self, dest: Path) -> None:
        from PIL import Image

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "v4l2", "-input_format", self.pix_fmt,
            "-video_size", f"{self.width}x{self.height}",
            "-i", self.cfg.device,
            "-vf", f"select=gte(n\\,{self.cfg.warmup_frames})",
            "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"camera {self.name}: ffmpeg timed out reading {self.cfg.device}") from e
        if res.returncode != 0 or not res.stdout:
            err = res.stderr.decode(errors="replace").strip()[-500:]
            raise RuntimeError(f"camera {self.name}: USB capture failed ({res.returncode}) on {self.cfg.device}: {err}")
        with Image.open(io.BytesIO(res.stdout)) as img:
            if img.size != (self.width, self.height):
                # V4L2 silently substitutes the nearest supported mode.
                raise RuntimeError(
                    f"camera {self.name}: asked for {self.width}x{self.height}, got {img.size[0]}x{img.size[1]}"
                )
            img.convert("RGB").save(dest, format="JPEG", quality=self.cfg.jpeg_quality)


def list_usb_cameras() -> list[dict]:
    """USB video capture nodes (one per physical camera), with stable by-id paths where available."""
    def links(d: str) -> dict[str, str]:
        out = {}
        for link in sorted(Path(d).glob("*")):
            out.setdefault(os.path.realpath(link), str(link))
        return out

    by_id, by_path = links("/dev/v4l/by-id"), links("/dev/v4l/by-path")
    cams = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:] or 0)):
        try:
            if (node / "index").read_text().strip() != "0":
                continue  # index 1+ are metadata nodes of the same camera
            if "/usb" not in os.path.realpath(node / "device"):
                continue  # ISP, codec and CSI nodes
            name = (node / "name").read_text().strip()
        except OSError:
            continue
        dev = f"/dev/{node.name}"
        try:
            formats = probe_v4l2(dev)
        except Exception:  # noqa: BLE001 - not a capture node / busy
            continue
        real = os.path.realpath(dev)
        cams.append({"backend": "usb", "model": name, "node": dev, "formats": formats,
                     "by_id": by_id.get(real), "by_path": by_path.get(real)})
    # by-id names come from the camera's serial number; identical cameras often share one, so
    # only one of them gets the link. Use the USB-port path for those instead.
    models = [c["model"] for c in cams]
    for c in cams:
        unique = models.count(c["model"]) == 1
        c["device"] = (c["by_id"] if unique else None) or c["by_path"] or c["by_id"] or c["node"]
    return cams


# =========================================================================== Pi ribbon camera

def list_csi_cameras() -> list[dict]:
    """Raspberry Pi ribbon cameras known to libcamera (USB webcams are excluded)."""
    try:
        from picamera2 import Picamera2
        infos = Picamera2.global_camera_info()
    except Exception:  # noqa: BLE001 - picamera2/libcamera not installed
        return []
    csi = [c for c in infos if "usb" not in c.get("Id", "").lower()]
    return [{"backend": "picamera2", "model": c.get("Model", "?"), "device": c["Id"],
             "index": i, "libcamera_num": c["Num"]} for i, c in enumerate(csi)]


def resolve_csi(cfg: CameraConfig) -> dict:
    """Find the ribbon camera selected by cfg.device (libcamera Id) or cfg.index."""
    cams = list_csi_cameras()
    found = [c for c in cams if c["device"] == cfg.device] if cfg.device else cams[cfg.index:cfg.index + 1]
    if not found:
        want = f"Id {cfg.device!r}" if cfg.device else f"index {cfg.index}"
        have = ", ".join(f"{c['index']}: {c['model']} ({c['device']})" for c in cams) or "none"
        raise RuntimeError(f"camera {cfg.name}: no Raspberry Pi camera with {want}; detected: {have}")
    return found[0]


class Picamera2Camera(Camera):
    """Uses the libcamera Python bindings; keeps the camera open between shots."""

    def __init__(self, cfg: CameraConfig):
        super().__init__(cfg)
        from picamera2 import Picamera2  # system package (python3-picamera2)

        self.csi = resolve_csi(cfg)
        self._cam = Picamera2(camera_num=self.csi["libcamera_num"])
        size = (cfg.width, cfg.height) if cfg.width else tuple(self._cam.sensor_resolution)
        self.width, self.height = size
        self._cam.configure(self._cam.create_still_configuration(main={"size": size}))
        self._cam.options["quality"] = cfg.jpeg_quality
        self._cam.start()
        log.info("camera %s: picamera2 %s at %dx%d", cfg.name, self.csi["model"], *size)

    def info(self) -> dict:
        return {**super().info(), "device": self.csi["device"], "model": self.csi["model"],
                "width": self.width, "height": self.height}

    def _capture(self, dest: Path) -> None:
        self._cam.capture_file(str(dest), format="jpeg")

    def close(self) -> None:
        try:
            self._cam.stop()
            self._cam.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing camera %s", self.name)


class RpicamStillCamera(Camera):
    """Shells out to rpicam-still for each shot (camera released between shots)."""

    def __init__(self, cfg: CameraConfig):
        super().__init__(cfg)
        if shutil.which("rpicam-still") is None:
            raise RuntimeError("rpicam-still not found; install it with: sudo apt install rpicam-apps")
        # rpicam-apps number ribbon cameras only, which matches our CSI index.
        self.csi = resolve_csi(cfg)
        log.info("camera %s: rpicam-still %s (camera %d)", cfg.name, self.csi["model"], self.csi["index"])

    def info(self) -> dict:
        return {**super().info(), "device": self.csi["device"], "model": self.csi["model"]}

    def _capture(self, dest: Path) -> None:
        cmd = ["rpicam-still", "-n", "--immediate", "--camera", str(self.csi["index"])]
        if self.cfg.width:
            cmd += ["--width", str(self.cfg.width), "--height", str(self.cfg.height)]
        cmd += ["-q", str(self.cfg.jpeg_quality), "-e", "jpg", "-o", str(dest)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"camera {self.name}: rpicam-still failed ({res.returncode}): {res.stderr.strip()[-500:]}")


# =========================================================================== mock

class MockCamera(Camera):
    """Generates a synthetic JPEG; for development without camera hardware."""

    def _capture(self, dest: Path) -> None:
        from datetime import datetime, timezone

        from PIL import Image, ImageDraw

        size = (self.cfg.width or 640, self.cfg.height or 480)
        img = Image.new("RGB", size, (20, 60, 110))
        ImageDraw.Draw(img).text((20, 20), f"{self.name} {datetime.now(timezone.utc).isoformat()}",
                                 fill=(255, 255, 255))
        img.save(dest, format="JPEG", quality=self.cfg.jpeg_quality)


BACKEND_CLASSES = {
    "usb": UsbCamera,
    "picamera2": Picamera2Camera,
    "rpicam-still": RpicamStillCamera,
    "mock": MockCamera,
}


def make_camera(cfg: CameraConfig) -> Camera:
    return BACKEND_CLASSES[cfg.backend](cfg)


def make_cameras(cfgs: list[CameraConfig]) -> list[Camera]:
    """Open every configured camera; any missing camera is a start-up error."""
    if not cfgs:
        raise RuntimeError("no cameras configured")
    cams: list[Camera] = []
    try:
        for c in cfgs:
            cams.append(make_camera(c))
    except Exception:
        for cam in cams:
            cam.close()
        raise
    return cams


def discover_cameras() -> list[dict]:
    """Every camera attached to this machine (USB webcams + Pi ribbon cameras)."""
    return list_usb_cameras() + list_csi_cameras()

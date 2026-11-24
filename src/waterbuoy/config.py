"""Configuration loading (TOML)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


BACKENDS = ("usb", "picamera2", "rpicam-still", "mock")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass
class CameraConfig:
    """One camera. Any field left out of a [[camera]] table falls back to [capture]."""

    name: str = "cam0"                 # used in file names and IoT messages
    backend: str = "usb"               # "usb" | "picamera2" | "rpicam-still" | "mock"
    width: int = 0                     # 0 = largest resolution the camera supports
    height: int = 0
    jpeg_quality: int = 90
    # usb: V4L2 device node, preferably a stable /dev/v4l/by-id/... link.
    # picamera2/rpicam-still: optional libcamera camera Id (see `waterbuoy --list-cameras`).
    device: str = ""
    # picamera2/rpicam-still: which ribbon camera (0 = first), used when `device` is empty.
    index: int = 0
    input_format: str = "auto"         # usb: "auto" | "mjpeg" | "yuyv422" | ... (V4L2 format)
    warmup_frames: int = 30            # usb: frames discarded so auto-exposure can settle

    def validate(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"camera name {self.name!r} must be 1-32 chars of A-Z a-z 0-9 _ -")
        if self.backend not in BACKENDS:
            raise ValueError(f"camera {self.name}: unknown backend {self.backend!r} (one of {BACKENDS})")
        if self.width < 0 or self.height < 0 or (self.width == 0) != (self.height == 0):
            raise ValueError(f"camera {self.name}: set both width and height, or both to 0 (auto)")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError(f"camera {self.name}: jpeg_quality must be 1..100")
        if self.warmup_frames < 0 or self.index < 0:
            raise ValueError(f"camera {self.name}: warmup_frames and index must be >= 0")
        if self.backend == "usb" and not self.device:
            raise ValueError(f"camera {self.name}: usb backend needs `device`")


CAMERA_KEYS = {f.name for f in fields(CameraConfig)}


@dataclass
class CaptureConfig:
    interval_seconds: int = 300
    align_to_clock: bool = True


@dataclass
class StorageConfig:
    base_dir: Path = Path("/home/pi/waterbuoy-data/images")
    min_free_mb: int = 1024


@dataclass
class IotConfig:
    enabled: bool = False
    endpoint: str = ""
    cert_path: Path = Path("certs/device.pem.crt")
    key_path: Path = Path("certs/private.pem.key")
    ca_path: Path = Path("certs/AmazonRootCA1.pem")
    port: int = 8883
    keep_alive_seconds: int = 30
    topic_prefix: str = "waterbuoy/{thing}"


@dataclass
class AlgaeConfig:
    enabled: bool = False
    # Region of interest as fractions of width/height: [x0, y0, x1, y1]. Use it to cut out sky,
    # shoreline or parts of the buoy that are in frame.
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    analysis_width: int = 320          # images are downscaled before analysis (speed)
    min_value: float = 0.12            # HSV value below this = too dark
    max_value: float = 0.97            # ... above this = glare
    min_saturation: float = 0.30       # below this = grey/white, ignored
    hue_min: float = 60.0              # degrees; ~60 yellow-green
    hue_max: float = 170.0             # degrees; ~170 blue-green
    min_exg: float = 0.04              # excess green on chromaticity (0..2)
    green_margin: float = 0.12         # green must exceed red and blue by this relative margin
    smooth: int = 3                    # box-filter size (pixels) applied before classifying; 1 = off
    min_valid_fraction: float = 0.25   # below this the image is labelled "unknown"
    suspect_fraction: float = 0.05
    bloom_fraction: float = 0.20

    def __post_init__(self):
        self.roi = tuple(float(v) for v in self.roi)
        x0, y0, x1, y1 = self.roi
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError(f"algae.roi must be [x0, y0, x1, y1] fractions with x0<x1, y0<y1: {self.roi}")
        if not 0 <= self.suspect_fraction <= self.bloom_fraction <= 1:
            raise ValueError("need 0 <= algae.suspect_fraction <= algae.bloom_fraction <= 1")


@dataclass
class Config:
    thing_name: str = "waterbuoy-01"
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    cameras: list[CameraConfig] = field(default_factory=list)
    storage: StorageConfig = field(default_factory=StorageConfig)
    iot: IotConfig = field(default_factory=IotConfig)
    algae: AlgaeConfig = field(default_factory=AlgaeConfig)

    @property
    def topic_prefix(self) -> str:
        return self.iot.topic_prefix.format(thing=self.thing_name)


def _resolve(base: Path, p: str | Path) -> Path:
    p = Path(p).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("rb") as f:
        raw = tomllib.load(f)

    # Relative paths are resolved against the project root (parent of config/).
    root = path.parent.parent if path.parent.name == "config" else path.parent

    cap_raw = dict(raw.get("capture", {}))
    # Camera keys under [capture] are defaults for every [[camera]].
    defaults = {k: cap_raw.pop(k) for k in list(cap_raw) if k in CAMERA_KEYS}
    cap = CaptureConfig(**cap_raw)
    if cap.interval_seconds < 1:
        raise ValueError("capture.interval_seconds must be >= 1")
    cameras = parse_cameras(raw.get("camera"), defaults)

    st_raw = dict(raw.get("storage", {}))
    if "base_dir" in st_raw:
        st_raw["base_dir"] = _resolve(root, st_raw["base_dir"])
    st = StorageConfig(**st_raw)

    iot_raw = dict(raw.get("iot", {}))
    for key in ("cert_path", "key_path", "ca_path"):
        if key in iot_raw:
            iot_raw[key] = _resolve(root, iot_raw[key])
    iot = IotConfig(**iot_raw)

    algae = AlgaeConfig(**raw.get("algae", {}))

    thing = raw.get("device", {}).get("thing_name", "waterbuoy-01")
    return Config(thing_name=thing, capture=cap, cameras=cameras, storage=st, iot=iot, algae=algae)


def parse_cameras(tables: list[dict] | None, defaults: dict | None = None) -> list[CameraConfig]:
    """Build the camera list from [[camera]] tables (or one camera from [capture] defaults)."""
    defaults = defaults or {}
    if not tables:
        tables = [{}]  # old single-camera config: everything is in [capture]
    if not isinstance(tables, list):
        raise ValueError("cameras must be declared as [[camera]] tables")
    cams = []
    for i, t in enumerate(tables):
        unknown = set(t) - CAMERA_KEYS
        if unknown:
            raise ValueError(f"[[camera]] #{i + 1}: unknown keys {sorted(unknown)}")
        merged = {"name": f"cam{i}", **defaults, **t}
        cam = CameraConfig(**merged)
        cam.validate()
        cams.append(cam)
    names = [c.name for c in cams]
    if len(set(names)) != len(names):
        raise ValueError(f"camera names must be unique: {names}")
    return cams

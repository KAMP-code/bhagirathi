"""Configuration loading (TOML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CaptureConfig:
    interval_seconds: int = 300
    align_to_clock: bool = True
    backend: str = "picamera2"
    width: int = 2304
    height: int = 1296
    jpeg_quality: int = 90


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

    cap = CaptureConfig(**raw.get("capture", {}))
    if cap.interval_seconds < 1:
        raise ValueError("capture.interval_seconds must be >= 1")
    if cap.backend not in ("picamera2", "rpicam-still", "mock"):
        raise ValueError(f"unknown capture.backend: {cap.backend!r}")

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
    return Config(thing_name=thing, capture=cap, storage=st, iot=iot, algae=algae)

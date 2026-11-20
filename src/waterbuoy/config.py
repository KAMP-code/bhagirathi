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
class Config:
    thing_name: str = "waterbuoy-01"
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    iot: IotConfig = field(default_factory=IotConfig)

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

    thing = raw.get("device", {}).get("thing_name", "waterbuoy-01")
    return Config(thing_name=thing, capture=cap, storage=st, iot=iot)

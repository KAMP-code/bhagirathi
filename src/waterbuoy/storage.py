"""Image storage on local disk: dated folders, atomic writes, free-space retention."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class ImageStore:
    def __init__(self, base_dir: Path, min_free_mb: int = 1024):
        self.base_dir = Path(base_dir)
        self.min_free_bytes = min_free_mb * 1024 * 1024
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, ts: datetime, camera: str | None = None) -> Path:
        """<base>/YYYY/MM/DD/YYYYMMDDTHHMMSSZ[_<camera>].jpg (UTC).

        The timestamp comes first so file names still sort chronologically across cameras
        (retention deletes the oldest first).
        """
        ts = ts.astimezone(timezone.utc)
        day_dir = self.base_dir / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
        stem = f"{ts:%Y%m%dT%H%M%SZ}" + (f"_{camera}" if camera else "")
        path = day_dir / f"{stem}.jpg"
        n = 1
        while path.exists():  # two captures in the same second (e.g. manual + scheduled)
            path = day_dir / f"{stem}_{n}.jpg"
            n += 1
        return path

    def save(self, ts: datetime, writer, camera: str | None = None) -> Path:
        """Call writer(tmp_path) to produce the image, then atomically move it into place."""
        self.ensure_free_space()
        dest = self.path_for(ts, camera)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            writer(tmp)
            with tmp.open("rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.base_dir).free

    def ensure_free_space(self) -> None:
        if self.free_bytes() >= self.min_free_bytes:
            return
        for img in sorted(self.base_dir.rglob("*.jpg")):  # names sort chronologically
            img.unlink(missing_ok=True)
            log.warning("low disk space: deleted %s", img)
            self._prune_empty_dirs(img.parent)
            if self.free_bytes() >= self.min_free_bytes:
                return
        log.error("still below min free space after cleanup")

    def _prune_empty_dirs(self, d: Path) -> None:
        while d != self.base_dir and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    def stats(self) -> dict:
        usage = shutil.disk_usage(self.base_dir)
        return {
            "base_dir": str(self.base_dir),
            "disk_free_mb": usage.free // (1024 * 1024),
            "disk_total_mb": usage.total // (1024 * 1024),
        }

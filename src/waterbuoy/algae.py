"""Algal-bloom detection from RGB images using colour indices.

Cyanobacteria / green-algae blooms make surface water distinctly green (pea-soup, scum,
streaks). Clean water is usually blue, grey or brown (sediment). Each pixel of the region
of interest (after a small box blur to suppress noise) is classified as *bloom-coloured* when it is:

* well exposed      - not too dark (night, shadow) and not specular glare
* saturated enough  - grey/white pixels (foam, overcast reflections, a colour cast) are ignored
* green hue         - hue between ``hue_min`` and ``hue_max`` degrees, and green brighter than
                      red and blue by at least ``green_margin`` (relative)
* excess green      - ExG = 2g - r - b on chromaticity coordinates above ``min_exg``

The image label is derived from the fraction of valid pixels that are bloom-coloured:

    unknown   too few valid pixels (night, fog, lens covered) - no decision
    clean     bloom_fraction <  suspect_fraction
    suspect   suspect_fraction <= bloom_fraction < bloom_fraction
    bloom     bloom_fraction >= bloom_fraction threshold

This is a heuristic screen, not a lab measurement: calibrate the thresholds (``[algae]`` in the
config) on images from your site, e.g. with ``waterbuoy-algae scan --sort-into``.

CLI:  python -m waterbuoy.algae scan <dir-or-files...> [--csv out.csv] [--sort-into DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .config import AlgaeConfig

log = logging.getLogger(__name__)

LABELS = ("clean", "suspect", "bloom", "unknown")


@dataclass
class AlgaeResult:
    label: str
    bloom_fraction: float      # bloom pixels / valid pixels
    valid_fraction: float      # valid pixels / ROI pixels
    mean_exg: float            # mean excess green over valid pixels
    bloom_exg: float           # mean excess green over bloom pixels (intensity)
    mean_rgb: tuple[int, int, int] = field(default=(0, 0, 0))

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("bloom_fraction", "valid_fraction", "mean_exg", "bloom_exg"):
            d[k] = round(d[k], 4)
        d["mean_rgb"] = list(self.mean_rgb)
        return d


def load_rgb(path: str | Path, width: int) -> np.ndarray:
    """Decode a (JPEG) image downscaled to about ``width`` pixels wide, as float RGB in [0, 1]."""
    with Image.open(path) as img:
        if img.width > width:
            h = max(1, round(img.height * width / img.width))
            img.draft("RGB", (width, h))  # fast DCT-domain downscale for JPEG
            img = img.convert("RGB").resize((width, h), Image.BILINEAR)
        else:
            img = img.convert("RGB")
        return np.asarray(img, dtype=np.float32) / 255.0


def box_smooth(rgb: np.ndarray, k: int) -> np.ndarray:
    """k x k mean filter (edge-padded); suppresses sensor noise and tiny specks."""
    if k <= 1:
        return rgb
    pad = k // 2
    padded = np.pad(rgb, ((pad, k - 1 - pad), (pad, k - 1 - pad), (0, 0)), mode="edge")
    h, w = rgb.shape[:2]
    out = np.zeros_like(rgb)
    for dy in range(k):
        for dx in range(k):
            out += padded[dy:dy + h, dx:dx + w]
    return out / (k * k)


def bloom_mask(rgb: np.ndarray, cfg: AlgaeConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (valid, bloom, exg) per-pixel arrays for an RGB float image."""
    rgb = box_smooth(rgb, cfg.smooth)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    delta = mx - mn
    sat = np.where(mx > 0, delta / np.maximum(mx, 1e-6), 0.0)

    # Hue in degrees (standard HSV formula).
    d = np.maximum(delta, 1e-6)
    hue = np.select(
        [mx == r, mx == g],
        [(60 * (g - b) / d) % 360, 60 * (b - r) / d + 120],
        default=60 * (r - g) / d + 240,
    )

    total = r + g + b + 1e-6
    exg = (2 * g - r - b) / total  # excess green on chromaticity coordinates

    valid = (mx >= cfg.min_value) & (mx <= cfg.max_value)
    bloom = (
        valid
        & (sat >= cfg.min_saturation)
        & (hue >= cfg.hue_min) & (hue <= cfg.hue_max)
        & (g >= r * (1 + cfg.green_margin)) & (g >= b * (1 + cfg.green_margin))
        & (exg >= cfg.min_exg)
    )
    return valid, bloom, exg


def analyze_array(rgb: np.ndarray, cfg: AlgaeConfig) -> AlgaeResult:
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = cfg.roi
    roi = rgb[int(y0 * h):max(int(y1 * h), int(y0 * h) + 1), int(x0 * w):max(int(x1 * w), int(x0 * w) + 1)]
    valid, bloom, exg = bloom_mask(roi, cfg)

    n_valid = int(valid.sum())
    valid_fraction = n_valid / valid.size
    bloom_fraction = float(bloom.sum()) / n_valid if n_valid else 0.0
    mean_exg = float(exg[valid].mean()) if n_valid else 0.0
    bloom_exg = float(exg[bloom].mean()) if bloom.any() else 0.0
    mean_rgb = tuple(int(v) for v in (roi[valid].mean(axis=0) * 255 if n_valid else (0, 0, 0)))

    if valid_fraction < cfg.min_valid_fraction:
        label = "unknown"
    elif bloom_fraction >= cfg.bloom_fraction:
        label = "bloom"
    elif bloom_fraction >= cfg.suspect_fraction:
        label = "suspect"
    else:
        label = "clean"
    return AlgaeResult(label, bloom_fraction, valid_fraction, mean_exg, bloom_exg, mean_rgb)


def analyze_image(path: str | Path, cfg: AlgaeConfig | None = None) -> AlgaeResult:
    cfg = cfg or AlgaeConfig()
    return analyze_array(load_rgb(path, cfg.analysis_width), cfg)


def save_overlay(path: str | Path, dest: str | Path, cfg: AlgaeConfig | None = None) -> None:
    """Write a debug image: bloom pixels painted magenta, invalid pixels darkened, ROI outlined."""
    cfg = cfg or AlgaeConfig()
    rgb = load_rgb(path, cfg.analysis_width)
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = (int(cfg.roi[0] * w), int(cfg.roi[1] * h), int(cfg.roi[2] * w), int(cfg.roi[3] * h))
    out = rgb.copy()
    valid, bloom, _ = bloom_mask(rgb[y0:y1, x0:x1], cfg)
    sub = out[y0:y1, x0:x1]
    sub[~valid] *= 0.35
    sub[bloom] = (1.0, 0.0, 1.0)
    out[y0:y1, [x0, x1 - 1]] = (1, 1, 0)
    out[[y0, y1 - 1], x0:x1] = (1, 1, 0)
    Image.fromarray((out * 255).astype(np.uint8)).save(dest, quality=85)


# --------------------------------------------------------------------------- CLI

def iter_images(paths: list[str]) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png"}
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(f for f in p.rglob("*") if f.suffix.lower() in exts and f.is_file())
        elif p.is_file():
            found.append(p)
        else:
            log.warning("not found: %s", p)
    return found


def _cmd_scan(args: argparse.Namespace) -> int:
    cfg = AlgaeConfig()
    if args.config:
        from .config import load_config
        cfg = load_config(args.config).algae

    images = iter_images(args.paths)
    if not images:
        print("no images found", file=sys.stderr)
        return 1

    rows = []
    for img in images:
        try:
            res = analyze_image(img, cfg)
        except Exception as e:  # noqa: BLE001 - corrupt file etc.
            log.warning("cannot analyse %s: %s", img, e)
            continue
        rows.append({"path": str(img), **res.to_dict()})
        if args.sort_into:
            target = Path(args.sort_into) / res.label
            target.mkdir(parents=True, exist_ok=True)
            link = target / img.name
            n = 1
            while link.exists() or link.is_symlink():
                link = target / f"{img.stem}_{n}{img.suffix}"
                n += 1
            os.symlink(img.resolve(), link)
            if args.overlay:
                save_overlay(img, target / f"{link.stem}.overlay.jpg", cfg)

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        print(f"{'label':8} {'bloom%':>7} {'valid%':>7} {'exg':>6}  path")
        for r in rows:
            print(f"{r['label']:8} {r['bloom_fraction']*100:7.1f} {r['valid_fraction']*100:7.1f} "
                  f"{r['mean_exg']:6.3f}  {r['path']}")
        counts = {lbl: sum(r["label"] == lbl for r in rows) for lbl in LABELS}
        print("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items()))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="waterbuoy-algae", description="Algal-bloom detection on buoy images")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="classify images as clean / suspect / bloom / unknown")
    s.add_argument("paths", nargs="+", help="image files or directories (searched recursively)")
    s.add_argument("-c", "--config", help="config.toml to read [algae] thresholds from")
    s.add_argument("--csv", help="write results to this CSV file")
    s.add_argument("--json", action="store_true", help="print JSON instead of a table")
    s.add_argument("--sort-into", metavar="DIR", help="symlink images into DIR/<label>/")
    s.add_argument("--overlay", action="store_true", help="with --sort-into: also write debug overlays")
    s.set_defaults(func=_cmd_scan)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

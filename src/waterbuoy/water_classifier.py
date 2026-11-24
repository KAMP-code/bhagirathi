"""Classify an image as clean water, dirty water or no water, with no training.

Two steps:

1. **Water or not** - a pretrained YOLOv8 image classifier (ImageNet, 1000 classes) is
   run on the image. ImageNet has no "water" class, but it has many water scenes,
   boats and water animals (lakeside, dam, sandbar, canoe, water snake, coral reef...).
   The total probability the model puts on those classes is the *water score*: photos
   of water score ~0.6-0.7, a room or a person ~0.01.
2. **Dirty or clean** - water is dirty when enough of the image is discoloured:
   - *green fraction*: strongly green pixels (hue 60-150 degrees, saturation >= 0.3).
     Algae blooms and scum are lime/pea green over much of the frame.
   - *red fraction*: saturated red / rust pixels (hue 345-30 degrees, saturation >= 0.3).
     Red tides are red-brown; sand and beaches are paler and more yellow (hue 30-50).
   Clean water is blue, grey or shows the brown bed. Pixels too dark to judge are ignored.

    from waterbuoy.water_classifier import WaterClassifier

    clf = WaterClassifier()
    r = clf.classify("photo.jpg")      # path, PIL image, or RGB numpy array
    r.label                            # "clean_water" | "dirty_water" | "no_water"

The YOLOv8 weights (yolov8s-cls.pt, ~13 MB) are downloaded once into `model_dir`.
Requires the `ultralytics` package (`pip install -e .[vision]`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

LABELS = ("clean_water", "dirty_water", "no_water")
DEFAULT_MODEL = "yolov8s-cls.pt"
DEFAULT_MODEL_DIR = Path.home() / "waterbuoy-data" / "models"

# ImageNet classes whose presence means the picture shows open water.
WATER_CLASSES = (
    # scenes and structures
    "lakeside", "seashore", "sandbar", "dam", "boathouse", "breakwater", "dock", "wreck",
    "coral_reef",
    # boats
    "canoe", "paddle", "lifeboat", "speedboat", "fireboat", "gondola", "catamaran",
    "trimaran", "yawl", "schooner", "container_ship", "liner", "submarine",
    # people in water
    "scuba_diver", "snorkel",
    # fish and other water animals
    "tench", "goldfish", "coho", "gar", "sturgeon", "eel", "stingray", "electric_ray",
    "anemone_fish", "lionfish", "puffer", "barracouta", "great_white_shark", "tiger_shark",
    "hammerhead", "water_snake", "sea_snake", "American_alligator", "African_crocodile",
    "bullfrog", "mud_turtle", "terrapin", "loggerhead", "leatherback_turtle", "otter",
    "beaver", "platypus", "sea_lion", "dugong", "grey_whale", "killer_whale", "hippopotamus",
    "jellyfish", "sea_anemone", "brain_coral", "starfish", "sea_urchin", "sea_cucumber",
    "sea_slug",
    # water birds
    "drake", "red-breasted_merganser", "goose", "black_swan", "American_coot", "pelican",
    "flamingo", "little_blue_heron", "American_egret", "bittern",
)

GREEN_HUE = (60.0, 150.0)   # degrees
GREEN_MIN_SAT = 0.30
RED_HUE = (345.0, 30.0)     # degrees, wraps through 0
RED_MIN_SAT = 0.30
MIN_VALUE = 0.15            # ignore dark pixels (shadow, night)


@dataclass
class WaterResult:
    label: str                  # "clean_water" | "dirty_water" | "no_water"
    water_score: float          # probability mass on water classes, 0..1
    green_fraction: float       # strongly green pixels / well-lit pixels, 0..1
    red_fraction: float = 0.0   # red / rust pixels / well-lit pixels, 0..1
    top_classes: list[tuple[str, float]] = field(default_factory=list)  # YOLO top 5

    def __str__(self) -> str:
        return (f"{self.label} (water {self.water_score:.2f}, green {self.green_fraction:.2f}, "
                f"red {self.red_fraction:.2f})")


def to_rgb_image(image: str | Path | Image.Image | np.ndarray) -> Image.Image:
    """Path / PIL image / HxWx3 uint8 RGB array -> PIL RGB image."""
    if isinstance(image, (str, Path)):
        with Image.open(image) as im:
            return im.convert("RGB")
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected an HxWx3 RGB array, got shape {arr.shape}")
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def colour_fractions(img: Image.Image, size: int = 320) -> tuple[float, float]:
    """(green, red) fractions of well-lit pixels: strongly green, and red / rust."""
    small = img.copy()
    small.thumbnail((size, size))
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32) / 255.0
    h, s, v = hsv[..., 0] * 360.0, hsv[..., 1], hsv[..., 2]
    lit = v >= MIN_VALUE
    n = int(lit.sum())
    if n == 0:
        return 0.0, 0.0
    green = lit & (h >= GREEN_HUE[0]) & (h <= GREEN_HUE[1]) & (s >= GREEN_MIN_SAT)
    red = lit & ((h >= RED_HUE[0]) | (h < RED_HUE[1])) & (s >= RED_MIN_SAT)
    return float(green.sum()) / n, float(red.sum()) / n


def green_fraction(img: Image.Image, size: int = 320) -> float:
    """Fraction of well-lit pixels that are strongly green."""
    return colour_fractions(img, size)[0]


def red_fraction(img: Image.Image, size: int = 320) -> float:
    """Fraction of well-lit pixels that are red / rust (red tide)."""
    return colour_fractions(img, size)[1]


class WaterClassifier:
    """clean water / dirty water / no water, from a pretrained YOLOv8 classifier plus colour.

    `water_threshold`: minimum water score to call it water (water photos ~0.6, rooms ~0.01).
    `green_threshold`: minimum green fraction to call water dirty (clean ~0.01, blooms 0.25+).
    `red_threshold`: minimum red fraction to call water dirty (clean/sand <= 0.02, red tide 0.15+).
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        model_dir: str | Path = DEFAULT_MODEL_DIR,
        water_threshold: float = 0.15,
        green_threshold: float = 0.10,
        red_threshold: float = 0.08,
    ):
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError("ultralytics not installed; run: pip install -e .[vision]") from e
        self.water_threshold = water_threshold
        self.green_threshold = green_threshold
        self.red_threshold = red_threshold
        path = Path(model)
        if not path.is_absolute():
            path = Path(model_dir) / path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            from ultralytics.utils.downloads import attempt_download_asset

            log.info("downloading %s to %s", path.name, path.parent)
            attempt_download_asset(path)
        self.model = YOLO(str(path), task="classify")
        names = self.model.names
        index = {n: i for i, n in names.items()}
        missing = [c for c in WATER_CLASSES if c not in index]
        if missing:
            log.warning("water classes not in model, ignored: %s", missing)
        self.water_ids = [index[c] for c in WATER_CLASSES if c in index]
        if not self.water_ids:
            raise RuntimeError(f"{path.name} is not an ImageNet classifier (no water classes found)")

    def classify(self, image: str | Path | Image.Image | np.ndarray) -> WaterResult:
        img = to_rgb_image(image)
        # ultralytics treats numpy arrays as BGR (OpenCV order): flip RGB -> BGR
        r = self.model(np.asarray(img)[..., ::-1], verbose=False)[0]
        probs = r.probs.data.cpu().numpy()
        water = float(probs[self.water_ids].sum())
        green, red = colour_fractions(img)
        if water < self.water_threshold:
            label = "no_water"
        elif green >= self.green_threshold or red >= self.red_threshold:
            label = "dirty_water"
        else:
            label = "clean_water"
        top = [(r.names[i], round(float(probs[i]), 3)) for i in r.probs.top5]
        return WaterResult(label, water, green, red, top)

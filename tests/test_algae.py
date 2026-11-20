import numpy as np
import pytest
from PIL import Image

from waterbuoy.algae import analyze_array, analyze_image, iter_images, main, save_overlay
from waterbuoy.app import BuoyApp
from waterbuoy.camera import MockCamera
from waterbuoy.config import AlgaeConfig, CaptureConfig, Config, StorageConfig, load_config

from .test_app import FakeIot

RNG = np.random.default_rng(0)


def scene(rgb, size=(120, 160), noise=12):
    """Uniform colour plus sensor-like noise, uint8 HxWx3."""
    base = np.broadcast_to(np.array(rgb, dtype=np.float32), (*size, 3))
    img = base + RNG.normal(0, noise, (*size, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def as_float(img):
    return img.astype(np.float32) / 255.0


CLEAR_BLUE = (35, 85, 135)
MUDDY_BROWN = (120, 100, 70)
PEA_SOUP = (70, 125, 45)
DARK_TEAL_CLEAN = (30, 70, 80)   # deep clean water: blue-green but blue-dominant


@pytest.mark.parametrize("rgb", [CLEAR_BLUE, MUDDY_BROWN, DARK_TEAL_CLEAN])
def test_clean_water(rgb):
    res = analyze_array(as_float(scene(rgb)), AlgaeConfig())
    assert res.label == "clean", res
    assert res.bloom_fraction < 0.05


def test_full_bloom():
    res = analyze_array(as_float(scene(PEA_SOUP)), AlgaeConfig())
    assert res.label == "bloom"
    assert res.bloom_fraction > 0.9
    assert res.bloom_exg > 0.1


def test_partial_scum_patch_is_suspect_then_bloom():
    img = scene(CLEAR_BLUE)
    img[:, :16] = scene(PEA_SOUP, size=(120, 16))         # 10% of frame
    assert analyze_array(as_float(img), AlgaeConfig()).label == "suspect"
    img[:, :48] = scene(PEA_SOUP, size=(120, 48))         # 30% of frame
    assert analyze_array(as_float(img), AlgaeConfig()).label == "bloom"


def test_night_frame_is_unknown():
    res = analyze_array(as_float(scene((8, 10, 12), noise=3)), AlgaeConfig())
    assert res.label == "unknown"


def test_glare_is_ignored():
    img = scene(CLEAR_BLUE)
    img[:60] = 255                                        # half the frame blown out
    res = analyze_array(as_float(img), AlgaeConfig())
    assert res.label == "clean"
    assert 0.4 < res.valid_fraction < 0.6


def test_greenish_colour_cast_on_grey_is_not_bloom():
    # Like a white ceiling under fluorescent light: grey with a slight green/cyan tint.
    res = analyze_array(as_float(scene((150, 175, 165), noise=6)), AlgaeConfig())
    assert res.label == "clean", res


def test_roi_excludes_green_shore():
    img = scene(CLEAR_BLUE)
    img[:40] = scene((60, 120, 40), size=(40, 160))       # vegetation on top third
    assert analyze_array(as_float(img), AlgaeConfig()).label == "bloom"
    cfg = AlgaeConfig(roi=(0, 0.4, 1, 1))
    assert analyze_array(as_float(img), cfg).label == "clean"


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        AlgaeConfig(roi=(0.5, 0, 0.2, 1))
    with pytest.raises(ValueError):
        AlgaeConfig(suspect_fraction=0.5, bloom_fraction=0.1)


def test_analyze_jpeg_file_downscaled(tmp_path):
    p = tmp_path / "big.jpg"
    Image.fromarray(scene(PEA_SOUP, size=(1296, 2304))).save(p, quality=90)
    res = analyze_image(p, AlgaeConfig(analysis_width=320))
    assert res.label == "bloom"
    save_overlay(p, tmp_path / "ov.jpg")
    assert Image.open(tmp_path / "ov.jpg").width == 320


def test_cli_scan_sorts_images(tmp_path, capsys):
    src = tmp_path / "images" / "2026" / "11" / "20"
    src.mkdir(parents=True)
    Image.fromarray(scene(CLEAR_BLUE)).save(src / "a.jpg")
    Image.fromarray(scene(PEA_SOUP)).save(src / "b.jpg")
    (src / "notes.txt").write_text("x")
    out = tmp_path / "sorted"
    rc = main(["scan", str(tmp_path / "images"), "--sort-into", str(out), "--overlay",
               "--csv", str(tmp_path / "r.csv")])
    assert rc == 0
    assert (out / "clean" / "a.jpg").is_symlink()
    assert (out / "bloom" / "b.jpg").is_symlink()
    assert (out / "bloom" / "b.overlay.jpg").exists()
    assert "clean: 1  suspect: 0  bloom: 1" in capsys.readouterr().out
    assert len((tmp_path / "r.csv").read_text().splitlines()) == 3
    assert len(iter_images([str(tmp_path / "images")])) == 2


def test_app_publishes_algae_result(tmp_path):
    cfg = Config(
        capture=CaptureConfig(backend="mock", width=64, height=48, align_to_clock=False),
        storage=StorageConfig(base_dir=tmp_path, min_free_mb=0),
        algae=AlgaeConfig(enabled=True),
    )
    app = BuoyApp(cfg, camera=MockCamera(cfg.capture), iot=FakeIot())
    app.capture_once()
    kind, info = app.iot.messages[-1]
    assert kind == "capture"
    assert info["algae"]["label"] == "clean"   # mock camera produces blue water
    assert app.last_algae == info["algae"]


def test_example_config_has_algae_section():
    from pathlib import Path
    cfg = load_config(Path(__file__).parent.parent / "config" / "config.example.toml")
    assert cfg.algae.enabled
    assert cfg.algae.roi == (0.0, 0.0, 1.0, 1.0)


def test_dilute_bloom_still_detected():
    # Paler, less saturated green water than pea soup.
    res = analyze_array(as_float(scene((85, 125, 70))), AlgaeConfig())
    assert res.label == "bloom", res


def test_camera_green_cast_on_white_is_not_bloom():
    # Measured on this buoy's OV5647: a white ceiling renders around RGB (106, 135, 111).
    res = analyze_array(as_float(scene((106, 135, 111), noise=8)), AlgaeConfig())
    assert res.label == "clean", res

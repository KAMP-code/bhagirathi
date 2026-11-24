import json

import pytest
import threading
import time
from datetime import datetime, timezone

from waterbuoy.app import BuoyApp, seconds_until_next
from waterbuoy.config import CameraConfig, CaptureConfig, Config, StorageConfig, load_config
from waterbuoy.camera import MockCamera
from waterbuoy.storage import ImageStore


class FakeIot:
    def __init__(self):
        self.messages = []

    def publish_capture(self, **info):
        self.messages.append(("capture", info))

    def publish_status(self, state, **extra):
        self.messages.append(("status", {"state": state, **extra}))

    def connect(self):
        pass

    def disconnect(self):
        pass


def make_app(tmp_path, interval=300, names=("cam0",)):
    cfg = Config(
        capture=CaptureConfig(interval_seconds=interval, align_to_clock=False),
        cameras=[CameraConfig(name=n, backend="mock", width=64, height=48) for n in names],
        storage=StorageConfig(base_dir=tmp_path, min_free_mb=0),
    )
    return BuoyApp(cfg, iot=FakeIot())


def test_seconds_until_next_aligned():
    assert seconds_until_next(300, True, now=1_000_000_000) == 300 - (1_000_000_000 % 300)
    assert seconds_until_next(300, False) == 300


def test_store_paths_are_dated_and_unique(tmp_path):
    store = ImageStore(tmp_path, min_free_mb=0)
    ts = datetime(2026, 11, 20, 13, 5, 0, tzinfo=timezone.utc)
    p1 = store.save(ts, lambda p: p.write_bytes(b"a"))
    p2 = store.save(ts, lambda p: p.write_bytes(b"b"))
    assert p1 == tmp_path / "2026/11/20/20261120T130500Z.jpg"
    assert p2.name == "20261120T130500Z_1.jpg"
    p3 = store.save(ts, lambda p: p.write_bytes(b"c"), camera="north")
    p4 = store.save(ts, lambda p: p.write_bytes(b"d"), camera="north")
    assert p3.name == "20261120T130500Z_north.jpg"
    assert p4.name == "20261120T130500Z_north_1.jpg"
    assert not list(tmp_path.rglob("*.part"))


def test_failed_write_leaves_no_partial(tmp_path):
    store = ImageStore(tmp_path, min_free_mb=0)

    def boom(p):
        p.write_bytes(b"half")
        raise RuntimeError("camera died")

    try:
        store.save(datetime.now(timezone.utc), boom)
    except RuntimeError:
        pass
    assert not list(tmp_path.rglob("*.jpg*"))


def test_capture_once_publishes(tmp_path):
    app = make_app(tmp_path)
    app.capture_once()
    imgs = list(tmp_path.rglob("*.jpg"))
    assert len(imgs) == 1 and imgs[0].read_bytes()[:2] == b"\xff\xd8"
    kind, info = app.iot.messages[-1]
    assert kind == "capture" and info["size_bytes"] > 0


def test_run_loop_and_commands(tmp_path):
    app = make_app(tmp_path, interval=1)
    t = threading.Thread(target=app.run)
    t.start()
    time.sleep(2.5)
    app.handle_command({"action": "set_interval", "seconds": 3600})
    app.handle_command({"action": "capture"})
    time.sleep(0.5)
    app.stop()
    t.join(5)
    assert not t.is_alive()
    triggers = [m[1]["trigger"] for m in app.iot.messages if m[0] == "capture"]
    assert triggers.count("schedule") >= 2
    assert triggers[-1] == "command"
    assert app.interval == 3600


def test_example_config_loads(tmp_path):
    from pathlib import Path
    cfg = load_config(Path(__file__).parent.parent / "config" / "config.example.toml")
    assert cfg.capture.interval_seconds == 300
    assert cfg.topic_prefix == "waterbuoy/waterbuoy-01"
    assert cfg.iot.cert_path.is_absolute()
    assert [(c.name, c.backend, c.jpeg_quality) for c in cfg.cameras] == [("cam0", "usb", 90)]


class FailingCamera(MockCamera):
    def _capture(self, dest):
        dest.write_bytes(b"partial")
        raise RuntimeError("lens fell off")


def test_multiple_cameras_one_image_each(tmp_path):
    app = make_app(tmp_path, names=("north", "south", "csi0"))
    app.capture_once()
    names = sorted(p.name.split("_", 1)[1] for p in tmp_path.rglob("*.jpg"))
    assert names == ["csi0.jpg", "north.jpg", "south.jpg"]
    caps = [m[1] for m in app.iot.messages if m[0] == "capture"]
    assert [c["camera"] for c in caps] == ["north", "south", "csi0"]
    assert app.capture_count == 3
    assert all(s["captures"] == 1 for s in app.camera_stats.values())


def test_capture_single_camera(tmp_path):
    app = make_app(tmp_path, names=("north", "south"))
    app.capture_once(camera="south")
    assert [p.name.split("_", 1)[1] for p in tmp_path.rglob("*.jpg")] == ["south.jpg"]
    with pytest.raises(ValueError):
        app.capture_once(camera="east")
    with pytest.raises(ValueError):
        app.handle_command({"action": "capture", "camera": "east"})


def test_failing_camera_does_not_block_others(tmp_path):
    good = CameraConfig(name="good", backend="mock", width=64, height=48)
    bad = CameraConfig(name="bad", backend="mock", width=64, height=48)
    cfg = Config(cameras=[bad, good], storage=StorageConfig(base_dir=tmp_path, min_free_mb=0))
    app = BuoyApp(cfg, cameras=[FailingCamera(bad), MockCamera(good)], iot=FakeIot())
    app.capture_once()
    assert [p.name.split("_", 1)[1] for p in tmp_path.rglob("*.jpg*")] == ["good.jpg"]
    assert app.camera_stats["bad"]["errors"] == 1 and "lens fell off" in app.camera_stats["bad"]["last_error"]
    assert app.camera_stats["good"]["captures"] == 1
    err = [m[1] for m in app.iot.messages if m[0] == "status"][0]
    assert err["state"] == "error" and err["camera"] == "bad"


def test_status_reports_every_camera(tmp_path):
    app = make_app(tmp_path, names=("a", "b"))
    app.capture_once()
    app.handle_command({"action": "status"})
    status = app.iot.messages[-1][1]
    assert set(status["cameras"]) == {"a", "b"}
    assert status["cameras"]["a"]["captures"] == 1
    assert status["last_image"].endswith((".jpg"))


def test_app_requires_a_camera(tmp_path):
    cfg = Config(cameras=[], storage=StorageConfig(base_dir=tmp_path, min_free_mb=0))
    with pytest.raises(RuntimeError, match="no cameras configured"):
        BuoyApp(cfg)


def test_command_capture_targets_one_camera_in_run_loop(tmp_path):
    app = make_app(tmp_path, interval=3600, names=("a", "b"))
    t = threading.Thread(target=app.run)
    t.start()
    app.handle_command({"action": "capture", "camera": "b"})
    time.sleep(0.5)
    app.stop()
    t.join(5)
    caps = [m[1] for m in app.iot.messages if m[0] == "capture"]
    assert [(c["camera"], c["trigger"]) for c in caps] == [("b", "command")]

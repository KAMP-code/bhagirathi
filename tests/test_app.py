import json
import threading
import time
from datetime import datetime, timezone

from waterbuoy.app import BuoyApp, seconds_until_next
from waterbuoy.config import CaptureConfig, Config, StorageConfig, load_config
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


def make_app(tmp_path, interval=300):
    cfg = Config(
        capture=CaptureConfig(interval_seconds=interval, backend="mock", width=64, height=48, align_to_clock=False),
        storage=StorageConfig(base_dir=tmp_path, min_free_mb=0),
    )
    return BuoyApp(cfg, camera=MockCamera(cfg.capture), iot=FakeIot())


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

"""Dirty-water colour measures and the demo's servo alarm logic (no hardware, no model)."""

import importlib.util
from pathlib import Path

import numpy as np
from PIL import Image

from waterbuoy.servo import Servo
from waterbuoy.water_classifier import green_fraction, red_fraction

ROOT = Path(__file__).resolve().parents[1]


def load_demo():
    spec = importlib.util.spec_from_file_location("water_demo", ROOT / "scripts" / "water_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDriver:
    def __init__(self):
        self.pulses = []

    def set_pulse(self, pulse_us, frequency):
        self.pulses.append(pulse_us)

    def stop(self):
        pass

    def close(self):
        pass


def solid(rgb):
    return Image.fromarray(np.full((50, 50, 3), rgb, np.uint8), "RGB")


def test_green_fraction():
    assert green_fraction(solid((60, 200, 40))) == 1.0      # lime green
    assert green_fraction(solid((40, 90, 200))) == 0.0      # blue water
    assert green_fraction(solid((120, 120, 120))) == 0.0    # grey
    assert green_fraction(solid((5, 20, 5))) == 0.0         # too dark to judge
    half = np.zeros((50, 50, 3), np.uint8)
    half[:25] = (60, 200, 40)
    half[25:] = (40, 90, 200)
    assert green_fraction(Image.fromarray(half)) == 0.5


def test_red_fraction():
    assert red_fraction(solid((170, 60, 30))) == 1.0        # red tide, rust
    assert red_fraction(solid((200, 30, 40))) == 1.0        # red, hue just below 360
    assert red_fraction(solid((210, 190, 150))) == 0.0      # pale sand
    assert red_fraction(solid((40, 90, 200))) == 0.0        # blue water
    assert red_fraction(solid((60, 200, 40))) == 0.0        # green
    assert red_fraction(solid((20, 5, 5))) == 0.0           # too dark to judge


def make_alarm(demo):
    d = FakeDriver()
    s = Servo(16, driver=d)
    alarm = demo.DirtyWaterAlarm(s, hold_s=0, confirm=2)
    d.pulses.clear()
    return alarm, d, s


def test_alarm_needs_confirmation_and_cycles_servo():
    demo = load_demo()
    alarm, d, s = make_alarm(demo)
    assert alarm.update("dirty_water") is False             # 1st dirty frame: not yet
    assert alarm.update("dirty_water") is True              # confirmed
    assert d.pulses == [s.pulse_for(180), s.pulse_for(0)]   # out to 180 and back to 0
    assert not s.attached                                   # released while idle


def test_alarm_fires_once_until_rearmed():
    demo = load_demo()
    alarm, d, _ = make_alarm(demo)
    fired = [alarm.update(label) for label in
             ["dirty_water"] * 5 + ["clean_water"] + ["dirty_water"] * 2 + ["no_water", "dirty_water"]]
    assert fired == [False, True, False, False, False, False, False, True, False, False]


def test_single_dirty_frames_do_not_fire():
    demo = load_demo()
    alarm, _, _ = make_alarm(demo)
    assert not any(alarm.update(label) for label in ["dirty_water", "clean_water"] * 5)

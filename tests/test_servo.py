"""Servo logic with a fake pulse driver (no hardware)."""

import time

import pytest

from waterbuoy.servo import Servo


class FakeDriver:
    def __init__(self):
        self.pulses = []      # (pulse_us, frequency, t)
        self.stopped = 0
        self.closed = 0

    def set_pulse(self, pulse_us, frequency):
        self.pulses.append((pulse_us, frequency, time.monotonic()))

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def servo():
    return Servo(16, driver=FakeDriver())


@pytest.mark.parametrize("angle, pulse", [(0, 500), (90, 1450), (180, 2400), (45, 975)])
def test_angle_to_pulse_sg90_defaults(servo, angle, pulse):
    servo.angle = angle
    assert servo.driver.pulses[-1][:2] == (pulse, 50)
    assert servo.angle == angle and servo.attached


def test_custom_range():
    s = Servo(16, min_pulse_us=1000, max_pulse_us=2000, min_angle=-90, max_angle=90, driver=FakeDriver())
    assert [s.pulse_for(a) for a in (-90, 0, 90)] == [1000, 1500, 2000]


@pytest.mark.parametrize("angle", [-1, 180.5, 360])
def test_out_of_range_rejected(servo, angle):
    with pytest.raises(ValueError, match="outside 0..180"):
        servo.angle = angle
    assert servo.driver.pulses == []


@pytest.mark.parametrize("kw", [dict(min_pulse_us=2400, max_pulse_us=500), dict(max_pulse_us=25000),
                                dict(min_angle=10, max_angle=10)])
def test_invalid_setup(kw):
    with pytest.raises(ValueError):
        Servo(16, driver=FakeDriver(), **kw)


def test_move_to_sweeps_smoothly_at_speed(servo):
    servo.angle = 0
    t0 = time.monotonic()
    servo.move_to(60, speed=300, step=2)      # 60 deg at 300 deg/s = 0.2 s, 30 steps
    elapsed = time.monotonic() - t0
    pulses = [p for p, _, _ in servo.driver.pulses[1:]]
    assert len(pulses) == 30
    assert pulses == sorted(pulses) and pulses[-1] == servo.pulse_for(60)
    assert 0.18 <= elapsed < 0.4
    assert servo.angle == 60


def test_move_to_without_speed_or_when_detached_jumps(servo):
    servo.move_to(120)
    assert len(servo.driver.pulses) == 1
    servo.detach()
    servo.move_to(30, speed=10)              # start unknown -> jump
    assert len(servo.driver.pulses) == 2 and servo.angle == 30


def test_center_detach_close(servo):
    servo.center()
    assert servo.angle == 90
    servo.detach()
    assert servo.angle is None and not servo.attached and servo.driver.stopped == 1
    servo.close()
    servo.close()
    assert servo.driver.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        servo.angle = 10


def test_context_manager_closes():
    d = FakeDriver()
    with Servo(16, driver=d) as s:
        s.angle = 10
    assert d.closed == 1

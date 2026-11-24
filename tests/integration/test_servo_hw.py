"""Integration tests for the servo on GPIO16 (TowerPro SG90).

Run:  pytest tests/integration/test_servo_hw.py -v
Pin:  WATERBUOY_SERVO_PIN=16 (default)

These tests move the servo and measure the real control signal by reading the pin back:
pulse width (sets the angle) and pulse frequency (50 Hz). An SG90 has no position
feedback, so the signal is what can be verified; watch the horn to see that it follows.
Fails if the GPIO pin cannot be driven.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

from waterbuoy.servo import Servo

PIN = int(os.environ.get("WATERBUOY_SERVO_PIN", "16"))
PULSE_TOLERANCE_US = 50     # lgpio pulses are software-timed; measured error is ~20 us
SETTLE_S = 0.3              # let the servo get there before measuring


def measure(servo: Servo, seconds: float = 0.5) -> tuple[float, float]:
    """Sample the pin as fast as possible -> (median pulse width in us, pulse frequency in Hz).

    Each pulse is timed from its rising to its falling edge and the median is taken, so
    a pulse distorted by the sampling thread being descheduled does not skew the result.
    """
    read, now = servo.driver.read, time.perf_counter
    widths, rising = [], 0
    prev, t_rise = read(), None
    t0 = now()
    while (t := now()) - t0 < seconds:
        v = read()
        if v and not prev:
            rising += 1
            t_rise = t
        elif prev and not v and t_rise is not None:
            widths.append((t - t_rise) * 1e6)
        prev = v
    freq = rising / (t - t0)
    return (statistics.median(widths) if widths else 0.0), freq


@pytest.fixture
def servo():
    s = Servo(PIN)          # fails (not skips) if the pin cannot be opened
    yield s
    s.close()


@pytest.mark.parametrize("angle", [0, 45, 90, 135, 180])
def test_angle_produces_correct_pulse(servo, angle):
    servo.angle = angle
    time.sleep(SETTLE_S)
    pulse, freq = measure(servo)
    expected = servo.pulse_for(angle)
    assert abs(pulse - expected) <= PULSE_TOLERANCE_US, f"{angle} deg: {pulse:.0f} us, expected {expected} us"
    assert 47 <= freq <= 53, f"pulse frequency {freq:.1f} Hz, expected 50 Hz"


def test_smooth_sweep_takes_expected_time_and_ends_on_target(servo):
    servo.angle = 0
    time.sleep(0.5)
    t0 = time.monotonic()
    servo.move_to(180, speed=180)   # 1 s sweep
    elapsed = time.monotonic() - t0
    assert 0.95 <= elapsed <= 1.3, f"sweep took {elapsed:.2f}s"
    time.sleep(SETTLE_S)
    pulse, _ = measure(servo)
    assert abs(pulse - servo.pulse_for(180)) <= PULSE_TOLERANCE_US


def test_detach_stops_pulses(servo):
    servo.angle = 90
    time.sleep(SETTLE_S)
    assert measure(servo, 0.2)[1] > 40
    servo.detach()
    time.sleep(0.05)
    pulse, freq = measure(servo, 0.3)
    assert freq == 0 and servo.driver.read() == 0, "pulses still present after detach"
    servo.angle = 90                 # re-attach
    time.sleep(SETTLE_S)
    assert measure(servo, 0.2)[1] > 40


def test_custom_calibration_changes_signal():
    with Servo(PIN, min_pulse_us=1000, max_pulse_us=2000) as s:
        s.angle = 180
        time.sleep(SETTLE_S)
        pulse, _ = measure(s)
        assert abs(pulse - 2000) <= PULSE_TOLERANCE_US
        s.center()
        time.sleep(SETTLE_S)


def test_close_without_ever_moving(servo):
    servo.close()


def test_pin_is_exclusive_and_released_on_close(servo):
    with pytest.raises(RuntimeError, match=f"cannot use GPIO{PIN}"):
        Servo(PIN)
    servo.close()
    with Servo(PIN) as again:       # pin was released
        again.center()
        time.sleep(SETTLE_S)
        assert measure(again, 0.2)[1] > 40


def test_out_of_range_angle_leaves_servo_where_it_was(servo):
    servo.angle = 90
    with pytest.raises(ValueError):
        servo.angle = 200
    time.sleep(0.1)
    pulse, _ = measure(servo, 0.3)
    assert abs(pulse - servo.pulse_for(90)) <= PULSE_TOLERANCE_US

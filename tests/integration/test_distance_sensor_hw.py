"""Integration tests for the ultrasonic distance sensor (Trig GPIO23, Echo GPIO24).

Run:  pytest tests/integration/test_distance_sensor_hw.py -v
Pins: WATERBUOY_DIST_TRIG=23, WATERBUOY_DIST_ECHO=24 (defaults)

The sensor must be connected, powered, and pointed at something within 2 cm - 4 m that
does not move during the test (a ceiling, wall or table top is fine). Fails if the
sensor gives no echo.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

from waterbuoy.distance_sensor import MIN_INTERVAL_S, DistanceSensor

TRIG = int(os.environ.get("WATERBUOY_DIST_TRIG", "23"))
ECHO = int(os.environ.get("WATERBUOY_DIST_ECHO", "24"))


@pytest.fixture
def sensor():
    with DistanceSensor(TRIG, ECHO) as s:
        yield s


def test_sensor_answers_pings(sensor):
    readings = [sensor.ping() for _ in range(10)]
    got = [r for r in readings if r is not None]
    assert len(got) >= 8, f"only {len(got)}/10 pings got an echo: {readings} (wiring? power?)"
    assert all(0.02 <= r <= 4.0 for r in got)


def test_readings_of_a_still_target_are_consistent(sensor):
    got = [r for r in (sensor.ping() for _ in range(15)) if r is not None]
    assert len(got) >= 10
    spread = statistics.median(abs(r - statistics.median(got)) for r in got)
    assert spread < 0.01, f"readings vary by {spread * 100:.1f} cm: {got}"


def test_read_returns_median_of_pings(sensor):
    d = sensor.read(samples=5)
    assert d is not None
    assert abs(d - sensor.read(samples=5)) < 0.02


def test_pings_are_rate_limited(sensor):
    t0 = time.monotonic()
    for _ in range(5):
        sensor.echo_time()
    assert time.monotonic() - t0 >= 4 * MIN_INTERVAL_S


def test_temperature_compensation_scales_reading(sensor):
    sensor.temperature_c = 0
    cold = sensor.read(samples=5)
    sensor.temperature_c = 30
    warm = sensor.read(samples=5)
    assert cold is not None and warm is not None
    # same target, sound faster in warm air -> same echo time means a longer distance
    assert warm / cold == pytest.approx((331.3 + 0.606 * 30) / 331.3, rel=0.01)


def test_pins_are_exclusive_and_released_on_close():
    s = DistanceSensor(TRIG, ECHO)
    with pytest.raises(RuntimeError, match="in use"):
        DistanceSensor(TRIG, ECHO)
    s.close()
    s.close()  # idempotent
    with DistanceSensor(TRIG, ECHO) as again:
        assert again.ping() is not None or again.ping() is not None
    with pytest.raises(RuntimeError, match="closed"):
        s.ping()


def test_same_pin_for_trig_and_echo_rejected():
    with pytest.raises(ValueError):
        DistanceSensor(TRIG, TRIG)

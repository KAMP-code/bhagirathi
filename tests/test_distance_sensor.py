"""Distance-sensor maths (no hardware)."""

import pytest

from waterbuoy.distance_sensor import echo_to_distance, speed_of_sound


def test_speed_of_sound():
    assert speed_of_sound(0) == pytest.approx(331.3)
    assert speed_of_sound(20) == pytest.approx(343.42)


@pytest.mark.parametrize("echo_us, metres", [(583, 0.1), (5830, 1.0), (23300, 4.0)])
def test_echo_to_distance_at_20c(echo_us, metres):
    assert echo_to_distance(echo_us / 1e6) == pytest.approx(metres, rel=0.01)


def test_warmer_air_reads_further():
    assert echo_to_distance(0.01, 30) > echo_to_distance(0.01, 0)

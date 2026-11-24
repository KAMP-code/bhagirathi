"""Ultrasonic distance sensor (HC-SR04 style: Trig/Echo) on Raspberry Pi GPIO pins.

A 10 us pulse on Trig makes the sensor send a burst of ultrasound; it then holds Echo
high for as long as the sound takes to reach the target and come back. Distance is
half that time multiplied by the speed of sound.

The Echo edges are timestamped by the kernel (lgpio alerts), so the measurement does
not depend on Python's scheduling: readings are repeatable to a few millimetres.

    from waterbuoy.distance_sensor import DistanceSensor

    with DistanceSensor(trig=23, echo=24) as d:
        print(d.read())         # metres (median of 5 pings), or None if no echo

Wiring: VCC = +5 V, GND = GND, Trig -> GPIO23, Echo -> GPIO24. The sensor drives Echo
at 5 V; Pi GPIO pins are 3.3 V only, so put a voltage divider on Echo (e.g. 1 kOhm in
series, 2 kOhm to GND).

Requires the system package python3-lgpio and the user in the `gpio` group.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time

from waterbuoy.servo import find_gpiochip

log = logging.getLogger(__name__)

TRIGGER_PULSE_S = 10e-6     # sensor needs >= 10 us high on Trig
MIN_INTERVAL_S = 0.06       # datasheet: >= 60 ms between pings so old echoes die out
ECHO_TIMEOUT_S = 0.05       # sensor gives up after ~38 ms when nothing echoes back


def speed_of_sound(temperature_c: float) -> float:
    """Speed of sound in dry air, m/s."""
    return 331.3 + 0.606 * temperature_c


def echo_to_distance(echo_s: float, temperature_c: float = 20.0) -> float:
    """Echo pulse width (seconds) -> distance to target (metres)."""
    return echo_s * speed_of_sound(temperature_c) / 2


class DistanceSensor:
    """Distance readings from a Trig/Echo ultrasonic sensor.

    Readings outside `min_distance_m`..`max_distance_m` (the sensor's usable range,
    2 cm - 4 m) are reported as None, as are pings that get no echo. Set
    `temperature_c` to the air temperature for best accuracy (~0.17 % per degree).
    """

    def __init__(
        self,
        trig: int = 23,
        echo: int = 24,
        *,
        temperature_c: float = 20.0,
        min_distance_m: float = 0.02,
        max_distance_m: float = 4.0,
        chip: int | None = None,
    ):
        try:
            import lgpio
        except ImportError as e:
            raise RuntimeError("lgpio not installed; run: sudo apt install python3-lgpio") from e
        if trig == echo:
            raise ValueError("trig and echo must be different pins")
        self._lgpio = lgpio
        self.trig, self.echo = trig, echo
        self.temperature_c = temperature_c
        self.min_distance_m, self.max_distance_m = min_distance_m, max_distance_m
        self.chip = find_gpiochip() if chip is None else chip
        try:
            self.handle = lgpio.gpiochip_open(self.chip)
        except lgpio.error as e:
            raise RuntimeError(f"cannot open /dev/gpiochip{self.chip}: {e} (is the user in group 'gpio'?)") from e
        self._callback = None
        try:
            for pin, claim in ((trig, lambda: lgpio.gpio_claim_output(self.handle, trig, 0)),
                               (echo, lambda: lgpio.gpio_claim_alert(self.handle, echo, lgpio.BOTH_EDGES))):
                try:
                    claim()
                except lgpio.error as e:
                    raise RuntimeError(f"cannot use GPIO{pin}: {e} (in use by another program?)") from e
            self._callback = lgpio.callback(self.handle, echo, lgpio.BOTH_EDGES, self._on_edge)
        except BaseException:
            lgpio.gpiochip_close(self.handle)
            raise
        self._lock = threading.Lock()      # one ping at a time
        self._done = threading.Event()
        self._rise_ns: int | None = None
        self._width_ns: int | None = None
        self._last_ping = 0.0
        self._closed = False

    # --- edge timing (runs in lgpio's callback thread) --------------------
    def _on_edge(self, chip: int, gpio: int, level: int, tick_ns: int) -> None:
        if level == 1:
            self._rise_ns = tick_ns
        elif level == 0 and self._rise_ns is not None and not self._done.is_set():
            self._width_ns = tick_ns - self._rise_ns
            self._done.set()

    # --- measurement -----------------------------------------------------
    def echo_time(self) -> float | None:
        """Send one ping; return the echo pulse width in seconds, or None if none came."""
        lg = self._lgpio
        with self._lock:
            if self._closed:
                raise RuntimeError("distance sensor is closed")
            wait = self._last_ping + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._rise_ns = self._width_ns = None
            self._done.clear()
            lg.gpio_write(self.handle, self.trig, 1)
            time.sleep(TRIGGER_PULSE_S)
            lg.gpio_write(self.handle, self.trig, 0)
            self._last_ping = time.monotonic()
            got = self._done.wait(ECHO_TIMEOUT_S)
            self._done.set()                # ignore late edges from this ping
            if not got:
                log.debug("no echo (GPIO%d)", self.echo)
                return None
            return self._width_ns / 1e9

    def ping(self) -> float | None:
        """One distance reading in metres, or None (no echo / out of range)."""
        t = self.echo_time()
        if t is None:
            return None
        d = echo_to_distance(t, self.temperature_c)
        if not self.min_distance_m <= d <= self.max_distance_m:
            log.debug("out of range: %.3f m", d)
            return None
        return d

    def read(self, samples: int = 5) -> float | None:
        """Median of `samples` pings in metres (rejects stray echoes); None if fewer
        than half the pings got a valid reading. Takes ~60 ms per sample."""
        if samples < 1:
            raise ValueError("samples must be >= 1")
        good = [d for d in (self.ping() for _ in range(samples)) if d is not None]
        if len(good) * 2 < samples:
            return None
        return statistics.median(good)

    # --- lifecycle -------------------------------------------------------
    def close(self) -> None:
        """Release the GPIO pins. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        lg = self._lgpio
        try:
            if self._callback is not None:
                self._callback.cancel()
            for pin in (self.trig, self.echo):
                try:
                    lg.gpio_free(self.handle, pin)
                except lg.error:
                    pass
        finally:
            lg.gpiochip_close(self.handle)

    def __enter__(self) -> DistanceSensor:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DistanceSensor(trig={self.trig}, echo={self.echo})"

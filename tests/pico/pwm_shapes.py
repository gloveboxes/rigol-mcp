"""Deterministic integer-divider PWM configuration helpers."""

from __future__ import annotations


PWM_MAX_DIVIDER = 255
PWM_MAX_PERIOD = 65_536


def stable_pwm_configuration(
    system_clock_hz: int,
    requested_frequency_hz: int,
    duty_permille: int,
    phase_permille: int = 0,
) -> dict[str, int | float]:
    if system_clock_hz <= 0 or requested_frequency_hz <= 0:
        raise ValueError("clock and frequency must be positive")
    if not 1 <= duty_permille <= 999:
        raise ValueError("duty_permille must be between 1 and 999")
    if not 0 <= phase_permille <= 999:
        raise ValueError("phase_permille must be between 0 and 999")

    best: tuple[int, int, int] | None = None
    for divider in range(1, PWM_MAX_DIVIDER + 1):
        denominator = requested_frequency_hz * divider
        period = (system_clock_hz + denominator // 2) // denominator
        if not 2 <= period <= PWM_MAX_PERIOD:
            continue
        frequency_error = abs(system_clock_hz - requested_frequency_hz * divider * period)
        candidate = (frequency_error, -period, divider)
        if best is None or candidate < best:
            best = candidate

    if best is None:
        raise ValueError("no stable integer-divider PWM configuration")

    _, negative_period, divider = best
    period = -negative_period
    high_cycles = max(1, min(period - 1, (period * duty_permille + 500) // 1000))
    phase_cycles = (period * phase_permille + 500) // 1000
    return {
        "divider": divider,
        "top": period - 1,
        "period_cycles": period,
        "high_cycles": high_cycles,
        "phase_cycles": phase_cycles,
        "actual_frequency_hz": system_clock_hz / (divider * period),
        "actual_duty_percent": high_cycles / period * 100,
        "actual_phase_degrees": phase_cycles / period * 360,
        "pulse_width_s": high_cycles * divider / system_clock_hz,
    }
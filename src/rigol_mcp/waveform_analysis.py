"""Deterministic heuristics for waveform analysis."""

import math
import statistics

# Rigol scope screens are 8 vertical divisions tall, so full-scale Vpp = scale × 8.
SCREEN_V_DIVISIONS = 8
# A capture whose Vpp fills less than this fraction of the vertical screen is treated as
# noise-floor: the trace is dominated by ADC quantisation and front-end noise, so any
# frequency/shape reading would be spurious. Below this we suppress the interpretation
# entirely (and the secondary warnings it would otherwise spawn from noise crossings).
NOISE_FILL_FRACTION = 0.10
# Between NOISE_FILL_FRACTION and this, the signal is real but small relative to full
# scale (so noisy and imprecise): still analysed, but flagged with a low-amplitude warning.
LOW_FILL_FRACTION = 0.20
# Fraction of the half-amplitude the signal must travel past the mean before a mean-crossing
# is committed (a Schmitt-trigger band, the analogue of the scope's own trigger hysteresis).
# Without it, a noisy trace wobbles across the mean several times at each true crossing and
# the crossing count — and therefore the frequency — is inflated by an integer-ish factor.
CROSSING_HYSTERESIS = 0.25


def _fft(values: list[complex]) -> list[complex]:
    """In-place-style radix-2 FFT returned as a new list."""
    size = len(values)
    output = list(values)
    index = 0
    for position in range(1, size):
        bit = size >> 1
        while index & bit:
            index ^= bit
            bit >>= 1
        index ^= bit
        if position < index:
            output[position], output[index] = output[index], output[position]
    length = 2
    while length <= size:
        root = complex(math.cos(-2 * math.pi / length), math.sin(-2 * math.pi / length))
        for start in range(0, size, length):
            factor = 1 + 0j
            half = length // 2
            for offset in range(half):
                even = output[start + offset]
                odd = output[start + offset + half] * factor
                output[start + offset] = even + odd
                output[start + offset + half] = even - odd
                factor *= root
        length *= 2
    return output


def _coefficient_of_variation(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    deviation = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return deviation / mean


def _crossing_timing(crossings: list[float]) -> dict:
    intervals = [right - left for left, right in zip(crossings, crossings[1:])]
    periods = [crossings[index + 2] - crossings[index]
               for index in range(len(crossings) - 2)]
    period_cv = _coefficient_of_variation(periods)
    phase_cvs = [
        value
        for value in (
            _coefficient_of_variation(intervals[0::2]),
            _coefficient_of_variation(intervals[1::2]),
        )
        if value is not None
    ]
    return {
        "intervals": intervals,
        "periods": periods,
        "average_period_s": sum(periods) / len(periods) if periods else None,
        "period_jitter_percent": period_cv * 100 if period_cv is not None else None,
        "edge_timing_jitter_percent": max(phase_cvs) * 100 if phase_cvs else None,
    }


def _robust_rail_levels(voltages: list[float]) -> tuple[float, float]:
    """Estimate settled digital rails without letting edge ringing define them."""
    minimum = min(voltages)
    maximum = max(voltages)
    midpoint = (minimum + maximum) / 2
    low_samples = [value for value in voltages if value < midpoint]
    high_samples = [value for value in voltages if value >= midpoint]
    if not low_samples or not high_samples:
        return minimum, maximum
    return statistics.median(low_samples), statistics.median(high_samples)


def _pwm_envelope(data: dict) -> dict | None:
    voltages = data["voltages_v"]
    times = data["times_s"]
    if len(voltages) < 8 or len(times) != len(voltages):
        return None
    low_level, high_level = _robust_rail_levels(voltages)
    span = high_level - low_level
    if span <= 0:
        return None
    threshold = (low_level + high_level) / 2
    rising = []
    falling = []
    for index in range(1, len(voltages)):
        left = voltages[index - 1] - threshold
        right = voltages[index] - threshold
        if left == right or left * right > 0:
            continue
        crossing = times[index - 1] + (times[index] - times[index - 1]) * (-left) / (right - left)
        (rising if left < right else falling).append(crossing)
    if len(rising) < 3:
        return None
    periods = [right - left for left, right in zip(rising, rising[1:])]
    duties = []
    sample_times = []
    falling_index = 0
    for cycle_start, cycle_end in zip(rising, rising[1:]):
        while falling_index < len(falling) and falling[falling_index] <= cycle_start:
            falling_index += 1
        if falling_index >= len(falling) or falling[falling_index] >= cycle_end:
            continue
        duties.append((falling[falling_index] - cycle_start) / (cycle_end - cycle_start))
        sample_times.append((cycle_start + cycle_end) / 2)
    if len(duties) < 3:
        return None
    carrier_period = sum(periods) / len(periods)
    mean_duty = sum(duties) / len(duties)
    return {
        "carrier_frequency_hz": 1 / carrier_period,
        "carrier_period_jitter_percent": (_coefficient_of_variation(periods) or 0) * 100,
        "duty_cycle_percent": {
            "minimum": min(duties) * 100,
            "maximum": max(duties) * 100,
            "mean": mean_duty * 100,
            "peak_to_peak": (max(duties) - min(duties)) * 100,
        },
        "average_voltage_v": {
            "minimum": low_level + min(duties) * span,
            "maximum": low_level + max(duties) * span,
            "mean": low_level + mean_duty * span,
        },
        "low_level_v": low_level,
        "high_level_v": high_level,
        "edge_count": len(rising) + len(falling),
        "period_count": len(periods),
        "sample_times_s": sample_times,
        "duty_samples": duties,
    }


def _envelope_frequency(envelope: dict) -> tuple[float | None, complex | None]:
    values = envelope["duty_samples"]
    if len(values) < 8:
        return None, None
    centered = [value - sum(values) / len(values) for value in values]
    sample_rate = envelope["carrier_frequency_hz"]
    best_bin = max(
        range(1, len(values) // 2),
        key=lambda index: abs(sum(
            value * complex(
                math.cos(-2 * math.pi * index * position / len(values)),
                math.sin(-2 * math.pi * index * position / len(values)),
            )
            for position, value in enumerate(centered)
        )),
    )
    coefficient = sum(
        value * complex(
            math.cos(-2 * math.pi * best_bin * position / len(values)),
            math.sin(-2 * math.pi * best_bin * position / len(values)),
        )
        for position, value in enumerate(centered)
    )
    return best_bin * sample_rate / len(values), coefficient


def analyze_pwm_envelopes(waveforms: dict[str, dict]) -> dict:
    """Extract PWM carrier, duty envelope, modulation, and paired envelope phase."""
    results = {}
    coefficients = {}
    for source, data in waveforms.items():
        envelope = _pwm_envelope(data)
        if envelope is None:
            results[source] = {"valid": False, "warning": "Too few resolved PWM cycles."}
            continue
        duty_range = envelope["duty_cycle_percent"]["peak_to_peak"]
        if duty_range >= 5:
            modulation_frequency, coefficient = _envelope_frequency(envelope)
        else:
            modulation_frequency, coefficient = None, None
        coefficients[source] = coefficient
        envelope_samples = len(envelope["duty_samples"])
        confidence = "high" if envelope_samples >= 32 else "moderate" if envelope_samples >= 8 else "low"
        results[source] = {
            "valid": True,
            "carrier_frequency_hz": envelope["carrier_frequency_hz"],
            "carrier_period_jitter_percent": envelope["carrier_period_jitter_percent"],
            "modulation_frequency_hz": modulation_frequency,
            "duty_cycle_percent": envelope["duty_cycle_percent"],
            "average_voltage_v": envelope["average_voltage_v"],
            "low_level_v": envelope["low_level_v"],
            "high_level_v": envelope["high_level_v"],
            "edge_count": envelope["edge_count"],
            "period_count": envelope["period_count"],
            "envelope_samples": envelope_samples,
            "confidence": confidence,
        }
    phase = None
    valid_sources = [source for source in waveforms if results[source]["valid"]]
    if len(valid_sources) == 2 and all(coefficients.get(source) for source in valid_sources):
        first, second = valid_sources
        phase = math.degrees(math.atan2(
            (coefficients[second] / coefficients[first]).imag,
            (coefficients[second] / coefficients[first]).real,
        ))
    warnings = [
        "Average-voltage reconstruction is mathematical; physical outputs remain PWM without a low-pass filter."
    ]
    low_confidence = [source for source, result in results.items()
                      if result.get("valid") and result.get("confidence") == "low"]
    if low_confidence:
        warnings.append(
            f"Low-confidence PWM statistics for {', '.join(low_confidence)}: fewer than 8 complete duty samples."
        )
    return {
        "sources": results,
        "phase_degrees_second_relative_to_first": phase,
        "warnings": warnings,
    }


def analyze_waveform(data: dict, peak_count: int = 5) -> dict:
    """Return bounded numeric statistics and host-side FFT peaks for a capture."""
    if not 1 <= peak_count <= 20:
        raise ValueError("peak_count must be between 1 and 20")
    voltages = data["voltages_v"]
    times = data["times_s"]
    count = len(voltages)
    valid = (
        count >= 2 and len(times) == count
        and all(math.isfinite(value) and abs(value) < 9e37 for value in voltages)
        and all(math.isfinite(value) for value in times)
        and all(right > left for left, right in zip(times, times[1:]))
    )
    if not valid:
        return {
            "channel": data.get("channel"), "valid": False,
            "quality": "invalid", "warnings": [
                "Empty/mismatched samples, non-finite values, invalid sentinel, or invalid timing."
            ] + list(data.get("warnings") or []),
        }

    minimum = min(voltages)
    maximum = max(voltages)
    mean = sum(voltages) / count
    rms = math.sqrt(sum(value * value for value in voltages) / count)
    standard_deviation = math.sqrt(sum((value - mean) ** 2 for value in voltages) / count)
    peak_to_peak = maximum - minimum
    warnings = list(data.get("warnings") or [])
    scale = data.get("y_scale_v_per_div")
    fill_fraction = peak_to_peak / (scale * SCREEN_V_DIVISIONS) if scale else None
    quality = "good"
    if fill_fraction is not None and fill_fraction < NOISE_FILL_FRACTION:
        quality = "noise-floor"
        warnings.append("Signal fills less than 10% of the vertical range; frequency and FFT peaks are suppressed.")
    elif fill_fraction is not None and fill_fraction < LOW_FILL_FRACTION:
        quality = "low-amplitude"
        warnings.append("Signal fills less than 20% of the vertical range; results may be noisy.")

    centered = [value - mean for value in voltages]
    crossings = _hysteretic_crossings(
        centered, times, count, (peak_to_peak / 2) * CROSSING_HYSTERESIS,
    )
    timing = _crossing_timing(crossings)
    pwm_envelope = _pwm_envelope(data) if quality != "noise-floor" else None
    pwm_modulated = (
        pwm_envelope is not None
        and pwm_envelope["duty_cycle_percent"]["peak_to_peak"] >= 5
    )
    frequency_hz = None
    if quality != "noise-floor" and timing["average_period_s"]:
        frequency_hz = 1 / timing["average_period_s"]

    low_level, high_level = _robust_rail_levels(voltages)
    robust_peak_to_peak = high_level - low_level
    flat_epsilon = max(robust_peak_to_peak * 0.001, 1e-4)
    rail_band = max(robust_peak_to_peak * 0.08, 1e-3)
    minimum_run = max(int(count * 0.01), 8)
    near_rails = sum(
        abs(value - low_level) < rail_band or abs(value - high_level) < rail_band
        for value in voltages
    ) / count
    is_pulse = near_rails > 0.7
    clipped = False
    if not is_pulse:
        for target in (minimum, maximum):
            run = 0
            for index, value in enumerate(voltages):
                if abs(value - target) < rail_band:
                    run = run + 1 if run and abs(value - voltages[index - 1]) < flat_epsilon else 1
                    clipped = clipped or run >= minimum_run
                else:
                    run = 0
    if clipped:
        warnings.append("Possible clipping: a flat sample run is pinned near a voltage rail.")
    if pwm_modulated:
        warnings.append(
            "PWM duty modulation detected; edge/period jitter metrics are suppressed because pulse widths intentionally vary."
        )

    fft_size = 1 << (count.bit_length() - 1)
    sample_interval = (times[-1] - times[0]) / (count - 1)
    resolution_hz = 1 / (sample_interval * fft_size)
    peaks = []
    if quality != "noise-floor" and fft_size >= 4:
        samples = centered[:fft_size]
        window = [0.5 - 0.5 * math.cos(2 * math.pi * index / (fft_size - 1))
                  for index in range(fft_size)]
        spectrum = _fft([value * weight for value, weight in zip(samples, window)])
        amplitude_scale = 2 / sum(window)
        bins = [(index * resolution_hz, abs(spectrum[index]) * amplitude_scale)
                for index in range(1, fft_size // 2)]
        local_peaks = [item for index, item in enumerate(bins)
                       if (index == 0 or item[1] >= bins[index - 1][1])
                       and (index == len(bins) - 1 or item[1] >= bins[index + 1][1])]
        peaks = [{"frequency_hz": frequency, "amplitude_v": amplitude}
                 for frequency, amplitude in sorted(local_peaks, key=lambda item: item[1], reverse=True)[:peak_count]]

    acquisition_sample_rate = data.get("acquisition_sample_rate_hz")
    displayed_sample_rate = data.get("displayed_sample_rate_hz", 1 / sample_interval)
    effective_sample_rate = min(
        rate for rate in (acquisition_sample_rate, displayed_sample_rate)
        if rate is not None and rate > 0
    )
    return {
        "channel": data.get("channel"), "valid": True, "points": count,
        "time_start_s": times[0], "time_end_s": times[-1],
        "sample_interval_s": sample_interval,
        "acquisition_sample_rate_hz": acquisition_sample_rate,
        "displayed_sample_rate_hz": displayed_sample_rate,
        "displayed_points_interpolated": bool(
            acquisition_sample_rate and displayed_sample_rate > acquisition_sample_rate
        ),
        "analysis_nyquist_hz": effective_sample_rate / 2,
        "statistics": {
            "minimum_v": minimum, "maximum_v": maximum, "peak_to_peak_v": peak_to_peak,
            "low_level_v": low_level, "high_level_v": high_level,
            "robust_peak_to_peak_v": robust_peak_to_peak,
            "undershoot_v": max(0.0, low_level - minimum),
            "overshoot_v": max(0.0, maximum - high_level),
            "mean_v": mean, "rms_v": rms, "standard_deviation_v": standard_deviation,
        },
        "frequency_hz": frequency_hz, "fft_resolution_hz": resolution_hz,
        "fft_size": fft_size, "fft_peaks": peaks, "clipped": clipped,
        "period_jitter_percent": None if pwm_modulated else timing["period_jitter_percent"],
        "edge_timing_jitter_percent": None if pwm_modulated else timing["edge_timing_jitter_percent"],
        "signal_type": "pwm_modulated" if pwm_modulated else ("pulse" if is_pulse else "analog"),
        "vertical_fill_fraction": fill_fraction, "quality": quality, "warnings": warnings,
    }


def _fmt_si(value: float, unit: str) -> str:
    """Format a value with SI prefix (e.g. 1350000 Hz → '1.35 MHz')."""
    if value == 0:
        return f"0 {unit}"
    abs_v = abs(value)
    for threshold, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs_v >= threshold:
            return f"{value / threshold:.4g} {prefix}{unit}"
    return f"{value:.4g} {unit}"


def _hysteretic_crossings(v_c: list, times: list, n: int, hyst: float) -> list:
    """Mean-crossing instants detected with Schmitt-trigger hysteresis.

    A bare sign-change test (``v_c[i-1] * v_c[i] <= 0``) counts every wobble of a noisy
    signal across the mean, inflating the crossing count and hence the frequency. Here a
    crossing is only committed once the signal has travelled beyond ``±hyst`` of the mean,
    so noise wobble within the band is ignored. Returns interpolated true zero-crossing
    times, suitable for period estimation."""
    crossings = []
    state = 0        # +1 once above +hyst, -1 once below -hyst, 0 until the first commit
    pending = None   # latest raw mean-crossing time, awaiting confirmation by an extreme
    for i in range(1, n):
        if v_c[i - 1] * v_c[i] <= 0 and v_c[i] != v_c[i - 1]:
            pending = times[i - 1] + (times[i] - times[i - 1]) * (-v_c[i - 1]) / (v_c[i] - v_c[i - 1])
        if v_c[i] > hyst:
            if state == -1 and pending is not None:
                crossings.append(pending)
                pending = None
            state = 1
        elif v_c[i] < -hyst:
            if state == 1 and pending is not None:
                crossings.append(pending)
                pending = None
            state = -1
    return crossings


def describe_waveform(data: dict) -> str:
    """Produce a human-readable analysis of a waveform capture."""
    voltages = data["voltages_v"]
    times    = data["times_s"]
    n        = len(voltages)
    if (not n or len(times) != n or any(not math.isfinite(value) or abs(value) >= 9e37 for value in voltages)
            or any(not math.isfinite(value) for value in times)
            or any(right <= left for left, right in zip(times, times[1:]))):
        return (
            f"=== Waveform: {data['channel']} ===\n"
            "Invalid capture: empty/mismatched samples, non-finite values, invalid sentinel, or invalid timing.\n"
            "Amplitude, frequency and shape interpretation suppressed. Acquire valid data and retry.\n"
            + "\n".join(str(warning) for warning in data.get("warnings", []))
        )
    vmin     = data["vmin_v"]
    vmax     = data["vmax_v"]
    vmean    = data["vmean_v"]
    vpp      = vmax - vmin
    t_start  = data["time_start_s"]
    t_end    = data["time_end_s"]
    window_s = t_end - t_start
    x_inc    = data["time_increment_s"]
    ch       = data["channel"]
    # Warnings raised during capture (e.g. channel auto-enabled) — merged into the
    # analysis warnings so they survive every return path.
    upstream_warnings = list(data.get("warnings") or [])

    lines = [f"=== Waveform: {ch} ==="]

    # --- Time window ---
    lines.append(
        f"Window : {_fmt_si(t_start,'s')} → {_fmt_si(t_end,'s')}  "
        f"({_fmt_si(window_s,'s')} total, {_fmt_si(x_inc,'s')}/point, {n} pts)"
    )
    acquisition_rate = data.get("acquisition_sample_rate_hz")
    if acquisition_rate:
        lines.append(
            f"Rates  : acquisition {_fmt_si(acquisition_rate, 'Sa/s')}; displayed points "
            f"{_fmt_si(1 / x_inc, 'Sa/s')}; analysis Nyquist {_fmt_si(0.5 / x_inc, 'Hz')}"
        )

    # --- Amplitude ---
    lines.append(
        f"Voltage: Vpp={_fmt_si(vpp,'V')}, Vmin={_fmt_si(vmin,'V')}, "
        f"Vmax={_fmt_si(vmax,'V')}, DC offset={_fmt_si(vmean,'V')}"
    )

    # --- Vertical scale context (optional: only when the caller passes the channel scale) ---
    # Judging amplitude against the configured V/div is what tells noise apart from signal:
    # 320 mVpp is a real trace at 50 mV/div but pure noise at 1.25 V/div. Without this the
    # analyser would confidently report a frequency for what is just the noise floor.
    y_scale = data.get("y_scale_v_per_div")
    full_scale_vpp = y_scale * SCREEN_V_DIVISIONS if y_scale else None
    fill_frac = vpp / full_scale_vpp if full_scale_vpp else None
    if full_scale_vpp:
        lines.append(
            f"Vert   : {_fmt_si(y_scale,'V')}/div, {_fmt_si(full_scale_vpp,'V')} full screen "
            f"({SCREEN_V_DIVISIONS} div) — signal fills {fill_frac*100:.1f}% of vertical range"
        )

    # --- Noise-floor guard: a trace that barely fills the screen is almost certainly noise
    # (or an unconnected input). Flag it and suppress the shape/frequency interpretation
    # rather than reporting a spurious oscillation. ---
    if fill_frac is not None and fill_frac < NOISE_FILL_FRACTION:
        divs_pp = vpp / y_scale
        lines.append("Shape  : low-amplitude / likely noise — frequency & shape interpretation suppressed")
        lines.append("")
        lines.append("Warnings:")
        for w in upstream_warnings:
            lines.append(f"  ⚠ {w}")
        lines.append(
            f"  ⚠ Vpp ({_fmt_si(vpp,'V')}) is only {fill_frac*100:.1f}% of the {_fmt_si(full_scale_vpp,'V')} "
            f"vertical full-scale window ({_fmt_si(y_scale,'V')}/div, ≈{divs_pp:.2f} divisions peak-to-peak). "
            "At this level the trace is dominated by noise / ADC quantisation, so any frequency or shape "
            "reading would be meaningless. Reduce V/div (zoom in vertically) until the signal fills a few "
            "divisions, then re-capture — or check the probe/connection if you expected a larger signal."
        )
        return "\n".join(lines)

    # --- Zero crossings relative to mean (handles DC offset), with hysteresis so noise
    # wobble near the mean does not spawn spurious crossings and inflate the frequency. ---
    v_c = [v - vmean for v in voltages]
    hyst = (vpp / 2) * CROSSING_HYSTERESIS
    crossings = _hysteretic_crossings(v_c, times, n, hyst)
    timing = _crossing_timing(crossings)

    # --- Pulse / square wave detection (bimodal: most points near rails) ---
    rail_thr = vpp * 0.15
    near_rail = sum(
        1 for v in voltages
        if abs(v - vmin) < rail_thr or abs(v - vmax) < rail_thr
    )
    is_pulse = (near_rail / n) > 0.70 and vpp > 1e-3

    # --- Signal classification ---
    freq_est = None
    period_est = None
    half_periods = []

    if vpp < 1e-3:
        lines.append("Shape  : DC / flat (Vpp < 1 mV)")

    elif is_pulse:
        duty = sum(1 for v in voltages if v > vmean) / n * 100
        lines.append(f"Shape  : pulse / square wave (~{duty:.0f}% duty cycle)")

    elif timing["average_period_s"] is None:
        # No crossings → ramp or very slow signal
        diffs = [voltages[i] - voltages[i - 1] for i in range(1, min(50, n))]
        pos = sum(1 for d in diffs if d > 0)
        neg = sum(1 for d in diffs if d < 0)
        if pos > len(diffs) * 0.7:
            shape = "rising ramp / positive slope"
        elif neg > len(diffs) * 0.7:
            shape = "falling ramp / negative slope"
        else:
            shape = "non-periodic / complex"
        lines.append(f"Shape  : {shape} — timebase likely too narrow; widen to see complete cycles")

    else:
        period_est = timing["average_period_s"]
        freq_est   = 1.0 / period_est if period_est > 0 else None
        num_cycles = window_s / period_est if period_est > 0 else 0

        # Detect envelope trend via RMS of first vs last third
        third = n // 3
        def _rms(seg):
            m = sum(seg) / len(seg)
            return (sum((v - m) ** 2 for v in seg) / len(seg)) ** 0.5

        rms_first = _rms(voltages[:third])
        rms_last  = _rms(voltages[-third:])
        envelope_ratio = rms_last / rms_first if rms_first > 0 else 1.0

        if envelope_ratio < 0.7:
            decay_pct = (1 - envelope_ratio) * 100
            shape = f"damped oscillation (RMS amplitude decays ~{decay_pct:.0f}% over capture)"
        elif envelope_ratio > 1.4:
            grow_pct = (envelope_ratio - 1) * 100
            shape = f"growing oscillation (RMS amplitude grows ~{grow_pct:.0f}% — capture may include startup/ramp-up)"
        else:
            shape = "sustained oscillation"

        lines.append(f"Shape  : {shape}")
        if freq_est:
            lines.append(
                f"Freq   : ~{_fmt_si(freq_est,'Hz')}  (period ~{_fmt_si(period_est,'s')},  "
                f"{num_cycles:.1f} cycles visible,  {len(crossings)} zero crossings)"
            )

    # --- Data quality warnings ---
    warnings = list(upstream_warnings)
    edge_thr = max(vpp * 0.05, 4e-3)

    # Low amplitude: real signal, but small relative to full scale so noisy and imprecise.
    # (Below NOISE_FILL_FRACTION we already returned above; this is the 10–20% band.)
    if fill_frac is not None and fill_frac < LOW_FILL_FRACTION:
        warnings.append(
            f"Low amplitude: Vpp ({_fmt_si(vpp,'V')}) fills only {fill_frac*100:.0f}% of the "
            f"{_fmt_si(full_scale_vpp,'V')} vertical window ({_fmt_si(y_scale,'V')}/div). The reading is "
            "usable but noisy — reduce V/div so the signal fills more of the screen for a cleaner measurement."
        )

    # Clipping: a genuinely clipped signal has a FLAT top/bottom — a run of consecutive
    # samples pinned at (nearly) the same extreme voltage. Proximity to a rail alone is not
    # enough: a clean sine dwells near its peaks by curvature (~15% of its samples land within
    # 2% of a rail) without ever going flat, so a near-rail count fires on every clean sine.
    # Look for flat runs instead, where successive samples barely change near the extreme.
    flat_eps  = max(vpp * 0.001, 1e-4)   # max adjacent-sample change within a "flat" run
    rail_band = max(vpp * 0.03, 1e-3)    # how close to the rail the run must sit
    min_run   = max(int(n * 0.01), 8)    # min consecutive flat samples to call it clipping

    def _max_flat_run(target):
        best = run = 0
        for i in range(n):
            if abs(voltages[i] - target) < rail_band:
                run = run + 1 if (run and abs(voltages[i] - voltages[i - 1]) < flat_eps) else 1
                best = max(best, run)
            else:
                run = 0
        return best

    if not is_pulse and vpp > 1e-3:
        flat_run = max(_max_flat_run(vmax), _max_flat_run(vmin))
        if flat_run >= min_run:
            warnings.append(
                f"Possible clipping: flat run of {flat_run} samples pinned at the voltage rail. "
                "Increase V/div or reduce probe attenuation."
            )

    # Compare like edges and like pulse phases. Adjacent crossings have intentionally
    # unequal spacing on asymmetric PWM, so treating them as interchangeable creates a
    # false jitter warning for every non-50% duty cycle.
    edge_jitter = timing["edge_timing_jitter_percent"]
    if period_est and edge_jitter is not None and edge_jitter > 20:
        warnings.append(
            f"Edge timing jitter CV={edge_jitter:.0f}% across like pulse phases — "
            "signal may be non-periodic, frequency-modulated, or aliased. "
            "Verify sample rate vs signal frequency."
        )

    # Burst / partial capture: quiet segments at start or end
    if n >= 10:
        seg = max(n // 5, 2)
        def _seg_rms(s): return (sum((v - vmean) ** 2 for v in s) / len(s)) ** 0.5
        rms_head = _seg_rms(voltages[:seg])
        rms_tail = _seg_rms(voltages[-seg:])
        rms_body = _seg_rms(voltages[seg:-seg]) if n > 2 * seg else _seg_rms(voltages)
        if rms_body > 0:
            if rms_head < rms_body * 0.15:
                warnings.append(
                    "Signal is quiet at start then becomes active — burst/transient starts mid-capture. "
                    "Move trigger point earlier or use pre-trigger."
                )
            if rms_tail < rms_body * 0.15:
                warnings.append(
                    "Signal becomes quiet before capture ends — burst/transient ends mid-capture. "
                    "Widen timebase or move trigger point later."
                )

    # DC baseline wander: compare head vs tail means, but average each over an integer number
    # of detected cycles rather than a fixed 10% slice. A periodic capture that simply spans a
    # non-integer number of cycles has head/tail slices that differ purely from partial-cycle
    # averaging (e.g. a window starting near +peak and ending near −peak) — that is not a
    # baseline shift. Averaging whole cycles cancels the AC content so each window reflects the
    # true local DC. Needs a trustworthy period (post-hysteresis) and at least ~3 cycles.
    if period_est and period_est > 0 and x_inc > 0:
        cyc_samples = int(round(period_est / x_inc))
        num_cycles = window_s / period_est
        if cyc_samples >= 4 and num_cycles >= 3 and 2 * cyc_samples <= n:
            mean_head = sum(voltages[:cyc_samples]) / cyc_samples
            mean_tail = sum(voltages[-cyc_samples:]) / cyc_samples
            if abs(mean_tail - mean_head) > vpp * 0.15 and vpp > 1e-3:
                warnings.append(
                    f"DC baseline shifts {_fmt_si(mean_tail - mean_head, 'V')} from start to end — "
                    "capture may span a transient or settling event."
                )

    if len(crossings) < 4:
        warnings.append(
            "Fewer than 2 complete cycles captured — FREQUENCY measurement may return "
            "9.9E37 (scope's invalid sentinel). Widen timebase scale."
        )

    if abs(voltages[0] - vmean) > edge_thr:
        warnings.append(
            f"Left edge = {_fmt_si(voltages[0],'V')} (not at mean) — waveform starts mid-cycle. "
            "Trigger offset or timebase may need adjustment."
        )

    if abs(voltages[-1] - vmean) > edge_thr:
        warnings.append(
            f"Right edge = {_fmt_si(voltages[-1],'V')} (not at mean) — waveform ends mid-cycle."
        )

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  ⚠ {w}")

    return "\n".join(lines)

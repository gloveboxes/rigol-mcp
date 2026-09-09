"""VISA connection and SCPI helpers for Rigol oscilloscopes.

Family-specific SCPI differences (DS1000Z vs DHO vs …) live in :mod:`rigol_mcp.drivers`;
the functions here handle the generic orchestration and delegate the parts that differ to
the active :class:`~rigol_mcp.drivers.ScopeDriver`, selected once per connection.
"""

import os
import math
import shlex
import time

import pyvisa

from rigol_mcp.capabilities import evidence_for

from rigol_mcp.drivers import (
    ScopeDriver,
    driver_for,
    capabilities_for,
    ALL_TWO_SOURCE_ITEMS,
    # Re-exported so existing callers/tests keep reaching these via rigol_mcp.scope.
    screen_x_to_time,  # noqa: F401
    time_to_screen_x,  # noqa: F401
    _SCREEN_CENTER,    # noqa: F401
)

_LAN_TIMEOUT_MS = 30_000
_SLOW_OP_TIMEOUT_MS = 30_000

# Module-level cached connection
_rm: pyvisa.ResourceManager | None = None
_scope: pyvisa.resources.Resource | None = None
_driver: ScopeDriver | None = None


def get_lan_resource_string() -> str:
    ip = os.environ.get("RIGOL_IP", "").strip()
    if not ip:
        raise RuntimeError("RIGOL_IP environment variable is not set")
    return f"TCPIP0::{ip}::5555::SOCKET"


def get_scope() -> pyvisa.resources.Resource:
    """Return the cached TCP/IP socket connection, opening it if needed."""
    global _rm, _scope
    if _scope is None:
        resource = get_lan_resource_string()
        _rm = pyvisa.ResourceManager("@py")
        _scope = _rm.open_resource(resource)
        _scope.timeout = _LAN_TIMEOUT_MS
        _scope.chunk_size = 1024 * 1024
        _scope.write_termination = "\n"
        _scope.read_termination = "\n"
        try:
            _scope.clear()  # flush any stale data left in the receive buffer
        except pyvisa.errors.VisaIOError:
            pass
        _scope.write("*CLS")  # clear the SCPI error queue (clear() only flushes I/O buffers)
    return _scope


def invalidate_scope() -> None:
    """Close and discard the cached connection so the next call reconnects."""
    global _scope, _driver
    if _scope is not None:
        try:
            _scope.clear()
        except Exception:
            pass
        try:
            _scope.close()
        except Exception:
            pass
        _scope = None
    _driver = None


def get_driver(scope: pyvisa.resources.Resource) -> ScopeDriver:
    """Return the dialect driver for the connected scope.

    Detected once from ``*IDN?`` and cached until :func:`invalidate_scope`.
    """
    global _driver
    if _driver is None:
        _driver = driver_for(scope.query("*IDN?"))
    return _driver


def set_driver_from_idn(idn_str: str) -> ScopeDriver | None:
    """Set the cached driver from an already-known ``*IDN?`` string.

    Lets callers populate the driver cache without a second device round-trip when
    they've just queried ``*IDN?`` themselves (the ``idn`` tool, for example).
    Returns the selected driver, or None if no driver matched.
    """
    global _driver
    try:
        _driver = driver_for(idn_str)
    except RuntimeError:
        # No driver matched — leave _driver as None so a later dialect call surfaces
        # the same error against a concrete tool invocation.
        return None
    return _driver


def check_scpi_error(scope: pyvisa.resources.Resource) -> str | None:
    """Drain the SCPI error queue. Returns the first error encountered, None if queue was clear.

    Draining (not just reading one) prevents stale errors from a prior failed command
    leaking into the next tool's result. Capped to avoid pathological loops.
    """
    first_err: str | None = None
    for _ in range(16):
        response = scope.query(":SYSTem:ERRor?").strip()
        # No error returns '0' or '0,"No error"'
        if response == "0" or response.startswith("0,"):
            return first_err
        if first_err is None:
            first_err = response
    return first_err


def get_cursor_mode(scope: pyvisa.resources.Resource) -> str:
    return scope.query(":CURSor:MODE?").strip()


def set_cursor_mode(scope: pyvisa.resources.Resource, mode: str) -> None:
    """Set cursor mode: OFF, MANUAL, TRACK."""
    scope.write(f":CURSor:MODE {mode.upper()}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :CURSor:MODE: {err}")


def set_cursor_positions(
    scope: pyvisa.resources.Resource,
    mode: str,
    ax: float | None = None,
    bx: float | None = None,
) -> None:
    """Set cursor A and/or B X positions (in seconds). mode: MANUAL or TRACK.

    The axis addressing differs per family (pixels on DS1000Z, seconds on DHO); the active
    driver handles that — see ScopeDriver.write_cursor_axis.
    """
    prefix = ":CURSor:TRACk" if mode.upper() in ("TRACK", "TRAC") else ":CURSor:MANual"
    driver = get_driver(scope)
    for name, value in (("A", ax), ("B", bx)):
        if value is None:
            continue
        cmd = driver.write_cursor_axis(scope, prefix, name, value)
        if err := check_scpi_error(scope):
            raise RuntimeError(f"SCPI error after '{cmd}': {err}")


def get_cursor_values(scope: pyvisa.resources.Resource) -> dict:
    """Read current cursor mode and all available readouts. AX_s/BX_s are in seconds."""
    mode = scope.query(":CURSor:MODE?").strip()
    result: dict = {"mode": mode}
    driver = get_driver(scope)
    if mode == "XY" and driver.name == "DHO":
        for key, suffix in (("AX_value", "AXValue"), ("BX_value", "BXValue"),
                            ("AY_value", "AYValue"), ("BY_value", "BYValue"),
                            ("delta_x", "XDELta"), ("delta_y", "YDELta")):
            result[key] = scope.query(f":CURSor:XY:{suffix}?").strip()
        result["units"] = "XY amplitudes in the source channels' units, not seconds"
        return result
    if mode not in ("MANUAL", "MAN", "TRACK", "TRAC"):
        return result
    p = ":CURSor:TRACk" if mode in ("TRACK", "TRAC") else ":CURSor:MANual"
    if driver.name == "DHO":
        if mode in ("TRACK", "TRAC"):
            result["source_a"] = scope.query(f"{p}:SOURce1?").strip()
            result["source_b"] = scope.query(f"{p}:SOURce2?").strip()
            result["track_axis"] = scope.query(f"{p}:MODE?").strip()
        else:
            result["source"] = scope.query(f"{p}:SOURce?").strip()
            result["cursor_type"] = scope.query(f"{p}:TYPE?").strip()
    ax_s, bx_s = driver.read_cursor_axes_s(scope, p)
    result.update({
        "AX_s":        ax_s,
        "BX_s":        bx_s,
        "AX_value":    scope.query(f"{p}:AXValue?").strip(),
        "BX_value":    scope.query(f"{p}:BXValue?").strip(),
        "delta_x":     scope.query(f"{p}:XDELta?").strip(),
        "inv_delta_x": scope.query(f"{p}:IXDELta?").strip(),
    })
    if mode in ("TRACK", "TRAC") or driver.name == "DHO":
        result.update({
            "AY_value": scope.query(f"{p}:AYValue?").strip(),
            "BY_value": scope.query(f"{p}:BYValue?").strip(),
            "delta_y":  scope.query(f"{p}:YDELta?").strip(),
        })
    return result


def configure_cursors(scope: pyvisa.resources.Resource, mode: str | None = None,
                      ax: float | None = None, bx: float | None = None,
                      ay: float | None = None, by: float | None = None,
                      source: str | None = None, source_a: str | None = None,
                      source_b: str | None = None, cursor_type: str | None = None,
                      track_axis: str | None = None) -> dict:
    driver = get_driver(scope)
    selected = (mode or get_cursor_mode(scope)).upper()
    selected = {"MAN": "MANUAL", "TRAC": "TRACK"}.get(selected, selected)
    if selected not in {"OFF", "MANUAL", "TRACK", "XY"}:
        raise ValueError("Unsupported cursor mode")
    extended = any(value is not None for value in
                   (ay, by, source, source_a, source_b, cursor_type, track_axis))
    if driver.name != "DHO" and (extended or selected == "XY"):
        raise ValueError("Extended cursor controls require a DHO scope")
    if selected == "OFF" and (extended or ax is not None or bx is not None):
        raise ValueError("Cursor positions and sources require an enabled cursor mode")
    if selected != "MANUAL" and (source is not None or cursor_type is not None):
        raise ValueError("source and cursor_type require MANUAL mode")
    if selected != "TRACK" and any(value is not None for value in (source_a, source_b, track_axis)):
        raise ValueError("source_a, source_b and track_axis require TRACK mode")
    if selected == "XY" and any(value is not None for value in (ax, bx, ay, by)):
        raise ValueError("Use scpi_execute for XY cursor positions; ax/bx here are seconds")
    allowed_sources = {"NONE", "CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"}
    if any(value is not None and value.upper() not in allowed_sources for value in (source, source_a, source_b)):
        raise ValueError("Invalid cursor source")
    if cursor_type is not None and cursor_type.upper() not in {"TIME", "AMPLITUDE", "AMPL"}:
        raise ValueError("cursor_type must be TIME or AMPLITUDE")
    if track_axis is not None and track_axis.upper() not in {"X", "Y"}:
        raise ValueError("track_axis must be X or Y")
    if mode is not None:
        set_cursor_mode(scope, selected)
    if selected in {"OFF", "XY"}:
        return get_cursor_values(scope)
    prefix = ":CURSor:TRACk" if selected == "TRACK" else ":CURSor:MANual"
    settings = (("SOURce", source), ("TYPE", cursor_type)) if selected == "MANUAL" else (
        ("SOURce1", source_a), ("SOURce2", source_b), ("MODE", track_axis),
    )
    for suffix, value in settings:
        if value is not None:
            scope.write(f"{prefix}:{suffix} {value.upper()}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error configuring cursors: {err}")
    set_cursor_positions(scope, selected, ax=ax, bx=bx)
    for suffix, value in (("CAY", ay), ("CBY", by)):
        if value is not None:
            scope.write(f"{prefix}:{suffix} {value}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error positioning cursors: {err}")
    return get_cursor_values(scope)


def send_raw(scope: pyvisa.resources.Resource, command: str) -> str:
    """Send an arbitrary SCPI command; returns response for queries, empty string otherwise.
    Automatically checks the error queue after writes and raises on SCPI errors."""
    tokens = shlex.shlex(command, posix=False, punctuation_chars=";")
    tokens.whitespace_split = True
    tokens.commenters = ""
    header = True
    query = False
    for token in tokens:
        if header and token.endswith("?"):
            query = True
        header = token == ";"
    if query:
        return scope.query(command).strip()
    scope.write(command)
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after '{command}': {err}")
    return ""


def run(scope: pyvisa.resources.Resource) -> str:
    """Start continuous acquisition. Returns trigger status."""
    scope.write(":RUN")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :RUN: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def stop(scope: pyvisa.resources.Resource) -> str:
    """Stop acquisition and freeze the display. Returns trigger status."""
    scope.write(":STOP")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :STOP: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def single(scope: pyvisa.resources.Resource) -> str:
    """Capture a single acquisition then stop. Returns trigger status."""
    scope.write(":SINGle")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :SINGle: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def autoscale(scope: pyvisa.resources.Resource) -> None:
    """Run the scope's auto-setup (timebase, vertical scale, trigger).

    The action command differs per family (:AUToscale vs :AUToset) — the active driver
    issues it and blocks until it completes.
    """
    prev_timeout = scope.timeout
    scope.timeout = max(prev_timeout, _SLOW_OP_TIMEOUT_MS)
    try:
        get_driver(scope).autoscale(scope)
    finally:
        scope.timeout = prev_timeout
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after autoscale: {err}")


def idn(scope: pyvisa.resources.Resource) -> str:
    """Return the instrument identification string."""
    return scope.query("*IDN?").strip()


def connection_info() -> dict:
    """Report connection configuration and current session state.

    Performs NO device I/O and never raises — safe to call before, during, or after a
    connection failure. Use to build diagnostic output that surfaces what the server was
    *trying* to do, separately from whether the device responded. Without this, every
    failure surfaces as the same opaque ``VI_ERROR_TMO`` when the configured IP is unreachable.
    """
    rigol_ip     = os.environ.get("RIGOL_IP", "").strip()
    info: dict = {
        "transport": "LAN",
        "RIGOL_IP":  rigol_ip or "(unset)",
        "lan_target": (f"TCPIP0::{rigol_ip}::5555::SOCKET" if rigol_ip else "(RIGOL_IP not set)"),
    }
    if _scope is not None:
        info["session"]  = "cached/open"
        info["resource"] = getattr(_scope, "resource_name", "?")
        # _driver is set lazily on first dialect-using call; report it if known. Never
        # query *IDN? here — connection_info must remain I/O-free even when the device
        # has gone unresponsive.
        info["driver"]   = _driver.name if _driver is not None else "(not detected yet)"
    else:
        info["session"] = "not yet opened"
    return info


# All measurement items supported by DS1000Z :MEASure:ITEM
MEASURE_ITEMS = frozenset({
    # Voltage
    "VMAX", "VMIN", "VPP", "VTOP", "VBASE", "VAMP", "VAVG", "VRMS",
    "OVERSHOOT", "PRESHOOT", "MAREA", "MPAREA",
    "VUPPER", "VMID", "VLOWER", "VARIANCE", "PVRMS",
    # Time (single-source)
    "PERIOD", "FREQUENCY", "RTIME", "FTIME",
    "PWIDTH", "NWIDTH", "PDUTY", "NDUTY",
    "TVMAX", "TVMIN", "PSLEWRATE", "NSLEWRATE",
    "PPULSES", "NPULSES", "PEDGES", "NEDGES",
})

# Two-source (delay/phase) items across all families — used to reject them from the
# single-source measure(). Each family's own accepted set lives on its driver.
MEASURE_ITEMS_TWO_SOURCE = ALL_TWO_SOURCE_ITEMS


def measure(scope: pyvisa.resources.Resource, channel: str, item: str) -> str:
    """Query a single-source built-in measurement. Returns the raw value string."""
    ch = channel.upper()
    it = item.upper()
    if it in MEASURE_ITEMS_TWO_SOURCE:
        raise ValueError(f"'{item}' requires two sources — use measure_between()")
    driver = get_driver(scope)
    items = MEASURE_ITEMS | driver.extra_measure_items
    if it not in items:
        raise ValueError(f"Unknown item '{item}'. Valid: {sorted(items)}")
    note = ensure_channel_displayed(scope, ch)
    driver.register_measure_item(scope, it, ch)
    value = annotate_measurement_value(scope.query(f":MEASure:ITEM? {it},{ch}").strip())
    if note:
        value += f"\n⚠ {note}"
    return value


def measure_between(
    scope: pyvisa.resources.Resource,
    source1: str,
    source2: str,
    item: str,
) -> str:
    """Query a two-source measurement (delay or phase) between two channels.

    Accepts the canonical DS1000Z names (RDELAY, FDELAY, RPHASE, FPHASE); on families that
    use a different naming the driver maps/validates them (e.g. DHO's rising/falling matrix,
    where RDELAY→RRDELAY etc. and RFDELAY/FRDELAY are also accepted verbatim).

    source1/source2: CHAN1–CHAN4.
    Returns the raw value string (delay in seconds, phase in degrees).
    """
    driver = get_driver(scope)
    it = driver.resolve_two_source_item(item)
    s1 = source1.upper()
    s2 = source2.upper()
    notes = [n for src in (s1, s2) if (n := ensure_channel_displayed(scope, src))]
    driver.register_measure_item(scope, it, s1, s2)
    value = annotate_measurement_value(scope.query(f":MEASure:ITEM? {it},{s1},{s2}").strip())
    for note in notes:
        value += f"\n⚠ {note}"
    return value


# How long get_waveform keeps polling for a non-empty payload before reporting that the
# channel has no data. Covers a few sweeps at moderate timebases; very slow timebases
# (>~500 ms/div) may still need a manual re-capture after the first sweep completes.
_WAVEFORM_DATA_RETRY_S = 6.0

# Rigol scopes return ±9.9E37 when a measurement cannot be made (overflow, no signal,
# or — indistinguishably — the source channel being disabled). Anything at or beyond
# this magnitude is the sentinel, never a real reading.
_INVALID_SENTINEL = 9.0e37


def ensure_channel_displayed(scope: pyvisa.resources.Resource, channel: str) -> str | None:
    """Enable a CHANn source's display if it is OFF, so measurements don't silently
    return the 9.9E37 invalid sentinel.

    Returns a note string when the channel had to be enabled (for surfacing in the
    tool result), None if it was already on. Non-CHAN sources (MATH, digital) use
    different display SCPI, so they are left untouched.
    """
    ch = channel.upper()
    if not ch.startswith("CHAN"):
        return None
    if scope.query(f":{ch}:DISP?").strip() not in ("0", "OFF"):
        return None
    scope.write(f":{ch}:DISP ON")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error enabling {ch} display: {err}")
    # Let at least one acquisition complete before measuring; a just-enabled channel
    # has no data yet and would still return the invalid sentinel.
    time.sleep(0.5)
    return (
        f"{ch} display was OFF — auto-enabled it. If the value looks invalid, the scope "
        "may need more time to acquire (slow timebase); re-run the measurement."
    )


def annotate_measurement_value(value: str) -> str:
    """Append an explanation when the scope returned its invalid/overflow sentinel."""
    try:
        f = float(value)
    except ValueError:
        return value
    if not math.isfinite(f) or abs(f) >= _INVALID_SENTINEL:
        return (
            f"{value} (scope invalid/overflow sentinel — measurement could not be made; "
            "check timebase, V/div, trigger, and that the signal is on screen)"
        )
    return value


def get_scope_state(scope: pyvisa.resources.Resource) -> dict:
    """Return a snapshot of the scope's current configuration."""
    capabilities = get_capabilities(scope)
    state: dict = {"capabilities": capabilities}

    state["timebase"] = get_timebase_state(scope)
    state["channels"] = {channel: get_channel_state(scope, channel) for channel in capabilities["channels"]}
    state["trigger"] = get_trigger_state(scope)
    return state


def get_timebase_state(scope: pyvisa.resources.Resource) -> dict:
    return {
        "scale_s_div": scope.query(":TIM:SCAL?").strip(),
        "offset_s":    scope.query(":TIM:OFFS?").strip(),
        "mode":        scope.query(":TIM:MODE?").strip(),
    }


def get_channel_state(scope: pyvisa.resources.Resource, channel: str) -> dict:
    channel = channel.upper()
    return {
        "display": scope.query(f":{channel}:DISP?").strip().upper() in ("1", "ON"),
        "scale_v_div": scope.query(f":{channel}:SCAL?").strip(),
        "offset_v": scope.query(f":{channel}:OFFS?").strip(),
        "coupling": scope.query(f":{channel}:COUP?").strip(),
        "probe": scope.query(f":{channel}:PROB?").strip(),
    }


def get_trigger_state(scope: pyvisa.resources.Resource) -> dict:
    trig_mode = scope.query(":TRIGger:MODE?").strip()
    state = {
        "mode":   trig_mode,
        "status": scope.query(":TRIGger:STATus?").strip(),
    }
    if trig_mode.upper() in ("EDGE", "EDGMODE"):
        state.update({
            "source":  scope.query(":TRIGger:EDGE:SOURce?").strip(),
            "slope":   scope.query(":TRIGger:EDGE:SLOPe?").strip(),
            "level_v": scope.query(":TRIGger:EDGE:LEVel?").strip(),
        })

    return state


def set_channel(
    scope: pyvisa.resources.Resource,
    channel: str,
    display: bool | None = None,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
) -> None:
    """Configure a channel. Only specified parameters are changed.

    Order matters: PROBe is written before SCALe/OFFSet because changing
    probe attenuation rescales the displayed scale by the probe ratio on
    both DS1000Z and DHO — writing SCAL first would cause a subsequent
    PROB change to multiply it and land at the wrong V/div.
    """
    ch = channel.upper()
    if display is not None:
        scope.write(f":{ch}:DISP {'ON' if display else 'OFF'}")
    if probe is not None:
        # Format with :g so 10.0 → "10" (DHO rejects "10.0" with -222 because its
        # probe enum lists integers, not floats); fractional values like 0.1 stay "0.1".
        scope.write(f":{ch}:PROB {probe:g}")
    if coupling is not None:
        scope.write(f":{ch}:COUP {coupling.upper()}")
    if scale is not None:
        scope.write(f":{ch}:SCAL {scale}")
    if offset is not None:
        scope.write(f":{ch}:OFFS {offset}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_channel({ch}): {err}")


def set_timebase(
    scope: pyvisa.resources.Resource,
    scale: float | None = None,
    offset: float | None = None,
) -> None:
    """Set timebase scale (s/div) and/or offset (s)."""
    if scale is not None:
        scope.write(f":TIM:SCAL {scale}")
    if offset is not None:
        scope.write(f":TIM:OFFS {offset}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_timebase: {err}")


def set_trigger(
    scope: pyvisa.resources.Resource,
    source: str | None = None,
    slope: str | None = None,
    level: float | None = None,
) -> None:
    """Configure edge trigger. source: CHAN1–CHAN4, EXT. slope: POS, NEG, RFAL."""
    if source is not None:
        capabilities = get_capabilities(scope)
        allowed = capabilities["channels"] + (["EXT"] if capabilities["external_trigger"] else [])
        source = source.upper().replace("CHANNEL", "CHAN")
        if source not in allowed:
            raise ValueError(f"Trigger source {source} is not supported by {capabilities['model']}")
    scope.write(":TRIGger:MODE EDGE")
    if source is not None:
        scope.write(f":TRIGger:EDGE:SOURce {source.upper()}")
    if slope is not None:
        scope.write(f":TRIGger:EDGE:SLOPe {slope.upper()}")
    if level is not None:
        scope.write(f":TRIGger:EDGE:LEVel {level}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_trigger: {err}")


def get_capabilities(scope: pyvisa.resources.Resource, verify_hardware: bool = False) -> dict:
    identity = scope.query("*IDN?").strip()
    capabilities = capabilities_for(identity)
    driver = driver_for(identity)
    capabilities["measurement_items"] = sorted(capabilities.get("measurement_items", MEASURE_ITEMS | driver.extra_measure_items))
    capabilities["two_source_items"] = sorted(capabilities.get("two_source_items", driver.two_source_items))
    evidence = {
        field: {"status": "unverified", "source": "family-driver assumption"}
        for field in capabilities
    }
    documented = evidence_for(capabilities["model"])
    evidence.update(documented)
    evidence["model"] = {"status": "hardware-verified", "source": "*IDN?"}
    evidence["command_catalog"] = {
        "status": "documented", "source": "server policy: catalog enabled only for DHO814; not hardware coverage",
    }
    if verify_hardware and {"channels", "horizontal_divisions"} <= documented.keys():
        for field, command in (("channels", ":SYSTem:RAMount?"),
                               ("horizontal_divisions", ":SYSTem:GAMount?")):
            response = scope.query(command).strip()
            error = check_scpi_error(scope)
            try:
                if error:
                    raise ValueError(error)
                value = int(response)
                if not 1 <= value <= (4 if field == "channels" else 100):
                    raise ValueError(f"Unexpected value: {response}")
            except ValueError as exc:
                evidence[field] = {"status": "unverified", "source": command,
                                   "error": str(exc)[:160], "fallback": "model definition"}
                continue
            observed = [f"CHAN{number}" for number in range(1, value + 1)] if field == "channels" else value
            evidence[field] = {"status": "hardware-verified", "source": command}
            if observed != capabilities[field]:
                evidence[field]["model_value"] = capabilities[field]
                evidence[field]["mismatch"] = True
            capabilities[field] = observed
    capabilities["evidence"] = evidence
    return capabilities


def get_waveform(scope: pyvisa.resources.Resource, channel: str) -> dict:
    """Download waveform data for a channel (screen buffer, NORM mode).

    Returns time/voltage arrays plus summary statistics.
    Stop or single-trigger the scope first for consistent data.
    """
    ch = channel.upper()
    warnings = []
    if note := ensure_channel_displayed(scope, ch):
        warnings.append(note)
    scope.write(f":WAV:SOUR {ch}")
    scope.write(":WAV:MODE NORM")
    scope.write(":WAV:FORM ASC")
    get_driver(scope).prepare_waveform(scope)

    pre_str = scope.query(":WAV:PRE?").strip()
    pre = pre_str.split(",")
    x_inc   = float(pre[4])
    x_origin = float(pre[5])
    x_ref   = float(pre[6])

    # Read the ASCII waveform payload. The framing differs by family (DS1000Z wraps the CSV
    # in an IEEE 488.2 definite-length block, DHO sends bare CSV) so the read strategy is
    # delegated to the driver — see ScopeDriver.read_waveform_data.
    data_str = get_driver(scope).read_waveform_data(scope)
    voltages = [float(v) for v in data_str.split(",") if v.strip()]
    # A just-enabled channel serves an empty payload until a full sweep lands (~2 s after
    # DISP ON at 50 ms/div on a DS1104Z), so poll briefly before giving up.
    deadline = time.monotonic() + _WAVEFORM_DATA_RETRY_S
    while not voltages and time.monotonic() < deadline:
        time.sleep(1.0)
        data_str = get_driver(scope).read_waveform_data(scope)
        voltages = [float(v) for v in data_str.split(",") if v.strip()]
    if not voltages:
        reason = (
            f"{ch} returned no waveform data — the channel has not acquired anything yet. "
            "Ensure acquisition is running (run tool, or single/autoscale) and re-capture."
        )
        if warnings:
            reason += f" Note: {warnings[0]}"
        raise RuntimeError(reason)
    if not all(math.isfinite(value) for value in voltages):
        raise ValueError("Waveform contains non-finite samples; acquire valid data before capture")
    if x_inc == 0:
        # The preamble was read before the first sweep on a just-enabled channel completed —
        # the scope reports 0 s/point until then. Refresh it now that data exists.
        pre = scope.query(":WAV:PRE?").strip().split(",")
        x_inc    = float(pre[4])
        x_origin = float(pre[5])
        x_ref    = float(pre[6])
    if not all(math.isfinite(value) for value in (x_inc, x_origin, x_ref)) or x_inc <= 0:
        raise ValueError("Invalid waveform timing; acquire data before capture")
    n = len(voltages)
    times = [x_origin + (i - x_ref) * x_inc for i in range(n)]

    # Vertical scale/offset let the analyser judge amplitude against the full-screen range
    # (8 vertical divisions) and flag noise-floor captures. Best-effort: if the scope does
    # not answer, the analysis degrades gracefully to amplitude-only reporting.
    try:
        y_scale = float(scope.query(f":{ch}:SCAL?"))
    except Exception:
        y_scale = None
    try:
        y_offset = float(scope.query(f":{ch}:OFFS?"))
    except Exception:
        y_offset = None

    if any(abs(v) >= _INVALID_SENTINEL for v in voltages):
        warnings.append(
            "Waveform contains 9.9E37 invalid-sentinel samples — the scope had no valid "
            "data for this channel; min/max/mean statistics are unreliable. Re-capture "
            "after the channel has acquired data."
        )

    return {
        "channel":        ch,
        "points":         n,
        "time_increment_s": x_inc,
        "time_start_s":   times[0] if times else 0.0,
        "time_end_s":     times[-1] if times else 0.0,
        "vmin_v":         min(voltages),
        "vmax_v":         max(voltages),
        "vmean_v":        sum(voltages) / n if n else 0.0,
        "y_scale_v_per_div": y_scale,
        "y_offset_v":     y_offset,
        "times_s":        times,
        "voltages_v":     voltages,
        "warnings":       warnings,
    }


class BlockReadError(ValueError):
    """The stream is no longer trustworthy after malformed binary framing."""


def _read_definite_block(scope: pyvisa.resources.Resource) -> bytes:
    """Read an IEEE 488.2 block payload by exact byte count over TCP/IP."""
    return _read_block_via_bytecount(scope)


def _read_block_via_bytecount(scope: pyvisa.resources.Resource) -> bytes:
    """Read precisely the declared payload length, including any embedded newlines."""
    prefix = scope.read_bytes(2)  # '#' + digit-count byte
    if prefix[0:1] != b"#":
        raise BlockReadError(f"Expected TMC block header starting with '#', got {prefix!r}")
    if len(prefix) != 2 or prefix[1:2] not in b"123456789":
        raise BlockReadError("Expected a definite-length block with 1..9 length digits")
    n = int(prefix[1:2])
    length_bytes = scope.read_bytes(n)
    if len(length_bytes) != n or not length_bytes.isdigit():
        raise BlockReadError("Invalid or truncated block length")
    data_length = int(length_bytes)
    raw = scope.read_bytes(data_length + 1)  # +1 for the trailing newline
    if len(raw) != data_length + 1:
        raise BlockReadError(f"Truncated block: expected {data_length + 1} bytes, received {len(raw)}")
    if raw[-1:] != b'\n':
        raise BlockReadError(f"Expected \\n after definite-length block, got {raw[-1:]!r}")
    return raw[:-1]


def screenshot_png(scope: pyvisa.resources.Resource) -> bytes:
    """Return raw PNG bytes from the scope display.

    The `:DISP:DATA?` query form differs per family (the active driver supplies it).
    Strips the IEEE 488.2 TMC block header (#NXXXXXXXXX) and trailing \\n.
    """
    scope.write(get_driver(scope).screenshot_query())
    return _read_definite_block(scope)

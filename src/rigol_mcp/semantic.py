"""Semantic DHO814 operations built on the validated SCPI catalog."""

from __future__ import annotations

import math
from collections.abc import Iterable

from rigol_mcp import scpi
from rigol_mcp.scope import _read_definite_block, check_scpi_error, get_capabilities


def _require_catalog(instrument) -> None:
    if not get_capabilities(instrument)["command_catalog"]:
        raise ValueError("This semantic operation requires a DHO814")


def _prepared(header: str, operation: str, arguments: list | None = None) -> str:
    return scpi.prepare(header, operation, arguments)[1]


def _query(instrument, header: str, arguments: list | None = None) -> str:
    return instrument.query(_prepared(header, "query", arguments)).strip()


def _configure(instrument, requested: dict, writes: Iterable[tuple[str, list]],
               readbacks: dict[str, tuple[str, list]]) -> dict:
    writes = [(header, _prepared(header, "write", arguments)) for header, arguments in writes]
    queries = {key: _prepared(header, "query", arguments)
               for key, (header, arguments) in readbacks.items()}
    _require_catalog(instrument)
    before = {key: instrument.query(command).strip() for key, command in queries.items()}
    for header, command in writes:
        instrument.write(command)
        if error := check_scpi_error(instrument):
            raise RuntimeError(f"SCPI error after {header}: {error}")
    applied = {key: instrument.query(command).strip() for key, command in queries.items()}
    mismatches = {
        key: {"requested": requested[key], "applied": value}
        for key, value in applied.items()
        if key in requested and not _readback_matches(requested[key], value)
    }
    return {
        "requested": requested,
        "applied": applied,
        "changed": before != applied,
        "fully_applied": not mismatches,
        "mismatches": mismatches,
    }


def _readback_matches(expected, applied: str) -> bool:
    if isinstance(expected, bool):
        return applied.strip().upper() in ({"1", "ON"} if expected else {"0", "OFF"})
    if isinstance(expected, (int, float)):
        try:
            actual = float(applied)
        except ValueError:
            return False
        return math.isclose(actual, float(expected), rel_tol=0.02, abs_tol=1e-15)
    aliases = {
        "NORMAL": "NORM", "AVERAGES": "AVER", "POSITIVE": "POS",
        "NEGATIVE": "NEG", "SUBTRACT": "SUBT", "MULTIPLY": "MULT",
        "DIVIDE": "DIV", "DIFFERENTIAL": "DIFF", "SINGLE": "SING",
        "FORWARD": "FORW", "LPASS": "LPAS", "HPASS": "HPAS",
        "BPASS": "BPAS", "BSTOP": "BSTO", "ASCII": "ASC",
        "I2C": "IIC", "TIMEOUT": "TIM", "START": "STAR",
    }
    expected_text = aliases.get(str(expected).strip().upper(), str(expected).strip().upper())
    applied_text = aliases.get(applied.strip().strip('"').upper(), applied.strip().strip('"').upper())
    return expected_text.replace("CHANNEL", "CHAN") == applied_text.replace("CHANNEL", "CHAN")


def clear_measurements(instrument) -> dict:
    command = _prepared(":MEASure:CLEar", "write")
    _require_catalog(instrument)
    instrument.write(command)
    if error := check_scpi_error(instrument):
        raise RuntimeError(f"SCPI error after {command}: {error}")
    return {"cleared": True}


def configure_acquisition(instrument, *, acquisition_type=None, memory_depth=None,
                          averages=None, ultra_mode=None, ultra_timeout_s=None,
                          ultra_max_frames=None) -> dict:
    values = {
        "acquisition_type": acquisition_type,
        "memory_depth": memory_depth,
        "averages": averages,
        "ultra_mode": ultra_mode,
        "ultra_timeout_s": ultra_timeout_s,
        "ultra_max_frames": ultra_max_frames,
    }
    commands = {
        "acquisition_type": ":ACQuire:TYPE",
        "memory_depth": ":ACQuire:MDEPth",
        "averages": ":ACQuire:AVERages",
        "ultra_mode": ":ACQuire:ULTRa:MODE",
        "ultra_timeout_s": ":ACQuire:ULTRa:TIMeout",
        "ultra_max_frames": ":ACQuire:ULTRa:MAXFrame",
    }
    requested = {key: value for key, value in values.items() if value is not None}
    if not requested:
        raise ValueError("Supply at least one acquisition setting")
    writes = [(commands[key], [value]) for key, value in requested.items()]
    readbacks = {key: (commands[key], []) for key in requested}
    readbacks["sample_rate_hz"] = (":ACQuire:SRATe", [])
    return _configure(instrument, requested, writes, readbacks)


def configure_meter(instrument, *, meter: str, enabled=None, source=None, mode=None) -> dict:
    meter = meter.upper()
    if meter not in {"DVM", "COUNTER"}:
        raise ValueError("meter must be DVM or COUNTER")
    if meter == "COUNTER" and mode is not None:
        raise ValueError("mode applies only to DVM")
    prefix = ":DVM" if meter == "DVM" else ":MEASure:COUNter"
    values = {"enabled": enabled, "source": source}
    if meter == "DVM":
        values["mode"] = mode
    requested = {key: value for key, value in values.items() if value is not None}
    if not requested:
        raise ValueError("Supply at least one meter setting")
    suffixes = {"enabled": "ENABle", "source": "SOURce", "mode": "MODE"}
    writes = [(f"{prefix}:{suffixes[key]}", [value]) for key, value in requested.items()]
    readbacks = {key: (f"{prefix}:{suffixes[key]}", []) for key in requested}
    return _configure(instrument, {"meter": meter, **requested}, writes, readbacks)


def get_meter_value(instrument, meter: str) -> dict:
    meter = meter.upper()
    command = {"DVM": ":DVM:CURRent", "COUNTER": ":MEASure:COUNter:VALue"}.get(meter)
    if command is None:
        raise ValueError("meter must be DVM or COUNTER")
    prepared = _prepared(command, "query")
    _require_catalog(instrument)
    value = instrument.query(prepared).strip()
    try:
        numeric = float(value)
    except ValueError:
        numeric = None
    return {"meter": meter, "value": numeric, "raw": value}


def measure_statistics(instrument, *, item: str, source1: str,
                       source2: str | None = None, statistics: list[str] | None = None) -> dict:
    statistics = statistics or ["CURRENT", "AVERAGE", "MINIMUM", "MAXIMUM", "DEVIATION", "COUNT"]
    aliases = {"AVERAGE": "AVERAGES", "AVERAGES": "AVERAGES", "CURRENT": "CURRENT",
               "MINIMUM": "MINIMUM", "MAXIMUM": "MAXIMUM", "DEVIATION": "DEVIATION",
               "COUNT": "COUNT"}
    scpi_tokens = {"CURRENT": "CURRent", "AVERAGES": "AVERages", "MINIMUM": "MINimum",
                   "MAXIMUM": "MAXimum", "DEVIATION": "DEViation", "COUNT": "CNT"}
    normalized = []
    for statistic in statistics:
        key = statistic.upper()
        if key not in aliases:
            raise ValueError(f"Unsupported statistic: {statistic}")
        normalized.append(aliases[key])
    sources = [source1] + ([source2] if source2 else [])
    _require_catalog(instrument)
    instrument.write(_prepared(":MEASure:STATistic:ITEM", "write", [item, *sources]))
    if error := check_scpi_error(instrument):
        raise RuntimeError(f"SCPI error after registering statistic: {error}")
    values = {}
    for statistic in normalized:
        raw = _query(
            instrument, ":MEASure:STATistic:ITEM",
            [scpi_tokens[statistic], item, *sources],
        )
        try:
            numeric = float(raw)
            valid = math.isfinite(numeric) and abs(numeric) < 9.9e37
        except ValueError:
            numeric, valid = None, False
        values[statistic.lower()] = {"value": numeric if valid else None, "raw": raw, "valid": valid}
    return {"item": item.upper(), "source1": source1.upper(), "source2": source2.upper() if source2 else None,
            "statistics": values}


def configure_mask_test(instrument, *, enabled=None, source=None, horizontal_tolerance=None,
                        vertical_tolerance=None, create: bool = False, running=None,
                        output_enabled=None, output_event=None, output_time_s=None) -> dict:
    if create and enabled is False:
        raise ValueError("create=true requires enabled=true")
    if create and enabled is None:
        enabled = True
    values = {
        "enabled": (enabled, ":MASK:ENABle"), "source": (source, ":MASK:SOURce"),
        "horizontal_tolerance": (horizontal_tolerance, ":MASK:X"),
        "vertical_tolerance": (vertical_tolerance, ":MASK:Y"),
        "running": (("RUN" if running else "STOP") if running is not None else None, ":MASK:OPERate"),
        "output_enabled": (output_enabled, ":MASK:OUTPut:ENABle"),
        "output_event": (output_event, ":MASK:OUTPut:EVENt"),
        "output_time_s": (output_time_s, ":MASK:OUTPut:TIME"),
    }
    requested = {key: value for key, (value, _) in values.items() if value is not None}
    if create:
        requested["create"] = True
    if not requested:
        raise ValueError("Supply at least one mask setting or create=true")
    writes = [
        (header, [value]) for key, (value, header) in values.items()
        if value is not None and key != "running"
    ]
    if create:
        writes.append((":MASK:OPERate", ["STOP"]))
        writes.append((":MASK:CREate", []))
    if running is not None:
        writes.append((":MASK:OPERate", ["RUN" if running else "STOP"]))
    readbacks = {key: (header, []) for key, (value, header) in values.items() if value is not None}
    return _configure(instrument, requested, writes, readbacks)


def get_mask_results(instrument) -> dict:
    _require_catalog(instrument)
    raw = {key: _query(instrument, header) for key, header in {
        "failed": ":MASK:FAILed", "passed": ":MASK:PASSed", "total": ":MASK:TOTal",
        "enabled": ":MASK:ENABle", "running": ":MASK:OPERate", "source": ":MASK:SOURce",
    }.items()}
    counts = {}
    for key in ("failed", "passed", "total"):
        try:
            counts[key] = int(float(raw[key]))
        except ValueError:
            counts[key] = None
    total, failed = counts["total"], counts["failed"]
    return {**counts, "failure_ratio": failed / total if total and failed is not None else None,
            "enabled": raw["enabled"], "running": raw["running"], "source": raw["source"]}


def configure_search(instrument, *, mode: str, enabled=None, source=None, slope=None,
                     threshold_v=None, polarity=None, qualifier=None,
                     upper_width_s=None, lower_width_s=None) -> dict:
    mode = mode.upper()
    if mode not in {"EDGE", "PULSE"}:
        raise ValueError("mode must be EDGE or PULSE")
    requested = {"mode": mode}
    writes = [(":SEARch:MODE", [mode])]
    readbacks = {"mode": (":SEARch:MODE", [])}
    common = {"enabled": (enabled, ":SEARch:STATe")}
    fields = ({"source": (source, ":SEARch:EDGE:SOURce"),
               "slope": (slope, ":SEARch:EDGE:SLOPe"),
               "threshold_v": (threshold_v, ":SEARch:EDGE:THReshold")}
              if mode == "EDGE" else
              {"source": (source, ":SEARch:PULSe:SOURce"),
               "polarity": (polarity, ":SEARch:PULSe:POLarity"),
               "qualifier": (qualifier, ":SEARch:PULSe:QUALifier"),
               "upper_width_s": (upper_width_s, ":SEARch:PULSe:UWIDth"),
               "lower_width_s": (lower_width_s, ":SEARch:PULSe:LWIDth"),
               "threshold_v": (threshold_v, ":SEARch:PULSe:THReshold")})
    supplied = {key for key, (value, _) in {**common, **fields}.items() if value is not None}
    invalid = ({"polarity", "qualifier", "upper_width_s", "lower_width_s"} if mode == "EDGE" else {"slope"})
    arguments = {"slope": slope, "polarity": polarity, "qualifier": qualifier,
                 "upper_width_s": upper_width_s, "lower_width_s": lower_width_s}
    if any(arguments[key] is not None for key in invalid):
        raise ValueError(f"Unsupported {mode} search setting")
    for key, (value, header) in {**common, **fields}.items():
        if value is not None:
            requested[key] = value
            writes.append((header, [value]))
            readbacks[key] = (header, [])
    return _configure(instrument, requested, writes, readbacks)


def get_search_results(instrument, *, offset: int = 0, limit: int = 100) -> dict:
    _require_catalog(instrument)
    count = int(float(_query(instrument, ":SEARch:COUNt")))
    if offset > count:
        raise ValueError("offset exceeds search event count")
    end = min(count, offset + limit)
    events = []
    for index in range(offset + 1, end + 1):
        raw = _query(instrument, ":SEARch:VALue", [index])
        try:
            value = float(raw)
        except ValueError:
            value = None
        events.append({"index": index, "time_s": value, "raw": raw})
    return {"count": count, "offset": offset, "returned": len(events),
            "next_offset": end if end < count else None, "events": events}


def configure_recording(instrument, *, enabled=None, frames=None, interval_s=None,
                        prompt=None, running=None, use_max_frames: bool = False) -> dict:
    values = {
        "enabled": (enabled, ":RECord:WRECord:ENABle"),
        "frames": (frames, ":RECord:WRECord:FRAMes"),
        "interval_s": (interval_s, ":RECord:WRECord:FINTerval"),
        "prompt": (prompt, ":RECord:WRECord:PROMpt"),
        "running": (("RUN" if running else "STOP") if running is not None else None,
                    ":RECord:WRECord:OPERate"),
    }
    requested = {key: value for key, (value, _) in values.items() if value is not None}
    if use_max_frames:
        requested["use_max_frames"] = True
    if not requested:
        raise ValueError("Supply at least one recording setting or use_max_frames=true")
    writes = [(header, [value]) for value, header in values.values() if value is not None]
    if use_max_frames:
        writes.append((":RECord:WRECord:FRAMes:MAX", []))
    readbacks = {key: (header, []) for key, (value, header) in values.items() if value is not None}
    if use_max_frames:
        readbacks["frames"] = (":RECord:WRECord:FRAMes", [])
    return _configure(instrument, requested, writes, readbacks)


def get_recording_state(instrument) -> dict:
    _require_catalog(instrument)
    headers = {
        "recording_enabled": ":RECord:WRECord:ENABle",
        "recording_operation": ":RECord:WRECord:OPERate",
        "recording_frames": ":RECord:WRECord:FRAMes",
        "recording_max_frames": ":RECord:WRECord:FMAX",
        "recording_interval_s": ":RECord:WRECord:FINTerval",
        "current_frame": ":RECord:WREPlay:FCURrent",
        "current_frame_time_s": ":RECord:WREPlay:FCURrent:TIME",
        "replay_start": ":RECord:WREPlay:FSTart", "replay_end": ":RECord:WREPlay:FEND",
        "replay_max_frames": ":RECord:WREPlay:FMAX", "replay_interval_s": ":RECord:WREPlay:FINTerval",
        "replay_mode": ":RECord:WREPlay:MODE", "replay_direction": ":RECord:WREPlay:DIRection",
        "replay_operation": ":RECord:WREPlay:OPERate",
    }
    return {key: _query(instrument, header) for key, header in headers.items()}


def control_recording_replay(instrument, *, action: str, frame=None, start_frame=None,
                             end_frame=None, interval_s=None, mode=None, direction=None) -> dict:
    action = action.upper()
    if action not in {"SELECT", "PLAY", "STOP", "PREVIOUS", "NEXT", "FIRST", "LAST"}:
        raise ValueError("Unsupported replay action")
    values = {
        "frame": (frame, ":RECord:WREPlay:FCURrent"),
        "start_frame": (start_frame, ":RECord:WREPlay:FSTart"),
        "end_frame": (end_frame, ":RECord:WREPlay:FEND"),
        "interval_s": (interval_s, ":RECord:WREPlay:FINTerval"),
        "mode": (mode, ":RECord:WREPlay:MODE"),
        "direction": (direction, ":RECord:WREPlay:DIRection"),
    }
    writes = [(header, [value]) for value, header in values.values() if value is not None]
    readbacks = {key: (header, []) for key, (value, header) in values.items() if value is not None}
    action_commands = {
        "PLAY": (":RECord:WREPlay:OPERate", ["RUN"]),
        "STOP": (":RECord:WREPlay:OPERate", ["STOP"]),
        "PREVIOUS": (":RECord:WREPlay:BACK", []), "NEXT": (":RECord:WREPlay:NEXT", []),
        "FIRST": (":RECord:WREPlay:PLAY", ["FFIRST"]), "LAST": (":RECord:WREPlay:PLAY", ["FEND"]),
    }
    if action in action_commands:
        writes.append(action_commands[action])
    if action == "SELECT" and frame is None:
        raise ValueError("SELECT requires frame")
    if action in {"SELECT", "PREVIOUS", "NEXT", "FIRST", "LAST"}:
        readbacks.setdefault("current_frame", (":RECord:WREPlay:FCURrent", []))
    if action in {"PLAY", "STOP"}:
        readbacks["replay_operation"] = (":RECord:WREPlay:OPERate", [])
    result = _configure(instrument, {"action": action, **{key: value for key, (value, _) in values.items()
                                                          if value is not None}}, writes, readbacks)
    result["action"] = action
    return result


_DECODE_FIELDS = {
    "PARALLEL": {
        "source": "BUS", "clock": "CLK", "slope": "SLOPe", "width": "WIDTh",
        "bit": "BITX", "source_channel": "SOURce", "endian": "ENDian", "polarity": "POLarity",
    },
    "RS232": {
        "tx": "TX", "rx": "RX", "polarity": "POLarity", "parity": "PARity",
        "endian": "ENDian", "baud": "BAUD", "data_bits": "DBITs", "stop_bits": "SBITs",
    },
    "IIC": {
        "scl": "SCLK:SOURce", "sda": "SDA:SOURce", "exchange": "EXCHange",
        "address_mode": "ADDRess",
    },
    "SPI": {
        "clock": "SCLK:SOURce", "clock_slope": "SCLK:SLOPe", "miso": "MISO:SOURce",
        "mosi": "MOSI:SOURce", "polarity": "POLarity", "miso_polarity": "MISO:POLarity",
        "mosi_polarity": "MOSI:POLarity", "data_bits": "DBITs", "endian": "ENDian",
        "cs_mode": "MODE", "timeout_s": "TIMeout:TIME", "chip_select": "SS:SOURce",
        "chip_select_polarity": "SS:POLarity",
    },
    "CAN": {
        "source": "SOURce", "signal_type": "STYPe", "baud": "BAUD",
        "sample_point": "SPOint",
    },
}

_DECODE_THRESHOLD_TYPES = {
    "PARALLEL": {"PAL", "PALCLK"},
    "RS232": {"TX", "RX"},
    "IIC": {"SCL", "SDA"},
    "SPI": {"CLK", "MISO", "MOSI", "CS"},
    "CAN": {"CAN"},
}


def configure_decode(instrument, *, bus: int, protocol: str, display=None, format=None,
                     settings: dict | None = None) -> dict:
    protocol = protocol.upper()
    if protocol == "I2C":
        protocol = "IIC"
    if protocol not in _DECODE_FIELDS:
        raise ValueError("protocol must be PARALLEL, RS232, I2C, SPI, or CAN")
    settings = settings or {}
    unknown = settings.keys() - (_DECODE_FIELDS[protocol].keys() | {"thresholds_v"})
    if unknown:
        raise ValueError(f"Unsupported {protocol} decode settings: {', '.join(sorted(unknown))}")
    thresholds = settings.get("thresholds_v", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds_v must be an object")
    normalized_thresholds = {str(key).upper(): value for key, value in thresholds.items()}
    unknown_thresholds = normalized_thresholds.keys() - _DECODE_THRESHOLD_TYPES[protocol]
    if unknown_thresholds:
        raise ValueError(f"Unsupported {protocol} threshold types: {', '.join(sorted(unknown_thresholds))}")
    requested = {"bus": bus, "protocol": "I2C" if protocol == "IIC" else protocol}
    writes = [(f":BUS{bus}:MODE", [protocol])]
    readbacks = {"protocol": (f":BUS{bus}:MODE", [])}
    for key, value in (("display", display), ("format", format)):
        if value is not None:
            requested[key] = value
            writes.append((f":BUS{bus}:{'DISPlay' if key == 'display' else 'FORMat'}", [value]))
            readbacks[key] = (writes[-1][0], [])
    for key, value in settings.items():
        if key == "thresholds_v":
            continue
        requested[key] = value
        header = f":BUS{bus}:{protocol}:{_DECODE_FIELDS[protocol][key]}"
        writes.append((header, [value]))
        readbacks[key] = (header, [])
    for threshold_type, value in normalized_thresholds.items():
        key = f"{threshold_type.lower()}_threshold_v"
        requested[key] = value
        writes.append((f":BUS{bus}:THReshold", [value, threshold_type]))
        readbacks[key] = (f":BUS{bus}:THReshold", [threshold_type])
    return _configure(instrument, requested, writes, readbacks)


def get_decode_result(instrument, bus: int) -> dict:
    commands = {
        "protocol": _prepared(f":BUS{bus}:MODE", "query"),
        "display": _prepared(f":BUS{bus}:DISPlay", "query"),
        "format": _prepared(f":BUS{bus}:FORMat", "query"),
    }
    _require_catalog(instrument)
    result = {"bus": bus, **{key: instrument.query(command).strip()
                              for key, command in commands.items()}}
    instrument.write(_prepared(f":BUS{bus}:DATA", "query"))
    result["data"] = _read_definite_block(instrument).decode("utf-8", errors="replace").strip()
    if error := check_scpi_error(instrument):
        raise RuntimeError(f"SCPI error after :BUS{bus}:DATA?: {error}")
    return result


_TRIGGER_FIELDS = {
    "EDGE": {"source": "SOURce", "slope": "SLOPe", "level": "LEVel"},
    "PULSE": {"source": "SOURce", "polarity": "POLarity", "condition": "WHEN",
              "upper_s": "UWIDth", "lower_s": "LWIDth", "level": "LEVel"},
    "SLOPE": {"source": "SOURce", "polarity": "POLarity", "condition": "WHEN",
              "upper_s": "TUPPer", "lower_s": "TLOWer", "window": "WINDow",
              "level_a": "ALEVel", "level_b": "BLEVel"},
    "VIDEO": {"source": "SOURce", "polarity": "POLarity", "video_mode": "MODE",
              "line": "LINE", "standard": "STANdard", "level": "LEVel"},
    "PATTERN": {"pattern": "PATTern", "source": "SOURce", "level": "LEVel"},
    "DURATION": {"source": "SOURce", "pattern": "TYPE", "condition": "WHEN",
                 "upper_s": "TUPPer", "lower_s": "TLOWer", "level": "LEVel"},
    "TIMEOUT": {"source": "SOURce", "slope": "SLOPe", "timeout_s": "TIME", "level": "LEVel"},
    "RUNT": {"source": "SOURce", "polarity": "POLarity", "condition": "WHEN",
             "upper_s": "WUPPer", "lower_s": "WLOWer", "level_a": "ALEVel", "level_b": "BLEVel"},
    "WINDOW": {"source": "SOURce", "slope": "SLOPe", "position": "POSition",
               "timeout_s": "TIME", "level_a": "ALEVel", "level_b": "BLEVel"},
    "DELAY": {"source_a": "SA", "slope_a": "ASLop", "source_b": "SB", "slope_b": "BSLop",
              "condition": "TYPE", "upper_s": "TUPPer", "lower_s": "TLOWer",
              "level_a": "ALEVel", "level_b": "BLEVel"},
    "SETUP": {"data_source": "DSRC", "clock_source": "CSRC", "slope": "SLOPe",
              "pattern": "PATTern", "condition": "TYPE", "setup_s": "STIMe",
              "hold_s": "HTIMe", "data_level": "DLEVel", "clock_level": "CLEVel"},
    "NEDGE": {"source": "SOURce", "slope": "SLOPe", "idle_s": "IDLE",
              "count": "EDGE", "level": "LEVel"},
        "RS232": {"source": "SOURce", "level": "LEVel", "polarity": "POLarity",
              "condition": "WHEN", "data": "DATA", "baud": "BAUD",
              "data_bits": "WIDTh", "stop_bits": "STOP", "parity": "PARity"},
        "IIC": {"scl": "SCL", "clock_level": "CLEVel", "sda": "SDA",
            "data_level": "DLEVel", "condition": "WHEN", "address_width": "AWIDth",
            "address": "ADDRess", "direction": "DIRection", "data_bytes": "DBYTes",
            "data": "DATA", "current_bit": "CURRbit", "code": "CODE"},
        "SPI": {"clock": "CLK", "clock_level": "CLEVel", "slope": "SLOPe",
            "miso": "MISO", "data_source": "SDA", "data_level": "DLEVel",
            "condition": "WHEN", "chip_select": "CS", "chip_select_level": "SLEVel",
            "chip_select_polarity": "MODE", "timeout_s": "TIMeout",
            "data_bits": "WIDTh", "data": "DATA", "current_bit": "CURRbit", "code": "CODE"},
        "CAN": {"baud": "BAUD", "source": "SOURce", "signal_type": "STYPe",
            "condition": "WHEN", "sample_point": "SPOint", "extended": "EXTended",
            "define": "DEFine", "data_width": "DWIDth", "data": "DATA",
            "current_bit": "CURRbit", "code": "CODE", "level": "LEVel"},
}
_TRIGGER_PREFIX = {"WINDOW": "WINDows", "NEDGE": "NEDGe", "SETUP": "SHOLd"}


def configure_trigger(instrument, *, trigger_type: str, settings: dict | None = None,
                      coupling=None, sweep=None, holdoff_s=None, noise_reject=None) -> dict:
    trigger_type = trigger_type.upper()
    if trigger_type == "I2C":
        trigger_type = "IIC"
    if trigger_type not in _TRIGGER_FIELDS:
        raise ValueError(f"Unsupported semantic trigger type: {trigger_type}")
    settings = settings or {}
    unknown = settings.keys() - _TRIGGER_FIELDS[trigger_type].keys()
    if unknown:
        raise ValueError(f"Unsupported {trigger_type} trigger settings: {', '.join(sorted(unknown))}")
    requested = {"type": "I2C" if trigger_type == "IIC" else trigger_type, **settings}
    writes = [(":TRIGger:MODE", [trigger_type])]
    readbacks = {"type": (":TRIGger:MODE", [])}
    prefix = _TRIGGER_PREFIX.get(trigger_type, trigger_type)
    for key, value in settings.items():
        header = f":TRIGger:{prefix}:{_TRIGGER_FIELDS[trigger_type][key]}"
        if key == "pattern" and trigger_type in {"PATTERN", "DURATION"}:
            if not isinstance(value, list) or len(value) != 4:
                raise ValueError(f"{trigger_type} pattern must contain four channel states")
            write_arguments = value
        elif key == "level" and trigger_type in {"PATTERN", "DURATION"}:
            if "source" not in settings:
                raise ValueError(f"{trigger_type} level requires source")
            write_arguments = [value, settings["source"]]
        else:
            write_arguments = [value]
        writes.append((header, write_arguments))
        query_arguments = [settings["source"]] if key == "level" and trigger_type in {"PATTERN", "DURATION"} else []
        readbacks[key] = (header, query_arguments)
    for key, value, header in (
        ("coupling", coupling, ":TRIGger:COUPling"),
        ("sweep", sweep, ":TRIGger:SWEep"),
        ("holdoff_s", holdoff_s, ":TRIGger:HOLDoff"),
        ("noise_reject", noise_reject, ":TRIGger:NREJect"),
    ):
        if value is not None:
            requested[key] = value
            writes.append((header, [value]))
            readbacks[key] = (header, [])
    return _configure(instrument, requested, writes, readbacks)


def configure_math(instrument, *, math_channel: int, display=None, operator=None,
                   source1=None, source2=None, scale=None, offset=None,
                   fft: dict | None = None, filter: dict | None = None) -> dict:
    values = {"display": display, "operator": operator, "source1": source1,
              "source2": source2, "scale": scale, "offset": offset}
    requested = {key: value for key, value in values.items() if value is not None}
    fft = fft or {}
    filter = filter or {}
    fft_fields = {"source": "SOURce", "window": "WINDow", "unit": "UNIT",
                  "scale": "SCALe", "offset": "OFFSet", "horizontal_scale": "HSCale",
                  "center_hz": "HCENter", "start_hz": "FREQuency:STARt", "end_hz": "FREQuency:END"}
    filter_fields = {"type": "TYPE", "cutoff1_hz": "W1", "cutoff2_hz": "W2"}
    unknown = fft.keys() - fft_fields.keys()
    if unknown:
        raise ValueError(f"Unsupported FFT settings: {', '.join(sorted(unknown))}")
    unknown = filter.keys() - filter_fields.keys()
    if unknown:
        raise ValueError(f"Unsupported filter settings: {', '.join(sorted(unknown))}")
    if not requested and not fft and not filter:
        raise ValueError("Supply at least one math setting")
    fields = {"display": "DISPlay", "operator": "OPERator", "source1": "SOURce1",
              "source2": "SOURce2", "scale": "SCALe", "offset": "OFFSet"}
    writes = [(f":MATH{math_channel}:{fields[key]}", [value]) for key, value in requested.items()]
    readbacks = {key: (f":MATH{math_channel}:{fields[key]}", []) for key in requested}
    for key, value in fft.items():
        requested[f"fft_{key}"] = value
        header = f":MATH{math_channel}:FFT:{fft_fields[key]}"
        writes.append((header, [value]))
        readbacks[f"fft_{key}"] = (header, [])
    for key, value in filter.items():
        requested[f"filter_{key}"] = value
        header = f":MATH{math_channel}:FILTer:{filter_fields[key]}"
        writes.append((header, [value]))
        readbacks[f"filter_{key}"] = (header, [])
    result = _configure(instrument, {"math_channel": math_channel, **requested}, writes, readbacks)
    warnings = []
    for key in ("cutoff1_hz", "cutoff2_hz"):
        if key not in filter:
            continue
        applied = float(result["applied"][f"filter_{key}"])
        requested_value = float(filter[key])
        if not math.isclose(applied, requested_value, rel_tol=1e-6, abs_tol=1e-12):
            warnings.append(
                f"Requested filter {key}={requested_value:g} Hz but scope applied "
                f"{applied:g} Hz; acquisition settings may constrain the cutoff."
            )
    if warnings:
        result["warnings"] = warnings
    return result


def configure_reference(instrument, *, slot: int, source=None, scale=None, offset=None,
                        color=None, label=None, action=None) -> dict:
    values = {"source": source, "scale": scale, "offset": offset, "color": color, "label": label}
    requested = {key: value for key, value in values.items() if value is not None}
    if action is not None:
        action = action.upper()
        if action not in {"CURRENT", "SAVE", "RESET"}:
            raise ValueError("action must be CURRENT, SAVE, or RESET")
        requested["action"] = action
    if not requested:
        raise ValueError("Supply at least one reference setting or action")
    commands = {
        "source": (":REFerence:SOURce", lambda value: [slot, value]),
        "scale": (":REFerence:VSCale", lambda value: [slot, value]),
        "offset": (":REFerence:VOFFset", lambda value: [slot, value]),
        "color": (":REFerence:COLor", lambda value: [slot, value]),
        "label": (":REFerence:LABel:CONTent", lambda value: [slot, value]),
    }
    writes = [(commands[key][0], commands[key][1](value)) for key, value in values.items()
              if value is not None]
    readbacks = {key: (commands[key][0], [slot]) for key, value in values.items()
                 if value is not None}
    if action is not None:
        writes.append((f":REFerence:{action}", [slot]))
    return _configure(instrument, {"slot": slot, **requested}, writes, readbacks)


def histogram_capability(instrument) -> dict:
    capabilities = get_capabilities(instrument)
    return {
        "supported": False,
        "model": capabilities["model"],
        "reason": "Histogram commands require DHO900 hardware and are excluded from the DHO814 catalog.",
    }


def configure_timing_capture(instrument, *, channels: list[dict], trigger: dict,
                             mode: str = "REPETITIVE", signal_frequency_hz=None,
                             time_scale_s_div=None, cycles_visible: float = 2,
                             memory_depth=None, acquisition_type: str = "NORMAL",
                             disable_unlisted: bool = False, purpose: str | None = None,
                             expected: str | None = None) -> dict:
    if not channels or len(channels) > 4:
        raise ValueError("channels must contain 1..4 channel configurations")
    channel_numbers = [entry.get("channel") for entry in channels]
    if any(not isinstance(number, int) or not 1 <= number <= 4 for number in channel_numbers):
        raise ValueError("channel numbers must be integers from 1 through 4")
    if len(set(channel_numbers)) != len(channel_numbers):
        raise ValueError("channel numbers must be unique")
    mode = mode.upper()
    if mode not in {"REPETITIVE", "SINGLE_SHOT"}:
        raise ValueError("mode must be REPETITIVE or SINGLE_SHOT")
    if time_scale_s_div is None:
        if signal_frequency_hz is None or not math.isfinite(float(signal_frequency_hz)) or signal_frequency_hz <= 0:
            raise ValueError("Supply a positive time_scale_s_div or signal_frequency_hz")
        if not math.isfinite(cycles_visible) or cycles_visible <= 0:
            raise ValueError("cycles_visible must be positive")
        time_scale_s_div = cycles_visible / (10 * float(signal_frequency_hz))
    if not math.isfinite(float(time_scale_s_div)) or time_scale_s_div <= 0:
        raise ValueError("time_scale_s_div must be positive")
    trigger_channel = trigger.get("channel")
    if trigger_channel not in channel_numbers:
        raise ValueError("trigger.channel must name one of the configured channels")
    trigger_edge = str(trigger.get("slope", "POS")).upper()
    if trigger_edge not in {"POS", "NEG", "RFAL"}:
        raise ValueError("trigger.slope must be POS, NEG, or RFAL")
    trigger_position = trigger.get("position_percent", 40)
    if not isinstance(trigger_position, int) or not 0 <= trigger_position <= 100:
        raise ValueError("trigger.position_percent must be an integer from 0 through 100")
    memory_depth = memory_depth or (
        "AUTO" if mode == "REPETITIVE" else {1: "25M", 2: "10M", 3: "5M", 4: "5M"}[len(channels)]
    )
    writes = []
    readbacks = {}
    configured = {entry["channel"]: entry for entry in channels}
    if disable_unlisted:
        for channel in set(range(1, 5)) - configured.keys():
            header = f":CHANnel{channel}:DISPlay"
            writes.append((header, [False]))
            readbacks[f"channel_{channel}_display"] = (header, [])
    for channel, entry in configured.items():
        domain = float(entry["voltage_domain_v"])
        if not math.isfinite(domain) or domain <= 0:
            raise ValueError("voltage_domain_v must be positive")
        scale = float(entry.get("scale_v_div", 1 if domain <= 3.6 else 2))
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("scale_v_div must be positive")
        channel_values = {
            "display": True, "coupling": entry.get("coupling", "DC"),
            "probe": entry.get("probe_ratio", 10), "scale": scale,
            "invert": entry.get("invert", False),
            "bandwidth_limit": entry.get("bandwidth_limit", "OFF"),
            "unit": "VOLT", "label": entry["label"],
            "label_visible": True,
        }
        suffixes = {
            "display": "DISPlay", "coupling": "COUPling", "probe": "PROBe", "scale": "SCALe",
            "invert": "INVert", "bandwidth_limit": "BWLimit", "unit": "UNITs",
            "label": "LABel:CONTent", "label_visible": "LABel:SHOW",
        }
        for key, value in channel_values.items():
            header = f":CHANnel{channel}:{suffixes[key]}"
            writes.append((header, [value]))
            readbacks[f"channel_{channel}_{key}"] = (header, [])
    common = [
        (":ACQuire:TYPE", [acquisition_type]), (":ACQuire:MDEPth", [memory_depth]),
        (":TIMebase:DELay:ENABle", [False]),
        (":TIMebase:MAIN:SCALe", [time_scale_s_div]), (":TIMebase:HREFerence:MODE", ["USER"]),
        (":TIMebase:MAIN:OFFSet", [0]),
        (":TIMebase:HREFerence:POSition", [trigger_position]), (":TRIGger:MODE", ["EDGE"]),
        (":TRIGger:EDGE:SOURce", [f"CHAN{trigger_channel}"]),
        (":TRIGger:EDGE:SLOPe", [trigger_edge]),
        (":TRIGger:EDGE:LEVel", [trigger.get("level_v", configured[trigger_channel]["voltage_domain_v"] / 2)]),
        (":TRIGger:COUPling", [trigger.get("coupling", "DC")]),
        (":TRIGger:SWEep", ["NORMAL" if mode == "REPETITIVE" else "SINGLE"]),
    ]
    writes.extend(common)
    initial_status = _query(instrument, ":TRIGger:STATus").upper()
    if initial_status == "STOP":
        writes.insert(0, (":RUN", []))
        writes.append((":STOP", []))
    for key, (header, _) in {
        "acquisition_type": (":ACQuire:TYPE", []), "memory_depth": (":ACQuire:MDEPth", []),
        "sample_rate_hz": (":ACQuire:SRATe", []), "time_scale_s_div": (":TIMebase:MAIN:SCALe", []),
        "delayed_timebase_enabled": (":TIMebase:DELay:ENABle", []),
        "time_offset_s": (":TIMebase:MAIN:OFFSet", []),
        "horizontal_reference": (":TIMebase:HREFerence:POSition", []),
        "trigger_source": (":TRIGger:EDGE:SOURce", []), "trigger_slope": (":TRIGger:EDGE:SLOPe", []),
        "trigger_level_v": (":TRIGger:EDGE:LEVel", []), "trigger_sweep": (":TRIGger:SWEep", []),
    }.items():
        readbacks[key] = (header, [])
    requested = {
        "channels": channels, "trigger": trigger, "mode": mode,
        "signal_frequency_hz": signal_frequency_hz, "cycles_visible": cycles_visible,
        "memory_depth": memory_depth, "time_scale_s_div": time_scale_s_div,
        "acquisition_type": acquisition_type, "disable_unlisted": disable_unlisted,
    }
    result = _configure(instrument, requested, writes, readbacks)
    expected_readbacks = {
        "acquisition_type": acquisition_type,
        "delayed_timebase_enabled": False,
        "time_scale_s_div": time_scale_s_div,
        "time_offset_s": 0,
        "horizontal_reference": trigger_position,
        "trigger_source": f"CHAN{trigger_channel}",
        "trigger_slope": trigger_edge,
        "trigger_level_v": trigger.get(
            "level_v", configured[trigger_channel]["voltage_domain_v"] / 2
        ),
        "trigger_sweep": "NORMAL" if mode == "REPETITIVE" else "SINGLE",
    }
    for channel, entry in configured.items():
        expected_readbacks.update({
            f"channel_{channel}_display": True,
            f"channel_{channel}_coupling": entry.get("coupling", "DC"),
            f"channel_{channel}_probe": entry.get("probe_ratio", 10),
            f"channel_{channel}_scale": entry.get(
                "scale_v_div", 1 if entry["voltage_domain_v"] <= 3.6 else 2
            ),
            f"channel_{channel}_invert": entry.get("invert", False),
            f"channel_{channel}_bandwidth_limit": entry.get("bandwidth_limit", "OFF"),
            f"channel_{channel}_unit": "VOLT",
            f"channel_{channel}_label": entry["label"],
            f"channel_{channel}_label_visible": True,
        })
    if disable_unlisted:
        for channel in set(range(1, 5)) - configured.keys():
            expected_readbacks[f"channel_{channel}_display"] = False
    mismatches = {
        key: {"requested": value, "applied": result["applied"].get(key)}
        for key, value in expected_readbacks.items()
        if key not in result["applied"]
        or not _readback_matches(value, result["applied"][key])
    }
    result["fully_applied"] = not mismatches
    result["mismatches"] = mismatches
    result["unverified"] = ["memory_depth"] if memory_depth == "AUTO" else []
    if mismatches:
        result["warnings"] = [
            "Some requested timing-capture settings did not match scope readback."
        ]
    result.update({
        "purpose": purpose,
        "expected": expected,
        "manual_checks": [
            "Bond scope chassis to protective earth before connecting probes.",
            "Match each physical probe switch to its configured probe ratio and compensate the probe.",
            "Use short ground springs on nearby circuit ground; never probe a floating node.",
            "Confirm 1 MOhm input and zero channel delay on the front panel.",
            "Use a logic analyzer as well when the claim depends on more simultaneous digital channels.",
        ],
    })
    return result

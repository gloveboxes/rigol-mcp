"""Rigol DS1000Z MCP server."""

import base64
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path


import asyncio
import time

from dotenv import load_dotenv

# Load configuration from a local .env file (e.g. RIGOL_IP).
# before anything reads os.environ. Existing environment variables (e.g. those passed via
# the MCP client's `env` block) take precedence and are not overridden.
load_dotenv()

import mcp_types as types
import pyvisa
from jsonschema import ValidationError, validate
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server

from rigol_mcp.waveform_analysis import analyze_waveform as _analyze_waveform
from rigol_mcp.waveform_analysis import analyze_pwm_envelopes as _analyze_pwm_envelopes
from rigol_mcp.waveform_analysis import describe_waveform as _describe_waveform
from rigol_mcp import scpi, semantic
from rigol_mcp.scope import (
    get_scope, invalidate_scope,
    screenshot_png,
    get_cursor_mode, set_cursor_mode, set_cursor_positions, get_cursor_values,
    send_raw, check_scpi_error,
    run, stop, single, autoscale,
    idn, connection_info, set_driver_from_idn,
    measure, measure_between, MEASURE_ITEMS, MEASURE_ITEMS_TWO_SOURCE,
    get_scope_state, set_channel, set_timebase, set_trigger, get_waveform,
    get_channel_state, get_timebase_state, get_trigger_state,
    get_capabilities,
    configure_cursors,
    BlockReadError,
)

async def _handle_list_tools(
    context: ServerRequestContext, params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=await list_tools())


_TEXT_BUDGET = scpi.RESPONSE_TEXT_LIMIT


def _bounded_content(content: list[types.ContentBlock]) -> list[types.ContentBlock]:
    compact = []
    for block in content:
        if (isinstance(block, types.TextContent) and len(block.text) <= _TEXT_BUDGET * 4
            and block.text.lstrip().startswith(("{", "["))):
            try:
                text = json.dumps(json.loads(block.text), separators=(",", ":"), ensure_ascii=False)
                block = types.TextContent(type="text", text=text)
            except (ValueError, RecursionError):
                pass
        compact.append(block)
    texts = [block.text for block in compact if isinstance(block, types.TextContent)]
    if sum(map(len, texts)) <= _TEXT_BUDGET:
        return compact
    full_text = "\n".join(texts)
    saved = scpi._save_data(full_text.encode("utf-8"), ".txt")
    summary = {"file_backed": True, **saved, "characters": len(full_text),
               "preview": full_text[:256], "next": "Use read_capture for a bounded excerpt; avoid reading the entire file into context."}
    return [types.TextContent(type="text", text=json.dumps(summary, separators=(",", ":")))] + [
        block for block in compact if not isinstance(block, types.TextContent)
    ]


async def _handle_call_tool(
    context: ServerRequestContext, params: types.CallToolRequestParams,
) -> types.CallToolResult:
    try:
        tool = next((tool for tool in await list_tools() if tool.name == params.name), None)
        if tool is None:
            raise ValueError(f"Unknown tool: {params.name}")
        arguments = params.arguments or {}
        validate(arguments, tool.input_schema)
        content = _bounded_content(await call_tool(params.name, arguments))
        _audit_event(params.name, arguments, "success")
        return types.CallToolResult(content=content)
    except ValidationError as exc:
        location = ".".join(map(str, exc.absolute_path))[:128] or "arguments"
        return types.CallToolResult(content=[types.TextContent(
            type="text", text=f"Invalid {location}: {exc.validator} constraint failed",
        )], is_error=True)
    except Exception as exc:
        _audit_event(params.name, params.arguments or {}, "error", str(exc)[:256])
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc)[:1024])], is_error=True,
        )


server = Server(
    "rigol-mcp",
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
    instructions=(
        "Rigol oscilloscope control over SCPI.\n"
        "- Read the scope with the data tools first: measure/measure_between for numeric "
        "readings, get_waveform for trace shape/frequency/amplitude analysis, "
        "get_scope_state for configuration. They return compact structured text that is "
        "far cheaper and easier to reason over than an image.\n"
        "- screenshot is a fallback for genuinely visual checks only (on-screen menus, "
        "cursor placement, confirming what a human sees) — do not use it as the default "
        "way to inspect signals.\n"
        "- Results are bounded: request numeric readings or waveform analysis first. "
        "Browse compact catalog pages, then fetch details for one command. Large results "
        "stay in local files; use read_capture only for specific excerpts, not to page "
        "entire captures into context. Images and small inline binary data are opt-in.\n"
        "- Container file paths are not host paths. Initial connection clears SCPI errors. "
        "Changes and error-queue reads are not replayed after failures; a failed readback "
        "can follow a successful write. Inspect state before repeating an action.\n"
        "- Call tools strictly sequentially, never concurrently: all commands share one "
        "instrument connection."
    ),
)

# Serialises all VISA operations — the underlying TCP socket is not thread/async safe.
_scope_lock = asyncio.Lock()
_last_call_time: float = 0.0
_MIN_INTERVAL = 0.1          # 100 ms minimum between SCPI operations
_POST_SCREENSHOT_DELAY = 2.0 # scope needs recovery time after large display transfer
_MAX_ATTEMPTS = 3            # initial try + 2 reconnect-and-retry attempts
_RETRY_BACKOFF = 0.2

# Communication faults that warrant a reconnect-and-retry. Other exceptions (e.g. bad
# arguments, SCPI errors) are bugs/usage errors and must propagate unretried.
_RETRYABLE = (pyvisa.errors.VisaIOError, UnicodeDecodeError, OSError)

# send_raw sends arbitrary SCPI and can put the scope in any state, so it is opt-in:
# it is only advertised and accepted when RIGOL_ENABLE_SEND_RAW is set to a truthy value.
_SEND_RAW_ENV = "RIGOL_ENABLE_SEND_RAW"
_CONFIRM_TTL_S = 60.0
_confirmations: dict[str, tuple[float, str]] = {}
_AUDIT_ENV = "RIGOL_AUDIT_LOG"

_READ_ONLY_TOOLS = {
    "read_capture", "scpi_catalog", "idn", "get_scope_state",
    "get_cursor_values", "get_meter_value", "get_decode_result", "configure_histogram",
    "get_mask_results", "get_search_results", "save_scope_setup", "get_recording_state",
}
_MEASUREMENT_TOOLS = {
    "get_capabilities", "screenshot", "measure", "measure_between", "get_waveform",
    "analyze_waveform", "analyze_pwm_envelope", "download_waveform", "capture_waveforms", "acquire_and_capture",
    "measure_statistics",
}
_DESTRUCTIVE_TOOLS = {"autoscale", "clear_measurements", "send_raw", "restore_scope_setup"}


def _send_raw_enabled() -> bool:
    return os.environ.get(_SEND_RAW_ENV, "").strip().lower() not in ("", "0", "false", "no", "off")


def _operation_class(tool: str, arguments: dict | None = None) -> str:
    if tool in _READ_ONLY_TOOLS:
        return "read"
    if tool in _MEASUREMENT_TOOLS:
        return "measurement"
    if tool in _DESTRUCTIVE_TOOLS:
        return "destructive"
    if tool == "scpi_execute":
        dangerous = arguments and _dangerous_scpi_write(arguments)
        if dangerous:
            return "destructive"
        return "configuration" if arguments and arguments.get("operation", "query") == "write" else "read"
    if tool in {"run", "stop", "single", "control_recording_replay"}:
        return "action"
    return "configuration"


def _audit_event(tool: str, arguments: dict, outcome: str, error: str | None = None) -> None:
    destination = os.environ.get(_AUDIT_ENV)
    if not destination:
        return
    safe_arguments = dict(arguments)
    if "confirm_token" in safe_arguments:
        safe_arguments["confirm_token"] = "<redacted>"
    if "data_base64" in safe_arguments:
        safe_arguments["data_base64"] = f"<{len(str(safe_arguments['data_base64']))} characters>"
    try:
        operation_class = _operation_class(tool, arguments)
    except (KeyError, TypeError, ValueError):
        operation_class = "unknown"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "operation_class": operation_class,
        "outcome": outcome,
        "arguments": safe_arguments,
    }
    if error:
        record["error"] = error
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as audit:
        audit.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")


def _confirmation_key(tool: str, arguments: dict) -> str:
    bound = {key: value for key, value in arguments.items() if key != "confirm_token"}
    return json.dumps({"tool": tool, "arguments": bound}, sort_keys=True, separators=(",", ":"))


def _require_confirmation(tool: str, arguments: dict, risk: str) -> dict | None:
    now = time.monotonic()
    key = _confirmation_key(tool, arguments)
    token = arguments.get("confirm_token")
    if token is not None:
        record = _confirmations.pop(token, None)
        if record is None or record[0] < now or record[1] != key:
            raise ValueError("Invalid, expired, mismatched, or already-used confirmation token")
        return None
    token = secrets.token_urlsafe(24)
    _confirmations[token] = (now + _CONFIRM_TTL_S, key)
    return {
        "code": "USER_CONFIRMATION_REQUIRED",
        "operation_class": "destructive",
        "confirm_token": token,
        "expires_in_s": _CONFIRM_TTL_S,
        "risk": risk,
        "instruction": "Ask the user to confirm this operation, then repeat the same call with confirm_token.",
    }


def _dangerous_scpi_write(arguments: dict) -> str | None:
    if arguments.get("operation", "query") != "write":
        return None
    entry, _ = scpi.resolve(arguments["command"])
    command = entry["command"].upper()
    if command == "*RST":
        return "Factory reset changes acquisition, channel, display, and system settings."
    if command.startswith((":SAVE", ":STORAGE", ":DISK", ":FILE")):
        return "This operation can create, overwrite, move, or delete files on the oscilloscope."
    if command.startswith(":SYSTEM") and any(
        part in command for part in ("SETUP", "LOCK", "LAN", "COMMUNICATE")
    ):
        return "This operation can replace setup, lock controls, or change instrument communications."
    if "IMPEDANCE" in command and any(str(value).strip().upper() in {"50", "FIFTY"}
                                      for value in arguments.get("arguments") or []):
        return "Selecting 50 ohm input impedance can damage the scope when excessive voltage is connected."
    return None


async def _call(fn, *args, _attempts=None, **kwargs):
    """Call fn(scope, *args, **kwargs) with the cached connection.

    Serialises concurrent calls via a lock, enforces a minimum inter-command gap, and
    recovers from communication faults by reconnecting and retrying (up to _MAX_ATTEMPTS).
    The last failure propagates to the caller.
    """
    global _last_call_time
    attempts = _MAX_ATTEMPTS if _attempts is None else _attempts
    async with _scope_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        try:
            for attempt in range(1, attempts + 1):
                try:
                    return fn(get_scope(), *args, **kwargs)
                except BlockReadError:
                    invalidate_scope()
                    raise
                except _RETRYABLE:
                    invalidate_scope()  # drop the session so the next attempt reconnects
                    if attempt == attempts:
                        raise
                    await asyncio.sleep(_RETRY_BACKOFF)
        finally:
            _last_call_time = time.monotonic()


async def _configure_and_read(setter, reader, *args, _requested=None, **kwargs):
    def operation(instrument):
        before = reader(instrument, *args)
        setter(instrument, *args, **kwargs)
        applied = reader(instrument, *args)
        requested = _requested or {key: value for key, value in kwargs.items() if value is not None}
        return {"requested": requested, "applied": applied, "changed": before != applied}

    return await _call(operation, _attempts=1)


def _capture_stopped_waveforms(instrument, channels: list[str]) -> dict:
    status = instrument.query(":TRIGger:STATus?").strip().upper()
    if status != "STOP":
        raise ValueError("Stop acquisition before capturing aligned multi-channel waveforms")
    return {channel: get_waveform(instrument, channel) for channel in channels}


def _state_mismatches(expected: dict, actual: dict) -> dict:
    mismatches = {}
    for group in ("timebase", "channels", "trigger"):
        expected_group = dict(expected.get(group, {}))
        actual_group = dict(actual.get(group, {}))
        if group == "trigger":
            expected_group.pop("status", None)
            actual_group.pop("status", None)
        if expected_group != actual_group:
            mismatches[group] = {"expected": expected_group, "actual": actual_group}
    return mismatches


def _acquire_and_capture(instrument, channels: list[str], timeout_s: float,
                         poll_interval_s: float) -> dict:
    instrument.write(":SINGle")
    if error := check_scpi_error(instrument):
        raise RuntimeError(f"SCPI error after :SINGle: {error}")
    deadline = time.monotonic() + timeout_s
    polls = 0
    while True:
        status = instrument.query(":TRIGger:STATus?").strip().upper()
        polls += 1
        if status == "STOP":
            return {"status": status, "polls": polls,
                    "waveforms": {channel: get_waveform(instrument, channel) for channel in channels}}
        if time.monotonic() >= deadline:
            instrument.write(":STOP")
            if error := check_scpi_error(instrument):
                raise RuntimeError(f"SCPI error after timeout :STOP: {error}")
            raise TimeoutError(f"Single acquisition did not complete within {timeout_s:g} seconds; acquisition stopped")
        time.sleep(poll_interval_s)


async def list_tools() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="read_capture",
            description="Read generated data files under RIGOL_DATA_DIR, not screenshots or arbitrary files. "
                        "No scope access. Byte offsets; escaping may shorten excerpts: follow next_offset. "
                        "UTF-8 boundaries may replace characters; base64 is lossless. Read only needed excerpts.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 2048, "default": 1024},
                "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
            }, "required": ["path"], "additionalProperties": False},
        ),
        types.Tool(
            name="download_waveform",
            description="Save a stopped DHO814 acquisition as time/value CSV; return path and timing. "
                        "RAW/MAX read memory; NORM reads screen (required for math). Analog source "
                        "must be enabled with data. Transfer settings are not restored; acquisition is unchanged.",
            inputSchema={"type": "object", "properties": {
                "source": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"]},
                "mode": {"type": "string", "enum": ["NORM", "RAW", "MAX"], "default": "RAW"},
                "start": {"type": "integer", "minimum": 1, "default": 1, "description": "First point (1-based)"},
                "points": {"type": "integer", "minimum": 1, "description": "Count; omitted means all remaining points"},
                "chunk_points": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10000},
            }, "required": ["source"], "additionalProperties": False},
        ),
        types.Tool(
            name="get_capabilities",
            description="Return model capabilities with per-field evidence: hardware-verified, "
                        "documented or unverified. By default probe DHO800/900 channel/grid counts "
                        "and report model mismatches; measurement lists are not accuracy validation. "
                        "Probes read and clear SCPI errors. Set verify_hardware=false for identity/model data only.",
            inputSchema={"type": "object", "properties": {
                "verify_hardware": {"type": "boolean", "default": True},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="scpi_catalog",
            description="Browse DHO814 command names (10 per page), or pass command for one "
                        "signature, parameter types/enums and manual section. Filter by subsystem "
                        "or search; command ignores other filters. No scope access. Fetch details before scpi_execute.",
            inputSchema={"type": "object", "properties": {
                "subsystem": {"type": "string", "description": "Subsystem prefix, e.g. trigger or acquire"},
                "search": {"type": "string", "description": "Command or parameter substring"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
                "command": {"type": "string", "description": "Exact header for detailed parameters"},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="scpi_execute",
            description="Execute one DHO814 catalog command; arguments are positional. Text over 2048 "
                        "characters and binary are file-backed; inline_binary permits up to 1024 bytes. "
                        "Scope enforces dynamic limits. Non-reset writes drain errors; queries may consume "
                        "status/errors. No retries or completion guarantee. Can overwrite files, reset, "
                        "change LAN or lock controls. Does not require send_raw enablement.",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "description": "Header only; replace <n> with an index, no embedded arguments or command chains"},
                "operation": {"type": "string", "enum": ["query", "write"], "default": "query"},
                "arguments": {"type": "array", "items": {"type": ["string", "number", "boolean"]}},
                "data_base64": {"type": "string", "description": "Setup payload without TMC header; :SYSTem:SETup write only, no arguments"},
                "data_path": {"type": "string", "description": "Generated setup file under RIGOL_DATA_DIR; :SYSTem:SETup write only, no arguments or data_base64"},
                "inline_binary": {"type": "boolean", "default": False},
                "confirm_token": {"type": "string", "description": "Single-use token returned for a restricted write"},
            }, "required": ["command"], "additionalProperties": False},
        ),
        types.Tool(
            name="screenshot",
            description=(
                "Save a display PNG and return its path. Set include_image=true only when "
                "visual inspection is necessary; otherwise prefer measure/get_waveform."
            ),
            inputSchema={"type": "object", "properties": {
                "include_image": {"type": "boolean", "default": False},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="idn",
            description=(
                "Read identity (model, serial, firmware) and LAN/session/driver diagnostics. "
                "Failure is reported in diagnostic text; inspect it, not only the MCP success flag."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_scope_state",
            description=(
                "Read channel settings, timebase and trigger mode/status; source/slope/level only for EDGE. "
                "Includes model capabilities without live channel/grid verification."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="set_channel",
            description=(
                "Change specified channel settings; return channel readback. Probe ratio is applied "
                "before scale/offset; changing it may rescale existing values. Use an installed channel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel":    {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "display":    {"type": "boolean", "description": "Turn channel on/off"},
                    "scale_v_div": {"type": ["number", "string"], "description": "Vertical scale in V/div"},
                    "offset_v":   {"type": ["number", "string"], "description": "Vertical offset in volts"},
                    "coupling":   {"type": "string", "enum": ["AC", "DC", "GND"]},
                    "probe":      {"type": ["number", "string"], "description": "Probe attenuation ratio (e.g. 1, 10, 100)"},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="set_timebase",
            description=(
                "Set horizontal scale/offset; return configuration. Centered window edges are "
                "offset +/- 5*scale on DHO800, +/- 6*scale on DS1000Z. Use measured waveform "
                "bounds for zoom/noncentral references. Trigger level need not be zero volts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scale_s_div": {"type": ["number", "string"], "description": "Time per division in seconds"},
                    "offset_s":    {"type": ["number", "string"], "description": "Trigger offset in seconds"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="set_trigger",
            description=(
                "Configure EDGE using top-level source/slope/level on all supported families, or select a "
                "DHO814 advanced trigger_type with validated type-specific settings. Returns applied readback."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trigger_type": {"type": "string", "enum": [
                        "EDGE", "PULSE", "SLOPE", "VIDEO", "PATTERN", "DURATION",
                        "TIMEOUT", "RUNT", "WINDOW", "DELAY", "SETUP", "NEDGE",
                        "RS232", "I2C", "SPI", "CAN",
                    ]},
                    "source": {"type": "string", "description": "Trigger source, e.g. CHAN1"},
                    "slope":  {"type": "string", "enum": ["POS", "NEG", "RFAL"]},
                    "level":  {"type": ["number", "string"], "description": "Trigger level in volts"},
                    "coupling": {"type": "string", "enum": ["AC", "DC", "LFREJECT", "HFREJECT"]},
                    "sweep": {"type": "string", "enum": ["AUTO", "NORMAL", "SINGLE"]},
                    "holdoff_s": {"type": "number", "minimum": 0},
                    "noise_reject": {"type": "boolean"},
                    "settings": {"type": "object", "properties": {
                        "source": {"type": "string"}, "slope": {"type": "string"},
                        "level": {"type": "number"}, "polarity": {"type": "string"},
                        "condition": {"type": "string"}, "upper_s": {"type": "number"},
                        "lower_s": {"type": "number"}, "window": {"type": "string"},
                        "level_a": {"type": "number"}, "level_b": {"type": "number"},
                        "video_mode": {"type": "string"}, "line": {"type": "integer"},
                        "standard": {"type": "string"}, "position": {"type": "string"},
                        "timeout_s": {"type": "number"}, "idle_s": {"type": "number"},
                        "count": {"type": "integer"}, "source_a": {"type": "string"},
                        "slope_a": {"type": "string"}, "source_b": {"type": "string"},
                        "slope_b": {"type": "string"}, "data_source": {"type": "string"},
                        "clock_source": {"type": "string"}, "setup_s": {"type": "number"},
                        "hold_s": {"type": "number"}, "data_level": {"type": "number"},
                        "clock_level": {"type": "number"},
                        "baud": {"type": "integer"}, "data": {"type": "integer"},
                        "data_bits": {"type": "integer"}, "stop_bits": {"type": "number"},
                        "parity": {"type": "string"}, "scl": {"type": "string"},
                        "sda": {"type": "string"}, "address_width": {"type": "integer"},
                        "address": {"type": "integer"}, "direction": {"type": "string"},
                        "data_bytes": {"type": "number"}, "current_bit": {"type": "integer"},
                        "code": {"type": "integer"}, "clock": {"type": "string"},
                        "miso": {"type": "string"}, "chip_select": {"type": "string"},
                        "chip_select_level": {"type": "number"},
                        "chip_select_polarity": {"type": "string"},
                        "signal_type": {"type": "string"}, "sample_point": {"type": "integer"},
                        "extended": {"type": "boolean"}, "define": {"type": "string"},
                        "data_width": {"type": "integer"},
                        "pattern": {"oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4}
                        ]}
                    }, "additionalProperties": False},
                },
                "required": [],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="measure",
            description=(
                "Read a built-in measurement as text; get_capabilities lists items. Registers the item "
                "on the scope (may populate its results panel) and enables disabled channels. DHO may "
                "need live acquisition. 9.9E37 is invalid. VAMP uses pulse levels, VPP extrema; VRMS "
                "covers the window, PVRMS one period, ACRMS the AC component (DHO)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "item":    {"type": "string", "description": "Measurement item (e.g. FREQUENCY, VPP, VRMS)"},
                },
                "required": ["channel", "item"],
            },
        ),
        types.Tool(
            name="measure_between",
            description=(
                "Read delay (seconds) or phase (degrees). DHO edge letters refer to source1 then "
                "source2; DS1000Z R/F names map to DHO RR/FF. Registers a scope measurement "
                "(may populate results panel) and enables disabled sources. DHO may need live "
                "acquisition; 9.9E37 is invalid."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source1": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Reference channel"},
                    "source2": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Measured channel"},
                    "item":    {"type": "string", "enum": [
                        "RDELAY", "FDELAY", "RPHASE", "FPHASE",
                        "RRDELAY", "RFDELAY", "FRDELAY", "FFDELAY",
                        "RRPHASE", "RFPHASE", "FRPHASE", "FFPHASE",
                    ]},
                },
                "required": ["source1", "source2", "item"],
            },
        ),
        types.Tool(
            name="get_waveform",
            description=(
                "Analyse NORM screen data from analog channels or displayed DHO math traces, suppressing unreliable "
                "interpretation. Does not stop acquisition; stop first for consistency. Enables disabled "
                "channels; acquisition may be needed. Leaves transfer settings changed. raw_data=true "
                "saves JSON and returns metadata/path, never samples. Use download_waveform for memory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel":  {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"]},
                    "raw_data": {"type": "boolean", "description": "Save raw JSON and return metadata/path instead of analysis", "default": False},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="analyze_waveform",
            description="Analyze an analog or DHO math screen trace: statistics, timing rates, FFT peaks and warnings.",
            inputSchema={"type": "object", "properties": {
                "channel": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"]},
                "peak_count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            }, "required": ["channel"], "additionalProperties": False},
        ),
        types.Tool(
            name="analyze_pwm_envelope",
            description="Analyze 1-2 stopped PWM traces: carrier, duty envelope, modulation and phase.",
            inputSchema={"type": "object", "properties": {
                "channels": {"type": "array", "items": {
                    "type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"],
                }, "minItems": 1, "maxItems": 2, "uniqueItems": True},
            }, "required": ["channels"], "additionalProperties": False},
        ),
        types.Tool(
            name="capture_waveforms",
            description="Capture 1-4 analog or DHO math screen traces from one stopped acquisition, save raw JSON, and return compact analysis for each source.",
            inputSchema={"type": "object", "properties": {
                "channels": {"type": "array", "items": {
                    "type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"],
                }, "minItems": 1, "maxItems": 4, "uniqueItems": True},
                "label": {"type": "string", "maxLength": 120},
                "peak_count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            }, "required": ["channels"], "additionalProperties": False},
        ),
        types.Tool(
            name="acquire_and_capture",
            description="Arm one acquisition, wait for STOP with a bounded timeout, then save and analyze aligned screen traces. Stops acquisition on timeout.",
            inputSchema={"type": "object", "properties": {
                "channels": {"type": "array", "items": {
                    "type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"],
                }, "minItems": 1, "maxItems": 4, "uniqueItems": True},
                "timeout_s": {"type": "number", "minimum": 0.1, "maximum": 300, "default": 10},
                "poll_interval_s": {"type": "number", "minimum": 0.01, "maximum": 1, "default": 0.1},
                "label": {"type": "string", "maxLength": 120},
                "peak_count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            }, "required": ["channels"], "additionalProperties": False},
        ),
        types.Tool(
            name="clear_measurements",
            description="Remove all measurement entries from the DHO814 result panel. This is destructive UI cleanup.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="configure_acquisition",
            description="Configure DHO814 acquisition type, memory depth, averaging, and UltraAcquire in one validated operation.",
            inputSchema={"type": "object", "properties": {
                "acquisition_type": {"type": "string", "enum": ["NORMAL", "PEAK", "AVERAGES", "ULTRA"]},
                "memory_depth": {"type": ["string", "integer"]},
                "averages": {"type": "integer", "minimum": 1},
                "ultra_mode": {"type": "string", "enum": ["ADJACENT", "OVERLAY", "WATERFALL", "PERSPECTIVE", "MOSAIC"]},
                "ultra_timeout_s": {"type": "number", "exclusiveMinimum": 0},
                "ultra_max_frames": {"type": "integer", "minimum": 1},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="measure_statistics",
            description="Register one DHO814 measurement and return structured current/average/min/max/deviation/count values with sentinel validation.",
            inputSchema={"type": "object", "properties": {
                "item": {"type": "string"},
                "source1": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"]},
                "source2": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4", "MATH1", "MATH2", "MATH3", "MATH4"]},
                "statistics": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {
                    "type": "string", "enum": ["CURRENT", "AVERAGE", "MINIMUM", "MAXIMUM", "DEVIATION", "COUNT"]}},
            }, "required": ["item", "source1"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_mask_test",
            description="Configure DHO814 mask pass/fail testing, optionally create a mask from the current waveform, and return applied settings.",
            inputSchema={"type": "object", "properties": {
                "enabled": {"type": "boolean"}, "source": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                "horizontal_tolerance": {"type": "number", "minimum": 0}, "vertical_tolerance": {"type": "number", "minimum": 0},
                "create": {"type": "boolean", "default": False}, "running": {"type": "boolean"},
                "output_enabled": {"type": "boolean"}, "output_event": {"type": "string", "enum": ["FAIL", "PASS"]},
                "output_time_s": {"type": "number", "minimum": 0},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="get_mask_results",
            description="Return DHO814 mask pass/fail/total counters and failure ratio without resetting them.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="configure_search",
            description="Configure DHO814 waveform search for edge or pulse events with validated mode-specific settings.",
            inputSchema={"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["EDGE", "PULSE"]}, "enabled": {"type": "boolean"},
                "source": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                "slope": {"type": "string", "enum": ["POSITIVE", "NEGATIVE", "EITHER"]},
                "threshold_v": {"type": "number"}, "polarity": {"type": "string", "enum": ["POSITIVE", "NEGATIVE"]},
                "qualifier": {"type": "string", "enum": ["GREATER", "LESS", "GLESS"]},
                "upper_width_s": {"type": "number", "minimum": 0}, "lower_width_s": {"type": "number", "minimum": 0},
            }, "required": ["mode"], "additionalProperties": False},
        ),
        types.Tool(
            name="get_search_results",
            description="Return a bounded page of waveform-search event times and the total event count.",
            inputSchema={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 250, "default": 100},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="save_scope_setup",
            description="Export the complete DHO814 setup as a generated binary file under RIGOL_DATA_DIR for later restoration.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="restore_scope_setup",
            description="Restore a generated setup snapshot. Replaces broad scope state, requires confirmation, and is never retried.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"},
                "confirm_token": {"type": "string", "description": "Single-use token returned by the first call"},
            }, "required": ["path"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_recording",
            description="Configure DHO814 waveform frame recording, including frame count, interval, prompting, and run state.",
            inputSchema={"type": "object", "properties": {
                "enabled": {"type": "boolean"}, "frames": {"type": "integer", "minimum": 1},
                "interval_s": {"type": "number", "minimum": 0}, "prompt": {"type": "boolean"},
                "running": {"type": "boolean"}, "use_max_frames": {"type": "boolean", "default": False},
            }, "additionalProperties": False},
        ),
        types.Tool(
            name="get_recording_state",
            description="Read DHO814 frame-recording progress and replay range, position, timing, mode, direction, and operation state.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="control_recording_replay",
            description="Select or navigate recorded frames and start/stop replay. Navigation actions are not retried.",
            inputSchema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["SELECT", "PLAY", "STOP", "PREVIOUS", "NEXT", "FIRST", "LAST"]},
                "frame": {"type": "integer", "minimum": 1}, "start_frame": {"type": "integer", "minimum": 1},
                "end_frame": {"type": "integer", "minimum": 1}, "interval_s": {"type": "number", "minimum": 0},
                "mode": {"type": "string", "enum": ["REPEAT", "SINGLE"]},
                "direction": {"type": "string", "enum": ["FORWARD", "BACKWARD"]},
            }, "required": ["action"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_meter",
            description="Configure the DHO814 DVM or hardware counter and return applied settings.",
            inputSchema={"type": "object", "properties": {
                "meter": {"type": "string", "enum": ["DVM", "COUNTER"]},
                "enabled": {"type": "boolean"},
                "source": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                "mode": {"type": "string", "enum": ["ACRMS", "DC", "DCRMS"]},
            }, "required": ["meter"], "additionalProperties": False},
        ),
        types.Tool(
            name="get_meter_value",
            description="Read the current DHO814 DVM or hardware-counter value.",
            inputSchema={"type": "object", "properties": {
                "meter": {"type": "string", "enum": ["DVM", "COUNTER"]},
            }, "required": ["meter"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_decode",
            description="Configure a DHO814 protocol decoder.",
            inputSchema={"type": "object", "properties": {
                "bus": {"type": "integer", "minimum": 1, "maximum": 4},
                "protocol": {"type": "string", "enum": ["PARALLEL", "RS232", "I2C", "SPI", "CAN"]},
                "display": {"type": "boolean"},
                "format": {"type": "string", "enum": ["HEX", "ASCII", "DEC", "BIN"]},
                "settings": {"type": "object", "properties": {
                    "source": {"type": "string"}, "clock": {"type": "string"},
                    "slope": {"type": "string"}, "width": {"type": "integer"},
                    "bit": {"type": "integer"}, "source_channel": {"type": "string"},
                    "endian": {"type": "string"}, "polarity": {"type": "string"},
                    "tx": {"type": "string"}, "rx": {"type": "string"},
                    "parity": {"type": "string"}, "baud": {"type": "number"},
                    "data_bits": {"type": "integer"}, "stop_bits": {"type": "number"},
                    "scl": {"type": "string"}, "sda": {"type": "string"},
                    "exchange": {"type": "boolean"}, "address_mode": {"type": "string"},
                    "clock_slope": {"type": "string"}, "miso": {"type": "string"},
                    "mosi": {"type": "string"}, "miso_polarity": {"type": "string"},
                    "mosi_polarity": {"type": "string"}, "cs_mode": {"type": "string"},
                    "timeout_s": {"type": "number"}, "chip_select": {"type": "string"},
                    "chip_select_polarity": {"type": "string"},
                    "signal_type": {"type": "string"}, "sample_point": {"type": "integer"},
                    "thresholds_v": {"type": "object", "additionalProperties": {"type": "number"}}
                }, "additionalProperties": False},
            }, "required": ["bus", "protocol"], "additionalProperties": False},
        ),
        types.Tool(
            name="get_decode_result",
            description="Read DHO814 decoder settings and data.",
            inputSchema={"type": "object", "properties": {
                "bus": {"type": "integer", "minimum": 1, "maximum": 4},
            }, "required": ["bus"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_math",
            description="Configure DHO814 math, FFT or filters; warn on clamped cutoffs.",
            inputSchema={"type": "object", "properties": {
                "math_channel": {"type": "integer", "minimum": 1, "maximum": 4},
                "display": {"type": "boolean"}, "operator": {"type": "string"},
                "source1": {"type": "string"}, "source2": {"type": "string"},
                "scale": {"type": "number"}, "offset": {"type": "number"},
                "fft": {"type": "object", "properties": {
                    "source": {"type": "string"}, "window": {"type": "string"},
                    "unit": {"type": "string"}, "scale": {"type": "number"},
                    "offset": {"type": "number"}, "horizontal_scale": {"type": "number"},
                    "center_hz": {"type": "number"}, "start_hz": {"type": "number"},
                    "end_hz": {"type": "number"}
                }, "additionalProperties": False},
                "filter": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["LPASs", "HPASs", "BPASs", "BSTop"]},
                    "cutoff1_hz": {"type": "number", "exclusiveMinimum": 0},
                    "cutoff2_hz": {"type": "number", "exclusiveMinimum": 0}
                }, "additionalProperties": False},
            }, "required": ["math_channel"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_reference",
            description="Configure or capture a DHO814 reference trace; SAVE/CURRENT/RESET actions are not retried.",
            inputSchema={"type": "object", "properties": {
                "slot": {"type": "integer", "minimum": 1, "maximum": 10},
                "source": {"type": "string"}, "scale": {"type": "number"},
                "offset": {"type": "number"}, "color": {"type": "string"},
                "label": {"type": "string"},
                "action": {"type": "string", "enum": ["CURRENT", "SAVE", "RESET"]},
            }, "required": ["slot"], "additionalProperties": False},
        ),
        types.Tool(
            name="configure_histogram",
            description="Report histogram availability. DHO814 does not implement the DHO900 histogram subsystem.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        types.Tool(
            name="configure_timing_capture",
            description="Configure a generic 1-4 channel digital timing capture from signal labels, voltage domains, trigger intent, and timebase goals.",
            inputSchema={"type": "object", "properties": {
                "channels": {"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                    "items": {"type": "object", "properties": {
                        "channel": {"type": "integer", "minimum": 1, "maximum": 4},
                        "label": {"type": "string", "minLength": 1, "maxLength": 32},
                        "voltage_domain_v": {"type": "number", "exclusiveMinimum": 0},
                        "scale_v_div": {"type": "number", "exclusiveMinimum": 0},
                        "probe_ratio": {"type": "number", "exclusiveMinimum": 0, "default": 10},
                        "coupling": {"type": "string", "enum": ["AC", "DC", "GND"], "default": "DC"},
                        "invert": {"type": "boolean", "default": False},
                        "bandwidth_limit": {"type": ["string", "number"], "default": "OFF"}
                    }, "required": ["channel", "label", "voltage_domain_v"], "additionalProperties": False}},
                "trigger": {"type": "object", "properties": {
                    "channel": {"type": "integer", "minimum": 1, "maximum": 4},
                    "slope": {"type": "string", "enum": ["POS", "NEG", "RFAL"], "default": "POS"},
                    "level_v": {"type": "number"},
                    "coupling": {"type": "string", "enum": ["AC", "DC", "LFREJECT", "HFREJECT"], "default": "DC"},
                    "position_percent": {"type": "integer", "minimum": 0, "maximum": 100, "default": 40}
                }, "required": ["channel"], "additionalProperties": False},
                "mode": {"type": "string", "enum": ["REPETITIVE", "SINGLE_SHOT"], "default": "REPETITIVE"},
                "signal_frequency_hz": {"type": "number", "exclusiveMinimum": 0},
                "time_scale_s_div": {"type": "number", "exclusiveMinimum": 0},
                "cycles_visible": {"type": "number", "exclusiveMinimum": 0, "default": 2},
                "memory_depth": {"type": ["string", "integer"]},
                "acquisition_type": {"type": "string", "enum": ["NORMAL", "PEAK", "AVERAGES", "ULTRA"], "default": "NORMAL"},
                "disable_unlisted": {"type": "boolean", "default": False},
                "purpose": {"type": "string", "maxLength": 200},
                "expected": {"type": "string", "maxLength": 500}
            }, "required": ["channels", "trigger"], "additionalProperties": False},
        ),
        types.Tool(
            name="set_cursors",
            description=(
                "Set cursors and return readouts; omitted mode is retained. DHO-only: source/type for "
                "MANUAL; source_a/source_b/track_axis for TRACK; Y positions. OFF accepts no positions "
                "or sources. XY is DHO-only, requires XY timebase, and rejects positions here; "
                "scpi_execute supports XY positioning on DHO814."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["OFF", "MANUAL", "TRACK", "XY"]},
                    "ax":   {"type": ["number", "string"], "description": "Cursor A X position in seconds"},
                    "bx":   {"type": ["number", "string"], "description": "Cursor B X position in seconds"},
                    "ay": {"type": "number", "description": "DHO MANUAL/TRACK cursor A Y position in volts"},
                    "by": {"type": "number", "description": "DHO MANUAL/TRACK cursor B Y position in volts"},
                    "source": {"type": "string", "description": "DHO manual source: CHAN1-4, MATH1-4, NONE"},
                    "source_a": {"type": "string", "description": "DHO track cursor A source"},
                    "source_b": {"type": "string", "description": "DHO track cursor B source"},
                    "cursor_type": {"type": "string", "enum": ["TIME", "AMPLITUDE"]},
                    "track_axis": {"type": "string", "enum": ["X", "Y"]},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_cursor_values",
            description=(
                "Read cursor values and DHO source/type. AX_s/BX_s are seconds; XY axes are "
                "amplitudes. inv_delta_x is reciprocal separation, not necessarily signal frequency."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="send_raw",
            description=(
                "Unrestricted SCPI escape hatch, enabled only by RIGOL_ENABLE_SEND_RAW. Text queries "
                "only; use scpi_execute for binary transfers. Writes drain errors and return '(no response)'. "
                "Large text is file-backed; no retries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "SCPI command, e.g. ':CHAN1:SCAL?' or ':CHAN1:DISP ON'"},
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="check_error",
            description="Read/clear up to 16 SCPI error entries; return only the first, or 'No error'. No retries.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run",
            description="Start continuous acquisition; immediate status may lag. Query get_scope_state to confirm.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="stop",
            description=(
                "Stop acquisition, retaining the displayed trace and settings. Immediate status may lag; "
                "confirm STOP via get_scope_state. Not a clear or factory reset."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="single",
            description=(
                "Arm one acquisition; return status without waiting for its trigger. "
                "Check get_scope_state for STOP before downloading."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="autoscale",
            description=(
                "Run auto-setup, changing channels, timebase and trigger; wait for completion and return state. "
                "Not capability discovery or factory reset; no automatic retry."
            ),
            inputSchema={"type": "object", "properties": {
                "confirm_token": {"type": "string", "description": "Single-use token returned by the first call"},
            }, "required": [], "additionalProperties": False},
        ),
    ]
    # send_raw is an arbitrary-SCPI escape hatch — only expose it when explicitly enabled.
    if not _send_raw_enabled():
        tools = [t for t in tools if t.name != "send_raw"]
    annotated = []
    for tool in tools:
        operation_class = _operation_class(tool.name)
        annotated.append(tool.model_copy(update={
            "annotations": types.ToolAnnotations(
                readOnlyHint=operation_class == "read",
                destructiveHint=operation_class == "destructive",
                idempotentHint=operation_class in {"read", "configuration"},
                openWorldHint=tool.name == "send_raw",
            ),
            "meta": {"rigol/operationClass": operation_class},
        }))
    return annotated


async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    global _last_call_time
    if name == "read_capture":
        result = scpi.read_capture(**arguments)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]
    if name == "download_waveform":
        result = await _call(scpi.download_waveform, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    if name == "get_capabilities":
        result = await _call(get_capabilities, verify_hardware=arguments.get("verify_hardware", True))
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    if name == "scpi_catalog":
        result = scpi.discover(**arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    if name == "scpi_execute":
        if risk := _dangerous_scpi_write(arguments):
            if confirmation := _require_confirmation(name, arguments, risk):
                return [types.TextContent(type="text", text=json.dumps(confirmation, separators=(",", ":")))]
        result = await _call(
            scpi.execute, arguments["command"], arguments.get("operation", "query"),
            arguments.get("arguments"), arguments.get("data_base64"),
            inline_binary=arguments.get("inline_binary", False), data_path=arguments.get("data_path"), _attempts=1,
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    if name == "screenshot":
        png_bytes = await _call(screenshot_png)
        # Advance the cooldown timestamp so the next _call waits for the scope to recover
        # after the large display transfer before sending further SCPI commands.
        _last_call_time = time.monotonic() + _POST_SCREENSHOT_DELAY

        save_dir = Path(os.environ.get("RIGOL_SCREENSHOT_DIR", "screenshots")).resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = save_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        filename.write_bytes(png_bytes)

        if not arguments.get("include_image", False):
            return [types.TextContent(type="text", text=f"Saved: {filename}")]
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        return [
            types.TextContent(type="text", text=f"Saved: {filename}"),
            types.ImageContent(type="image", data=b64, mimeType="image/png"),
        ]

    if name == "idn":
        # connection_info reads env vars + module-level state with no device I/O. It's
        # safe to call after any outcome: on success we show what got selected (driver,
        # resource, backend), on failure we show what was attempted (transport, env vars).
        # That way the diagnostic surfaces config issues (wrong IP) that
        # would otherwise be hidden behind an opaque VI_ERROR_TMO timeout.
        try:
            idn_str = await _call(idn)
            # Populate the driver cache from the IDN we already have, so the diagnostic
            # below shows which dialect was selected. Avoids a second *IDN? round-trip
            # that get_driver(scope) would otherwise do on first dialect use.
            set_driver_from_idn(idn_str)
            info = connection_info()
            diag = "\n".join(f"  {k:18s}: {v}" for k, v in info.items())
            return [types.TextContent(type="text",
                text=f"Connection:\n{diag}\n\nIDN: {idn_str}")]
        except Exception as exc:
            info = connection_info()
            diag = "\n".join(f"  {k:18s}: {v}" for k, v in info.items())
            return [types.TextContent(type="text",
                text=f"Connection:\n{diag}\n\n"
                     f"*IDN? query FAILED: {type(exc).__name__}: {exc}\n\n"
                     "The connection details above show what the server was attempting "
                     "when the query failed — check RIGOL_IP and TCP port 5555 connectivity.")]

    if name == "get_scope_state":
        state = await _call(get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "set_channel":
        def _f(key):
            v = arguments.get(key)
            return float(v) if v is not None else None

        state = await _configure_and_read(
            set_channel, get_channel_state,
            arguments["channel"],
            _requested={key: value for key, value in arguments.items() if key != "channel"},
            display=arguments.get("display"),
            scale=_f("scale_v_div"),
            offset=_f("offset_v"),
            coupling=arguments.get("coupling"),
            probe=_f("probe"),
        )
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "set_timebase":
        def _f(key):
            v = arguments.get(key)
            return float(v) if v is not None else None

        state = await _configure_and_read(
            set_timebase, get_timebase_state,
            _requested=arguments, scale=_f("scale_s_div"), offset=_f("offset_s"),
        )
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "set_trigger":
        if "trigger_type" in arguments:
            settings = dict(arguments.get("settings") or {})
            for key in ("source", "slope", "level"):
                if key in arguments:
                    settings.setdefault(key, arguments[key])
            result = await _call(
                semantic.configure_trigger,
                trigger_type=arguments["trigger_type"], settings=settings,
                coupling=arguments.get("coupling"), sweep=arguments.get("sweep"),
                holdoff_s=arguments.get("holdoff_s"), noise_reject=arguments.get("noise_reject"),
                _attempts=1,
            )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        level = arguments.get("level")
        state = await _configure_and_read(
            set_trigger, get_trigger_state,
            _requested=arguments,
            source=arguments.get("source"),
            slope=arguments.get("slope"),
            level=float(level) if level is not None else None,
        )
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "measure":
        value = await _call(measure, arguments["channel"], arguments["item"])
        return [types.TextContent(
            type="text",
            text=f"{arguments['item']} on {arguments['channel']}: {value}",
        )]

    if name == "measure_between":
        value = await _call(measure_between, arguments["source1"], arguments["source2"], arguments["item"])
        return [types.TextContent(
            type="text",
            text=f"{arguments['item']} from {arguments['source1']} to {arguments['source2']}: {value}",
        )]

    if name == "get_waveform":
        data = await _call(get_waveform, arguments["channel"])
        if arguments.get("raw_data"):
            saved = scpi._save_data(json.dumps(data, separators=(",", ":")).encode(), ".json")
            summary = {key: value for key, value in data.items() if key not in {"times_s", "voltages_v"}}
            return [types.TextContent(type="text", text=json.dumps({**summary, **saved}, separators=(",", ":")))]
        return [types.TextContent(type="text", text=_describe_waveform(data))]

    if name == "analyze_waveform":
        data = await _call(get_waveform, arguments["channel"])
        result = _analyze_waveform(data, arguments.get("peak_count", 5))
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "analyze_pwm_envelope":
        waveforms = await _call(_capture_stopped_waveforms, arguments["channels"], _attempts=1)
        result = _analyze_pwm_envelopes(waveforms)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "capture_waveforms":
        channels = arguments["channels"]
        waveforms = await _call(_capture_stopped_waveforms, channels, _attempts=1)
        payload = {
            "label": arguments.get("label"),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "waveforms": waveforms,
        }
        saved = scpi._save_data(json.dumps(payload, separators=(",", ":")).encode(), ".json")
        result = {
            "label": arguments.get("label"), **saved,
            "channels": {channel: _analyze_waveform(data, arguments.get("peak_count", 5))
                         for channel, data in waveforms.items()},
        }
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "acquire_and_capture":
        acquired = await _call(
            _acquire_and_capture, arguments["channels"], arguments.get("timeout_s", 10),
            arguments.get("poll_interval_s", 0.1), _attempts=1,
        )
        payload = {"label": arguments.get("label"), "captured_at": datetime.now(timezone.utc).isoformat(),
                   "waveforms": acquired["waveforms"]}
        saved = scpi._save_data(json.dumps(payload, separators=(",", ":")).encode(), ".json")
        result = {"label": arguments.get("label"), "status": acquired["status"],
                  "polls": acquired["polls"], **saved,
                  "channels": {channel: _analyze_waveform(data, arguments.get("peak_count", 5))
                               for channel, data in acquired["waveforms"].items()}}
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "clear_measurements":
        result = await _call(semantic.clear_measurements, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_acquisition":
        result = await _call(semantic.configure_acquisition, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "measure_statistics":
        result = await _call(semantic.measure_statistics, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name in {"configure_mask_test", "configure_search"}:
        fn = {"configure_mask_test": semantic.configure_mask_test,
              "configure_search": semantic.configure_search}[name]
        result = await _call(fn, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name in {"get_mask_results", "get_search_results"}:
        fn = {"get_mask_results": semantic.get_mask_results,
              "get_search_results": semantic.get_search_results}[name]
        result = await _call(fn, **arguments)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "save_scope_setup":
        result = await _call(scpi.execute, ":SYSTem:SETup", "query", _attempts=1)
        state = await _call(get_scope_state)
        state_path = Path(result["path"]).with_suffix(".json")
        state_path.write_text(json.dumps(state, separators=(",", ":")) + "\n")
        result.update({"state_path": str(state_path), "state_captured": True})
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "restore_scope_setup":
        if confirmation := _require_confirmation(
            name, arguments, "Restoring a setup replaces channel, acquisition, timebase, trigger, display, and analysis settings."
        ):
            return [types.TextContent(type="text", text=json.dumps(confirmation, separators=(",", ":")))]
        result = await _call(
            scpi.execute, ":SYSTem:SETup", "write", data_path=arguments["path"], _attempts=1,
        )
        state_path = Path(arguments["path"]).with_suffix(".json")
        try:
            actual = await _call(get_scope_state)
        except Exception as error:
            result["verification"] = {
                "verified": False,
                "warning": (
                    "Setup write succeeded, but state readback failed; inspect current state "
                    f"before retrying: {type(error).__name__}: {error}"
                ),
            }
            return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]
        if state_path.is_file():
            expected = json.loads(state_path.read_text())
            mismatches = _state_mismatches(expected, actual)
            result["verification"] = {
                "verified": not mismatches,
                "mismatches": mismatches,
                "state_path": str(state_path),
            }
        else:
            result["verification"] = {
                "verified": False,
                "warning": "No saved state sidecar was found; setup write succeeded but equality could not be verified.",
                "actual": actual,
            }
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_recording":
        result = await _call(semantic.configure_recording, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "get_recording_state":
        result = await _call(semantic.get_recording_state)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "control_recording_replay":
        result = await _call(semantic.control_recording_replay, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_meter":
        result = await _call(semantic.configure_meter, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "get_meter_value":
        result = await _call(semantic.get_meter_value, **arguments)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_decode":
        result = await _call(semantic.configure_decode, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "get_decode_result":
        result = await _call(semantic.get_decode_result, **arguments)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_math":
        result = await _call(semantic.configure_math, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_reference":
        result = await _call(semantic.configure_reference, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_histogram":
        result = await _call(semantic.histogram_capability)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "configure_timing_capture":
        result = await _call(semantic.configure_timing_capture, **arguments, _attempts=1)
        return [types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))]

    if name == "set_cursors":
        settings = dict(arguments)
        for key in ("ax", "bx", "ay", "by"):
            if key in settings:
                settings[key] = float(settings[key])
        values = await _call(configure_cursors, **settings, _attempts=1)
        lines = "\n".join(f"{k}: {v}" for k, v in values.items())
        return [types.TextContent(type="text", text=lines)]

    if name == "get_cursor_values":
        values = await _call(get_cursor_values)
        lines = "\n".join(f"{k}: {v}" for k, v in values.items())
        return [types.TextContent(type="text", text=lines)]

    if name == "send_raw":
        if not _send_raw_enabled():
            raise ValueError(
                f"send_raw is disabled. Set {_SEND_RAW_ENV}=1 to enable arbitrary SCPI commands."
            )
        response = await _call(send_raw, arguments["command"], _attempts=1)
        return [types.TextContent(type="text", text=response or "(no response)")]

    if name == "check_error":
        err = await _call(check_scpi_error, _attempts=1)
        return [types.TextContent(type="text", text=err or "No error")]

    if name in ("run", "stop", "single"):
        fn = {"run": run, "stop": stop, "single": single}[name]
        status = await _call(fn, _attempts=1)
        return [types.TextContent(type="text", text=f"trigger status: {status}")]

    if name == "autoscale":
        if confirmation := _require_confirmation(
            name, arguments, "Autoscale overwrites channel, timebase, and trigger settings."
        ):
            return [types.TextContent(type="text", text=json.dumps(confirmation, separators=(",", ":")))]
        state = await _configure_and_read(autoscale, get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()

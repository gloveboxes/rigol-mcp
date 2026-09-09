"""Rigol DS1000Z MCP server."""

import base64
import json
import os
from datetime import datetime
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

from rigol_mcp.waveform_analysis import describe_waveform as _describe_waveform
from rigol_mcp import scpi
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
        return types.CallToolResult(content=_bounded_content(await call_tool(params.name, arguments)))
    except ValidationError as exc:
        location = ".".join(map(str, exc.absolute_path))[:128] or "arguments"
        return types.CallToolResult(content=[types.TextContent(
            type="text", text=f"Invalid {location}: {exc.validator} constraint failed",
        )], is_error=True)
    except Exception as exc:
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


def _send_raw_enabled() -> bool:
    return os.environ.get(_SEND_RAW_ENV, "").strip().lower() not in ("", "0", "false", "no", "off")


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


async def _configure_and_read(setter, reader, *args, **kwargs):
    def operation(instrument):
        setter(instrument, *args, **kwargs)
        return reader(instrument, *args)

    return await _call(operation, _attempts=1)


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
                "Always selects EDGE, even with no arguments; return readback. POS rises, NEG falls, RFAL "
                "accepts either edge. EXT is available on DHO802/DHO812, not DHO814."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Trigger source, e.g. CHAN1"},
                    "slope":  {"type": "string", "enum": ["POS", "NEG", "RFAL"]},
                    "level":  {"type": ["number", "string"], "description": "Trigger level in volts"},
                },
                "required": [],
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
                "Analyse NORM screen data (DHO 1000, DS1000Z up to 1200 points), suppressing unreliable "
                "interpretation. Does not stop acquisition; stop first for consistency. Enables disabled "
                "channels; acquisition may be needed. Leaves transfer settings changed. raw_data=true "
                "saves JSON and returns metadata/path, never samples. Use download_waveform for memory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel":  {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "raw_data": {"type": "boolean", "description": "Save raw JSON and return metadata/path instead of analysis", "default": False},
                },
                "required": ["channel"],
            },
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
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]
    # send_raw is an arbitrary-SCPI escape hatch — only expose it when explicitly enabled.
    if not _send_raw_enabled():
        tools = [t for t in tools if t.name != "send_raw"]
    return tools


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

        state = await _configure_and_read(set_timebase, get_timebase_state, scale=_f("scale_s_div"), offset=_f("offset_s"))
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "set_trigger":
        level = arguments.get("level")
        state = await _configure_and_read(
            set_trigger, get_trigger_state,
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

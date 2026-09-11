"""MCP protocol regression tests without instrument access."""

import asyncio
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

import rigol_mcp.server as srv
from tests.conftest import FakeScope


@pytest.fixture(params=["auto", "legacy"])
def protocol_mode(request):
    return request.param


@pytest.fixture
def instrument_call(monkeypatch):
    mocked = AsyncMock(side_effect=AssertionError("Unexpected instrument access"))
    monkeypatch.setattr(srv, "_call", mocked)
    return mocked


async def test_tool_result(protocol_mode, instrument_call):
    instrument_call.side_effect = None
    instrument_call.return_value = 1000.0
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("measure", {"channel": "CHAN1", "item": "FREQUENCY"})
    assert not result.is_error
    assert result.content[0].text == "FREQUENCY on CHAN1: 1000.0"
    instrument_call.assert_awaited_once_with(srv.measure, "CHAN1", "FREQUENCY")


@pytest.mark.parametrize("verify_hardware", [None, False])
async def test_capability_evidence_over_mcp(protocol_mode, monkeypatch, verify_hardware):
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0",
        ":SYSTem:RAMount?": "4", ":SYSTem:GAMount?": "10", ":SYSTem:ERRor?": "0",
    })
    monkeypatch.setattr(srv, "get_scope", lambda: instrument)
    arguments = {} if verify_hardware is None else {"verify_hardware": verify_hardware}
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("get_capabilities", arguments)
    assert not result.is_error
    assert len(result.content[0].text) < srv._TEXT_BUDGET
    capabilities = json.loads(result.content[0].text)
    assert capabilities["model"] == "DHO814"
    assert capabilities["channels"] == ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]
    expected = "hardware-verified" if verify_hardware is None else "documented"
    assert capabilities["evidence"]["channels"]["status"] == expected
    assert capabilities["evidence"]["measurement_items"]["status"] == "documented"


@pytest.mark.parametrize("verify_hardware", [False, True])
async def test_inspect_scope_over_mcp(protocol_mode, monkeypatch, verify_hardware):
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0",
        ":SYSTem:RAMount?": "2", ":SYSTem:GAMount?": "10", ":SYSTem:ERRor?": "0",
        ":TIM:SCAL?": "0.001", ":TIM:OFFS?": "0", ":TIM:MODE?": "MAIN",
        ":TRIGger:MODE?": "EDGE", ":TRIGger:STATus?": "STOP",
        ":TRIGger:EDGE:SOURce?": "CHAN1", ":TRIGger:EDGE:SLOPe?": "POS",
        ":TRIGger:EDGE:LEVel?": "1",
    })
    channels = [f"CHAN{number}" for number in range(1, 3 if verify_hardware else 5)]
    for channel in channels:
        for suffix, value in {"DISP": "1", "SCAL": "1", "OFFS": "0", "COUP": "DC", "PROB": "1"}.items():
            instrument.responses[f":{channel}:{suffix}?"] = value
    queries = []
    original_query = instrument.query

    def query(command):
        queries.append(command)
        return original_query(command)

    monkeypatch.setattr(instrument, "query", query)
    monkeypatch.setattr(srv, "get_scope", lambda: instrument)
    async with Client(srv.server, mode=protocol_mode) as client:
        response = await client.call_tool("inspect_scope", {"verify_hardware": verify_hardware})
    assert not response.is_error
    assert len(response.content[0].text) < srv._TEXT_BUDGET
    result = json.loads(response.content[0].text)
    assert result["complete"] is True
    assert result["errors"] == {}
    assert result["identity"] == instrument.responses["*IDN?"]
    assert result["capabilities"]["channels"] == channels
    assert list(result["state"]["channels"]) == channels
    assert result["state"]["trigger"]["status"] == "STOP"
    assert "capabilities" not in result["state"]
    assert (":SYSTem:ERRor?" in queries) is verify_hardware
    assert queries.count("*IDN?") == 2
    assert instrument.written == []


@pytest.mark.parametrize("name, arguments", [
    ("inspect_scope", {"verify_hardware": "false"}),
    ("inspect_scope", {"unexpected": True}),
    ("measure", {}),
    ("measure", {"channel": "CHAN5", "item": "FREQUENCY"}),
    ("nonexistent_tool", {}),
    ("send_raw", {"command": "*IDN?"}),
])
async def test_invalid_calls_do_not_access_instrument(protocol_mode, instrument_call, name, arguments):
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert result.content[0].text
    instrument_call.assert_not_awaited()


async def test_instrument_error_is_tool_error(protocol_mode, instrument_call):
    instrument_call.side_effect = OSError("instrument disconnected")
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("measure", {"channel": "CHAN1", "item": "FREQUENCY"})
    assert result.is_error
    assert result.content[0].text == "instrument disconnected"


async def test_enabled_raw_command(protocol_mode, instrument_call, monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")
    instrument_call.side_effect = None
    instrument_call.return_value = "RIGOL TEST"
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("send_raw", {"command": "*IDN?"})
    assert not result.is_error
    assert result.content[0].text == "RIGOL TEST"
    instrument_call.assert_awaited_once_with(srv.send_raw, "*IDN?", _attempts=1)


@pytest.mark.parametrize("enable_raw", [False, True])
async def test_stdio_startup_and_discovery(protocol_mode, enable_raw, tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rigol_mcp.server"],
        cwd=tmp_path,
        env={
            "PYTHON_DOTENV_DISABLED": "1",
            "RIGOL_ENABLE_SEND_RAW": "1" if enable_raw else "0",
        },
    )
    async with asyncio.timeout(30):
        async with Client(parameters, mode=protocol_mode, read_timeout_seconds=10) as client:
            assert client.server_info.name == "rigol-mcp"
            assert client.server_capabilities.tools is not None
            assert "strictly sequentially" in client.instructions
            listing = await client.list_tools()
            names = [tool.name for tool in listing.tools]
            assert len(names) == len(set(names)) == (47 if enable_raw else 46)
            assert ("send_raw" in names) == enable_raw
            assert {
                "inspect_scope",
                "configure_timing_capture", "capture_waveforms", "acquire_and_capture",
                "analyze_pwm_envelope",
                "measure_statistics", "configure_mask_test", "get_search_results",
                "save_scope_setup", "restore_scope_setup", "configure_recording",
            } <= set(names)
            for tool in listing.tools:
                Draft202012Validator.check_schema(tool.input_schema)
            result = await client.call_tool("measure", {})
            assert result.is_error


async def test_catalog_available_without_hardware(protocol_mode, instrument_call):
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("scpi_catalog", {"subsystem": "acquire"})
    assert not result.is_error
    assert "ACQuire:MDEPth" in result.content[0].text
    instrument_call.assert_not_awaited()


async def test_large_result_is_bounded_and_retrievable(protocol_mode, instrument_call, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    instrument_call.side_effect = None
    instrument_call.return_value = "x" * 100000
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("measure", {"channel": "CHAN1", "item": "VPP"})
        assert not result.is_error
        assert len(result.content[0].text) < 1024
        metadata = json.loads(result.content[0].text)
        assert Path(metadata["path"]).read_text().endswith("x" * 100000)
        excerpt = await client.call_tool("read_capture", {"path": metadata["path"], "max_bytes": 128})
        assert not excerpt.is_error
        assert json.loads(excerpt.content[0].text)["next_offset"] == 128
    instrument_call.assert_awaited_once()


async def test_validation_error_does_not_echo_large_input(protocol_mode, instrument_call):
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("measure", {"channel": "x" * 100000, "item": "VPP"})
    assert result.is_error
    assert len(result.content[0].text) < 256
    instrument_call.assert_not_awaited()


async def test_escaped_capture_excerpt_does_not_create_more_capture_files(protocol_mode, instrument_call, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    saved = srv.scpi._save_data(b"\x00" * 4096, ".txt")
    async with Client(srv.server, mode=protocol_mode) as client:
        response = await client.call_tool("read_capture", {"path": saved["path"], "max_bytes": 2048})
    assert not response.is_error
    result = json.loads(response.content[0].text)
    assert "file_backed" not in result
    assert result["data"] == "\x00" * result["bytes"]
    assert result["next_offset"] == result["bytes"] > 0
    assert len(response.content[0].text) <= srv._TEXT_BUDGET
    assert len(list(tmp_path.iterdir())) == 1
    instrument_call.assert_not_awaited()


async def test_raw_waveform_arrays_stay_on_disk(protocol_mode, instrument_call, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    data = {"channel": "CHAN1", "points": 1200, "vmin_v": -1, "vmax_v": 1,
            "times_s": list(range(1200)), "voltages_v": [0.25] * 1200, "warnings": []}
    instrument_call.side_effect = None
    instrument_call.return_value = data
    async with Client(srv.server, mode=protocol_mode) as client:
        result = await client.call_tool("get_waveform", {"channel": "CHAN1", "raw_data": True})
    assert not result.is_error
    summary = json.loads(result.content[0].text)
    assert "times_s" not in summary and "voltages_v" not in summary
    assert summary["points"] == 1200
    assert json.loads(Path(summary["path"]).read_text()) == data


async def test_tool_definitions_have_a_context_budget():
    tools = await srv.list_tools()
    definitions = json.dumps([tool.model_dump(by_alias=True, exclude_none=True) for tool in tools], separators=(",", ":"))
    assert sum(len(tool.description or "") for tool in tools) < 7000
    assert len(definitions) < 33000


async def test_transfer_schema_defaults_match_implementation():
    tools = {tool.name: tool for tool in await srv.list_tools()}
    for name, function in (("download_waveform", srv.scpi.download_waveform),
                           ("read_capture", srv.scpi.read_capture),
                           ("scpi_catalog", srv.scpi.discover)):
        signature = inspect.signature(function)
        for parameter, schema in tools[name].input_schema["properties"].items():
            if "default" in schema:
                assert schema["default"] == signature.parameters[parameter].default


@pytest.mark.parametrize("name,phrases", [
    ("read_capture", ["not screenshots", "next_offset"]),
    ("download_waveform", ["DHO814", "not restored", "acquisition is unchanged"]),
    ("get_scope_state", ["only for EDGE", "without live"]),
    ("get_waveform", ["Does not stop", "transfer settings changed", "never samples"]),
    ("measure", ["Registers", "results panel", "disabled channels"]),
    ("measure_between", ["Registers", "results panel", "disabled sources"]),
    ("set_trigger", ["top-level source/slope/level", "advanced trigger_type", "validated type-specific"]),
    ("set_cursors", ["OFF accepts no", "rejects positions here", "DHO814"]),
    ("scpi_execute", ["Non-reset writes", "No retries or completion guarantee"]),
    ("check_error", ["16", "only the first"]),
    ("run", ["status may lag", "get_scope_state"]),
    ("stop", ["retaining", "Not a clear or factory reset"]),
    ("autoscale", ["changing", "Not capability discovery or factory reset"]),
    ("send_raw", ["Text queries", "binary transfers", "no retries"]),
])
async def test_tool_descriptions_disclose_operational_constraints(monkeypatch, name, phrases):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")
    tools = {tool.name: tool for tool in await srv.list_tools()}
    description = tools[name].description.lower()
    for phrase in phrases:
        assert phrase.lower() in description


async def test_identity_description_matches_available_diagnostics():
    tools = {tool.name: tool for tool in await srv.list_tools()}
    assert "backend" not in tools["idn"].description.lower()
    assert "diagnostic text" in tools["idn"].description
"""Unit tests for rigol_mcp.server retries and screenshot responses.
VISA access is faked; no hardware involved."""

import time
import json

import pyvisa
import pytest

import rigol_mcp.server as srv


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch):
    """Remove real timing and connection management from _call for unit testing."""
    monkeypatch.setattr(srv, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(srv, "get_scope", lambda: "FAKE_SCOPE")
    monkeypatch.setattr(srv, "invalidate_scope", lambda: invalidated.append(1))
    # advance the clock baseline so the min-interval gate doesn't sleep by default
    srv._last_call_time = time.monotonic() - 10.0
    invalidated.clear()


invalidated: list = []


def _tmo():
    return pyvisa.errors.VisaIOError(-1073807339)  # VI_ERROR_TMO


async def test_call_returns_result():
    assert await srv._call(lambda scope: f"ok:{scope}") == "ok:FAKE_SCOPE"


def test_large_numeric_text_is_preserved(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    payload = "1" * 10000
    result = srv._bounded_content([srv.types.TextContent(type="text", text=payload)])
    metadata = json.loads(result[0].text)
    assert Path(metadata["path"]).read_text() == payload
    assert len(result[0].text) < srv._TEXT_BUDGET


async def test_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky(scope):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _tmo()
        return "recovered"

    assert await srv._call(flaky) == "recovered"
    assert calls["n"] == 3
    assert len(invalidated) == 2          # reconnected before each of the 2 retries


async def test_call_exhausts_attempts_then_raises():
    calls = {"n": 0}

    def always(scope):
        calls["n"] += 1
        raise _tmo()

    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv._call(always)
    assert calls["n"] == srv._MAX_ATTEMPTS
    assert len(invalidated) == srv._MAX_ATTEMPTS  # invalidate after every failed attempt


async def test_call_can_disable_retries():
    calls = []

    def operation(scope):
        calls.append(scope)
        raise _tmo()

    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv._call(operation, _attempts=1)
    assert len(calls) == 1


@pytest.mark.parametrize("tool", ["run", "stop", "single", "autoscale", "check_error", "send_raw"])
async def test_action_tools_never_replay_after_timeout(monkeypatch, tool):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(1)
        raise _tmo()

    target = "check_scpi_error" if tool == "check_error" else tool
    monkeypatch.setattr(srv, target, uncertain)
    arguments = {"command": "*RST"} if tool == "send_raw" else {}
    if tool == "autoscale":
        monkeypatch.setattr(srv, "get_scope_state", lambda scope: {"state": "before"})
        request = json.loads((await srv.call_tool(tool, arguments))[0].text)
        arguments["confirm_token"] = request["confirm_token"]
    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv.call_tool(tool, arguments)
    assert calls == [1]


async def test_configuration_readback_failure_does_not_replay_write(monkeypatch):
    calls = []

    def setter(*args, **kwargs):
        calls.append("write")

    def reader(*args):
        calls.append("read")
        if calls.count("read") == 2:
            raise _tmo()
        return {"scale_s_div": "0.002"}

    monkeypatch.setattr(srv, "set_timebase", setter)
    monkeypatch.setattr(srv, "get_timebase_state", reader)
    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv.call_tool("set_timebase", {"scale_s_div": 0.001})
    assert calls == ["read", "write", "read"]


async def test_channel_setter_only_reads_its_own_channel(monkeypatch):
    from tests.conftest import FakeScope

    instrument = FakeScope(responses={
        ":SYSTem:ERRor?": "0", ":CHAN2:DISP?": "1", ":CHAN2:SCAL?": "0.1",
        ":CHAN2:OFFS?": "0", ":CHAN2:COUP?": "DC", ":CHAN2:PROB?": "1",
    })
    monkeypatch.setattr(srv, "get_scope", lambda: instrument)
    result = await srv.call_tool("set_channel", {"channel": "CHAN2", "scale_v_div": 0.1})
    assert '"scale_v_div": "0.1"' in result[0].text
    assert instrument.written == [":CHAN2:SCAL 0.1"]


async def test_call_does_not_retry_non_communication_errors():
    calls = {"n": 0}

    def bad(scope):
        calls["n"] += 1
        raise ValueError("bad argument")

    with pytest.raises(ValueError):
        await srv._call(bad)
    assert calls["n"] == 1                 # no retry
    assert invalidated == []               # no reconnect


async def test_framing_error_discards_stream_without_replaying():
    calls = []

    def malformed(scope):
        calls.append(scope)
        raise srv.BlockReadError("Truncated block")

    with pytest.raises(srv.BlockReadError):
        await srv._call(malformed)
    assert len(calls) == 1
    assert invalidated == [1]


@pytest.mark.parametrize("exc_factory", [
    _tmo,
    lambda: UnicodeDecodeError("utf-8", b"", 0, 1, "boom"),
    lambda: OSError("connection reset"),
])
async def test_call_retries_each_retryable_type(exc_factory):
    calls = {"n": 0}

    def flaky(scope):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc_factory()
        return "ok"

    assert await srv._call(flaky) == "ok"
    assert calls["n"] == 2


async def test_call_enforces_min_interval(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)
    srv._last_call_time = time.monotonic()   # just ran -> next call must wait
    await srv._call(lambda scope: "ok")
    assert slept and slept[0] > 0
    assert slept[0] <= srv._MIN_INTERVAL + 0.01


# --------------------------------------------------------------------------- screenshot reconnect

async def _run_screenshot(monkeypatch, tmp_path, *, include_image=False):
    """Drive call_tool('screenshot') with a faked _call and capture invalidate calls."""
    monkeypatch.setenv("RIGOL_SCREENSHOT_DIR", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"fakeimage"

    async def fake_call(fn, *a, **k):
        return png

    calls = {"invalidate": 0}
    monkeypatch.setattr(srv, "_call", fake_call)
    monkeypatch.setattr(srv, "invalidate_scope", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))

    result = await srv.call_tool("screenshot", {"include_image": include_image})
    return result, calls, tmp_path


async def test_screenshot_preserves_lan_connection(monkeypatch, tmp_path):
    result, calls, out = await _run_screenshot(monkeypatch, tmp_path)
    assert calls["invalidate"] == 0
    assert not any(getattr(b, "type", None) == "image" for b in result)
    assert list(out.glob("*.png"))                       # saved to disk


async def test_screenshot_image_is_opt_in(monkeypatch, tmp_path):
    result, _, _ = await _run_screenshot(monkeypatch, tmp_path, include_image=True)
    assert any(block.type == "image" for block in result)


# --------------------------------------------------------------------------- send_raw gating

async def test_send_raw_hidden_by_default():
    names = {t.name for t in await srv.list_tools()}
    assert "send_raw" not in names


async def test_send_raw_listed_when_enabled(monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")
    names = {t.name for t in await srv.list_tools()}
    assert "send_raw" in names


async def test_send_raw_call_rejected_when_disabled():
    with pytest.raises(ValueError, match="send_raw is disabled"):
        await srv.call_tool("send_raw", {"command": ":CHAN1:SCAL?"})


async def test_send_raw_call_works_when_enabled(monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")

    async def fake_call(fn, *a, **k):
        return "1.000000e+00"

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv.call_tool("send_raw", {"command": ":CHAN1:SCAL?"})
    assert result[0].text == "1.000000e+00"


# --------------------------------------------------------------------------- confirmation gating

async def test_autoscale_requires_bound_single_use_confirmation(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append(fn)
        return {"ok": True}

    monkeypatch.setattr(srv, "_call", fake_call)
    first = await srv.call_tool("autoscale", {})
    request = json.loads(first[0].text)
    assert request["code"] == "USER_CONFIRMATION_REQUIRED"
    assert calls == []

    confirmed = await srv.call_tool("autoscale", {"confirm_token": request["confirm_token"]})
    assert json.loads(confirmed[0].text) == {"ok": True}
    assert len(calls) == 1

    with pytest.raises(ValueError, match="confirmation token"):
        await srv.call_tool("autoscale", {"confirm_token": request["confirm_token"]})


async def test_dangerous_scpi_write_requires_confirmation(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return {"operation": "write"}

    monkeypatch.setattr(srv, "_call", fake_call)
    arguments = {"command": "*RST", "operation": "write"}
    first = await srv.call_tool("scpi_execute", arguments)
    request = json.loads(first[0].text)
    assert request["code"] == "USER_CONFIRMATION_REQUIRED"
    assert calls == []

    arguments["confirm_token"] = request["confirm_token"]
    result = await srv.call_tool("scpi_execute", arguments)
    assert json.loads(result[0].text) == {"operation": "write"}
    assert len(calls) == 1


async def test_setup_snapshot_and_restore_use_file_backed_scpi(monkeypatch):
    calls = []
    state = {
        "timebase": {"scale_s_div": "1e-6"},
        "channels": {"CHAN1": {"display": True}},
        "trigger": {"mode": "EDGE", "status": "STOP"},
    }

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        if fn is srv.get_scope_state:
            return state
        return {"path": "/data/captures/capture_0123456789abcdef0123456789abcdef.bin"}

    monkeypatch.setattr(srv, "_call", fake_call)
    monkeypatch.setattr(srv.Path, "write_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(srv.Path, "is_file", lambda self: False)
    saved = json.loads((await srv.call_tool("save_scope_setup", {}))[0].text)
    assert saved["path"].endswith(".bin")
    assert saved["state_captured"] is True
    assert calls[0][0] is srv.scpi.execute
    assert calls[0][1] == (":SYSTem:SETup", "query")

    arguments = {"path": saved["path"]}
    request = json.loads((await srv.call_tool("restore_scope_setup", arguments))[0].text)
    assert request["code"] == "USER_CONFIRMATION_REQUIRED"
    arguments["confirm_token"] = request["confirm_token"]
    restored = json.loads((await srv.call_tool("restore_scope_setup", arguments))[0].text)
    write_call = next(call for call in reversed(calls) if call[0] is srv.scpi.execute)
    assert write_call[1] == (":SYSTem:SETup", "write")
    assert write_call[2]["data_path"] == saved["path"]
    assert write_call[2]["_attempts"] == 1
    assert restored["verification"]["verified"] is False


def test_setup_state_comparison_ignores_trigger_run_status():
    expected = {
        "timebase": {"scale_s_div": "1e-6"},
        "channels": {"CHAN1": {"display": True}},
        "trigger": {"mode": "EDGE", "status": "RUN"},
    }
    actual = {
        "timebase": {"scale_s_div": "1e-6"},
        "channels": {"CHAN1": {"display": True}},
        "trigger": {"mode": "EDGE", "status": "STOP"},
    }
    assert srv._state_mismatches(expected, actual) == {}
    actual["timebase"]["scale_s_div"] = "2e-6"
    assert "timebase" in srv._state_mismatches(expected, actual)


async def test_scpi_query_does_not_require_confirmation(monkeypatch):
    async def fake_call(fn, *args, **kwargs):
        return {"operation": "query"}

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv.call_tool("scpi_execute", {"command": "*IDN"})
    assert json.loads(result[0].text) == {"operation": "query"}


async def test_semantic_tools_are_registered_with_safety_metadata():
    tools = {tool.name: tool for tool in await srv.list_tools()}
    assert {
        "analyze_waveform", "clear_measurements", "configure_acquisition",
        "configure_meter", "get_meter_value", "configure_decode", "get_decode_result",
        "configure_math", "configure_reference", "configure_histogram",
        "configure_timing_capture", "capture_waveforms", "acquire_and_capture",
        "measure_statistics", "configure_mask_test", "get_mask_results",
        "configure_search", "get_search_results", "save_scope_setup", "restore_scope_setup",
        "configure_recording", "get_recording_state", "control_recording_replay",
    } <= tools.keys()
    assert tools["analyze_waveform"].meta["rigol/operationClass"] == "measurement"
    assert tools["analyze_waveform"].annotations.read_only_hint is False
    assert tools["clear_measurements"].annotations.destructive_hint is True
    assert tools["configure_acquisition"].meta["rigol/operationClass"] == "configuration"
    assert tools["restore_scope_setup"].annotations.destructive_hint is True
    assert tools["control_recording_replay"].meta["rigol/operationClass"] == "action"


async def test_semantic_configuration_route_is_not_retried(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return {"requested": {"acquisition_type": "PEAK"}, "applied": {"acquisition_type": "PEAK"}}

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv.call_tool("configure_acquisition", {"acquisition_type": "PEAK"})
    assert json.loads(result[0].text)["applied"]["acquisition_type"] == "PEAK"
    assert calls[0][0] is srv.semantic.configure_acquisition
    assert calls[0][2]["_attempts"] == 1


async def test_advanced_trigger_route_preserves_nested_settings(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, kwargs))
        return {"applied": {"type": "PULSE"}}

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv.call_tool("set_trigger", {
        "trigger_type": "PULSE", "source": "CHAN1",
        "settings": {"condition": "LESS", "upper_s": 1e-6},
    })
    assert json.loads(result[0].text)["applied"]["type"] == "PULSE"
    assert calls[0][0] is srv.semantic.configure_trigger
    assert calls[0][1]["settings"] == {"source": "CHAN1", "condition": "LESS", "upper_s": 1e-6}
    assert calls[0][1]["_attempts"] == 1


async def test_existing_setter_returns_public_requested_names(monkeypatch):
    reads = iter([{"scale_s_div": "0.002"}, {"scale_s_div": "0.001"}])
    monkeypatch.setattr(srv, "set_timebase", lambda *args, **kwargs: None)
    monkeypatch.setattr(srv, "get_timebase_state", lambda *args: next(reads))
    result = json.loads((await srv.call_tool("set_timebase", {"scale_s_div": "0.001"}))[0].text)
    assert result["requested"] == {"scale_s_div": "0.001"}
    assert result["changed"] is True


def test_audit_log_redacts_tokens_and_setup_payload(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("RIGOL_AUDIT_LOG", str(audit))
    srv._audit_event("scpi_execute", {
        "command": "not-a-command", "operation": "write",
        "confirm_token": "secret", "data_base64": "AAAA",
    }, "error", "invalid")
    record = json.loads(audit.read_text())
    assert record["operation_class"] == "unknown"
    assert record["arguments"]["confirm_token"] == "<redacted>"
    assert record["arguments"]["data_base64"] == "<4 characters>"


async def test_timing_capture_route_is_generic_and_not_retried(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, kwargs))
        return {"applied": {"trigger_source": "CHAN1"}}

    monkeypatch.setattr(srv, "_call", fake_call)
    arguments = {
        "channels": [{"channel": 1, "label": "CLOCK", "voltage_domain_v": 3.3}],
        "trigger": {"channel": 1}, "signal_frequency_hz": 1_000_000,
        "purpose": "Verify translated clock",
    }
    result = await srv.call_tool("configure_timing_capture", arguments)
    assert json.loads(result[0].text)["applied"]["trigger_source"] == "CHAN1"
    assert calls[0][0] is srv.semantic.configure_timing_capture
    assert calls[0][1]["purpose"] == "Verify translated clock"
    assert calls[0][1]["_attempts"] == 1


def test_multi_channel_capture_requires_stopped_acquisition(monkeypatch):
    from tests.conftest import FakeScope

    instrument = FakeScope(responses={":TRIGger:STATus?": "RUN"})
    monkeypatch.setattr(srv, "get_waveform", lambda *args: pytest.fail("waveform read while running"))
    with pytest.raises(ValueError, match="Stop acquisition"):
        srv._capture_stopped_waveforms(instrument, ["CHAN1", "CHAN2"])


def test_acquire_and_capture_stops_after_timeout(monkeypatch):
    from tests.conftest import FakeScope

    instrument = FakeScope(responses={
        ":TRIGger:STATus?": "WAIT",
        ":SYSTem:ERRor?": "0,No error",
    })
    times = iter([0.0, 1.0])
    monkeypatch.setattr(srv.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(srv.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="acquisition stopped"):
        srv._acquire_and_capture(instrument, ["CHAN1"], 0.5, 0.1)
    assert instrument.written == [":SINGle", ":STOP"]


def test_acquire_and_capture_attributes_single_error_before_polling():
    from tests.conftest import FakeScope

    errors = ['-200,"Command execute failed"']
    instrument = FakeScope(responses={
        ":SYSTem:ERRor?": lambda: errors.pop(0) if errors else '0,"No error"',
    })

    with pytest.raises(RuntimeError, match='SCPI error after :SINGle: -200'):
        srv._acquire_and_capture(instrument, ["MATH1"], 0.5, 0.1)

    assert instrument.written == [":SINGle"]


async def test_capture_waveforms_saves_raw_and_returns_analysis(monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    waveform = {
        "channel": "CHAN1", "points": 4, "time_increment_s": 1e-6,
        "time_start_s": 0.0, "time_end_s": 3e-6,
        "vmin_v": -1.0, "vmax_v": 1.0, "vmean_v": 0.0,
        "times_s": [0.0, 1e-6, 2e-6, 3e-6], "voltages_v": [-1.0, 1.0, -1.0, 1.0],
    }

    async def fake_call(fn, *args, **kwargs):
        return {"CHAN1": waveform}

    monkeypatch.setattr(srv, "_call", fake_call)
    result = json.loads((await srv.call_tool(
        "capture_waveforms", {"channels": ["CHAN1"], "label": "clock"},
    ))[0].text)
    assert result["label"] == "clock"
    assert result["channels"]["CHAN1"]["valid"] is True
    assert (tmp_path / result["path"].split("/")[-1]).exists()

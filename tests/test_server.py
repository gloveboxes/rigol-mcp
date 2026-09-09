"""Unit tests for rigol_mcp.server retries and screenshot responses.
VISA access is faked; no hardware involved."""

import time

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
    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv.call_tool(tool, {"command": "*RST"} if tool == "send_raw" else {})
    assert calls == [1]


async def test_configuration_readback_failure_does_not_replay_write(monkeypatch):
    calls = []

    def setter(*args, **kwargs):
        calls.append("write")

    def reader(*args):
        calls.append("read")
        raise _tmo()

    monkeypatch.setattr(srv, "set_timebase", setter)
    monkeypatch.setattr(srv, "get_timebase_state", reader)
    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv.call_tool("set_timebase", {"scale_s_div": 0.001})
    assert calls == ["write", "read"]


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

"""Unit tests for rigol_mcp.scope — TCP/IP connections, exact-length block reads,
SCPI helpers and parsing. All VISA interaction is faked (see conftest)."""

import pytest

from rigol_mcp import scope as sc
from rigol_mcp import drivers
from tests.conftest import FakeScope, FakeResourceManager, make_block


# --------------------------------------------------------------------------- env / transport

@pytest.mark.parametrize("command", [
    ":MEASure:ITEM? VPP,CHAN1",
    ":STOP;:MEASure:ITEM? VPP,CHAN1",
    ":CHAN1:SCAL?",
])
def test_send_raw_reads_parameterized_and_compound_queries(command):
    instrument = FakeScope(responses={command: "2.0"})
    assert sc.send_raw(instrument, command) == "2.0"
    assert instrument.written == []


def test_send_raw_ignores_question_mark_in_quoted_label():
    command = ':CHAN1:LABel:CONTent "why;what?"'
    instrument = FakeScope(responses={":SYSTem:ERRor?": "0"})
    assert sc.send_raw(instrument, command) == ""
    assert instrument.written == [command]

def test_lan_resource_string(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "192.168.1.50")
    assert sc.get_lan_resource_string() == "TCPIP0::192.168.1.50::5555::SOCKET"


def test_lan_resource_string_missing_ip_raises():
    with pytest.raises(RuntimeError, match="RIGOL_IP"):
        sc.get_lan_resource_string()


@pytest.mark.parametrize("ip", ["", "   "])
def test_missing_ip_never_opens_a_session(monkeypatch, ip):
    monkeypatch.setenv("RIGOL_IP", ip)
    monkeypatch.setenv("RIGOL_USB", "1")

    def unexpected_manager(*args):
        pytest.fail("VISA must not be opened without a LAN address")

    monkeypatch.setattr(sc.pyvisa, "ResourceManager", unexpected_manager)
    with pytest.raises(RuntimeError, match="RIGOL_IP"):
        sc.get_scope()


# --------------------------------------------------------------------------- backend selection

def _patch_rms(monkeypatch, mapping):
    """Patch pyvisa.ResourceManager(backend) -> mapping[backend]."""
    def factory(backend=None):
        if backend not in mapping:
            raise AssertionError(f"unexpected backend requested: {backend!r}")
        return mapping[backend]
    monkeypatch.setattr(sc.pyvisa, "ResourceManager", factory)


# --------------------------------------------------------------------------- get_scope

@pytest.mark.parametrize("legacy_usb", ["0", "1"])
def test_get_scope_lan_configures_session(monkeypatch, legacy_usb):
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    monkeypatch.setenv("RIGOL_USB", legacy_usb)
    fake = FakeScope()
    opened = []

    def open_scope(resource):
        opened.append(resource)
        return fake

    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope_factory=open_scope)})
    s = sc.get_scope()
    assert s is fake
    assert s.timeout == sc._LAN_TIMEOUT_MS
    assert s.write_termination == "\n" and s.read_termination == "\n"
    assert s.chunk_size == 1024 * 1024
    assert s.cleared == 1            # initial flush attempted
    # cached on second call
    assert sc.get_scope() is fake
    assert opened == ["TCPIP0::10.0.0.9::5555::SOCKET"]


def test_get_scope_clear_unsupported_is_tolerated(monkeypatch):
    import pyvisa
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope()
    def boom():
        raise pyvisa.errors.VisaIOError(-1073807257)  # VI_ERROR_NSUP_OPER
    fake.clear = boom
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    # must not raise despite clear() being unsupported
    assert sc.get_scope() is fake


def test_invalidate_scope_closes_and_resets(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope()
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    sc.get_scope()
    sc.invalidate_scope()
    assert fake.closed is True
    assert sc._scope is None


# --------------------------------------------------------------------------- connection_info

def test_connection_info_lan_unconfigured():
    """No env vars set: report LAN as the default with the unset markers visible."""
    info = sc.connection_info()
    assert info["transport"] == "LAN"
    assert "RIGOL_USB" not in info
    assert "(unset)" in info["RIGOL_IP"]
    assert "RIGOL_IP not set" in info["lan_target"]
    assert info["session"] == "not yet opened"


def test_connection_info_lan_configured(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "192.168.1.47")
    info = sc.connection_info()
    assert info["transport"] == "LAN"
    assert info["RIGOL_IP"] == "192.168.1.47"
    # The full resource string is exposed so users can spot a wrong IP at a glance.
    assert info["lan_target"] == "TCPIP0::192.168.1.47::5555::SOCKET"


def test_connection_info_ignores_legacy_transport_settings(monkeypatch):
    monkeypatch.setenv("RIGOL_USB", "1")
    monkeypatch.setenv("RIGOL_IP", "192.168.1.47")
    info = sc.connection_info()
    assert info["transport"] == "LAN"
    assert "RIGOL_USB" not in info
    assert info["RIGOL_IP"] == "192.168.1.47"
    assert info["lan_target"] == "TCPIP0::192.168.1.47::5555::SOCKET"


def test_connection_info_reflects_open_session(monkeypatch):
    """Once a session is open, the resource string is exposed for debugging."""
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope(resource_name="TCPIP0::10.0.0.9::5555::SOCKET")
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    sc.get_scope()  # populates the cached session
    info = sc.connection_info()
    assert info["session"] == "cached/open"
    assert info["resource"] == "TCPIP0::10.0.0.9::5555::SOCKET"


def test_connection_info_never_raises_on_any_env(monkeypatch):
    """connection_info is the diagnostic of last resort — it must survive any env state."""
    monkeypatch.delenv("RIGOL_IP", raising=False)
    sc.connection_info()  # no env vars at all
    monkeypatch.setenv("RIGOL_IP", "   ")
    sc.connection_info()  # garbled values too — still must not raise


def test_set_driver_from_idn_populates_cache():
    """The idn handler uses this to surface the selected driver without a second *IDN?."""
    drv = sc.set_driver_from_idn("RIGOL TECHNOLOGIES,DS1054Z,SN,1.0")
    assert drv is not None and drv.name == "DS1000Z"
    assert sc._driver is drv  # cached for subsequent dialect calls


def test_set_driver_from_idn_returns_none_for_unknown():
    """Unknown IDN: driver cache stays empty so a later dialect tool raises the real error."""
    assert sc.set_driver_from_idn("RIGOL TECHNOLOGIES,XYZ9000,SN,1.0") is None
    assert sc._driver is None


def test_connection_info_shows_driver_when_session_open(monkeypatch):
    """After set_driver_from_idn, connection_info exposes the driver name to the user."""
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope(resource_name="TCPIP0::10.0.0.9::5555::SOCKET")
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    sc.get_scope()
    sc.set_driver_from_idn("RIGOL TECHNOLOGIES,DS1054Z,SN,1.0")
    info = sc.connection_info()
    assert info["driver"] == "DS1000Z"


# --------------------------------------------------------------------------- block-read framing

def test_read_block_via_bytecount():
    payload = b"1.0,2.0,3.0"
    s = FakeScope(read_buffer=make_block(payload))
    assert sc._read_block_via_bytecount(s) == payload


@pytest.mark.parametrize("ndigits", [1, 8, 9])
def test_read_definite_block_preserves_embedded_newlines(ndigits):
    payload = b"\x89PNG\n\x00\xff"
    instrument = FakeScope(read_buffer=make_block(payload, ndigits=ndigits))
    assert sc._read_definite_block(instrument) == payload


def test_read_definite_block_rejects_bad_header():
    with pytest.raises(ValueError, match="TMC block header"):
        sc._read_definite_block(FakeScope(read_buffer=b"NOTBLOCK"))


@pytest.mark.parametrize("response", [b"#15ab\n", b"#0abc\n", b"#2+3abc\n", b"#", b"#xabc\n"])
def test_read_definite_block_rejects_incomplete_or_invalid_framing(response):
    with pytest.raises(ValueError):
        sc._read_definite_block(FakeScope(read_buffer=response))


# --------------------------------------------------------------------------- screenshot / waveform

def test_screenshot_png_bytecount():
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    s = FakeScope(read_buffer=make_block(png))
    assert sc.screenshot_png(s) == png
    assert s.written == [":DISPlay:DATA? ON,OFF,PNG"]


def test_get_waveform_parses_ds1000z_response_and_stats():
    # PRE fields: [fmt,type,points,count,x_inc,x_origin,x_ref,...]
    pre = "0,0,5,1,1.000000e-06,-2.000000e-06,0,1,0,0"
    # DS1000Z wraps the ASCII CSV in an IEEE 488.2 block; read via _read_definite_block
    # which consumes the read_buffer (matching the byte-count reader's behaviour on @py).
    s = FakeScope(
        responses={":WAV:PRE?": pre, ":CHAN1:DISP?": "1",
                   ":CHAN1:SCAL?": "1.000000e+00", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"-1.0,0.0,2.5,1.0,-0.5"),
    )
    out = sc.get_waveform(s, "chan1")
    assert out["channel"] == "CHAN1"
    assert out["points"] == 5
    assert out["vmin_v"] == -1.0
    assert out["vmax_v"] == 2.5
    assert out["voltages_v"] == [-1.0, 0.0, 2.5, 1.0, -0.5]
    assert out["time_increment_s"] == 1e-6
    # times derived from x_origin + (i - x_ref) * x_inc
    assert out["time_start_s"] == pytest.approx(-2e-6)
    # vertical scale/offset captured for the analyser's noise-floor check
    assert out["y_scale_v_per_div"] == 1.0
    assert out["y_offset_v"] == 0.0


def test_get_waveform_tolerates_missing_vertical_scale():
    # If the scope doesn't answer :SCAL?/:OFFS?, the fields degrade to None (no crash).
    pre = "0,0,5,1,1.000000e-06,-2.000000e-06,0,1,0,0"
    s = FakeScope(
        responses={":WAV:PRE?": pre, ":CHAN1:DISP?": "1"},
        read_buffer=make_block(b"-1.0,0.0,2.5,1.0,-0.5"),
    )
    out = sc.get_waveform(s, "chan1")
    assert out["y_scale_v_per_div"] is None
    assert out["y_offset_v"] is None


@pytest.mark.parametrize("increment,samples", [("nan", b"1,2"), ("-1", b"1,2"), ("0.001", b"nan,2")])
def test_waveform_rejects_nonfinite_samples_or_timing(increment, samples):
    instrument = FakeScope(responses={
        ":CHAN1:DISP?": "1", ":WAV:PRE?": f"2,0,2,1,{increment},0,0,1,0,0",
    }, read_buffer=make_block(samples))
    with pytest.raises(ValueError, match="non-finite|timing"):
        sc.get_waveform(instrument, "CHAN1")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_measurement_is_annotated_invalid(value):
    assert "invalid/overflow" in sc.annotate_measurement_value(value)


# --------------------------------------------------------------------------- cursor math

def test_time_to_screen_x_roundtrip():
    s = FakeScope(responses={":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0"})
    # center of screen maps to offset (=0) time
    assert sc.time_to_screen_x(s, 0.0) == sc._SCREEN_CENTER
    # round-trip a representative time
    t = 0.002
    x = sc.time_to_screen_x(s, t)
    assert sc.screen_x_to_time(s, x) == pytest.approx(t, abs=1e-4)


def test_time_to_screen_x_clamps_to_range():
    s = FakeScope(responses={":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0"})
    assert sc.time_to_screen_x(s, +10.0) == 594   # far right clamps
    assert sc.time_to_screen_x(s, -10.0) == 5     # far left clamps


# --------------------------------------------------------------------------- check_scpi_error

@pytest.mark.parametrize("resp,expected", [
    ("0", None),
    ('0,"No error"', None),
    ('-113,"Undefined header"', '-113,"Undefined header"'),
])
def test_check_scpi_error(resp, expected):
    s = FakeScope(responses={":SYSTem:ERRor?": resp})
    assert sc.check_scpi_error(s) == expected


# --------------------------------------------------------------------------- measure validation

def test_measure_valid():
    s = FakeScope(responses={":MEASure:ITEM?": "1.234000e+00", ":CHAN1:DISP?": "1"})
    assert sc.measure(s, "chan1", "vpp") == "1.234000e+00"
    assert ":MEASure:ITEM VPP,CHAN1" in s.written


def test_measure_rejects_two_source_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="two sources"):
        sc.measure(s, "CHAN1", "RDELAY")


def test_measure_rejects_unknown_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="Unknown item"):
        sc.measure(s, "CHAN1", "BOGUS")


def test_measure_between_rejects_single_source_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="not a valid two-source item"):
        sc.measure_between(s, "CHAN1", "CHAN2", "VPP")


# ----------------------------------------------- disabled-channel auto-enable & 9.9E37 sentinel

def test_measure_auto_enables_disabled_channel(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    s = FakeScope(responses={":CHAN2:DISP?": "0", ":SYSTem:ERRor?": "0",
                             ":MEASure:ITEM?": "1.000000e+00"})
    out = sc.measure(s, "CHAN2", "VAVG")
    assert ":CHAN2:DISP ON" in s.written
    assert out.startswith("1.000000e+00")
    assert "CHAN2 display was OFF" in out
    assert "auto-enabled" in out


def test_measure_enabled_channel_not_toggled():
    s = FakeScope(responses={":CHAN1:DISP?": "1", ":MEASure:ITEM?": "1.234000e+00"})
    out = sc.measure(s, "CHAN1", "VPP")
    assert out == "1.234000e+00"
    assert not any("DISP ON" in c for c in s.written)


def test_measure_annotates_invalid_sentinel():
    s = FakeScope(responses={":CHAN1:DISP?": "1", ":MEASure:ITEM?": "9.900000e+37"})
    out = sc.measure(s, "CHAN1", "FREQUENCY")
    assert out.startswith("9.900000e+37")
    assert "invalid/overflow sentinel" in out
    assert "auto-enabled" not in out


def test_measure_auto_enable_scpi_error_raises(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    s = FakeScope(responses={":CHAN2:DISP?": "0",
                             ":SYSTem:ERRor?": '-113,"Undefined header"'})
    with pytest.raises(RuntimeError, match="enabling CHAN2 display"):
        sc.measure(s, "CHAN2", "VAVG")


def test_measure_between_enables_only_disabled_source(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    s = FakeScope(responses={":CHAN1:DISP?": "1", ":CHAN2:DISP?": "0",
                             ":SYSTem:ERRor?": "0", ":MEASure:ITEM?": "1.0e-06"})
    out = sc.measure_between(s, "CHAN1", "CHAN2", "RDELAY")
    assert ":CHAN2:DISP ON" in s.written
    assert ":CHAN1:DISP ON" not in s.written
    assert "CHAN2 display was OFF" in out
    assert "CHAN1 display" not in out


def test_get_waveform_auto_enables_before_source_select(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    pre = "0,0,3,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":CHAN1:DISP?": "0", ":SYSTem:ERRor?": "0", ":WAV:PRE?": pre,
                   ":CHAN1:SCAL?": "1.0", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"0.1,0.2,0.3"),
    )
    out = sc.get_waveform(s, "CHAN1")
    assert s.written.index(":CHAN1:DISP ON") < s.written.index(":WAV:SOUR CHAN1")
    assert any("auto-enabled" in w for w in out["warnings"])


def test_get_waveform_enabled_channel_no_warnings():
    pre = "0,0,3,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":CHAN1:DISP?": "1", ":WAV:PRE?": pre,
                   ":CHAN1:SCAL?": "1.0", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"0.1,0.2,0.3"),
    )
    out = sc.get_waveform(s, "CHAN1")
    assert out["warnings"] == []


def test_get_waveform_supports_displayed_dho_math_source(monkeypatch):
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,DHO8A000000000,00.01.05",
        ":MATH1:DISPlay?": "1",
        ":WAV:PRE?": "0,0,4,1,1e-6,0,0,0,0,0",
        ":WAV:DATA?": "0,1,0,-1",
        ":MATH1:SCAL?": "1",
        ":MATH1:OFFS?": "0",
        ":ACQuire:SRATe?": "1.25e9",
    })
    monkeypatch.setattr(sc, "_driver", None)
    result = sc.get_waveform(instrument, "MATH1")
    assert result["channel"] == "MATH1"
    assert result["acquisition_sample_rate_hz"] == 1.25e9
    assert result["displayed_sample_rate_hz"] == 1e6
    assert result["analysis_nyquist_hz"] == 500_000


def test_get_waveform_flags_sentinel_samples():
    pre = "0,0,3,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":CHAN1:DISP?": "1", ":WAV:PRE?": pre,
                   ":CHAN1:SCAL?": "1.0", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"9.9e37,0.2,0.3"),
    )
    out = sc.get_waveform(s, "CHAN1")
    assert any("invalid-sentinel" in w for w in out["warnings"])


def test_get_waveform_empty_payload_raises_clear_error(monkeypatch):
    # A channel with no acquired data (e.g. just auto-enabled on a stopped scope) returns
    # an empty ASCII payload — that must surface as a clear error, not float('').
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    monkeypatch.setattr(sc, "_WAVEFORM_DATA_RETRY_S", 0.0)  # skip the live polling window
    pre = "0,0,0,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":CHAN2:DISP?": "0", ":SYSTem:ERRor?": "0", ":WAV:PRE?": pre},
        read_buffer=make_block(b""),
    )
    with pytest.raises(RuntimeError, match="no waveform data") as exc:
        sc.get_waveform(s, "CHAN2")
    assert "auto-enabled" in str(exc.value)  # the auto-enable context is carried along


def test_get_waveform_empty_payload_drains_and_reports_scope_error(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    monkeypatch.setattr(sc, "_WAVEFORM_DATA_RETRY_S", 0.0)
    errors = ['-200,"Command execute failed"']
    pre = "0,0,0,1,1.000000e-06,0,0,1,0,0"
    instrument = FakeScope(
        responses={
            ":CHAN1:DISP?": "1",
            ":SYSTem:ERRor?": lambda: errors.pop(0) if errors else '0,"No error"',
            ":WAV:PRE?": pre,
        },
        read_buffer=make_block(b""),
    )

    with pytest.raises(RuntimeError, match='Scope error: -200,"Command execute failed"'):
        sc.get_waveform(instrument, "CHAN1")

    assert sc.check_scpi_error(instrument) is None


def test_get_waveform_empty_payload_reports_active_dho_decoder(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    monkeypatch.setattr(sc, "_WAVEFORM_DATA_RETRY_S", 0.0)
    errors = ['-200,"Command execute failed"']
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,00.01.05",
        ":CHAN1:DISP?": "1", ":WAV:PRE?": "0,0,0,1,1e-6,0,0,1,0,0",
        ":WAV:DATA?": "", ":SYSTem:ERRor?": lambda: errors.pop(0) if errors else "0",
        ":BUS1:DISPlay?": "1", ":BUS2:DISPlay?": "0",
        ":BUS3:DISPlay?": "0", ":BUS4:DISPlay?": "0",
    })

    with pytest.raises(RuntimeError, match="decoder overlays BUS1") as exc:
        sc.get_waveform(instrument, "CHAN1")

    assert "download_waveform with RAW mode" in str(exc.value)


def test_get_waveform_refreshes_zero_increment_preamble():
    # Right after a channel is enabled the scope reports 0 s/point in :WAV:PRE? until the
    # first sweep completes — get_waveform must re-read the preamble once data exists.
    pres = iter(["0,0,3,1,0,0,0,1,0,0", "0,0,3,1,1.000000e-06,0,0,1,0,0"])
    s = FakeScope(
        responses={":CHAN1:DISP?": "1", ":WAV:PRE?": lambda: next(pres),
                   ":CHAN1:SCAL?": "1.0", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"0.1,0.2,0.3"),
    )
    out = sc.get_waveform(s, "CHAN1")
    assert out["time_increment_s"] == 1e-6
    assert out["time_end_s"] == pytest.approx(2e-6)


def test_annotate_measurement_value_passthrough():
    assert sc.annotate_measurement_value("1.5e+03") == "1.5e+03"
    assert sc.annotate_measurement_value("not-a-number") == "not-a-number"
    assert "sentinel" in sc.annotate_measurement_value("-9.9e37")


# --------------------------------------------------------------------------- autoscale timeout guard

def test_autoscale_raises_timeout_during_call_and_restores():
    seen = {}

    class TimeoutRecordingScope(FakeScope):
        def query(self, cmd):
            if cmd.startswith(":AUToscale"):
                seen["timeout_during"] = self.timeout
            return super().query(cmd)

    s = TimeoutRecordingScope(responses={
        ":AUToscale;*OPC?": "1",
        ":SYSTem:ERRor?": "0",
    })
    s.timeout = 1000
    sc.autoscale(s)
    assert seen["timeout_during"] == sc._SLOW_OP_TIMEOUT_MS   # bumped for the slow op
    assert s.timeout == 1000


# --------------------------------------------------------------------------- dialect drivers

_DHO_IDN = "RIGOL TECHNOLOGIES,DHO924S,DHO9A000000000,00.01.05"


def test_dho814_capabilities_and_acrms():
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0",
        ":CHAN1:DISP?": "1", ":MEASure:ITEM? ACRMS,CHAN1": "0.5",
    })
    capabilities = sc.get_capabilities(instrument)
    assert capabilities["horizontal_divisions"] == 10
    assert capabilities["external_trigger"] is False
    assert capabilities["command_catalog"] is True
    assert len(capabilities["measurement_items"]) == 34
    assert set(capabilities["evidence"]) == capabilities.keys() - {"evidence"}
    assert capabilities["evidence"]["model"]["status"] == "hardware-verified"
    assert capabilities["evidence"]["channels"]["status"] == "documented"
    assert capabilities["evidence"]["measurement_items"]["status"] == "documented"
    assert sc.measure(instrument, "CHAN1", "ACRMS") == "0.5"


@pytest.mark.parametrize("channels,divisions", [(4, 10), (2, 12)])
def test_capability_verification_reports_live_values_and_mismatches(channels, divisions):
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0", ":SYSTem:ERRor?": "0",
        ":SYSTem:RAMount?": str(channels), ":SYSTem:GAMount?": str(divisions),
    })
    result = sc.get_capabilities(instrument, verify_hardware=True)
    assert len(result["channels"]) == channels
    assert result["horizontal_divisions"] == divisions
    for field in ("channels", "horizontal_divisions"):
        assert result["evidence"][field]["status"] == "hardware-verified"
        assert result["evidence"][field].get("mismatch", False) == (channels != 4)
    assert result["evidence"]["external_trigger"]["status"] == "documented"
    assert instrument.written == []


@pytest.mark.parametrize("response,error", [("invalid", "0"), ("0", "0"), ("4", '-113,"Undefined header"')])
def test_failed_capability_probe_is_not_hardware_verified(response, error):
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0", ":SYSTem:ERRor?": error,
        ":SYSTem:RAMount?": response, ":SYSTem:GAMount?": "10",
    })
    result = sc.get_capabilities(instrument, verify_hardware=True)
    assert len(result["channels"]) == 4
    assert result["evidence"]["channels"]["status"] == "unverified"
    assert result["evidence"]["channels"]["fallback"] == "model definition"


def test_unknown_dho_model_does_not_inherit_verified_capabilities():
    instrument = FakeScope(responses={"*IDN?": "RIGOL TECHNOLOGIES,DHO9999,SN,1.0"})
    result = sc.get_capabilities(instrument, verify_hardware=True)
    assert result["evidence"]["model"]["status"] == "hardware-verified"
    assert result["evidence"]["channels"]["status"] == "unverified"
    assert result["evidence"]["external_trigger"]["status"] == "unverified"


def test_capability_transport_error_propagates():
    def disconnected():
        raise OSError("connection reset")

    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0", ":SYSTem:RAMount?": disconnected,
    })
    with pytest.raises(OSError, match="connection reset"):
        sc.get_capabilities(instrument, verify_hardware=True)


def test_dho814_rejects_external_trigger_before_writes():
    instrument = FakeScope(responses={"*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0"})
    with pytest.raises(ValueError, match="not supported by DHO814"):
        sc.set_trigger(instrument, source="EXT")
    assert instrument.written == []


def test_dho812_has_two_channels_and_external_trigger():
    capabilities = drivers.capabilities_for("RIGOL TECHNOLOGIES,DHO812,SN,1.0")
    assert capabilities["channels"] == ["CHAN1", "CHAN2"]
    assert capabilities["external_trigger"] is True


def test_get_driver_detects_dho_and_caches():
    s = FakeScope(responses={"*IDN?": _DHO_IDN})
    assert sc.get_driver(s).name == "DHO"
    # Result is cached: changing the reported identity does not change the answer until
    # invalidate_scope() resets it.
    s.responses["*IDN?"] = FakeScope.DEFAULT_IDN
    assert sc.get_driver(s).name == "DHO"


def test_get_driver_defaults_to_ds1000z():
    s = FakeScope()  # DEFAULT_IDN is a DS1054Z
    assert sc.get_driver(s).name == "DS1000Z"


def test_driver_for_recognises_families_and_rejects_unknown():
    assert drivers.driver_for("RIGOL TECHNOLOGIES,DHO924S,SN,1.0").name == "DHO"
    assert drivers.driver_for("RIGOL TECHNOLOGIES,DS1054Z,SN,1.0").name == "DS1000Z"
    assert drivers.driver_for("RIGOL TECHNOLOGIES,MSO1104Z,SN,1.0").name == "DS1000Z"
    # An identity no driver claims is an error, not a silent guess.
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        drivers.driver_for("RIGOL TECHNOLOGIES,XYZ9000,SN,1.0")


def test_autoscale_dho_uses_autoset():
    s = FakeScope(responses={"*IDN?": _DHO_IDN, "*OPC?": "1", ":SYSTem:ERRor?": "0"})
    s.timeout = sc._LAN_TIMEOUT_MS
    sc.autoscale(s)
    assert ":AUToset" in s.written
    assert not any(c.startswith(":AUToscale") for c in s.written)


def test_screenshot_png_dho_uses_single_param():
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
    s = FakeScope(responses={"*IDN?": _DHO_IDN}, read_buffer=make_block(png))
    assert sc.screenshot_png(s) == png
    assert ":DISPlay:DATA? PNG" in s.written
    assert ":DISPlay:DATA? ON,OFF,PNG" not in s.written


def test_screenshot_png_ds1000z_uses_three_param():
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
    s = FakeScope(read_buffer=make_block(png))  # DS1000Z default identity
    assert sc.screenshot_png(s) == png
    assert ":DISPlay:DATA? ON,OFF,PNG" in s.written


def test_get_waveform_dho_sets_point_range():
    pre = "0,0,2,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(responses={
        "*IDN?":         _DHO_IDN,
        ":WAV:PRE?":     pre,
        ":WAV:DATA?":    "0.1,0.2",  # DHO sends bare CSV — no block header
        ":CHAN1:DISP?":  "1",
        ":CHAN1:SCAL?":  "0.5",
        ":CHAN1:OFFS?":  "0",
    })
    sc.get_waveform(s, "CHAN1")
    assert ":WAV:STAR 1" in s.written
    assert ":WAV:STOP 1000" in s.written


def test_get_waveform_dho_math_omits_unsupported_point_range():
    pre = "0,0,2,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(responses={
        "*IDN?": _DHO_IDN,
        ":WAV:PRE?": pre,
        ":WAV:DATA?": "3.2,3.3",
        ":MATH1:DISPlay?": "1",
        ":MATH1:SCAL?": "1.0",
        ":MATH1:OFFS?": "0",
    })

    out = sc.get_waveform(s, "MATH1")

    assert out["voltages_v"] == [3.2, 3.3]
    assert not any(command.startswith(":WAV:STAR") for command in s.written)
    assert not any(command.startswith(":WAV:STOP") for command in s.written)


def test_get_waveform_ds1000z_omits_point_range():
    pre = "0,0,2,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":WAV:PRE?": pre, ":CHAN1:DISP?": "1",
                   ":CHAN1:SCAL?": "0.5", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"0.1,0.2"),
    )
    sc.get_waveform(s, "CHAN1")
    assert not any(c.startswith(":WAV:STAR") for c in s.written)


def test_get_waveform_ds1000z_uses_block_reader():
    """Regression: DS1000Z must use the TCP byte-count reader, not a terminator
    read. Verifies the driver dispatches to ``_read_definite_block``: the test relies on
    the fake's ``read_buffer`` path (used by read_bytes / read_raw) rather than the
    ``:WAV:DATA?`` responses dict (used by scope.query)."""
    pre = "0,0,3,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(
        responses={":WAV:PRE?": pre, ":CHAN1:DISP?": "1",
                   ":CHAN1:SCAL?": "1.0", ":CHAN1:OFFS?": "0"},
        read_buffer=make_block(b"1.5,-2.5,3.0"),
    )
    out = sc.get_waveform(s, "CHAN1")
    assert out["voltages_v"] == [1.5, -2.5, 3.0]
    # The driver dispatches via :WAV:DATA?; the response comes back through the byte-count
    # block reader. Confirm the SCPI write was issued.
    assert ":WAV:DATA?" in s.written


def test_get_waveform_accepts_dho_bare_csv():
    """Regression: DHO returns ASCII waveforms as bare CSV with no block header. The reader
    must not insist on a '#' framing — historically it did, which on DHO consumed 2 bytes,
    raised on the missing header, and left the remaining CSV in the socket buffer for the
    next command to misread (the cause of the spurious -200 errors)."""
    pre = "0,0,3,1,1.000000e-06,0,0,1,0,0"
    s = FakeScope(responses={
        "*IDN?":         _DHO_IDN,
        ":WAV:PRE?":     pre,
        ":WAV:DATA?":    "1.5,-2.5,3.0",  # bare CSV — no block header
        ":CHAN1:DISP?":  "1",
        ":CHAN1:SCAL?":  "1.0",
        ":CHAN1:OFFS?":  "0",
    })
    out = sc.get_waveform(s, "CHAN1")
    assert out["voltages_v"] == [1.5, -2.5, 3.0]


def test_measure_dho_uses_statistic_item():
    s = FakeScope(responses={"*IDN?": _DHO_IDN, ":MEASure:ITEM?": "1.234000e+00",
                             ":CHAN1:DISP?": "1"})
    assert sc.measure(s, "CHAN1", "vpp") == "1.234000e+00"
    assert ":MEASure:STATistic:ITEM VPP,CHAN1" in s.written
    assert ":MEASure:ITEM VPP,CHAN1" not in s.written


def test_measure_between_dho_maps_ds1000z_names():
    s = FakeScope(responses={"*IDN?": _DHO_IDN, ":MEASure:ITEM?": "1.0e-06",
                             ":CHAN1:DISP?": "1", ":CHAN2:DISP?": "1"})
    sc.measure_between(s, "CHAN1", "CHAN2", "RDELAY")
    # DS1000Z RDELAY auto-maps to the DHO rise-to-rise item via STATistic registration.
    assert ":MEASure:STATistic:ITEM RRDELAY,CHAN1,CHAN2" in s.written


def test_measure_between_dho_accepts_native_matrix_item():
    s = FakeScope(responses={"*IDN?": _DHO_IDN, ":MEASure:ITEM?": "1.0e-06",
                             ":CHAN1:DISP?": "1", ":CHAN2:DISP?": "1"})
    sc.measure_between(s, "CHAN1", "CHAN2", "RFDELAY")
    assert ":MEASure:STATistic:ITEM RFDELAY,CHAN1,CHAN2" in s.written


def test_measure_between_dho_rejects_unknown_item():
    s = FakeScope(responses={"*IDN?": _DHO_IDN})
    with pytest.raises(ValueError, match="not a valid two-source item for DHO"):
        sc.measure_between(s, "CHAN1", "CHAN2", "BOGUS")


def test_set_cursor_positions_dho_uses_seconds():
    s = FakeScope(responses={"*IDN?": _DHO_IDN, ":SYSTem:ERRor?": "0"})
    sc.set_cursor_positions(s, mode="MANUAL", ax=0.001, bx=0.002)
    assert ":CURSor:MANual:CAX 0.001" in s.written
    assert ":CURSor:MANual:CBX 0.002" in s.written


def test_dho_manual_cursor_sources_type_and_y_readouts():
    instrument = FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0", ":SYSTem:ERRor?": "0",
        ":CURSor:MODE?": "MAN", ":CURSor:MANual:SOURce?": "CHAN2",
        ":CURSor:MANual:TYPE?": "AMPL", ":CURSor:MANual:": "0.5",
    })
    result = sc.configure_cursors(instrument, mode="MANUAL", source="CHAN2",
                                  cursor_type="AMPLITUDE", ay=0.1, by=0.2)
    assert result["source"] == "CHAN2"
    assert result["cursor_type"] == "AMPL"
    assert result["AY_value"] == "0.5"
    assert result["delta_y"] == "0.5"
    assert ":CURSor:MANual:SOURce CHAN2" in instrument.written
    assert ":CURSor:MANual:CAY 0.1" in instrument.written


def test_cursor_invalid_mode_settings_do_not_write():
    instrument = FakeScope(responses={"*IDN?": _DHO_IDN})
    with pytest.raises(ValueError, match="require TRACK"):
        sc.configure_cursors(instrument, mode="MANUAL", source_a="CHAN1")
    assert instrument.written == []


def test_set_cursor_positions_ds1000z_uses_pixels():
    s = FakeScope(responses={":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0",
                             ":SYSTem:ERRor?": "0"})
    sc.set_cursor_positions(s, mode="MANUAL", ax=0.0)  # screen-centre time
    assert f":CURSor:MANual:AX {sc._SCREEN_CENTER}" in s.written


def test_set_channel_writes_probe_before_scale_and_formats_g():
    s = FakeScope(responses={":SYSTem:ERRor?": "0"})
    sc.set_channel(s, "CHAN1", scale=0.5, probe=10.0)
    # PROBe must precede SCALe (probe attenuation rescales V/div), and 10.0 -> "10".
    assert ":CHAN1:PROB 10" in s.written
    assert ":CHAN1:PROB 10.0" not in s.written
    assert s.written.index(":CHAN1:PROB 10") < s.written.index(":CHAN1:SCAL 0.5")


def test_check_scpi_error_drains_queue_returns_first():
    class DrainScope(FakeScope):
        def __init__(self, errors):
            super().__init__()
            self._errs = list(errors)

        def query(self, cmd):
            if cmd.strip() == ":SYSTem:ERRor?":
                return self._errs.pop(0) if self._errs else "0"
            return super().query(cmd)

    s = DrainScope(['-113,"Undefined header"', '-222,"Data out of range"', "0"])
    assert sc.check_scpi_error(s) == '-113,"Undefined header"'
    assert s._errs == []  # queue fully drained, not left for the next tool call

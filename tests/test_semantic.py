"""Offline tests for DHO814 semantic catalog operations."""

import pytest

from rigol_mcp import semantic
from tests.conftest import FakeScope, make_block


DHO_IDN = "RIGOL TECHNOLOGIES,DHO814,DHO8A000000000,00.01.05"


def _scope(responses=None):
    return FakeScope(responses={
        "*IDN?": DHO_IDN,
        ":SYSTem:ERRor?": "0,No error",
        **(responses or {}),
    })


def test_configure_acquisition_returns_readback_and_one_write_per_setting():
    instrument = _scope({
        ":ACQuire:TYPE?": "AVER",
        ":ACQuire:AVERages?": "16",
        ":ACQuire:SRATe?": "1.25E9",
    })
    result = semantic.configure_acquisition(
        instrument, acquisition_type="AVERAGES", averages=16,
    )
    assert result == {
        "requested": {"acquisition_type": "AVERAGES", "averages": 16},
        "applied": {"acquisition_type": "AVER", "averages": "16", "sample_rate_hz": "1.25E9"},
        "changed": False,
        "fully_applied": True,
        "mismatches": {},
    }
    assert instrument.written == [":ACQuire:TYPE AVERAGES", ":ACQuire:AVERages 16"]


def test_configure_reports_scope_clamped_values():
    threshold_values = iter(["0.0", "0.45"])
    instrument = _scope({
        ":SEARch:MODE?": "EDGE",
        ":SEARch:EDGE:THReshold?": lambda: next(threshold_values),
    })

    result = semantic.configure_search(
        instrument, mode="EDGE", threshold_v=1.65,
    )

    assert result["fully_applied"] is False
    assert result["mismatches"] == {
        "threshold_v": {"requested": 1.65, "applied": "0.45"},
    }


def test_configure_attributes_error_to_exact_write():
    errors = iter(['0,"No error"', '-200,"Command execute failed"', '0,"No error"'])
    instrument = _scope({
        ":SEARch:MODE?": "EDGE",
        ":SEARch:EDGE:SOURce?": "CHAN1",
        ":SYSTem:ERRor?": lambda: next(errors),
    })

    with pytest.raises(RuntimeError, match="after :SEARch:EDGE:SOURce"):
        semantic.configure_search(instrument, mode="EDGE", source="CHAN1")

    assert instrument.written == [":SEARch:MODE EDGE", ":SEARch:EDGE:SOURce CHANNEL1"]


@pytest.mark.parametrize(("requested", "applied"), [
    ("SUBTract", "SUBT"),
    ("DIFFERENTIAL", "DIFF"),
    ("SINGLE", "SING"),
    ("FORWARD", "FORW"),
    ("LPASs", "LPAS"),
])
def test_readback_matches_dho_abbreviations(requested, applied):
    assert semantic._readback_matches(requested, applied)


def test_configure_math_filter_reports_clamped_cutoff():
    instrument = _scope({
        ":MATH1:FILTer:TYPE?": "LPAS",
        ":MATH1:FILTer:W1?": "1.5625E6",
    })
    result = semantic.configure_math(
        instrument, math_channel=1,
        filter={"type": "LPASs", "cutoff1_hz": 5000},
    )
    assert instrument.written == [
        ":MATH1:FILTer:TYPE LPASS", ":MATH1:FILTer:W1 5000",
    ]
    assert result["applied"]["filter_cutoff1_hz"] == "1.5625E6"
    assert "scope applied 1.5625e+06 Hz" in result["warnings"][0]


def test_invalid_decode_setting_performs_no_io():
    instrument = _scope()
    with pytest.raises(ValueError, match="Unsupported RS232"):
        semantic.configure_decode(
            instrument, bus=1, protocol="RS232", settings={"mystery": 1},
        )
    assert instrument.written == []


def test_rs232_decode_configures_threshold_and_accepts_ascii_abbreviation():
    instrument = _scope({
        ":BUS1:MODE?": "RS232", ":BUS1:DISPlay?": "1", ":BUS1:FORMat?": "ASC",
        ":BUS1:RS232:TX?": "CHAN1", ":BUS1:RS232:BAUD?": "115200",
        ":BUS1:THReshold? TX": "1.650000E0",
    })

    result = semantic.configure_decode(
        instrument, bus=1, protocol="RS232", display=True, format="ASCII",
        settings={"tx": "CHAN1", "baud": 115200, "thresholds_v": {"TX": 1.65}},
    )

    assert ":BUS1:THReshold 1.65,TX" in instrument.written
    assert result["fully_applied"] is True
    assert result["applied"]["tx_threshold_v"] == "1.650000E0"


def test_decode_readback_accepts_i2c_and_timeout_abbreviations():
    assert semantic._readback_matches("I2C", "IIC")
    assert semantic._readback_matches("TIMEOUT", "TIM")
    assert semantic._readback_matches("START", "STAR")


def test_can_decode_uses_validated_can_fields():
    instrument = _scope({
        ":BUS1:MODE?": "CAN", ":BUS1:CAN:SOURce?": "CHAN1",
        ":BUS1:CAN:STYPe?": "DIFF", ":BUS1:CAN:BAUD?": "500000",
    })
    result = semantic.configure_decode(
        instrument, bus=1, protocol="CAN",
        settings={"source": "CHAN1", "signal_type": "DIFFERENTIAL", "baud": 500000},
    )
    assert result["applied"]["protocol"] == "CAN"
    assert ":BUS1:CAN:BAUD 500000" in instrument.written


def test_get_decode_result_reads_complete_binary_block():
    instrument = _scope({
        ":BUS1:MODE?": "RS232",
        ":BUS1:DISPlay?": "1",
        ":BUS1:FORMat?": "HEX",
    })
    instrument.load(make_block(b"RS232,0,55,AA"))

    result = semantic.get_decode_result(instrument, 1)

    assert result["data"] == "RS232,0,55,AA"
    assert instrument.written == [":BUS1:DATA?"]


def test_configure_meter_and_read_value():
    instrument = _scope({
        ":DVM:ENABle?": "1", ":DVM:SOURce?": "CHAN1", ":DVM:MODE?": "DC",
        ":DVM:CURRent?": "1.25",
    })
    result = semantic.configure_meter(
        instrument, meter="DVM", enabled=True, source="CHAN1", mode="DC",
    )
    assert result["applied"] == {"enabled": "1", "source": "CHAN1", "mode": "DC"}
    assert semantic.get_meter_value(instrument, "DVM")["value"] == 1.25


def test_measure_statistics_returns_structured_values_and_rejects_sentinel():
    instrument = _scope({
        ":MEASure:STATistic:ITEM? CURRENT,VPP,CHANNEL1": "2.5",
        ":MEASure:STATistic:ITEM? MAXIMUM,VPP,CHANNEL1": "9.9E37",
    })
    result = semantic.measure_statistics(
        instrument, item="VPP", source1="CHAN1", statistics=["CURRENT", "MAXIMUM"],
    )
    assert instrument.written == [":MEASure:STATistic:ITEM VPP,CHANNEL1"]
    assert result["statistics"]["current"] == {"value": 2.5, "raw": "2.5", "valid": True}
    assert result["statistics"]["maximum"]["valid"] is False


def test_measure_statistics_defaults_map_to_catalog_tokens():
    instrument = _scope({
        ":MEASure:STATistic:ITEM? CURRENT,PERIOD,CHANNEL1": "0.001",
        ":MEASure:STATistic:ITEM? AVERAGES,PERIOD,CHANNEL1": "0.001",
        ":MEASure:STATistic:ITEM? MINIMUM,PERIOD,CHANNEL1": "0.000999",
        ":MEASure:STATistic:ITEM? MAXIMUM,PERIOD,CHANNEL1": "0.001001",
        ":MEASure:STATistic:ITEM? DEVIATION,PERIOD,CHANNEL1": "1e-9",
        ":MEASure:STATistic:ITEM? CNT,PERIOD,CHANNEL1": "128",
    })
    result = semantic.measure_statistics(instrument, item="PERIOD", source1="CHAN1")
    assert set(result["statistics"]) == {
        "current", "averages", "minimum", "maximum", "deviation", "count",
    }
    assert result["statistics"]["count"] == {"value": 128.0, "raw": "128", "valid": True}


def test_mask_configuration_and_structured_results():
    instrument = _scope({
        ":MASK:ENABle?": "1", ":MASK:SOURce?": "CHAN1", ":MASK:X?": "0.1",
        ":MASK:FAILed?": "2", ":MASK:PASSed?": "18", ":MASK:TOTal?": "20",
        ":MASK:OPERate?": "RUN",
    })
    semantic.configure_mask_test(instrument, enabled=True, source="CHAN1", horizontal_tolerance=0.1)
    result = semantic.get_mask_results(instrument)
    assert result["failure_ratio"] == 0.1
    assert result["passed"] == 18


def test_mask_create_enables_and_stops_before_creation_then_runs():
    instrument = _scope({
        ":MASK:ENABle?": "1",
        ":MASK:OPERate?": "RUN",
    })

    result = semantic.configure_mask_test(instrument, create=True, running=True)

    assert result["requested"]["enabled"] is True
    assert instrument.written == [
        ":MASK:ENABle ON",
        ":MASK:OPERate STOP",
        ":MASK:CREate",
        ":MASK:OPERate RUN",
    ]


def test_mask_create_rejects_disabled_state_before_io():
    instrument = _scope()

    with pytest.raises(ValueError, match="requires enabled=true"):
        semantic.configure_mask_test(instrument, create=True, enabled=False)

    assert instrument.written == []


def test_search_results_are_bounded_and_paginated():
    instrument = _scope({
        ":SEARch:COUNt?": "3", ":SEARch:VALue? 2": "0.002", ":SEARch:VALue? 3": "0.003",
    })
    result = semantic.get_search_results(instrument, offset=1, limit=2)
    assert [event["index"] for event in result["events"]] == [2, 3]
    assert result["next_offset"] is None


def test_recording_configuration_and_replay_navigation():
    frame_values = iter(["3", "4"])
    instrument = _scope({
        ":RECord:WRECord:ENABle?": "1", ":RECord:WRECord:FRAMes?": "100",
        ":RECord:WRECord:FINTerval?": "0.1",
        ":RECord:WREPlay:FCURrent?": lambda: next(frame_values),
    })
    configured = semantic.configure_recording(
        instrument, enabled=True, frames=100, interval_s=0.1,
    )
    assert configured["applied"]["frames"] == "100"
    navigated = semantic.control_recording_replay(instrument, action="NEXT")
    assert instrument.written[-1] == ":RECord:WREPlay:NEXT"
    assert navigated["applied"]["current_frame"] == "4"
    assert navigated["changed"] is True


def test_recording_select_requires_frame_before_io():
    instrument = _scope()
    with pytest.raises(ValueError, match="requires frame"):
        semantic.control_recording_replay(instrument, action="SELECT")
    assert instrument.written == []


def test_configure_advanced_trigger_uses_type_specific_commands():
    instrument = _scope({
        ":TRIGger:MODE?": "PULSE", ":TRIGger:PULSe:SOURce?": "CHAN1",
        ":TRIGger:PULSe:WHEN?": "LESS", ":TRIGger:PULSe:UWIDth?": "1E-6",
    })
    result = semantic.configure_trigger(
        instrument, trigger_type="PULSE",
        settings={"source": "CHAN1", "condition": "LESS", "upper_s": 1e-6},
    )
    assert result["applied"]["type"] == "PULSE"
    assert instrument.written == [
        ":TRIGger:MODE PULSE", ":TRIGger:PULSe:SOURce CHANNEL1",
        ":TRIGger:PULSe:WHEN LESS", ":TRIGger:PULSe:UWIDth 1e-06",
    ]


def test_can_protocol_trigger_uses_existing_trigger_api():
    instrument = _scope({
        ":TRIGger:MODE?": "CAN", ":TRIGger:CAN:SOURce?": "CHAN1",
        ":TRIGger:CAN:BAUD?": "500000", ":TRIGger:CAN:WHEN?": "SOF",
    })
    result = semantic.configure_trigger(
        instrument, trigger_type="CAN",
        settings={"source": "CHAN1", "baud": 500000, "condition": "SOF"},
    )
    assert result["applied"]["type"] == "CAN"
    assert ":TRIGger:CAN:WHEN SOF" in instrument.written


def test_pattern_trigger_prevalidates_four_channel_states():
    instrument = _scope()
    with pytest.raises(ValueError, match="four channel states"):
        semantic.configure_trigger(
            instrument, trigger_type="PATTERN", settings={"pattern": ["H", "L"]},
        )
    assert instrument.written == []


def test_reference_uses_slot_first_for_writes_and_slot_only_for_queries():
    instrument = _scope({
        ":REFerence:SOURce? 2": "CHAN1", ":REFerence:COLor? 2": "BLUE",
    })
    result = semantic.configure_reference(
        instrument, slot=2, source="CHAN1", color="BLUE",
    )
    assert result["applied"] == {"source": "CHAN1", "color": "BLUE"}
    assert instrument.written == [
        ":REFerence:SOURce 2,CHANNEL1", ":REFerence:COLor 2,BLUE",
    ]


def test_histogram_reports_dho814_exclusion_without_writing():
    instrument = _scope()
    result = semantic.histogram_capability(instrument)
    assert result["supported"] is False
    assert result["model"] == "DHO814"
    assert instrument.written == []


def test_timing_capture_derives_scale_and_configures_mixed_voltage_domains():
    instrument = _scope({":": "1"})
    result = semantic.configure_timing_capture(
        instrument,
        channels=[
            {"channel": 1, "label": "SOURCE", "voltage_domain_v": 3.3},
            {"channel": 2, "label": "BUFFERED", "voltage_domain_v": 5},
        ],
        trigger={"channel": 1, "slope": "POS"},
        signal_frequency_hz=1_000_000,
    )
    assert result["requested"]["time_scale_s_div"] == 200e-9
    assert any(command == ":TIMebase:HREFerence:POSition 40" for command in instrument.written)
    assert any(command == ":ACQuire:TYPE NORMAL" for command in instrument.written)
    assert ":CHANnel1:SCALe 1" in instrument.written
    assert ":CHANnel2:SCALe 2" in instrument.written
    assert ":TRIGger:EDGE:LEVel 1.65" in instrument.written


def test_single_shot_capture_uses_channel_count_depth_and_can_disable_unlisted():
    instrument = _scope({":": "1"})
    semantic.configure_timing_capture(
        instrument,
        channels=[{"channel": 2, "label": "EVENT", "voltage_domain_v": 5}],
        trigger={"channel": 2, "slope": "NEG", "level_v": 2.0},
        mode="SINGLE_SHOT", time_scale_s_div=50e-9, disable_unlisted=True,
    )
    assert ":ACQuire:MDEPth 25M" in instrument.written
    assert ":TRIGger:SWEep SINGLE" in instrument.written
    assert ":CHANnel1:DISPlay OFF" in instrument.written
    assert ":CHANnel3:DISPlay OFF" in instrument.written
    assert ":CHANnel4:DISPlay OFF" in instrument.written


def test_timing_capture_invalid_trigger_performs_no_io():
    instrument = _scope()
    with pytest.raises(ValueError, match="configured channels"):
        semantic.configure_timing_capture(
            instrument,
            channels=[{"channel": 1, "label": "CLOCK", "voltage_domain_v": 3.3}],
            trigger={"channel": 2}, time_scale_s_div=1e-6,
        )
    assert instrument.written == []


def test_stopped_timing_capture_runs_for_timebase_then_restores_stop():
    instrument = _scope({
        ":": "1",
        ":TRIGger:STATus?": "STOP",
        ":TIMebase:MAIN:SCALe?": "4.000000E-4",
        ":TIMebase:DELay:ENABle?": "0",
        ":TIMebase:MAIN:OFFSet?": "0",
        ":ACQuire:TYPE?": "NORM",
        ":TRIGger:EDGE:SOURce?": "CHAN1",
        ":TRIGger:EDGE:SLOPe?": "POS",
        ":TRIGger:EDGE:LEVel?": "1.65",
        ":TRIGger:SWEep?": "NORM",
        ":TIMebase:HREFerence:POSition?": "40",
        ":CHANnel1:COUPling?": "DC",
        ":CHANnel1:PROBe?": "10",
        ":CHANnel1:SCALe?": "1",
        ":CHANnel1:INVert?": "0",
        ":CHANnel1:BWLimit?": "OFF",
        ":CHANnel1:UNITs?": "VOLT",
        ":CHANnel1:LABel:CONTent?": '"CLOCK"',
    })
    result = semantic.configure_timing_capture(
        instrument,
        channels=[{"channel": 1, "label": "CLOCK", "voltage_domain_v": 3.3}],
        trigger={"channel": 1}, time_scale_s_div=400e-6,
        expected="1 kHz square wave",
    )

    assert instrument.written[0] == ":RUN"
    assert instrument.written[-1] == ":STOP"
    assert ":TIMebase:MAIN:OFFSet 0" in instrument.written
    assert ":TIMebase:DELay:ENABle OFF" in instrument.written
    assert result["applied"]["delayed_timebase_enabled"] == "0"
    assert result["fully_applied"] is True
    assert result["mismatches"] == {}
    assert result["unverified"] == ["memory_depth"]
    assert result["expected"] == "1 kHz square wave"


def test_timing_capture_reports_readback_mismatch():
    instrument = _scope({":": "1", ":TIMebase:MAIN:SCALe?": "1.06E-8"})
    result = semantic.configure_timing_capture(
        instrument,
        channels=[{"channel": 1, "label": "CLOCK", "voltage_domain_v": 3.3}],
        trigger={"channel": 1}, time_scale_s_div=400e-6,
    )

    assert result["fully_applied"] is False
    assert result["mismatches"]["time_scale_s_div"] == {
        "requested": 400e-6, "applied": "1.06E-8",
    }
    assert result["warnings"]

from argparse import Namespace
from pathlib import Path

import pytest

from tests.pico.run_acceptance import (
    configure_command,
    load_manifest,
    parse_duty_percent,
    parse_measurement,
    select_cases,
    stable_pio_configuration,
    validate_measurements,
)
from tests.pico.pwm_shapes import stable_pwm_configuration


def test_shipped_manifest_is_valid_and_selectable():
    manifest = load_manifest(Path(__file__).with_name("signals.json"))
    assert [case["id"] for case in select_cases(manifest, ["pulse_10khz_25pct"])] == [
        "pulse_10khz_25pct"
    ]
    with pytest.raises(ValueError, match="unknown signal"):
        select_cases(manifest, ["missing"])


@pytest.mark.parametrize("text, expected", [
    ("FREQUENCY on CHAN1: 1.000000e+03", 1000.0),
    ("PDUTY on CHAN1: 2.500000E+01", 25.0),
])
def test_parse_measurement(text, expected):
    assert parse_measurement(text) == expected


def test_parse_duty_ratio_as_percent():
    assert parse_duty_percent("PDUTY on CHAN1: 5.0000E-01") == 50.0


@pytest.mark.parametrize("text", ["no value", "VPP on CHAN1: 9.9E37", "VPP on CHAN1: nan"])
def test_parse_measurement_rejects_invalid_values(text):
    with pytest.raises(ValueError):
        parse_measurement(text)


def test_measurement_tolerances_report_each_failure():
    case = {
        "frequency_hz": 1000, "frequency_tolerance_percent": 1,
        "duty_percent": 50, "duty_tolerance_percent": 3,
        "vpp_min_v": 3.0, "vpp_max_v": 3.6,
    }
    assert validate_measurements(case, {"frequency_hz": 1005, "duty_percent": 52, "vpp_v": 3.3}) == []
    failures = validate_measurements(case, {"frequency_hz": 900, "duty_percent": 40, "vpp_v": 2.5})
    assert len(failures) == 3


def test_measurement_tolerances_reject_edge_jitter():
    case = {
        "frequency_hz": 1_000_000,
        "actual_frequency_hz": 1_000_000,
        "frequency_tolerance_percent": 1,
        "duty_percent": 50,
        "duty_tolerance_percent": 3,
        "edge_timing_jitter_tolerance_percent": 2,
        "vpp_min_v": 3.0,
        "vpp_max_v": 3.6,
    }
    measured = {
        "frequency_hz": 1_000_000,
        "duty_percent": 50,
        "edge_timing_jitter_percent": 2.5,
        "vpp_v": 3.3,
    }
    assert validate_measurements(case, measured) == [
        "edge timing jitter 2.500% exceeds 2%"
    ]


@pytest.mark.parametrize(("requested_hz", "duty_permille", "expected_hz"), [
    (1_000, 500, 1_000),
    (10_000, 250, 10_000),
    (1_000_000, 500, 1_000_000),
    (5_000_000, 250, 4_687_500),
    (6_000_000, 500, 5_769_231),
    (10_000_000, 500, 9_375_000),
    (10_000_000, 250, 9_375_000),
    (20_000_000, 500, 18_750_000),
    (50_000_000, 500, 37_500_000),
])
def test_stable_pio_configuration(requested_hz, duty_permille, expected_hz):
    configuration = stable_pio_configuration(150_000_000, requested_hz, duty_permille)
    assert configuration["actual_frequency_hz"] == expected_hz
    assert configuration["period"] * duty_permille % 1000 == 0
    assert configuration["divider"] <= 65535
    assert 1 <= configuration["high_cycles"] <= 32
    assert 1 <= configuration["low_cycles"] <= 32


def test_configure_command_contains_case_parameters(tmp_path):
    case = {"id": "test", "frequency_hz": 10000, "duty_percent": 25.0}
    arguments = Namespace(sdk=Path("/sdk"), board="pico2_w", toolchain=Path("/toolchain"))
    command = configure_command(
        case, {"gpio": 16, "system_clock_hz": 150_000_000}, tmp_path, arguments
    )
    assert "-DSIGNAL_FREQUENCY_HZ=10000" in command
    assert "-DSIGNAL_DUTY_PERMILLE=250" in command
    assert "-DSIGNAL_GPIO=16" in command
    assert "-DSIGNAL_SYSTEM_CLOCK_HZ=150000000" in command
    assert "-DPICO_BOARD=pico2_w" in command
    assert "-DPICO_TOOLCHAIN_PATH=/toolchain" in command


@pytest.mark.parametrize(("frequency_hz", "duty_permille", "expected_high"), [
    (1_000_000, 100, 15),
    (1_000_000, 250, 38),
    (1_000_000, 500, 75),
    (1_000_000, 750, 113),
    (1_000_000, 900, 135),
    (10_000, 1, 15),
    (1_000_000, 10, 2),
])
def test_stable_pwm_configuration_duty_and_narrow_pulses(
    frequency_hz, duty_permille, expected_high
):
    configuration = stable_pwm_configuration(150_000_000, frequency_hz, duty_permille)
    assert configuration["divider"] == 1
    assert configuration["high_cycles"] == expected_high


def test_stable_pwm_configuration_crosses_16_bit_period_boundary():
    assert stable_pwm_configuration(150_000_000, 2_288, 500)["divider"] > 1
    assert stable_pwm_configuration(150_000_000, 2_289, 500)["divider"] == 1


@pytest.mark.parametrize(("phase_permille", "expected_cycles"), [
    (0, 0),
    (250, 38),
    (500, 75),
])
def test_stable_pwm_configuration_phase(phase_permille, expected_cycles):
    configuration = stable_pwm_configuration(
        150_000_000, 1_000_000, 500, phase_permille
    )
    assert configuration["phase_cycles"] == expected_cycles
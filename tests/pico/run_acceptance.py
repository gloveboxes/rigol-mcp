"""Build Pico signal firmware and optionally validate it with a live DHO814."""

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters


PICO_DIR = Path(__file__).resolve().parent
REPOSITORY = PICO_DIR.parents[1]
DEFAULT_MANIFEST = PICO_DIR / "signals.json"
MEASUREMENT_PATTERN = re.compile(r":\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)")


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    if manifest.get("system_clock_hz") != 150_000_000:
        raise ValueError("Pico 2 W manifest system_clock_hz must be 150000000")
    if not isinstance(manifest.get("gpio"), int) or not 0 <= manifest["gpio"] <= 29:
        raise ValueError("manifest gpio must be an integer from 0 through 29")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest must contain at least one signal case")
    identifiers = [case.get("id") for case in cases]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise ValueError("every signal case must have a non-empty id")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("signal case ids must be unique")
    for case in cases:
        if case.get("frequency_hz", 0) <= 0:
            raise ValueError(f"{case['id']}: frequency_hz must be positive")
        if not 0 < case.get("duty_percent", 0) < 100:
            raise ValueError(f"{case['id']}: duty_percent must be between 0 and 100")
        if case.get("vpp_min_v", 0) >= case.get("vpp_max_v", 0):
            raise ValueError(f"{case['id']}: invalid Vpp range")
        configuration = stable_pio_configuration(
            manifest["system_clock_hz"], case["frequency_hz"],
            round(case["duty_percent"] * 10),
        )
        case["actual_frequency_hz"] = configuration["actual_frequency_hz"]
        case["edge_timing_jitter_tolerance_percent"] = manifest.get(
            "edge_timing_jitter_tolerance_percent", 2.0
        )
    return manifest


def stable_pio_configuration(
    system_clock_hz: int, requested_frequency_hz: int, duty_permille: int
) -> dict[str, int]:
    period_quantum = 1000 // math.gcd(duty_permille, 1000)
    best = None
    for period in range(period_quantum, 65, period_quantum):
        high_cycles = period * duty_permille // 1000
        low_cycles = period - high_cycles
        if not 1 <= high_cycles <= 32 or not 1 <= low_cycles <= 32:
            continue
        denominator = requested_frequency_hz * period
        rounded_divider = (system_clock_hz + denominator // 2) // denominator
        rounded_divider = min(65535, max(1, rounded_divider))
        for divider in {
            max(1, rounded_divider - 1),
            rounded_divider,
            min(65535, rounded_divider + 1),
        }:
            product = divider * period
            actual_frequency_hz = (system_clock_hz + product // 2) // product
            frequency_error = abs(actual_frequency_hz - requested_frequency_hz)
            candidate = (
                frequency_error, -period, divider, period, high_cycles, low_cycles
            )
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError("no stable PIO configuration")
    _, _, divider, period, high_cycles, low_cycles = best
    product = divider * period
    return {
        "divider": divider,
        "period": period,
        "high_cycles": high_cycles,
        "low_cycles": low_cycles,
        "actual_frequency_hz": (system_clock_hz + product // 2) // product,
    }


def select_cases(manifest: dict, requested: list[str]) -> list[dict]:
    if not requested:
        return manifest["cases"]
    by_id = {case["id"]: case for case in manifest["cases"]}
    unknown = sorted(set(requested) - by_id.keys())
    if unknown:
        raise ValueError(f"unknown signal case(s): {', '.join(unknown)}")
    return [by_id[identifier] for identifier in requested]


def parse_measurement(text: str) -> float:
    match = MEASUREMENT_PATTERN.search(text)
    if not match:
        raise ValueError(f"measurement has no numeric value: {text!r}")
    value = float(match.group(1))
    if not math.isfinite(value) or abs(value) >= 9e36:
        raise ValueError(f"invalid measurement value: {value}")
    return value


def parse_duty_percent(text: str) -> float:
    return parse_measurement(text) * 100


def validate_measurements(case: dict, measured: dict[str, float]) -> list[str]:
    failures = []
    expected_frequency_hz = case.get("actual_frequency_hz", case["frequency_hz"])
    frequency_error = abs(measured["frequency_hz"] - expected_frequency_hz) / expected_frequency_hz * 100
    if frequency_error > case["frequency_tolerance_percent"]:
        failures.append(
            f"frequency {measured['frequency_hz']:g} Hz is {frequency_error:.3f}% from "
            f"generated target {expected_frequency_hz:g} Hz "
            f"(requested {case['frequency_hz']:g} Hz; limit {case['frequency_tolerance_percent']:g}%)"
        )
    duty_error = abs(measured["duty_percent"] - case["duty_percent"])
    if duty_error > case["duty_tolerance_percent"]:
        failures.append(
            f"duty {measured['duty_percent']:g}% is {duty_error:.3f} percentage points from "
            f"{case['duty_percent']:g}% (limit {case['duty_tolerance_percent']:g})"
        )
    jitter = measured.get("edge_timing_jitter_percent")
    jitter_limit = case.get("edge_timing_jitter_tolerance_percent")
    if jitter_limit is not None:
        if jitter is None:
            failures.append("edge timing jitter could not be measured")
        elif jitter > jitter_limit:
            failures.append(
                f"edge timing jitter {jitter:.3f}% exceeds {jitter_limit:g}%"
            )
    if not case["vpp_min_v"] <= measured["vpp_v"] <= case["vpp_max_v"]:
        failures.append(
            f"Vpp {measured['vpp_v']:g} V is outside "
            f"[{case['vpp_min_v']:g}, {case['vpp_max_v']:g}] V"
        )
    return failures


def configure_command(case: dict, manifest: dict, build_dir: Path, arguments) -> list[str]:
    command = [
        "cmake", "-S", str(PICO_DIR), "-B", str(build_dir), "-G", "Ninja",
        f"-DPICO_SDK_PATH={arguments.sdk}", f"-DPICO_BOARD={arguments.board}",
        f"-DSIGNAL_GPIO={manifest['gpio']}", f"-DSIGNAL_FREQUENCY_HZ={case['frequency_hz']}",
        f"-DSIGNAL_DUTY_PERMILLE={round(case['duty_percent'] * 10)}",
        f"-DSIGNAL_SYSTEM_CLOCK_HZ={manifest['system_clock_hz']}",
    ]
    if arguments.toolchain:
        command.append(f"-DPICO_TOOLCHAIN_PATH={arguments.toolchain}")
    return command


def build_case(case: dict, manifest: dict, build_root: Path, arguments) -> Path:
    build_dir = build_root / case["id"]
    subprocess.run(configure_command(case, manifest, build_dir, arguments), check=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--parallel"], check=True)
    firmware = build_dir / "rigol_pico_signal.uf2"
    if not firmware.is_file():
        raise FileNotFoundError(f"build did not produce {firmware}")
    return firmware


def flash_firmware(firmware: Path) -> None:
    subprocess.run(["picotool", "load", "-f", "-v", "-x", str(firmware)], check=True)
    time.sleep(1.0)


async def call(client: Client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    text = "\n".join(block.text for block in result.content if block.type == "text")
    if result.is_error:
        raise RuntimeError(f"{name}: {text}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def restore_scope(client: Client, initial: dict) -> None:
    channel = initial["channels"]["CHAN1"]
    await call(client, "set_channel", {
        "channel": "CHAN1", "display": channel["display"], "scale_v_div": channel["scale_v_div"],
        "offset_v": channel["offset_v"], "coupling": channel["coupling"], "probe": channel["probe"],
    })
    await call(client, "set_timebase", {
        "scale_s_div": initial["timebase"]["scale_s_div"], "offset_s": initial["timebase"]["offset_s"],
    })
    trigger = initial["trigger"]
    await call(client, "set_trigger", {
        "source": trigger["source"], "slope": trigger["slope"], "level": trigger["level_v"],
    })
    await call(client, "run" if trigger["status"] != "STOP" else "stop")


async def exercise_scope(ip: str, cases: list[tuple[dict, Path]], manifest: dict) -> dict:
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "rigol_mcp.server"], cwd=str(REPOSITORY),
        env={"PYTHON_DOTENV_DISABLED": "1", "RIGOL_IP": ip, "RIGOL_ENABLE_SEND_RAW": "0"},
    )
    report = {"ip": ip, "gpio": manifest["gpio"], "cases": []}
    async with Client(parameters, read_timeout_seconds=100) as client:
        report["identity"] = await call(client, "idn")
        capabilities = await call(client, "get_capabilities")
        if capabilities["model"] != "DHO814":
            raise RuntimeError(f"Pico acceptance tests target DHO814, found {capabilities['model']}")
        initial = await call(client, "get_scope_state")
        if initial["trigger"]["mode"] != "EDGE":
            raise RuntimeError("scope must start in EDGE trigger mode so its state can be restored")
        report["initial_state"] = initial
        try:
            for case, firmware in cases:
                flash_firmware(firmware)
                await call(client, "set_channel", {
                    "channel": "CHAN1", "display": True, "scale_v_div": 1.0,
                    "offset_v": 0.0, "coupling": "DC", "probe": 10,
                })
                await call(client, "set_timebase", {
                    "scale_s_div": 4 / (10 * case["actual_frequency_hz"]), "offset_s": 0.0,
                })
                await call(client, "set_trigger", {"source": "CHAN1", "slope": "POS", "level": 1.65})
                await call(client, "run")
                await asyncio.sleep(0.5)
                capture = await call(client, "acquire_and_capture", {
                    "channels": ["CHAN1"], "timeout_s": 10,
                    "label": case["id"], "peak_count": 5,
                })
                analysis = capture["channels"]["CHAN1"]
                measured = {
                    "frequency_hz": analysis["frequency_hz"],
                    "vpp_v": analysis["statistics"]["peak_to_peak_v"],
                    "period_jitter_percent": analysis["period_jitter_percent"],
                    "edge_timing_jitter_percent": analysis["edge_timing_jitter_percent"],
                }
                await call(client, "run")
                await asyncio.sleep(0.5)
                measured["duty_percent"] = parse_duty_percent(await call(
                    client, "measure", {"channel": "CHAN1", "item": "PDUTY"}))
                failures = validate_measurements(case, measured)
                report["cases"].append({
                    "id": case["id"], "firmware": str(firmware), "expected": case,
                    "measured": measured, "passed": not failures, "failures": failures,
                })
                print(f"{'PASS' if not failures else 'FAIL'} {case['id']}: {measured}", flush=True)
        finally:
            await restore_scope(client, initial)
            report["final_state"] = await call(client, "get_scope_state")
            report["final_error"] = await call(client, "check_error")
    return report


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--case", action="append", default=[], dest="cases", help="Case id; repeat to select several")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("PICO_SDK_PATH", "/Users/dave/GitHub/pico/pico-sdk")))
    parser.add_argument("--toolchain", type=Path, default=Path(os.environ["PICO_TOOLCHAIN_PATH"]) if os.environ.get("PICO_TOOLCHAIN_PATH") else None)
    parser.add_argument("--board", default=os.environ.get("PICO_BOARD", "pico2_w"))
    parser.add_argument("--build-root", type=Path, default=Path("/tmp/rigol-mcp-pico"))
    parser.add_argument("--flash", action="store_true", help="Flash each case and run live scope validation")
    parser.add_argument("--ip", help="DHO814 LAN address; required with --flash")
    parser.add_argument("--report", type=Path, default=Path("captures/pico2_w_acceptance_report.json"))
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    if arguments.flash and not arguments.ip:
        raise SystemExit("--ip is required with --flash")
    manifest = load_manifest(arguments.manifest)
    selected = select_cases(manifest, arguments.cases)
    built = [(case, build_case(case, manifest, arguments.build_root, arguments)) for case in selected]
    if not arguments.flash:
        for case, firmware in built:
            print(f"BUILT {case['id']}: {firmware}")
        return
    report = asyncio.run(exercise_scope(arguments.ip, built, manifest))
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2) + "\n")
    failures = [case for case in report["cases"] if not case["passed"]]
    print(f"Report: {arguments.report.resolve()}")
    if failures or report["final_error"] != "No error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
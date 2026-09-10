"""Build deterministic Pico PWM duty, pulse, boundary, and phase test images."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

try:
    from tests.pico.pwm_shapes import stable_pwm_configuration
except ModuleNotFoundError:
    from pwm_shapes import stable_pwm_configuration


PICO_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PICO_DIR / "pwm_shapes.json"


def load_cases(path: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads(path.read_text())
    cases = []
    for source_case in manifest["cases"]:
        case = dict(source_case)
        phase_permille = round(case.get("phase_degrees", 0) / 360 * 1000)
        case["configuration"] = stable_pwm_configuration(
            manifest["system_clock_hz"],
            case["frequency_hz"],
            round(case["duty_percent"] * 10),
            phase_permille,
        )
        cases.append(case)
    return manifest, cases


def configure_command(
    case: dict, manifest: dict, build_dir: Path, arguments: argparse.Namespace
) -> list[str]:
    configuration = case["configuration"]
    secondary_gpio = manifest["secondary_gpio"] if case["group"] == "phase" else 255
    command = [
        "cmake", "-S", str(PICO_DIR), "-B", str(build_dir), "-G", "Ninja",
        f"-DPICO_SDK_PATH={arguments.sdk}", f"-DPICO_BOARD={arguments.board}",
        f"-DSIGNAL_GPIO={manifest['gpio']}",
        f"-DSIGNAL_SYSTEM_CLOCK_HZ={manifest['system_clock_hz']}",
        f"-DPWM_DIVIDER={configuration['divider']}",
        f"-DPWM_TOP={configuration['top']}",
        f"-DPWM_HIGH_CYCLES={configuration['high_cycles']}",
        f"-DPWM_SECONDARY_GPIO={secondary_gpio}",
        f"-DPWM_PHASE_CYCLES={configuration['phase_cycles']}",
    ]
    if arguments.toolchain:
        command.append(f"-DPICO_TOOLCHAIN_PATH={arguments.toolchain}")
    return command


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--case", action="append", default=[], dest="cases")
    parser.add_argument("--build-root", type=Path, default=Path("/tmp/rigol-mcp-pwm-tests"))
    parser.add_argument("--board", default=os.environ.get("PICO_BOARD", "pico2_w"))
    parser.add_argument(
        "--sdk",
        type=Path,
        default=Path(os.environ.get("PICO_SDK_PATH", "/Users/dave/GitHub/pico/pico-sdk")),
    )
    parser.add_argument(
        "--toolchain",
        type=Path,
        default=Path(os.environ["PICO_TOOLCHAIN_PATH"])
        if os.environ.get("PICO_TOOLCHAIN_PATH")
        else None,
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    manifest, cases = load_cases(arguments.manifest)
    requested = set(arguments.cases)
    unknown = requested - {case["id"] for case in cases}
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(sorted(unknown))}")
    selected = [case for case in cases if not requested or case["id"] in requested]

    for case in selected:
        build_dir = arguments.build_root / case["id"]
        subprocess.run(configure_command(case, manifest, build_dir, arguments), check=True)
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--target", "rigol_pico_pwm_test", "--parallel"],
            check=True,
        )
        firmware = build_dir / "rigol_pico_pwm_test.uf2"
        if not firmware.is_file():
            raise FileNotFoundError(f"build did not produce {firmware}")
        print(f"BUILT {case['id']}: {firmware} {case['configuration']}")


if __name__ == "__main__":
    main()
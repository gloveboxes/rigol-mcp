"""Opt-in, signal-independent MCP smoke test; temporarily changes and restores settings."""

import argparse
import asyncio
import json
import math
import os
import struct
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def exercise(ip: str):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "rigol_mcp.server"], cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHON_DOTENV_DISABLED": "1", "RIGOL_IP": ip, "RIGOL_ENABLE_SEND_RAW": "0"},
    )
    passed = []
    failures = []
    restores = []

    async with Client(parameters, read_timeout_seconds=100) as client:
        async def call(name, arguments=None):
            result = await client.call_tool(name, arguments or {})
            text = "\n".join(block.text for block in result.content if block.type == "text")
            if result.is_error or "*IDN? query FAILED" in text:
                raise RuntimeError(f"{name}: {text}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

        async def query(command):
            result = await call("scpi_execute", {"command": command})
            return result["value"]

        async def write(command, value):
            return await call("scpi_execute", {"command": command, "operation": "write", "arguments": [value]})

        def success(label):
            passed.append(label)
            print(f"PASS {label}", flush=True)

        async def check(label, operation):
            try:
                value = await operation()
                success(label)
                return value
            except Exception as error:
                failures.append({"test": label, "error": str(error)})
                print(f"FAIL {label}: {error}", flush=True)
                raise

        identity = await call("idn")
        print(identity, flush=True)
        capabilities = await call("get_capabilities")
        if capabilities["model"] != "DHO814":
            raise RuntimeError("This live test targets DHO814 only")
        initial = await call("get_scope_state")
        initial_error = await call("check_error")
        if initial_error != "No error":
            raise RuntimeError(f"Scope has an initial error: {initial_error}")
        success("identity, capabilities, state and empty error queue")

        async def setting_roundtrip(tool, arguments, state_key, expected, restore_arguments):
            restores.append((tool, restore_arguments))
            result = await call(tool, arguments)
            actual = result[state_key]
            if not math.isclose(float(actual), expected, rel_tol=1e-5, abs_tol=1e-12):
                raise AssertionError(f"Readback {actual} != {expected}")
            await call(tool, restore_arguments)
            restores.pop()

        try:
            for command in (
                ":ACQuire:TYPE", ":ACQuire:AVERages", ":ACQuire:MDEPth", ":ACQuire:SRATe",
                ":TRIGger:SWEep", ":TRIGger:HOLDoff", ":TRIGger:NREJect",
                ":CHANnel1:BWLimit", ":CHANnel1:INVert", ":CHANnel1:UNITs", ":CHANnel1:VERNier",
                ":DISPlay:TYPE", ":DISPlay:GRID", ":DISPlay:WBRightness", ":CURSor:MODE",
                ":DVM:ENABle", ":COUNter:ENABle", ":SYSTem:RAMount", ":SYSTem:GAMount", ":SYSTem:VERSion",
            ):
                value = await check(f"query {command}", lambda command=command: query(command))
                print(f"  {value}", flush=True)
                error = await call("check_error")
                if error != "No error":
                    raise RuntimeError(f"{command}: {error}")

            channel = initial["channels"]["CHAN1"]
            await check("channel scale write/readback/restore", lambda: setting_roundtrip(
                "set_channel", {"channel": "CHAN1", "scale_v_div": 0.1}, "scale_v_div", 0.1,
                {"channel": "CHAN1", "scale_v_div": channel["scale_v_div"]},
            ))
            await check("timebase write/readback/restore", lambda: setting_roundtrip(
                "set_timebase", {"scale_s_div": 5e-6}, "scale_s_div", 5e-6,
                {"scale_s_div": initial["timebase"]["scale_s_div"], "offset_s": initial["timebase"]["offset_s"]},
            ))
            if initial["trigger"]["mode"] == "EDGE":
                await check("trigger level write/readback/restore", lambda: setting_roundtrip(
                    "set_trigger", {"level": 0.01}, "level_v", 0.01,
                    {"level": initial["trigger"]["level_v"]},
                ))

            inversion = await query(":CHANnel1:INVert")
            restores.append(("scpi_execute", {"command": ":CHANnel1:INVert", "operation": "write", "arguments": [inversion]}))
            await write(":CHANnel1:INVert", inversion != "1")
            assert await query(":CHANnel1:INVert") == ("0" if inversion == "1" else "1")
            await write(":CHANnel1:INVert", inversion)
            restores.pop()
            success("catalog write/readback/restore")

            screenshot = await call("screenshot")
            image_path = Path(screenshot.removeprefix("Saved: "))
            image = image_path.read_bytes()
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            dimensions = struct.unpack(">II", image[16:24])
            assert all(dimension > 0 for dimension in dimensions)
            success(f"screenshot PNG {dimensions[0]}x{dimensions[1]} ({len(image)} bytes)")
            print(f"  {image_path}", flush=True)

            waveform_settings = [(command, await query(command)) for command in (
                ":WAVeform:SOURce", ":WAVeform:MODE", ":WAVeform:FORMat", ":WAVeform:STARt", ":WAVeform:STOP",
            )]
            if initial["trigger"]["status"] != "STOP":
                restores.append(("run", {}))
            for command, value in reversed(waveform_settings):
                restores.append(("scpi_execute", {"command": command, "operation": "write", "arguments": [value]}))
            await call("stop")
            source = next(name for name, settings in initial["channels"].items() if settings["display"])
            summary = await call("get_waveform", {"channel": source})
            assert isinstance(summary, str) and summary
            success("unconnected-input waveform analysis")
            print(summary, flush=True)
            raw = await call("get_waveform", {"channel": source, "raw_data": True})
            assert raw["points"] > 0 and "times_s" not in raw
            captured = json.loads(Path(raw["path"]).read_text())
            assert len(captured["times_s"]) == len(captured["voltages_v"]) == raw["points"]
            success(f"file-backed waveform JSON ({raw['points']} points)")
            excerpt = await call("read_capture", {"path": raw["path"], "max_bytes": 128})
            assert excerpt["bytes"] == 128
            success("bounded capture read without reacquisition")
            for mode in ("NORM", "RAW"):
                capture = await check(f"{mode} chunked waveform download", lambda mode=mode: call(
                    "download_waveform", {"source": source, "mode": mode, "points": 64, "chunk_points": 32},
                ))
                assert capture["points"] == 64
                assert len(Path(capture["path"]).read_text().splitlines()) == 65

        except Exception as error:
            if not failures or failures[-1]["error"] != str(error):
                failures.append({"test": "live exercise", "error": str(error)})
            print(f"STOPPING TESTS: {error}", flush=True)
        finally:
            for tool, arguments in reversed(restores):
                try:
                    await call(tool, arguments)
                    print(f"RESTORED {tool} {arguments}", flush=True)
                except Exception as error:
                    failures.append({"test": "restore", "tool": tool, "arguments": arguments, "error": str(error)})
                    print(f"RESTORE FAILED: {error}", flush=True)
            final = await call("get_scope_state")
            for group in ("channels", "timebase", "trigger"):
                expected = dict(initial[group])
                actual = dict(final[group])
                if group == "trigger":
                    expected.pop("status", None)
                    actual.pop("status", None)
                if actual != expected:
                    failures.append({"test": f"restore {group}", "expected": expected, "actual": actual})
            final_error = await call("check_error")
            if final_error != "No error":
                failures.append({"test": "final error queue", "error": final_error})
            report = {"ip": ip, "identity": identity, "passed": passed, "failures": failures,
                      "initial_state": initial, "final_state": final, "final_error": final_error}
            output = Path(os.environ.get("RIGOL_DATA_DIR", "captures")) / "live_scope_report.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(f"RESULT: {len(passed)} checks passed, {len(failures)} failures. Report: {output.resolve()}", flush=True)
            if failures:
                raise RuntimeError("Live scope checks failed; see report")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True)
    arguments = parser.parse_args()
    asyncio.run(exercise(arguments.ip))


if __name__ == "__main__":
    main()
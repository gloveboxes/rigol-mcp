"""Documented DHO814 SCPI discovery and execution."""

import base64
import csv
import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from rigol_mcp import scope as scope_api


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(files("rigol_mcp").joinpath("dho814_commands.json").read_text())


def _keyword_pattern(keyword: str) -> str:
    if matched := re.fullmatch(r"([A-Za-z]+)(\d+)", keyword):
        return _keyword_pattern(matched[1]) + re.escape(matched[2])
    if not keyword.isalpha():
        return re.escape(keyword)
    required = "".join(character for character in keyword if character.isupper())
    optional = keyword[len(required):]
    return re.escape(required) + (f"(?:{re.escape(optional)})?" if optional else "")


def _header_pattern(template: str) -> str:
    parts = re.findall(r"\[:[A-Za-z]+\]|<n>|[A-Za-z]+|.", template)
    pattern = ""
    for part in parts:
        if part.startswith("[:"):
            pattern += f"(?::{_keyword_pattern(part[2:-1])})?"
        elif part == "<n>":
            pattern += r"(?P<index>\d+)"
        elif part.isalpha():
            pattern += _keyword_pattern(part)
        else:
            pattern += re.escape(part)
    return pattern


@lru_cache(maxsize=1)
def _matchers():
    return [(re.compile(_header_pattern(entry["command"]), re.IGNORECASE), entry)
            for entry in catalog()["commands"]]


def resolve(header: str) -> tuple[dict, int | None]:
    normalized = header.strip().rstrip("?")
    if not normalized.startswith((":", "*")):
        normalized = ":" + normalized
    for pattern, entry in _matchers():
        if matched := pattern.fullmatch(normalized):
            index = matched.groupdict().get("index")
            if index is not None:
                _validate_value(int(index), entry["parameters"]["n"])
            return entry, int(index) if index is not None else None
    raise ValueError(f"Not a documented DHO814 command: {header}")


INLINE_TEXT_LIMIT = 2048
INLINE_BINARY_LIMIT = 1024
RESPONSE_TEXT_LIMIT = 4096


def discover(subsystem: str = "", search: str = "", offset: int = 0, limit: int = 10,
             command: str | None = None) -> dict:
    if command is not None:
        for entry in catalog()["commands"]:
            if entry["command"].upper() == command.upper():
                return entry
        entry, _ = resolve(command)
        return entry
    if offset < 0 or not 1 <= limit <= 25:
        raise ValueError("offset must be nonnegative; limit must be between 1 and 25")
    entries = [entry for entry in catalog()["commands"]
               if (not subsystem or entry["subsystem"].startswith(subsystem.lower()))
               and search.lower() in json.dumps(entry).lower()]
    return {
        "model": "DHO814",
        "total": len(entries),
        "next_offset": offset + limit if offset + limit < len(entries) else None,
        "commands": [{"command": entry["command"], "operations": list(entry["operations"])}
                 for entry in entries[offset:offset + limit]],
        "detail": "Pass command to retrieve its parameter types/enums and manual section.",
    }


def _validate_value(value, parameter: dict) -> str:
    kind = parameter["type"]
    if kind == "bool" and isinstance(value, bool):
        return "ON" if value else "OFF"
    text = str(value)
    if any(character in text for character in "\r\n\x00"):
        raise ValueError("SCPI parameters cannot contain line breaks or NUL")
    if "enum" in parameter:
        for choice in parameter["enum"]:
            if re.fullmatch(_keyword_pattern(choice), text, re.IGNORECASE):
                return choice.upper()
        raise ValueError(f"Invalid value {value!r}; expected one of {parameter['enum']}")
    if kind == "integer":
        try:
            number = Decimal(text)
        except InvalidOperation as error:
            raise ValueError(f"Expected integer, got {value!r}") from error
        if isinstance(value, bool) or not number.is_finite() or number != number.to_integral_value():
            raise ValueError("Expected a finite integer")
        if number.adjusted() > 255:
            raise ValueError("Integer exceeds the 256-digit limit")
        return str(int(number))
    if kind == "real":
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Expected {kind}, got {value!r}") from error
        if isinstance(value, bool) or not math.isfinite(number):
            raise ValueError("Expected a finite number")
        return format(number, ".15g")
    if kind in {"ascii string", "string"}:
        text.encode("ascii")
        return '"' + text.replace('"', '""') + '"'
    if kind == "binary":
        raise ValueError("Use data_base64 for binary setup uploads")
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+", text):
        raise ValueError("Invalid SCPI parameter")
    return text


def prepare(header: str, operation: str, arguments: list | None = None) -> tuple[dict, str]:
    entry, index = resolve(header)
    if operation not in entry["operations"]:
        raise ValueError(f"{operation} is not supported for {entry['command']}")
    spec = entry["operations"][operation]
    arguments = arguments or []
    required = len(re.findall(r"<[^>]+>", spec["syntax"].split("[", 1)[0]))
    if not required <= len(arguments) <= len(spec["parameters"]):
        raise ValueError(f"Expected {required}..{len(spec['parameters'])} parameters: {spec['syntax']}")
    values = [_validate_value(value, entry["parameters"][name])
              for value, name in zip(arguments, spec["parameters"])]
    command = entry["command"].replace("<n>", str(index))
    command = command.replace("[", "").replace("]", "")
    if operation == "query":
        command += "?"
    if values:
        command += " " + ",".join(values)
    return entry, command


def _save_data(payload: bytes, suffix: str) -> dict:
    directory = Path(os.environ.get("RIGOL_DATA_DIR", "captures")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"capture_{uuid4().hex}{suffix}"
    destination.write_bytes(payload)
    return {"path": str(destination), "bytes": len(payload)}


def _capture_path(path: str) -> Path:
    directory = Path(os.environ.get("RIGOL_DATA_DIR", "captures")).resolve()
    destination = Path(path).resolve()
    if destination.parent != directory or not re.fullmatch(
        r"(?:capture|waveform)_[0-9a-f]{32}\.(?:txt|bin|csv|json)", destination.name,
    ):
        raise ValueError("Only generated files inside RIGOL_DATA_DIR may be read")
    return destination


def read_capture(path: str, offset: int = 0, max_bytes: int = 1024,
                 encoding: str = "utf-8") -> dict:
    if offset < 0 or not 1 <= max_bytes <= 2048:
        raise ValueError("offset must be nonnegative; max_bytes must be 1..2048")
    if encoding not in {"utf-8", "base64"}:
        raise ValueError("encoding must be utf-8 or base64")
    destination = _capture_path(path)
    with destination.open("rb") as capture:
        size = os.fstat(capture.fileno()).st_size
        capture.seek(offset)
        payload = capture.read(max_bytes)
    while True:
        next_offset = offset + len(payload)
        result = {
            "offset": offset, "bytes": len(payload), "total_bytes": size,
            "next_offset": next_offset if next_offset < size else None,
            "encoding": encoding,
            "data": base64.b64encode(payload).decode("ascii") if encoding == "base64"
                    else payload.decode("utf-8", errors="replace"),
        }
        if len(json.dumps(result, separators=(",", ":"), ensure_ascii=False)) <= RESPONSE_TEXT_LIMIT:
            return result
        payload = payload[:len(payload) // 2]


def execute(instrument, header: str, operation: str = "query", arguments: list | None = None,
            data_base64: str | None = None, inline_binary: bool = False,
            data_path: str | None = None) -> dict:
    capabilities = scope_api.get_capabilities(instrument)
    if not capabilities["command_catalog"]:
        raise ValueError("The complete command catalog is verified against the DHO814 reference only")
    if data_base64 is not None or data_path is not None:
        entry, _ = resolve(header)
        if entry["command"] != ":SYSTem:SETup" or operation != "write" or arguments:
            raise ValueError("Setup data is only valid for :SYSTem:SETup writes without arguments")
        if data_base64 is not None and data_path is not None:
            raise ValueError("Supply data_path or data_base64, not both")
        payload = _capture_path(data_path).read_bytes() if data_path is not None else base64.b64decode(data_base64, validate=True)
        length = str(len(payload)).encode("ascii")
        if len(length) > 9:
            raise ValueError("Setup block is too large")
        instrument.write_raw(b":SYSTem:SETup #" + str(len(length)).encode("ascii") + length + payload + b"\n")
        command = ":SYSTem:SETup"
    else:
        entry, command = prepare(header, operation, arguments)
        if operation == "query":
            binary = entry["command"] in {":DISPlay:DATA", ":SAVE:IMAGe:DATA", ":SYSTem:SETup"}
            if entry["command"] == ":WAVeform:DATA":
                binary = instrument.query(":WAVeform:FORMat?").strip().upper() != "ASC"
            if binary:
                instrument.write(command)
                payload = scope_api._read_definite_block(instrument)
                result = _save_data(payload, ".bin")
                if inline_binary and len(payload) <= INLINE_BINARY_LIMIT:
                    result["data_base64"] = base64.b64encode(payload).decode("ascii")
                elif inline_binary:
                    result["inline_omitted"] = "Binary payload exceeds the 1024-byte inline limit"
                return {"command": command, "encoding": "base64", **result}
            response = instrument.query(command).strip()
            if len(response) > INLINE_TEXT_LIMIT:
                return {"command": command, "encoding": "utf-8", "preview": response[:256],
                        "characters": len(response), **_save_data(response.encode(), ".txt")}
            return {"command": command, "value": response}
        instrument.write(command)
    if command not in {":SYSTem:RESet", "*RST"}:
        if error := scope_api.check_scpi_error(instrument):
            raise RuntimeError(f"SCPI error after {command}: {error}")
    return {"command": command, "written": True}


def download_waveform(instrument, source: str, mode: str = "RAW", start: int = 1,
                      points: int | None = None, chunk_points: int = 10000) -> dict:
    capabilities = scope_api.get_capabilities(instrument)
    if not capabilities["command_catalog"]:
        raise ValueError("Memory downloads currently require DHO814")
    source = source.upper()
    mode = mode.upper()
    if source not in capabilities["channels"] + [f"MATH{number}" for number in range(1, 5)]:
        raise ValueError("Invalid waveform source")
    if mode not in {"NORM", "RAW", "MAX"}:
        raise ValueError("mode must be NORM, RAW, or MAX")
    if source.startswith("MATH") and mode != "NORM":
        raise ValueError("Math waveforms support NORM mode only")
    if start < 1 or not 1 <= chunk_points <= 10000 or (points is not None and points < 1):
        raise ValueError("Invalid waveform range or chunk size")
    if instrument.query(":TRIGger:STATus?").strip().upper() != "STOP":
        raise ValueError("Stop acquisition before downloading a stable waveform")
    if source.startswith("CHAN") and instrument.query(f":{source}:DISPlay?").strip().upper() not in {"1", "ON"}:
        raise ValueError("Enable the source and acquire data before stopping and downloading")
    available = 1000 if mode == "NORM" else int(float(instrument.query(":ACQuire:MDEPth?").strip()))
    count = available - start + 1 if points is None else points
    if count < 1 or start + count - 1 > available:
        raise ValueError(f"Requested range exceeds {available} available points")
    instrument.write(f":WAVeform:SOURce {source}")
    instrument.write(f":WAVeform:MODE {mode}")
    instrument.write(":WAVeform:FORMat ASC")
    instrument.write(f":WAVeform:STARt {start}")
    instrument.write(f":WAVeform:STOP {min(start + chunk_points - 1, start + count - 1)}")
    if error := scope_api.check_scpi_error(instrument):
        raise RuntimeError(f"Waveform setup failed: {error}")
    preamble = instrument.query(":WAVeform:PREamble?").strip()
    fields = preamble.split(",")
    if len(fields) != 10:
        raise ValueError("Invalid waveform preamble")
    increment, origin, reference = map(float, fields[4:7])
    if not all(math.isfinite(value) for value in (increment, origin, reference)) or increment <= 0:
        raise ValueError("Waveform timing is invalid; acquire data before downloading")
    directory = Path(os.environ.get("RIGOL_DATA_DIR", "captures")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"waveform_{uuid4().hex}.csv"
    partial = destination.with_suffix(".partial")
    transferred = 0
    invalid = 0
    try:
        with partial.open("x", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["time_s", "value"])
            while transferred < count:
                first = start + transferred
                last = first + min(chunk_points, count - transferred) - 1
                instrument.write(f":WAVeform:STARt {first}")
                instrument.write(f":WAVeform:STOP {last}")
                if error := scope_api.check_scpi_error(instrument):
                    raise RuntimeError(f"Waveform range failed: {error}")
                values = [float(value) for value in instrument.query(":WAVeform:DATA?").strip().split(",") if value.strip()]
                if len(values) != last - first + 1:
                    raise ValueError(f"Truncated waveform: expected {last - first + 1} points, received {len(values)}")
                for index, value in enumerate(values, first - 1):
                    invalid += int(not math.isfinite(value) or abs(value) >= scope_api._INVALID_SENTINEL)
                    writer.writerow([format(origin + (index - reference) * increment, ".17g"), format(value, ".17g")])
                transferred += len(values)
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {
        "path": str(destination), "source": source, "mode": mode, "points": transferred,
        "start_point": start, "time_increment_s": increment,
        "time_start_s": origin + (start - 1 - reference) * increment,
        "time_end_s": origin + (start + count - 2 - reference) * increment,
        "preamble": preamble, "invalid_samples": invalid,
        "value_units": "Source units (volts for voltage channels; math units depend on the operator)",
    }
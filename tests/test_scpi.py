import base64
import csv
import json
from pathlib import Path

import pytest

from rigol_mcp import scpi
from tests.conftest import FakeScope, make_block


def test_integer_parameter_preserves_exact_decimal_value():
    assert scpi._validate_value("9007199254740993", {"type": "integer"}) == "9007199254740993"
    with pytest.raises(ValueError, match="integer"):
        scpi._validate_value("1.0000000000000001", {"type": "integer"})


def test_capture_excerpt_fits_budget_even_with_control_characters(monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    saved = scpi._save_data(b"\x00" * 4096, ".txt")
    result = scpi.read_capture(saved["path"], max_bytes=2048)
    assert len(json.dumps(result, separators=(",", ":"), ensure_ascii=False)) <= 4096
    assert result["next_offset"] == result["bytes"] > 0
    assert result["data"].encode() == b"\x00" * result["bytes"]


@pytest.fixture
def instrument():
    return FakeScope(responses={
        "*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0", ":SYSTem:ERRor?": "0",
    })


def test_catalog_has_complete_unique_signatures():
    entries = scpi.catalog()["commands"]
    assert len(entries) == 505
    assert len({entry["command"] for entry in entries}) == 505
    for entry in entries:
        header = entry["command"].replace("<n>", "1").replace("[", "").replace("]", "")
        resolved, _ = scpi.resolve(header)
        assert resolved == entry
        for spec in entry["operations"].values():
            assert set(spec["parameters"]) <= entry["parameters"].keys()


def test_discovery_paginates_and_filters():
    result = scpi.discover(subsystem="acquire", limit=2)
    assert result["total"] == 7
    assert len(result["commands"]) == 2
    assert result["next_offset"] == 2
    assert scpi.discover(search="ACRMs")["total"] >= 2
    assert "parameters" not in result["commands"][0]
    assert "parameters" in scpi.discover(command=":ACQ:TYPE")


def test_catalog_default_response_is_compact():
    import json

    result = scpi.discover()
    assert len(result["commands"]) == 10
    assert len(json.dumps(result)) < 2048


def test_every_catalog_operation_can_be_prepared():
    prepared = 0
    for entry in scpi.catalog()["commands"]:
        header = entry["command"].replace("<n>", "1").replace("[", "").replace("]", "")
        for operation, spec in entry["operations"].items():
            if any(entry["parameters"][name]["type"] == "binary" for name in spec["parameters"]):
                continue
            arguments = []
            for name in spec["parameters"]:
                parameter = entry["parameters"][name]
                arguments.append(parameter["enum"][0] if parameter.get("enum") else
                                 1 if parameter["type"] in {"integer", "real"} else "test")
            assert scpi.prepare(header, operation, arguments)[0] == entry
            prepared += 1
    assert prepared == 896


@pytest.mark.parametrize("header,operation,arguments,expected", [
    (":MEAS:ITEM", "query", ["ACRMS", "CHAN1"], ":MEASure:ITEM? ACRMS,CHANNEL1"),
    (":TRIG:PULS:UWID", "write", [0.001], ":TRIGger:PULSe:UWIDth 0.001"),
    (":CHAN1:DISP", "write", [True], ":CHANnel1:DISPlay ON"),
    (":TIM:SCAL", "write", [0.001], ":TIMebase:MAIN:SCALe 0.001"),
    (":MEAS:SET:DSB", "query", [], ":MEASure:SETup:DSB?"),
])
def test_prepare_documented_commands(header, operation, arguments, expected):
    assert scpi.prepare(header, operation, arguments)[1] == expected


@pytest.mark.parametrize("header,operation,arguments", [
    (":TRIG:EDGE:SOUR", "write", ["EXT"]),
    (":TRIG:MODE", "write", ["LIN"]),
    (":CHAN5:DISP", "write", [True]),
    (":LA:DISP", "query", []),
    (":MEAS:ITEM", "query", ["VPP", "D0"]),
    (":ACQ:MDEP", "write", ["50M"]),
    (":CHAN1:SCAL", "write", [float("nan")]),
    (":CHAN1:DISP;:RUN", "write", [True]),
    (":TRIG:EDGE:SOUR", "write", ["CHAN1;:RUN"]),
    (":CHAN1:SCAL", "write", []),
])
def test_prepare_rejects_unsupported_or_invalid_commands(header, operation, arguments):
    with pytest.raises(ValueError):
        scpi.prepare(header, operation, arguments)


def test_execute_query_reads_response(instrument):
    instrument.responses[":MEASure:ITEM? VPP,CHANNEL1"] = "2.0"
    result = scpi.execute(instrument, ":MEAS:ITEM", arguments=["VPP", "CHAN1"])
    assert result["value"] == "2.0"
    assert instrument.written == []


def test_execute_write_checks_errors(instrument):
    result = scpi.execute(instrument, ":CHAN1:DISP", "write", [True])
    assert result["written"]
    assert instrument.written == [":CHANnel1:DISPlay ON"]


def test_binary_query_preserves_data(instrument, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    payload = b"test\n\x00\xff"
    instrument.load(make_block(payload))
    result = scpi.execute(instrument, ":SYST:SET", "query")
    assert "data_base64" not in result
    assert Path(result["path"]).read_bytes() == payload
    assert result["bytes"] == len(payload)


@pytest.mark.parametrize("size", [16, 2048])
def test_inline_binary_is_explicit_and_bounded(instrument, monkeypatch, tmp_path, size):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    payload = b"x" * size
    instrument.load(make_block(payload))
    result = scpi.execute(instrument, ":SYST:SET", inline_binary=True)
    assert ("data_base64" in result) == (size <= scpi.INLINE_BINARY_LIMIT)
    assert Path(result["path"]).read_bytes() == payload


def test_large_text_is_saved_with_bounded_preview(instrument, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    response = "1,2,3," * 1000
    instrument.responses[":BUS1:DATA?"] = response
    result = scpi.execute(instrument, ":BUS1:DATA")
    assert len(result["preview"]) == 256
    assert "value" not in result
    assert Path(result["path"]).read_text() == response


def test_binary_setup_upload(instrument):
    writes = []
    instrument.write_raw = writes.append
    scpi.execute(instrument, ":SYST:SET", "write", data_base64=base64.b64encode(b"abc").decode())
    assert writes == [b":SYSTem:SETup #13abc\n"]


def test_setup_restore_uses_saved_file(instrument, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    saved = scpi._save_data(b"abc", ".bin")
    writes = []
    instrument.write_raw = writes.append
    scpi.execute(instrument, ":SYST:SET", "write", data_path=saved["path"])
    assert writes == [b":SYSTem:SETup #13abc\n"]


def test_capture_reads_are_bounded_and_lossless_as_base64(monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    payload = bytes(range(256)) * 20
    saved = scpi._save_data(payload, ".bin")
    result = scpi.read_capture(saved["path"], offset=10, max_bytes=32, encoding="base64")
    assert base64.b64decode(result["data"]) == payload[10:42]
    assert result["next_offset"] == 42
    assert result["total_bytes"] == len(payload)
    assert scpi.read_capture(saved["path"], offset=len(payload))["next_offset"] is None
    with pytest.raises(ValueError, match="max_bytes"):
        scpi.read_capture(saved["path"], max_bytes=100000)


def test_capture_reads_reject_outside_files_and_symlinks(monkeypatch, tmp_path):
    directory = tmp_path / "captures"
    directory.mkdir()
    monkeypatch.setenv("RIGOL_DATA_DIR", str(directory))
    external = tmp_path / "private.txt"
    external.write_text("private")
    link = directory / ("capture_" + "a" * 32 + ".txt")
    link.symlink_to(external)
    for path in (external, link, directory / "../private.txt"):
        with pytest.raises(ValueError, match="Only generated"):
            scpi.read_capture(str(path))


def test_catalog_rejects_other_instruments(instrument):
    instrument.responses["*IDN?"] = FakeScope.DEFAULT_IDN
    with pytest.raises(ValueError, match="DHO814"):
        scpi.execute(instrument, ":RUN", "write")
    assert instrument.written == []


def test_download_waveform_streams_chunks(instrument, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    chunks = iter(["1,2", "3,4", "5"])
    instrument.responses.update({
        ":TRIGger:STATus?": "STOP", ":CHAN1:DISPlay?": "1",
        ":ACQuire:MDEPth?": "6", ":WAVeform:PREamble?": "2,2,2,1,0.001,-0.003,0,1,0,0",
        ":WAVeform:DATA?": lambda: next(chunks),
    })
    result = scpi.download_waveform(instrument, "CHAN1", start=2, points=5, chunk_points=2)
    assert result["points"] == 5
    assert result["time_start_s"] == pytest.approx(-0.002)
    assert result["time_end_s"] == pytest.approx(0.002)
    with Path(result["path"]).open() as captured:
        rows = list(csv.reader(captured))
    assert len(rows) == 6
    assert [float(row[1]) for row in rows[1:]] == [1, 2, 3, 4, 5]
    assert ":WAVeform:STARt 6" in instrument.written
    assert not list(tmp_path.glob("*.partial"))


def test_download_rejects_running_scope(instrument):
    instrument.responses[":TRIGger:STATus?"] = "RUN"
    with pytest.raises(ValueError, match="Stop acquisition"):
        scpi.download_waveform(instrument, "CHAN1")
    assert instrument.written == []


def test_download_removes_truncated_capture(instrument, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_DATA_DIR", str(tmp_path))
    instrument.responses.update({
        ":TRIGger:STATus?": "STOP", ":CHAN1:DISPlay?": "1", ":ACQuire:MDEPth?": "3",
        ":WAVeform:PREamble?": "2,2,3,1,0.001,0,0,1,0,0", ":WAVeform:DATA?": "1,2",
    })
    with pytest.raises(ValueError, match="Truncated waveform"):
        scpi.download_waveform(instrument, "CHAN1")
    assert not list(tmp_path.iterdir())
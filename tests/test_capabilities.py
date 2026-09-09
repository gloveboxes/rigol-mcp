import copy

import pytest

from rigol_mcp import capabilities
from rigol_mcp import drivers, scope
from rigol_mcp.scpi import catalog
from tests.conftest import FakeScope
from tools.build_scpi_catalog import parameters_for
from tools.check_capability_reference import check_reference


def test_pinned_source_matches_command_catalog():
    data = capabilities.dataset()
    assert data["schema_version"] == 1
    assert data["dataset_version"] == "dho800900-v1"
    assert data["review"]["status"] == "reviewed"
    assert data["source"]["sha256"] == catalog()["source_sha256"]
    assert data["source"]["url"] == catalog()["source"]
    assert data["source"]["publication"] == "PGA39106-1110"
    assert data["source"]["software_version"] == "00.01.03"


def test_all_claims_have_exact_citations():
    data = capabilities.dataset()
    for model, overrides in data["models"].items():
        facts = data["shared"] | overrides
        assert set(capabilities.reviewed_facts(model)) == set(facts)
        for field, evidence in capabilities.evidence_for(model).items():
            assert evidence["status"] == "documented"
            assert evidence["section"]
            assert all(isinstance(page, int) and page > 0 for page in evidence["pages"])
            assert facts[field]["citation"] in data["citations"]


def test_measurement_claims_match_catalog_enums():
    facts = capabilities.reviewed_facts("DHO814")
    single = facts["measurement_items"]["value"]
    dual = facts["two_source_items"]["value"]
    assert len(single) == len(set(single)) == 34
    assert len(dual) == len(set(dual)) == 8
    assert not set(single) & set(dual)
    for section in ("3.17.2", "3.17.8"):
        entry = next(entry for entry in catalog()["commands"] if entry["section"] == section)
        assert set(single) | set(dual) == {name.upper() for name in entry["parameters"]["item"]["enum"]}


def test_repeated_pdf_table_header_is_not_an_enum_value():
    block = "\nParameter\nName Type Range Default\n<item> Discrete {RFDelay|\n-\nName Type Range Default\nFRDelay|FFDelay} -\nRemarks\n"
    assert parameters_for(block)["item"]["enum"] == ["RFDelay", "FRDelay", "FFDelay"]


@pytest.mark.parametrize("model", ["DHO802", "DHO804", "DHO812", "DHO814", "DHO914", "DHO914S", "DHO924", "DHO924S"])
def test_model_table_and_external_trigger_restriction(model):
    facts = capabilities.reviewed_facts(model)
    external = model in {"DHO802", "DHO812"}
    assert facts["channels"]["value"] == [f"CHAN{number}" for number in range(1, (2 if external else 4) + 1)]
    assert facts["external_trigger"]["value"] is external
    assert facts["horizontal_divisions"]["value"] == 10


def test_unreviewed_and_uncited_claims_are_not_promoted(monkeypatch):
    data = copy.deepcopy(capabilities.dataset())
    monkeypatch.setattr(capabilities, "dataset", lambda: data)
    data["review"]["status"] = "pending"
    assert capabilities.reviewed_facts("DHO814") == {}
    assert capabilities.evidence_for("DHO814") == {}
    data["review"]["status"] = "reviewed"
    del data["models"]["DHO814"]["external_trigger"]["citation"]
    assert "external_trigger" not in capabilities.evidence_for("DHO814")
    assert capabilities.reviewed_facts("DHO9999") == {}


def test_returned_facts_do_not_mutate_dataset():
    capabilities.reviewed_facts("DHO814")["channels"]["value"].clear()
    assert len(capabilities.reviewed_facts("DHO814")["channels"]["value"]) == 4


def test_runtime_values_and_citations_use_the_reviewed_dataset():
    instrument = FakeScope(responses={"*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,00.01.05"})
    result = scope.get_capabilities(instrument)
    facts = capabilities.reviewed_facts("DHO814")
    for field, fact in facts.items():
        expected = sorted(fact["value"]) if field.endswith("_items") else fact["value"]
        assert result[field] == expected
        assert result["evidence"][field]["dataset"] == capabilities.dataset()["dataset_version"]
        assert result["evidence"][field]["section"] == capabilities.dataset()["citations"][fact["citation"]]["section"]
    assert set(result["measurement_items"]) == scope.MEASURE_ITEMS | drivers.DHO.extra_measure_items
    assert set(result["two_source_items"]) == drivers.DHO.two_source_items


def test_runtime_does_not_label_uncited_claims_documented(monkeypatch):
    data = copy.deepcopy(capabilities.dataset())
    del data["models"]["DHO814"]["external_trigger"]["citation"]
    monkeypatch.setattr(capabilities, "dataset", lambda: data)
    instrument = FakeScope(responses={"*IDN?": "RIGOL TECHNOLOGIES,DHO814,SN,1.0"})
    result = scope.get_capabilities(instrument)
    assert result["evidence"]["external_trigger"]["status"] == "unverified"
    assert result["evidence"]["channels"]["status"] == "documented"


def test_changed_reference_is_rejected_before_parsing(tmp_path):
    candidate = tmp_path / "changed-reference.pdf"
    candidate.write_bytes(b"not the reviewed reference")
    with pytest.raises(ValueError, match="Reference changed"):
        check_reference(candidate)
    assert candidate.read_bytes() == b"not the reviewed reference"
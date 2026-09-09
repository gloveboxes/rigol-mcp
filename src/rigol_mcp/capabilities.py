"""Local, versioned evidence for documented model capabilities."""

import copy
import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def dataset() -> dict:
    data = json.loads(files("rigol_mcp").joinpath("capabilities.json").read_text())
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported capability dataset schema")
    return data


def reviewed_facts(model: str) -> dict:
    data = dataset()
    if data.get("review", {}).get("status") != "reviewed" or model not in data.get("models", {}):
        return {}
    facts = data.get("shared", {}) | data["models"][model]
    return copy.deepcopy({
        field: fact for field, fact in facts.items()
        if "value" in fact and fact.get("citation") in data.get("citations", {})
        and data["citations"][fact["citation"]].get("section")
        and data["citations"][fact["citation"]].get("pages")
    })


def evidence_for(model: str) -> dict:
    data = dataset()
    return {
        field: {
            "status": "documented",
            "source": data["source"]["publication"],
            "dataset": data["dataset_version"],
            **copy.deepcopy(data["citations"][fact["citation"]]),
        }
        for field, fact in reviewed_facts(model).items()
    }
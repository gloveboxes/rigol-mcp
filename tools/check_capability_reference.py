"""Audit a local Rigol PDF against reviewed artifacts without changing them or using the network."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from rigol_mcp.capabilities import dataset
from rigol_mcp.scpi import catalog
from tools.build_scpi_catalog import build_catalog


def check_reference(pdf: Path) -> dict:
    data = dataset()
    if data.get("review", {}).get("status") != "reviewed":
        raise ValueError("Capability dataset is not reviewed")
    actual_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if actual_hash != data["source"]["sha256"]:
        raise ValueError(
            f"Reference changed: SHA-256 {actual_hash}; expected {data['source']['sha256']}. "
            "Review document changes and increment dataset_version before updating pinned artifacts."
        )

    from pypdf import PdfReader

    pages = [page.extract_text() for page in PdfReader(pdf).pages]
    text = "\n".join(pages)
    for name in ("publication", "software_version"):
        if data["source"][name] not in text:
            raise ValueError(f"Source {name} not found in PDF")

    for name, citation in data["citations"].items():
        cited = []
        for page_number in citation["pages"]:
            page = pages[page_number + 21]
            if not re.search(rf"(?m)^\s*{page_number}\s*$", page):
                raise ValueError(f"Printed page mismatch for citation {name}: {page_number}")
            cited.append(page)
        if not re.search(rf"(?m)^\s*{re.escape(citation['section'])}\s", "\n".join(cited)):
            raise ValueError(f"Section not found on cited pages: {name}")

    model_table = " ".join(pages[23].split())
    for model, facts in data["models"].items():
        match = re.search(rf"\b{re.escape(model)}\s+\d+\s+MHz\s+(\d)(\+EXT)?\b", model_table)
        if not match:
            raise ValueError(f"Model missing from reference table: {model}")
        if int(match[1]) != len(facts["channels"]["value"]) or bool(match[2]) != facts["external_trigger"]["value"]:
            raise ValueError(f"Model-table capability mismatch: {model}")

    generated = build_catalog(text)
    shipped = catalog()
    if shipped["source_sha256"] != actual_hash:
        raise ValueError("Command catalog uses a different reference revision")
    for field in ("model", "source", "commands", "excluded", "errata"):
        if generated[field] != shipped[field]:
            raise ValueError(f"Command catalog drift in {field}; review regeneration before updating")
    return {"dataset": data["dataset_version"], "sha256": actual_hash,
            "models_checked": len(data["models"]), "citations_checked": len(data["citations"]),
            "commands_checked": len(generated["commands"]), "result": "consistent"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    arguments = parser.parse_args()
    try:
        result = check_reference(arguments.pdf)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Reference audit failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
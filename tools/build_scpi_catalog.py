"""Extract command signatures and parameter facts from Rigol's DHO800/900 guide."""

import argparse
import hashlib
import json
import re
from pathlib import Path


SOURCE_URL = (
    "https://cdn-aorpci9.actonsoftware.com/acton/cdna/1579/"
    "f-f798be30-18ec-4846-9ffe-40b125f49133/1/5/DHO800900_ProgrammingGuide_EN.pdf"
)
EXCLUDED_SECTIONS = {
    "3.5": "Bode plot requires DHO914S/DHO924S",
    "3.11": "Histogram requires DHO900",
    "3.13": "Digital channels require DHO900",
    "3.25": "Signal generator requires DHO914S/DHO924S",
    "3.4.15": "LIN decoding requires DHO900",
    "3.27.24": "LIN triggering requires DHO900",
}
SECTION = re.compile(r"^(3\.\d+(?:\.\d+)+)\s+([:*][^\n]+)$", re.MULTILINE)
HEADER = re.compile(r"^([:*](?:[A-Za-z0-9:*_?]|\[:[A-Za-z]+\]|<n>)+)(.*)$")
PARAMETER = re.compile(r"(<[^>]+>)\s+(ASCII String|Discrete|Integer|Real|Bool|Binary|String)\b", re.IGNORECASE)


def clean_text(text):
    return "\n".join(
        line.strip() for line in text.splitlines()
        if line.strip() not in {"Command System", "Guide", "DHO800/DHO900 Programming"}
        and not line.strip().startswith(("Copyright", "PDF PAGE"))
        and not line.strip().isdigit()
    )


def parameters_for(block):
    if "\nParameter\n" not in block:
        return {}
    table = block.split("\nParameter\n", 1)[1]
    table = re.split(r"\n(?:Remarks|Return Format|Example)\n", table, maxsplit=1)[0]
    table = " ".join(table.split())
    table = re.sub(r"(?:-\s*)?Name Type Range Default\s*", "", table)
    matches = list(PARAMETER.finditer(table))
    parameters = {}
    for index, match in enumerate(matches):
        name = match[1][1:-1]
        kind = match[2].lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(table)
        constraint = table[match.end():end].strip()
        choices = re.search(r"\{([^{}]+)\}", constraint)
        parameter = {"type": kind}
        if kind == "bool":
            parameter["enum"] = ["ON", "OFF", "1", "0"]
        elif choices:
            values = []
            for value in choices[1].split("|"):
                value = value.strip()
                tokens = value.split()
                if len(tokens) > 1 and len({token.casefold() for token in tokens}) == 1:
                    value = tokens[0]
                if not re.fullmatch(r"D\d+|EXT|LIN", value, re.IGNORECASE):
                    values.append(value)
            parameter["enum"] = values
        parameters[name] = parameter
    return parameters


def build_catalog(text):
    text = clean_text(text)
    headings = [match for match in SECTION.finditer(text) if "..." not in match[2]]
    commands = []
    excluded = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end():end]
        if "\nSyntax\n" not in block:
            continue
        section = heading[1]
        reason = next((reason for prefix, reason in EXCLUDED_SECTIONS.items()
                       if section == prefix or section.startswith(prefix + ".")), None)
        if reason:
            excluded.append({"section": section, "command": heading[2], "reason": reason})
            continue
        syntax = block.split("\nSyntax\n", 1)[1].split("\nDescription\n", 1)[0]
        if section == "3.17.15":
            syntax = ":MEASure:SETup:DSB <source>\n:MEASure:SETup:DSB?"
        syntax = re.sub(r":\s+", ":", syntax)
        syntax = re.sub(r"\s+<n>(?=:)", "<n>", syntax)
        forms = [" ".join(form.split()) for form in re.split(r"(?m)(?=^[:*])", syntax) if form.strip()]
        operations = {}
        template = None
        for form in forms:
            match = HEADER.fullmatch(form)
            if not match:
                raise ValueError(f"Unrecognized syntax in {section}: {form!r}")
            template = match[1].rstrip("?")
            operation = "query" if match[1].endswith("?") else "write"
            arguments = match[2].strip()
            if operation in operations:
                raise ValueError(f"Duplicate {operation} form in {section}")
            operations[operation] = {
                "syntax": arguments,
                "parameters": re.findall(r"<([^>]+)>", arguments),
            }
        if template is None:
            raise ValueError(f"No command found in {section}")
        parameters = parameters_for(block)
        if template.startswith(":MATH<n>") and "n" not in parameters:
            parameters["n"] = {"type": "discrete", "enum": ["1", "2", "3", "4"]}
        if section == "3.27.21.8":
            parameters["direction"] = parameters.pop("dir")
        if section == "3.3.2":
            parameters["mdep"]["enum"] = [value for value in parameters["mdep"]["enum"]
                                           if value.upper() not in {"50M", "50000000", "5E7"}]
        for operation in operations.values():
            if missing := set(operation["parameters"]) - parameters.keys():
                raise ValueError(f"Missing parameter types in {section}: {missing}")
        commands.append({
            "command": template,
            "section": section,
            "subsystem": template.lstrip(":*").split(":")[0].replace("<n>", "").rstrip("[").lower(),
            "operations": operations,
            "parameters": parameters,
        })
    if not commands:
        raise ValueError("No programming-guide commands found")
    return {
        "model": "DHO814",
        "source": SOURCE_URL,
        "errata": {
            "3.17.15": "Query question mark restored from the section's example",
            "3.27.21.8": "Direction parameter name aligned with the syntax",
        },
        "commands": commands,
        "excluded": excluded,
    }


def main():
    from pypdf import PdfReader

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    reader = PdfReader(arguments.pdf)
    catalog = build_catalog("\n".join(page.extract_text() for page in reader.pages))
    catalog["source_sha256"] = hashlib.sha256(arguments.pdf.read_bytes()).hexdigest()
    arguments.output.write_text(json.dumps(catalog, indent=2, ensure_ascii=True) + "\n")
    print(f"Catalog: {len(catalog['commands'])} commands; {len(catalog['excluded'])} model exclusions")


if __name__ == "__main__":
    main()
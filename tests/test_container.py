"""Opt-in Docker checks: set RIGOL_TEST_IMAGE to a locally built image tag."""

import asyncio
import json
import os
import subprocess

import pytest
from mcp import Client, StdioServerParameters


IMAGE = os.environ.get("RIGOL_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="Set RIGOL_TEST_IMAGE to test Docker packaging")
RUN_FLAGS = [
    "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
    "--tmpfs", "/data:rw,noexec,nosuid,uid=10001,gid=10001,size=16m",
]


def test_container_runtime_is_minimal_and_non_root():
    inspection = """
import importlib.metadata
import json
import os
from pathlib import Path
from rigol_mcp.scpi import catalog
from rigol_mcp.capabilities import dataset, evidence_for
directory = Path(os.environ['RIGOL_DATA_DIR'])
directory.mkdir(parents=True, exist_ok=True)
(directory / 'write-test').write_text('ok')
print(json.dumps({
    'uid': os.getuid(),
    'commands': len(catalog()['commands']),
    'capability_dataset': dataset()['dataset_version'],
    'external_trigger_section': evidence_for('DHO814')['external_trigger']['section'],
    'packages': [package.metadata['Name'].lower() for package in importlib.metadata.distributions()],
    'dotenv_disabled': os.environ.get('PYTHON_DOTENV_DISABLED'),
    'local_env_baked_in': Path('/app/.env').exists() or Path('/app/.env.example').exists(),
}))
"""
    result = subprocess.run(
        ["docker", "run", *RUN_FLAGS, "--entrypoint", "python", IMAGE, "-c", inspection],
        check=True, capture_output=True, text=True, timeout=60,
    )
    runtime = json.loads(result.stdout)
    assert runtime["uid"] == 10001
    assert runtime["commands"] == 505
    assert runtime["capability_dataset"] == "dho800900-v1"
    assert runtime["external_trigger_section"] == "3.27.8.1"
    assert runtime["dotenv_disabled"] == "1"
    assert not runtime["local_env_baked_in"]
    assert not {"pytest", "uv", "hatchling", "pyusb", "libusb-package", "typer", "pypdf"} & set(runtime["packages"])


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_container_stdio_protocol(mode):
    parameters = StdioServerParameters(command="docker", args=["run", "-i", *RUN_FLAGS, IMAGE])
    async with asyncio.timeout(60):
        async with Client(parameters, mode=mode, read_timeout_seconds=30) as client:
            assert client.server_info.name == "rigol-mcp"
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"get_capabilities", "scpi_catalog", "scpi_execute", "read_capture"} <= names
            assert "send_raw" not in names
            catalog = await client.call_tool("scpi_catalog", {"subsystem": "acquire"})
            assert not catalog.is_error
            assert json.loads(catalog.content[0].text)["total"] == 7
            invalid = await client.call_tool("measure", {})
            assert invalid.is_error
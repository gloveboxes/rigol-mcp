# rigol-mcp

A standards-based [Model Context Protocol](https://modelcontextprotocol.io/) server for controlling a Rigol oscilloscope over LAN/TCPIP. Any MCP client supporting stdio can discover its tools, configure the instrument, take measurements and retrieve captures. No particular agent or client is required.

![Scope](media/example.png)

## Scope Support

The programming reference is Rigol's **DHO800/DHO900** guide. The **DHO814 belongs to the DHO800 series** and is the primary tested target. Its model-filtered command catalog and streamed memory downloads are enabled specifically for DHO814; this is not a claim of complete support for every model covered by the guide.

Existing DS1000Z/MSO1000Z and DHO924S convenience-tool support is retained separately. Other families are not verified. See [DHO814 support](docs/dho814-support.md) for reference provenance, coverage, model exclusions and hardware verification.

## Requirements

- A supported scope reachable over Ethernet on TCP port **5555**.
- An MCP client that can launch a stdio server.
- Docker, or Apple's `container` runtime on Apple silicon, installed and running
  with its CLI available to the MCP client.

The server runs only in containers. No host Python, uv, or native installation is required.

## Installation

```bash
git clone https://github.com/gloveboxes/rigol-mcp
cd rigol-mcp
```

### Docker

Build the lightweight Alpine image and pass the scope address at runtime:

```sh
docker build -t rigol-mcp:local .
docker run --rm -i \
  -e RIGOL_IP=192.168.1.123 \
  --mount type=volume,src=rigol-mcp-data,dst=/data \
  rigol-mcp:local
```

The MCP client normally launches this command. Use `-i`, not `-t`; no ports need publishing. See [Docker setup](docs/docker.md) for client configuration, persistent storage, hardened launch options and the stdio-versus-HTTP tradeoff.

### Apple Silicon Container

On Apple silicon, [Apple Container](https://github.com/apple/container) can build the same image and run it without Docker Desktop. Apple's supported requirements are an Apple silicon Mac and macOS 26; older macOS versions are not supported.

Install the [Container formula](https://formulae.brew.sh/formula/container) using [Homebrew](https://brew.sh), then verify the CLI:

```sh
brew install container
container --version
```

Ensure Homebrew's bin directory (normally `/opt/homebrew/bin` on Apple silicon) is on the PATH used by your terminal and MCP client. Restart VS Code if it cannot find `container` after installation.

Start the service and build the MCP image from this repository's root:

```sh
container system start
container build -t rigol-mcp:local .
```

Initialize the Apple capture volume once so the non-root server can write to it:

```sh
container run --rm --progress none --user 0 \
  --mount type=volume,source=rigol-mcp-data,target=/data \
  --entrypoint sh rigol-mcp:local -c \
  'mkdir -p /data/captures /data/screenshots && chown 10001:10001 /data /data/captures /data/screenshots'
```

Then launch the server:

```sh
container run --rm -i --read-only --progress none \
  --tmpfs /tmp --cap-drop ALL \
  -e RIGOL_IP=192.168.1.123 \
  --mount type=volume,source=rigol-mcp-data,target=/data \
  rigol-mcp:local
```

Apple Container keeps images and volumes separately from Docker. See the full [Apple Container setup](https://github.com/gloveboxes/rigol-mcp/blob/main/docs/apple-container.md) for VS Code configuration, storage and networking details.

## Configuration

Find the scope's address in its LAN settings and replace `192.168.1.123` in the examples. Ensure TCP port 5555 is reachable. LAN/TCPIP is the only supported instrument transport.

Pass `RIGOL_IP` with Docker's `-e` option, as shown above, or set it in the MCP client configuration below. No environment file is required and no IP address is baked into the image. Docker's `--env-file` remains an optional alternative for managing runtime variables.

| Variable | Container Default | Purpose |
|---|---|---|
| `RIGOL_IP` | (required) | Scope IP address |
| `RIGOL_ENABLE_SEND_RAW` | (unset) | Set to `1` for unrestricted SCPI; see [Safety](#safety) |
| `RIGOL_SCREENSHOT_DIR` | `/data/screenshots` | Directory for saved PNG screenshots |
| `RIGOL_DATA_DIR` | `/data/captures` | Directory for waveform CSV downloads and binary/large SCPI responses |

Docker stores captures and screenshots under `/data`; mount it to retain files after the container exits. Returned paths are container paths, not host paths.

## MCP Client Setup

Choose one runtime and place its configuration in `.vscode/mcp.json`. Do not add both entries for the same scope.

### Docker

```json
{
  "servers": {
    "rigol-scope": {
      "type": "stdio",
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "-e", "RIGOL_IP", "-e", "RIGOL_ENABLE_SEND_RAW",
        "--mount", "type=volume,src=rigol-mcp-data,dst=/data",
        "rigol-mcp:local"
      ],
      "env": {
        "RIGOL_IP": "192.168.1.43",
        "RIGOL_ENABLE_SEND_RAW": "0"
      }
    }
  }
}
```

### Apple Container

```json
{
  "servers": {
    "rigol-scope": {
      "type": "stdio",
      "command": "container",
      "args": [
        "run", "--rm", "-i", "--read-only", "--progress", "none",
        "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "-e", "RIGOL_IP", "-e", "RIGOL_ENABLE_SEND_RAW",
        "--mount", "type=volume,source=rigol-mcp-data,target=/data",
        "rigol-mcp:local"
      ],
      "env": {
        "RIGOL_IP": "192.168.1.43",
        "RIGOL_ENABLE_SEND_RAW": "0"
      }
    }
  }
}
```

Edit `env.RIGOL_IP` for your scope. The included [.vscode/mcp.json](.vscode/mcp.json) contains both working definitions for repository development, but a normal client setup should use only the selected runtime. Clients that use an `mcpServers` section can adapt the chosen process entry to their configuration format. See [Docker MCP client configuration](docs/docker.md#mcp-client-configuration) or [Apple Container setup](docs/apple-container.md) for prerequisites.

## Discover Before Acting

MCP tool discovery supplies descriptions and input schemas. Measurement lists and command signatures do not need to be copied from this README.

1. Call `inspect_scope` for identity, capabilities and current channel, timebase
  and trigger settings in one sequential operation. Check `complete` and `errors`
  before acting. Failed stages stop inspection without retrying; completed results
  are retained. Set `verify_hardware=true` to enable channel/grid count probes
  that read and clear SCPI errors; the default is `false`.
2. Use `idn`, `get_capabilities` or `get_scope_state` for targeted queries.
3. Use the advertised convenience tools for common tasks. For other DHO814 operations,
   search `scpi_catalog`, request one command's details, then use `scpi_execute`.

`get_capabilities` labels each fact in an `evidence` map as **hardware-verified**, **documented**, or **unverified**. By default it checks DHO800/900 channel and grid counts against the scope, reporting model mismatches. Other facts still rely on model definitions; measurement lists do not establish measurement accuracy. Documented facts come from a locally reviewed, versioned dataset with publication, section and page citations. CI checks its consistency with the pinned command catalog; no reference material is fetched or trusted automatically at runtime. Use `verify_hardware=false` to skip these extra queries. See [capability evidence](docs/dho814-support.md#capability-evidence) for limitations. `scpi_catalog` comes from the bundled programming-guide reference, not from an API downloaded from the scope.

Example request to an agent:

> Identify the scope, check its capabilities and current settings, then measure
> frequency and peak-to-peak voltage on channel 1.

## Semantic Workflows

Common DHO814 operations have validated semantic tools for acquisition, measurement statistics, meter, serial/CAN decode, protocol triggers, math, reference, mask testing, waveform search, and frame recording/replay. `analyze_waveform` returns compact statistics, frequency and FFT results without saving the raw samples. To retain raw data, use `capture_waveforms`, `acquire_and_capture`, or `get_waveform` with `raw_data=true`. Its rate metadata distinguishes the hardware acquisition rate from the displayed point rate, identifies interpolated display points, and caps the analysis Nyquist limit at the lower of those rates. DHO math traces (`MATH1` through `MATH4`) are accepted by the screen waveform and aligned capture tools.

Decoder configuration accepts protocol-specific voltage thresholds through `settings.thresholds_v`, for example `{"TX": 1.65}` or `{"SCL": 1.65, "SDA": 1.65}`. DHO814 NORM waveform transfer can return no data while a decoder overlay is displayed; disable that bus display or use a RAW download.

`configure_math` supports FFT and filter settings. Filter cutoff readback is compared with the requested value and reports a warning when acquisition limits force the scope to clamp it. `analyze_pwm_envelope` extracts PWM carrier stability, duty and reconstructed average-voltage envelopes, modulation frequency, and phase between two aligned channels. Settled rail estimates exclude edge overshoot from the reconstructed voltage, while generic analysis reports robust swing, overshoot, and undershoot separately. PWM results include edge, period, and envelope sample counts with a confidence level. Reconstruction does not turn an unfiltered PWM pin into an analog output.

`acquire_and_capture` arms a single acquisition, waits for completion with a bounded timeout, and saves aligned channel traces. It stops acquisition on timeout. For an already stopped record, use `capture_waveforms`. Search event results are paginated; mask counters and measurement statistics return structured numeric validity.

Use `save_scope_setup` before temporary reconfiguration. `restore_scope_setup` only accepts generated files, requires a single-use confirmation, and is not retried. New snapshots include a represented-state sidecar; restoration reads the scope back and reports any channel, timebase, or trigger mismatch. Recording and replay controls are also not retried after uncertain outcomes.

For timing work, use `configure_timing_capture` with caller-defined signal labels, voltage domains and trigger criteria. It can derive a useful time scale from signal frequency and cycles visible; it has no protocol- or project-specific assumptions. On a stopped DHO it briefly runs acquisition while applying horizontal scale, then returns to STOP. It also disables a retained delayed/zoom timebase so measurements and captures use the requested main view. Its result includes `fully_applied`, `mismatches`, and fields whose scope readback cannot verify directly. After stopping acquisition, `capture_waveforms` reads several channels from the same stopped acquisition, saves the raw arrays together and returns compact per-channel analysis.

Example arguments for `configure_timing_capture`:

```json
{
  "channels": [
    {"channel": 1, "label": "CLOCK", "voltage_domain_v": 3.3},
    {"channel": 2, "label": "DATA", "voltage_domain_v": 5.0}
  ],
  "trigger": {"channel": 1, "slope": "POS"},
  "signal_frequency_hz": 1000000,
  "cycles_visible": 4,
  "purpose": "Check clock-to-data timing"
}
```

Labels and purpose are evidence metadata supplied by the caller, not interpreted as device semantics. Verify attenuation, probe loading and threshold assumptions against the actual circuit before relying on timing or voltage conclusions.

## Agent Data Usage

Prefer numeric readings and local waveform analysis. Large results and raw arrays stay in files with compact metadata; images and inline binary are opt-in. Use `read_capture` for specific bounded excerpts, not entire files. Full data is retained. See [transfer and output limits](docs/dho814-support.md#transfers).

## Safety

Use one server instance per physical scope and call instrument tools sequentially. Only trusted clients should have access: operations can change acquisition, overwrite scope files, reset the instrument, lock controls or change LAN settings.

Unrestricted `send_raw` is disabled by default. The documented DHO814 catalog is available without enabling it; catalog membership does not make an operation non-destructive. State-changing operations, error-queue reads and memory downloads are not automatically replayed after communication failures. A failed readback can follow a successful write; inspect current state before repeating a change.

## Testing

The default suite is offline and needs no instrument. See [container-based development checks](docs/development.md) to run tests or audit reference data without installing Python on the host. Image smoke tests also run in CI; see [Docker tests](docs/docker.md#container-tests). DHO814 LAN smoke tests passed on firmware 00.01.05 without an input signal. Additional Pico-driven acceptance tests cover statistics, cursors, search, mask, recording/replay, meters, math/reference, RAW transfers, setup restoration, and UART, I2C, and SPI decoding. CAN and parallel decoding, exhaustive behavior for all 897 catalog forms, destructive operations, and deliberate transport-fault recovery remain unverified. See the [advanced acceptance report](docs/reports/dho814_advanced_mcp_acceptance_report.json) and [hardware test coverage and instructions](docs/dho814-support.md#verification-and-maintenance).

## Acknowledgements

This independent project is based on [erebusnz/rigol-mcp](https://github.com/erebusnz/rigol-mcp), originally created by Stig Manning. The original MIT license and copyright notice are preserved.

## License

MIT — see [LICENSE](LICENSE).

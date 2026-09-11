---
name: rigol-scope
description: "Use when controlling or testing a Rigol oscilloscope through MCP: inspect the DHO814, configure acquisition and triggers, measure signals, analyze waveforms or PWM envelopes, decode protocols, use math, meters, masks, search or recording, preserve setup state, download captures, or discover SCPI commands. Use the registered rigol MCP tools."
---

# Rigol Scope

Operate the scope through the registered `rigol-scope` MCP server. The primary
target is the Rigol DHO814 (DHO800 series). Use runtime capabilities to determine
which operations are available on the connected model.

## Connection

The repository's [MCP configuration](../../../.vscode/mcp.json) runs
`rigol-mcp:local` with Apple Container (`container`) on macOS. Docker (`docker`)
is also supported on Windows, Linux and macOS; see the
[Docker setup](../../../docs/docker.md) for build and MCP configuration instructions.
Determine the runtime from the configured command, not the host OS.
MCP uses stdio; the server
connects to the instrument at `RIGOL_IP` over LAN/TCP port 5555. Unrestricted
`send_raw` is disabled by default with `RIGOL_ENABLE_SEND_RAW=0`.

1. Discover the registered tools and their schemas through tool search. Use the
   client-exposed names, which may include a namespace; names below are logical
   tool names.
2. Call `idn`, `get_capabilities`, then `get_scope_state` sequentially. Confirm
   the intended instrument and inspect its settings before making changes.
3. Choose the smallest workflow needed for the task. Call all instrument tools
   sequentially through this server; do not open another server or connection.

If tools are unavailable, ask the user to start `rigol-scope` through **MCP: List
Servers** and enable its tools. After rebuilding the image, ask the user to
restart the MCP server and wait for confirmation before instrument calls.
Use the configured runtime; do not manage its active container yourself.

## Measurements and Captures

- Numeric readings: `measure` or `measure_between`, using measurement items from
  `get_capabilities`. Use `measure_statistics` for current, average, extrema,
  deviation and count values.
- Trace analysis: `get_waveform` for a compact screen-trace interpretation;
  `analyze_waveform` for statistics, timing rates and FFT peaks. Both support
  analog channels and displayed DHO math traces.
- Aligned traces: `acquire_and_capture` arms, waits for STOP with a bounded
  timeout, then saves and analyzes screen traces. It stops acquisition on timeout.
  Use `capture_waveforms` for an already stopped acquisition. `single` only arms;
  it does not wait. Use `run` and `stop` for explicit acquisition control.
- PWM: `analyze_pwm_envelope` analyzes one or two stopped analog traces for
  carrier frequency, duty envelope, modulation and phase.
- Memory export: `download_waveform` saves CSV with timing metadata. Stop
  acquisition before a DHO814 RAW/full-memory transfer.
- Visual inspection: use `screenshot` for on-screen menus, layout or other visual
  questions, with `include_image=true` when the image must be inspected. Prefer
  numeric readings or waveform analysis for signal questions.

## Configuration and Analysis Tools

Inspect each tool's schema for accepted parameters and model restrictions.

| Task | Tools |
| --- | --- |
| Channels, timebase and analog/protocol triggers | `set_channel`, `set_timebase`, `set_trigger` |
| Coordinated channels, timebase, acquisition and trigger | `configure_timing_capture` |
| Acquisition mode, memory depth, averaging and UltraAcquire | `configure_acquisition` |
| Cursors | `set_cursors`, `get_cursor_values` |
| DVM and hardware counter | `configure_meter`, `get_meter_value` |
| Parallel, RS-232, I2C, SPI and CAN decoding | `configure_decode`, `get_decode_result` |
| Math, FFT and filters | `configure_math` |
| Reference traces | `configure_reference` |
| Mask pass/fail testing | `configure_mask_test`, `get_mask_results` |
| Edge and pulse search | `configure_search`, `get_search_results` |
| Frame recording and replay | `configure_recording`, `get_recording_state`, `control_recording_replay` |
| Setup snapshots | `save_scope_setup`, `restore_scope_setup` |

Set decoder `settings.thresholds_v` for the connected logic levels. Follow
`next_offset` for paginated search results. DHO814 has no histogram subsystem;
`configure_histogram` reports availability rather than configuring it.

For operations outside these tools, search `scpi_catalog` by subsystem or command,
then retrieve one command's parameters and manual reference. Use `scpi_execute`
with the command header, explicit `operation` and positional `arguments`.
Replace `<n>` with the intended channel, bus or math index.

## State and Safety

- Make only authorized changes. Before temporary tests, record settings and
  restore them afterward, including acquisition and waveform transfer settings.
  Use `save_scope_setup` before broad reconfiguration; restore the returned path
  with `restore_scope_setup` and its single-use confirmation flow.
- Reset, autoscale, file overwrite, networking changes, control locking,
  self-tests and setup imports require task authorization. Keep `send_raw`
  disabled unless explicitly needed and authorized.
- Measurements and waveform reads can enable channels; measurements may need
  live acquisition, and waveform reads change transfer settings. Screen-trace
  reads do not stop acquisition; stop first when consistency is required.
- After a communication failure, inspect state before retrying: a write may
  already have succeeded. Do not replay state-changing actions blindly.
- Initial connection clears SCPI errors. Capability verification also reads and
  clears errors; use `verify_hardware=false` when probes are unnecessary or the
  existing queue should be preserved. `check_error` drains the queue and returns
  the first error.

## Results and Evidence

Keep results compact. Captures, raw waveform JSON and binary responses are
file-backed. Use `read_capture` only for a specific bounded excerpt; do not load
entire captures, catalogs or base64 into context. Files under `/data` belong to
the container's persistent `rigol-mcp-data` volume, not the host filesystem.
Pass saved setup paths directly to the restore tool without reading their bytes.

Respect capability evidence and mismatch flags: `hardware-verified` describes
facts queried in that call, `documented` describes reviewed references or server
policy, and `unverified` describes assumptions or failed probes. Command support
does not establish measurement accuracy. Open inputs do not provide valid signal
tests, and 9.9E37 is an invalid/overflow sentinel, not a measurement.

Report observed model/firmware, results, warnings, changes and restoration, and
anything unverified. Distinguish offline checks from hardware validation.

For setup and maintenance, consult [Apple Container](../../../docs/apple-container.md),
[Docker](../../../docs/docker.md) or [DHO814 support](../../../docs/dho814-support.md)
as needed.
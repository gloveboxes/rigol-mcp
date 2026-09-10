---
name: rigol-scope
description: "Use when controlling or testing a Rigol oscilloscope through MCP: connect to the DHO814 over LAN, inspect capabilities or settings, configure acquisition, decode and triggers, measure or analyze signals, run mask/search/recording workflows, preserve setup state, download waveforms, capture screenshots, or discover SCPI commands. Prefer the registered rigol MCP tools over direct sockets or ad hoc scripts."
---

# Rigol Scope

Use the configured MCP server. Standalone setups name it `rigol-scope`; the
repository development configuration uses `rigol-docker` and
`rigol-apple-container`. Determine the runtime from the selected entry's command
(`docker` or `container`), not its name. Never use both against one scope.
It speaks MCP over stdio and connects to
the instrument over LAN/TCP port 5555. The main target is DHO814 (DHO800 series);
the reference covers DHO800/DHO900, not DHO8000/DHO9000. Do not infer support for
other models from a family name alone.

## Start and Discover

1. Use the selected server in `.vscode/mcp.json`. Its runtime must be running and
  `rigol-mcp:local` must exist in that runtime's image store. Build from the root
  with `docker build -t rigol-mcp:local .` or `container build -t rigol-mcp:local .`,
  matching the selected runtime. After rebuilding, ask the user to restart the
  selected MCP server and wait for confirmation before resuming instrument calls.
  Do not stop, replace, or restart its container yourself. Apple Container also
  needs one-time capture-volume initialization; see `docs/apple-container.md`.
2. The configuration passes `env.RIGOL_IP` into the container with `-e RIGOL_IP`.
   Edit that value for a different scope; the image contains no scope address.
  It also forwards `RIGOL_ENABLE_SEND_RAW`, which remains `0` by default.
  The container runtime must reach the scope's TCP port 5555.
3. If tools are unavailable, have the user run **MCP: List Servers**, select
  the configured server (`rigol-scope`, or the chosen development entry), and
  start/restart it, accepting any trust prompt themselves. Ensure
   the server's tools are enabled in chat. A skill cannot grant tool access.
4. Discover the available tools. Names below are the server's logical tool names;
   use the actual names exposed by the MCP client, which may include a namespace.
   Load deferred tools through tool search when the client requires it. Never
   invent a tool prefix or substitute terminal commands for unavailable MCP tools.
5. Call `idn`, then `get_capabilities`, then `get_scope_state` sequentially.
   Confirm the intended instrument before making any changes.

Do not launch another server or open a second VISA/socket connection while the
registered server controls the scope. Do not use Docker `-t`, publish a port for
stdio, or assume an HTTP endpoint exists.

## Choose the Tool

- For one numeric reading, use `measure` or `measure_between` with items returned
  by `get_capabilities`. Use `measure_statistics` for structured current, average,
  extrema, deviation and count values. Inspect existing settings before changing them.
- For a quick trace interpretation, use `get_waveform` or `analyze_waveform`. For
  a new stable acquisition, prefer `acquire_and_capture`: it arms, waits with a
  bounded timeout, and saves aligned traces. Use `capture_waveforms` only when the
  acquisition is already stopped. `single` merely arms and does not wait.
- For large/full-memory captures on DHO814, use `download_waveform` after stopping
  acquisition. It saves CSV and returns a path plus timing metadata.
- For common changes use `set_channel`, `set_timebase`, `set_trigger`, or
  `set_cursors`. `set_trigger` includes analog and RS-232/I2C/SPI/CAN triggers.
  Use `configure_timing_capture` for coordinated channels, timebase, acquisition,
  and trigger settings. Discover schemas rather than guessing parameter names.
- Prefer `configure_decode`, mask, search, and recording/replay tools for those
  multi-command workflows. Supply decoder `settings.thresholds_v` for the connected
  logic levels. Search results are paginated; inspect `next_offset`.
- Before temporary broad reconfiguration, use `save_scope_setup`. Restore with
  `restore_scope_setup`, using its exact single-use confirmation flow. Do not read
  and resend the binary setup through model context.
- For other DHO814 operations, search `scpi_catalog` by subsystem or command,
  then pass `command` to retrieve one entry's parameters and manual section.
  Invoke `scpi_execute` with a header, an explicit `operation`, and positional
  `arguments`. Replace `<n>` with the intended channel/bus/math index.
- Use `screenshot` only for a visual question. It returns a saved path by default;
  request `include_image=true` when the agent actually needs to inspect the image.

Examples of logical tool calls (use the client's discovered tool identifiers):

```json
{"tool":"scpi_catalog","arguments":{"search":"AVERages","subsystem":"acquire"}}
```

```json
{"tool":"scpi_catalog","arguments":{"command":":ACQuire:AVERages"}}
```

```json
{"tool":"scpi_execute","arguments":{"command":":ACQuire:AVERages","operation":"query"}}
```

These examples inspect configuration only. Do not interpret them as authorization
to enable averaging or change the acquisition mode.

## Evidence and Data Budget

Treat `get_capabilities.evidence` as part of the result. `hardware-verified` means
a fact was queried in that call; `documented` means a cited reviewed reference or
server policy; `unverified` means an assumption or failed probe. Respect mismatch
flags. A documented command list is not proof of accuracy or firmware behavior.

Capability verification reads and clears SCPI errors. Use `verify_hardware=false`
when extra probes are unnecessary or preserving the existing error queue matters.
Opening the initial scope connection itself clears its SCPI error queue.

Keep results compact. Do not dump catalogs, raw samples, or base64 into context.
Use `read_capture` for a specific bounded excerpt, not to page through entire
files. Raw waveform JSON, binary responses and large results are file-backed.
Container paths live under `/data` in that runtime's persistent `rigol-mcp-data`
volume; Docker and Apple volumes are separate and are not host paths.
Saved setup paths are container paths. Pass one directly to `restore_scope_setup`
without reading and resending its bytes, and only with authorization to replace state.

## Instrument Safety

- Call instrument tools strictly sequentially. One server instance per scope.
- Make only changes needed for the user's task. Record prior settings before
  temporary tests and restore them afterward, including acquisition state and
  waveform transfer settings. Report failed or unverifiable restoration.
- Do not reset, autoscale, overwrite scope files, change networking, lock controls,
  run self-tests, or import setups unless the requested task authorizes it.
  Catalog availability is not a safety guarantee. Leave unrestricted `send_raw`
  disabled unless explicitly needed and authorized.
- When no signal/device is connected, test identity, configuration reads,
  reversible settings, screenshots and bounded transfers. Do not claim valid
  frequency, timing or accuracy from an open input. Low-level noise is expected;
  9.9E37 is an invalid/overflow sentinel, not a measurement.
- Measurements may auto-enable disabled channels; DHO measurement items may need
  live acquisition. Account for these side effects before using them in a test.
- `acquire_and_capture` stops acquisition on timeout. Recording/replay navigation,
  setup restoration, acquisition actions, and semantic configuration writes are
  not automatically replayed after communication failure.
- After a communication failure, a state-changing command may already have run.
  Inspect state before retrying; do not blindly replay resets or other actions.
  Use `check_error` deliberately: it drains the queue and reports the first error.

Report observed model/firmware, successful checks, warnings, changes/restoration,
and what remains unverified. Never equate offline tests with hardware validation.

For installation, coverage, and maintenance details, consult the repository's
`docs/docker.md` and `docs/dho814-support.md` only when needed. Do not load the
entire bundled command catalog into agent context.
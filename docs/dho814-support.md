# DHO814 Support

The DHO814 is the four-channel, 100 MHz DHO800-series oscilloscope.
The source of truth is Rigol's [DHO800/900 Programming Guide](https://cdn-aorpci9.actonsoftware.com/acton/cdna/1579/f-f798be30-18ec-4846-9ffe-40b125f49133/1/5/DHO800900_ProgrammingGuide_EN.pdf).

## Coverage

The shipped catalog contains 505 command headers and 897 query/write forms,
including binary setup upload. All are accessible through `scpi_execute`;
`scpi_catalog` lists compact command names and operations by default; pass `command`
to retrieve one signature, parameter types/enums and manual section. Convenience
tools are a smaller ergonomic layer, not the coverage boundary.

| Subsystem | Accessible operations |
|---|---|
| Root/IEEE | Run, stop, single, force trigger, clear, identification, status registers, operation completion, saved states, reset, self-test |
| Autoset/acquisition | Autoset options, sample type, averages, depth, sample rate, UltraAcquire |
| Analog channels | Display, coupling, bandwidth limit, inversion, scale/offset, probe, deskew, units, labels, vernier, position |
| Timebase | Main/delayed scale and offset, zoom, mode, horizontal reference |
| Trigger | Edge, pulse, slope, video, pattern, duration, timeout, runt, window, delay, setup/hold, Nth edge, RS232, I2C, SPI, CAN; sweep, coupling, holdoff, noise rejection |
| Measurement | 34 single-source items including ACRMS, eight delay/phase items, statistics, thresholds, analog/math sources |
| Cursors | Manual, track and XY modes; sources, axis selection, X/Y positions and readouts |
| Waveform | NORM/RAW/MAX; ASCII/BYTE/WORD; ranges, preamble, X/Y conversion metadata |
| Math/FFT | Four math channels, operators, sources, FFT and display settings |
| Bus decoding | Parallel, RS232, I2C, SPI, CAN setup, display, results and export |
| Analysis | Counter, DVM, mask/pass-fail, search and navigation |
| Storage/recording | Reference waveforms, scope-side save/load, recording/replay, binary setup import/export |
| Display/system/LAN | Screen captures/settings, front-panel controls, power settings, network configuration and status |

Sixty guide entries are excluded because they require DHO900 or DHO9xxS hardware:
digital channels, histogram, LIN, signal generation, and Bode plot. Their section
numbers and exclusion reasons are retained in the catalog. D0-D15, EXT, LIN and
50 Mpoint choices are removed where inappropriate for DHO814. Remaining memory
depth limits depend on enabled channels and are enforced by the instrument.

## Capability Evidence

`get_capabilities` preserves its existing value fields and adds an `evidence` map
with one entry per field. Status is local to the current call, not remembered from
a previously connected instrument:

- `hardware-verified`: the model was read with `*IDN?`, or a successful DHO800/900
   `:SYSTem:RAMount?` / `:SYSTem:GAMount?` query provided channel/grid counts.
- `documented`: a cited claim in the reviewed DHO800/900 dataset, or an explicit server policy.
   `command_catalog` describes server availability, not hardware command validation.
- `unverified`: a broader family-driver assumption or an unsuccessful hardware probe.

Each entry includes a `source`. A valid hardware reply replaces the model value;
disagreements include `mismatch: true` and `model_value` in the evidence. Invalid
replies or SCPI errors retain the model fallback but mark it unverified, with an
error. Transport exceptions propagate to normal reconnect/error handling rather
than being disguised as successful verification.

Guide-derived evidence includes the dataset version, publication number, section
and printed page numbers. The local [capability dataset](../src/rigol_mcp/capabilities.json)
pins publication **PGA39106-1110**, which states software version **00.01.03**.
That is a documentation qualification, not a guarantee for every firmware release;
our separate live smoke test used 00.01.05. The dataset includes its review status,
review method/date, source URL and PDF SHA-256. "Reviewed" means locally curated
against the reference, not certified or published as a machine-readable list by Rigol.

Only cited fields in a reviewed dataset are promoted to documented. Unreviewed
datasets, unknown models and uncited fields retain unverified driver fallbacks.
The model identity and explicit server catalog policy retain their independent
evidence labels. No network requests or PDF parsing occur at runtime.

The MCP tool defaults to `verify_hardware=true`. These probes are restricted to
known DHO800/900 models and read/clear the SCPI error queue; they do not change
acquisition settings. Set `verify_hardware=false` for identity/model data only.
Internal callers, including `get_scope_state`, use this cheaper mode and label
their model-derived facts accordingly. They do not reuse earlier verification.

External-trigger support and measurement lists remain documented, not probed.
The lists describe accepted names, not accuracy or successful operation on the
current signal. Unknown models and legacy families remain unverified except for
their queried identity and explicit server policy. No complete API inventory can
be downloaded from the scope.

## MCP Workflow

1. Call `get_capabilities` to verify the connected model.
2. Call `scpi_catalog` with a subsystem or search string (10 results by default,
   maximum 25). Fetch the selected entry with `command` for parameter details.
   Indexed headers use `<n>` for channel/bus/math number.
3. Call `scpi_execute` with a header, `operation` (`query` or `write`), and positional
   `arguments`. Do not embed arguments or semicolon-separated commands in the header.

Examples of tool arguments:

```json
{"command": ":ACQuire:TYPE", "operation": "write", "arguments": ["AVERages"]}
```

```json
{"command": ":MEASure:STATistic:ITEM", "operation": "query", "arguments": ["MAXimum", "VPP", "CHAN1"]}
```

```json
{"command": ":TRIGger:PULSe:UWIDth", "operation": "write", "arguments": [0.000001]}
```

Select the appropriate trigger or analysis mode before configuring its subordinate
commands. The server checks command membership, index, argument count, primitive
types, and documented enums. State-dependent ranges and prerequisites are checked
by the instrument; consult the linked manual section for them. Queries can also
have side effects, such as consuming an error or status register.

Catalog writes can reset the instrument, overwrite scope-side files, change LAN
settings, or lock controls. They are intentionally available without the unrestricted
`send_raw` flag, but must only be used for the requested operation. Catalog calls
are serialized and never retried automatically: after a transport failure the
operation's outcome may be unknown.

Convenience setters perform their write and targeted readback under one instrument
lock, without replay on failure. Acquisition actions, autoset, unrestricted raw
commands and error-queue reads likewise do not automatically retry. A failed
readback does not imply the preceding write failed; inspect state before deciding
whether to repeat it. Pure configuration reads retain communication retries.

### Generic timing capture

`configure_timing_capture` configures one or more analog channels, acquisition and
trigger as one validated operation. Signal labels, purpose and expected behavior
remain caller-supplied metadata, so the same API applies to buses, clocks, control
lines and unrelated mixed-voltage systems. When frequency is known, `cycles_visible`
selects the horizontal scale unless `time_scale_s_div` is supplied explicitly.
Because DHO814 can ignore horizontal-scale writes while stopped, the operation
briefly runs a stopped acquisition while applying settings and returns it to STOP.
The response reports requested/readback mismatches and unverified settings instead
of treating any partial change as complete success.

Call `stop` before `capture_waveforms`. The latter rejects a running acquisition,
then transfers each requested channel from that stopped record, stores all raw
arrays in one JSON file and returns compact analyses. This provides aligned evidence
without claiming protocol decoding or interpreting project-specific signal names.

The DHO814 analog bandwidth and sample rate are useful for many digital timing
checks but do not replace a logic analyzer for long digital captures or protocol
history. Probe attenuation, loading, grounding and voltage limits remain physical
setup responsibilities outside the MCP server.

### Acquisition and analysis workflows

`acquire_and_capture` performs a complete single-shot transaction under one
instrument lock: arm, poll trigger status with a bounded timeout, stop on timeout,
then save and analyze selected screen traces. It is not replayed after communication
failure. `measure_statistics` returns requested current, average, extrema, deviation,
and count values with explicit validity for Rigol overflow sentinels.

Screen waveform tools accept displayed DHO `MATH1` through `MATH4` traces as well
as analog channels. Their metadata reports hardware acquisition sample rate,
displayed-point rate, whether displayed points are interpolated, and an analysis
Nyquist frequency capped by the hardware acquisition rate.
`configure_math` supports filter type and cutoff settings and warns when readback
shows that acquisition constraints clamped a requested cutoff.

`configure_timing_capture` disables the delayed/zoom timebase so measurements and
screen waveform transfers refer to the configured main timebase. DHO814 NORM
waveform transfer can return no data while a serial decoder overlay is displayed;
the waveform tools report the active bus and recommend disabling its display or
using a RAW download.

`analyze_pwm_envelope` operates on one or two aligned, stopped analog traces. It
extracts carrier frequency and period stability, duty range, reconstructed average
voltage, modulation frequency, and two-channel envelope phase. It uses robust settled
rail levels so edge overshoot is reported separately rather than distorting digital
swing or reconstructed voltage, and reports sample counts with a confidence level.
The reconstructed voltage is analytical only: the physical pin remains PWM without a low-pass filter.
General waveform analysis labels detected duty modulation and suppresses edge/period
jitter metrics whose assumptions do not hold for intentionally variable pulse widths.

`configure_mask_test` and `get_mask_results` cover pass/fail testing without hiding
counter state. `configure_search` supports edge and pulse searches;
`get_search_results` returns bounded pages of event times. Serial decoding includes
parallel, RS-232, I2C, SPI, and CAN. The existing trigger tool supports those protocol
trigger families alongside analog trigger types.

Decoder `settings.thresholds_v` maps the protocol signal name (`TX`, `RX`, `SCL`,
`SDA`, `CLK`, `MISO`, `MOSI`, `CS`, `PAL`, `PALCLK`, or `CAN`) to its threshold in
volts. Set thresholds explicitly when logic levels differ from the scope defaults.
Mask creation requires competing math and decoder analysis displays to be disabled;
requested-versus-applied results expose ignored enables, clamped tolerances, or a
RUN operation that has already returned to STOP.

`configure_recording`, `get_recording_state`, and `control_recording_replay` cover
frame recording, progress, selection, navigation, and replay. Navigation actions are
non-idempotent and never retried. Use `save_scope_setup` to preserve complete state
before temporary workflows. `restore_scope_setup` accepts only server-generated files,
requires exact single-use confirmation, and can replace broad instrument state. New
snapshots save represented channel/timebase/trigger state beside the binary setup;
restore compares readback with that snapshot and reports mismatches. If readback fails
after the write, the result explicitly says the write succeeded but verification is
uncertain, so callers do not blindly repeat the destructive operation.

## Transfers

`download_waveform` writes time/value CSV files under `RIGOL_DATA_DIR` (default
`captures`). Stop acquisition first; disabled channels must acquire data before
being stopped. RAW/MAX downloads default to the current memory depth. Math traces
support NORM only. Chunk size is bounded to 10,000 points; truncated downloads fail
and their partial files are removed. Transfer settings are changed and not restored.

Use catalog waveform commands for BYTE/WORD transfers. Binary query responses are
saved as files without inline base64 by default. `inline_binary=true` permits at
most 1,024 bytes inline. Text responses above 2,048 characters are saved with a
256-character preview. Use `read_capture` for selected excerpts (default 1,024 bytes,
maximum 2,048), without repeating instrument operations. Offsets are bytes; UTF-8
snippets may replace characters split at a boundary. Base64 excerpts are lossless.
Excerpts may contain fewer bytes than requested when JSON escaping would exceed
the response budget; use the returned `next_offset`, not the requested size.
Binary blocks with malformed headers or incomplete payloads fail and invalidate
the connection without replaying the operation.

`:SYSTem:SETup` writes accept a generated capture's `data_path`, allowing export
and restore without carrying the bytes through agent context. Alternatively,
`data_base64` supplies a payload without its TMC header. The server constructs the
header and writes it in one transaction. Scope-side SAVE/LOAD paths refer to the
scope, not the host. Capture reads/restores are restricted to generated filenames
within `RIGOL_DATA_DIR`; paths and symlinks escaping it are rejected.

All MCP text results have a 4,096-character aggregate budget after JSON compaction.
Oversized results are saved and replaced with metadata and a preview; complete data
is retained. Errors are capped at 1,024 characters and schema errors do not echo
large arguments. Raw `get_waveform` arrays always stay in JSON files; default
waveform analysis stays compact. Screenshots return a path unless `include_image`
is explicitly true. Avoid paging entire captures back into context. These byte and
character limits reduce payload size; model-specific token costs can differ.

Invalid sentinel/non-finite samples suppress waveform amplitude, frequency and
shape interpretation. Non-finite samples or invalid timing fail screen captures
rather than emitting unusable JSON. Catalog integer parameters use exact decimal
conversion (up to 256 digits); use strings for integers beyond a client's numeric
precision. Measurement validity still depends on acquisition and the input signal.

## Verification and Maintenance

Offline tests cover every catalog text operation, invalid/model-incompatible
arguments, binary setup round trips, chunked downloads, and modern/legacy MCP
stdio clients. This is **documented API coverage, not a claim that all commands
have been tested on a physical DHO814**.

On 2026-09-09, a real DHO814 running firmware **00.01.05** passed **31 checks**
through the stdio MCP server over LAN, with no input devices connected:

- Identification, capabilities, configuration and error-queue checks.
- Twenty queries across acquisition, trigger, channel, display, cursor mode, DVM,
  counter and system settings.
- Reversible channel scale, timebase, trigger level and channel inversion changes.
- A 1024x600 PNG screenshot and 1,000-point screen waveform analysis/JSON export.
- Bounded capture reads and 64-point NORM/RAW downloads in two 32-point chunks.

The open input was correctly reported as low-amplitude noise, with frequency and
shape interpretation suppressed. Channel/timebase/trigger configuration matched
the initial snapshot exactly afterward, acquisition returned to AUTO, waveform
transfer settings were restored, and the final error queue was clear.

On 2026-09-10, the same DHO814 passed a Pico 2 W-driven advanced acceptance run.
Live coverage included measurement statistics, cursors, search pagination, mask
creation and counters, recording/replay, meters, math/reference traces, aligned and
RAW transfers, setup restoration, and UART, I2C, and SPI decoding. The run exposed
and verified fixes for decoder block reads and thresholds, semantic readback
mismatches, delayed/zoom state, replay state, protocol-trigger catalog enums, and
decoder-overlay waveform diagnostics. Results and explicit exclusions are recorded
in the [advanced acceptance report](../captures/dho814_advanced_mcp_acceptance_report.json).

Repeat this opt-in test only on an idle, unconnected scope. It temporarily changes
settings and acquisition state, restores them in a `finally` block, and writes
`captures/live_scope_report.json`. Do not interrupt it during restoration.

Use the [container-based live smoke test](development.md#live-smoke-test).
It requires no host Python installation. Stop the registered MCP server before
running it, and restart that server only after the test exits.

CAN and parallel decoding, every catalog command, destructive operations, and
TCP/IP fault recovery still require separate hardware acceptance tests. Resets,
autoset, networking changes, self-test, and scope-side file overwrites are
deliberately excluded from routine acceptance testing.

### Reference Review

Normal CI tests cross-check the capability dataset against both the implementation
and the bundled command catalog. Checks cover source URL/checksum alignment, exact
citations, the model/channel/EXT matrix, and single/two-source measurement enums
for both normal and statistical queries. Independent lists caught and corrected
a repeated PDF table header accidentally included in the FRDELAY statistics enum.
These consistency checks are not a substitute for hardware acceptance tests.

Download the linked official PDF to a local file and follow the
[container-based reference audit](development.md#reference-audit). This checks
the shipped artifacts without modifying them or installing tools on the host.

The audit requires the exact pinned checksum, checks publication/software metadata,
verifies sections on their cited printed pages and model facts against the source
table, then compares a freshly extracted catalog with the shipped one. It exits
nonzero on source changes or extraction drift. CI uses the bundled artifacts offline;
this PDF audit is an explicit maintainer check and performs no download itself.

When Rigol updates the reference, do not overwrite the shipped data automatically:

1. Keep the existing pinned PDF for comparison; download the candidate separately.
2. Review changed model tables, command parameters, restrictions and firmware notes.
   Cross-check affected hardware claims against the official model datasheet/user guide.
3. Extract a candidate catalog to a temporary file and inspect its diff. Update the
   dataset version, source metadata, citations and review record only after review.
   Leave unsupported claims unverified.
4. Update both artifacts together, rerun the PDF audit and offline suite, and perform
   relevant hardware tests before claiming firmware-specific verification.

The generator retains signatures and parameter facts, not the manual's prose.
Explicit manual errata (DSB query punctuation and I2C parameter naming) are recorded
in the catalog. The [development checks](development.md) include container commands
for extracting a candidate to a separate output directory and running the offline
suite before replacing any reviewed artifacts.

The manual is not required at runtime and is not redistributed with the package.
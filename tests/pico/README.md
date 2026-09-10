# Pico PIO Signal Acceptance Tests

This opt-in suite builds deterministic Raspberry Pi Pico PIO signals, flashes one
firmware image per case, and asks the Rigol MCP server to validate frequency,
positive duty cycle, and peak-to-peak voltage on DHO814 channel 1. Normal pytest
runs exercise only the manifest, parsing, tolerance, and command-construction code;
they never flash hardware or contact an oscilloscope.

## Probe Connections

These tests target Raspberry Pi Pico 2 W with the RP2350, selected by
`PICO_BOARD=pico2_w`. The shipped manifest drives **GP16**, physical header **pin
21**. Its nearest ground is physical header **pin 23**; physical pin 22 between
them is GP17 and is not used. Pico 2 W retains the same header positions used by
the original Pico for these three pins.

1. Disconnect the Pico USB cable before attaching or moving probe clips.
2. Set the passive probe's physical attenuation switch to **10X**. Confirm the
   DHO814 channel 1 input is **1 MOhm**, DC coupled, and the probe is compensated.
3. Attach the channel 1 probe tip to **GP16, physical pin 21**.
4. Attach the probe's short ground spring or ground clip to **GND, physical pin
   23**. Keep the tip and ground connection short, especially for the 1 MHz case.
5. Inspect the pin labels before powering the board. Never attach the probe ground
   to 3V3, 3V3_EN, VSYS, VBUS, or a GPIO. A bench scope probe ground is normally
   bonded to protective earth; connecting it anywhere except Pico GND can short
   that node to earth through the oscilloscope.
6. Reconnect the Pico USB cable. Power the Pico only from USB for these tests and
   do not connect other target hardware to its header.

The expected waveform is approximately 0 V to 3.3 V. Do not select a 50 ohm scope
termination or add a 50 ohm feed-through terminator; the Pico GPIO is intended for
the scope's high-impedance input. The runner configures MCP's channel attenuation
to 10X, matching the probe switch.

```text
Pico 2 W top view, USB connector above the headers

                         GP16  pin 21  o---- probe tip (DHO814 CH1)
                         GP17  pin 22  o     unused
                          GND  pin 23  o---- short probe ground
```

## Build and Deploy

Prerequisites are CMake, Ninja, `picotool`, the Pico SDK, and a complete Arm GNU
toolchain with newlib. This machine's Homebrew `arm-none-eabi-gcc` lacks newlib;
the toolchain used by the neighboring Z80ROMlessSBC project is selected as follows:

```sh
export PICO_SDK_PATH=/Users/dave/GitHub/pico/pico-sdk
export PICO_TOOLCHAIN_PATH=/Users/dave/.local/share/arm-gnu-toolchain-15.3.rel1
uv run --no-sync python tests/pico/run_acceptance.py
```

That command builds all cases under `/tmp/rigol-mcp-pico` and does not touch
hardware. For a first flash, hold BOOTSEL while connecting the Pico, then release
it. Subsequent runs can normally force a compatible running Pico into BOOTSEL.

Stop any other MCP server instance that controls the same scope, connect the probe
as described above, leave the DHO814 in EDGE trigger mode, and run:

```sh
uv run --no-sync python tests/pico/run_acceptance.py --flash --ip 192.168.1.43
```

Use `--case square_1khz_50pct` to run one case or repeat `--case` to select
several. The runner defaults to `--board pico2_w`, and each image is loaded with
`picotool load -f -v -x`. The runner verifies the write, starts the image, waits
for the signal, and performs MCP calls strictly in sequence.

The runner snapshots channel 1, timebase, trigger, and acquisition state before
testing and restores those represented settings in a `finally` block. It refuses
to start from a non-EDGE trigger because that trigger cannot be reconstructed from
`get_scope_state`. Rigol measurement entries remain visible in the results panel;
clearing them is a separately confirmed destructive UI action. Results are written
to `captures/pico2_w_acceptance_report.json`.

Do not interrupt the runner during scope restoration. If flashing or communication
fails, inspect the scope state before retrying and verify channel 1 manually against
the saved report. Restart the registered MCP server after the acceptance run exits.

The firmware fixes the Pico 2 W RP2350 `clk_sys` at its standard 150 MHz. A
two-instruction PIO loop drives each high and low interval with fixed integer
instruction delays and an integer-only state-machine clock divider. This avoids
fractional-divider edge dither while keeping the requested duty cycle exact.
Since the listed frequencies are test neighborhoods, the firmware selects the
nearest stable rate and the runner records both the requested and actual values.
GP16 remains the default output.

## Signal Cases

| Case | Requested | Actual | Positive duty | Purpose |
| --- | ---: | ---: | ---: | --- |
| `square_1khz_50pct` | 1 kHz | 1 kHz | 50% | Baseline square wave and millisecond timebase |
| `pulse_10khz_25pct` | 10 kHz | 10 kHz | 25% | Asymmetric pulse and duty-cycle measurement |
| `square_1mhz_50pct` | 1 MHz | 1 MHz | 50% | Fast edges and short grounding |
| `pulse_5mhz_25pct` | 5 MHz | 4.6875 MHz | 25% | High-speed asymmetric duty stress |
| `square_6mhz_50pct` | 6 MHz | 5.769231 MHz | 50% | Integer-divider qualification clock |
| `square_10mhz_50pct` | 10 MHz | 9.375 MHz | 50% | Integer-divider qualification clock |
| `pulse_10mhz_25pct` | 10 MHz | 9.375 MHz | 25% | Pico 2 W asymmetric-duty ceiling test |
| `square_20mhz_50pct` | 20 MHz | 18.75 MHz | 50% | Integer-divider qualification clock |
| `square_50mhz_50pct` | 50 MHz | 37.5 MHz | 50% | Nearest stable high-speed clock |

Add cases to `signals.json`; the runner compiles frequency and duty values into a
separate UF2 image. Keep outputs within the Pico GPIO electrical limits and adjust
tolerances only from repeatable hardware evidence. Each PIO `SET` instruction can
hold its output for 1 through 32 cycles, so a complete period is limited to 64
cycles. At 150 MHz `clk_sys`, exact 25% duty requires a period divisible by four;
9.375 MHz is the nearest stable 25% case to 10 MHz. A 25% waveform at 50 MHz is
not representable because it requires at least four source-clock ticks per
period, limiting it to 37.5 MHz.

## PWM Shape Tests

`pwm_shapes.json` defines the unfiltered PWM tests: a 1 MHz duty sweep, narrow
pulses, the integer-divider transition near 2.289 kHz, and two-channel phase and
skew. Build every image with:

```sh
PICO_TOOLCHAIN_PATH=/Users/dave/.local/share/arm-gnu-toolchain-15.3.rel1 \
   uv run --no-sync python tests/pico/build_pwm_tests.py
```

The images are written below `/tmp/rigol-mcp-pwm-tests`. The timing calculator
uses only integer PWM dividers and selects the closest representable period;
`pwm_test_generator.c` receives the resulting divider, wrap, compare, and phase
values as compile definitions.

Single-channel tests retain the connection above. For `phase_*`, add a second
10X probe: connect DHO814 CH2 to **GP18, physical pin 24**, and its short ground
spring to **GND, physical pin 23**. Keep CH1 on GP16/pin 21. GP16 and GP18 use
different PWM slices, which are enabled simultaneously; GP18's initial counter
encodes its rising-edge delay. Measure `RRDELAY` and `RRPHASE` from CH1 to CH2.

The 1 MHz period is 150 PWM clocks, so a requested 90-degree offset quantizes to
38 clocks, or 91.2 degrees. The 180-degree case is exact at 75 clocks. The
zero-degree case measures the intrinsic skew between slices, GPIOs, and probes.

## Dual Sine PWM

The `rigol_pico_pwm_sine` target drives two synchronized sine-modulated PWM
streams:

- GP16/pin 21: 1 kHz sine duty envelope at 0 degrees
- GP18/pin 24: 1 kHz sine duty envelope at +90 degrees
- Both outputs: exact 100 kHz integer-divider PWM carrier
- Modulation: 100 samples per cycle, spanning 5% through 95% duty

Build and flash it with:

```sh
cmake -S tests/pico -B /tmp/rigol-mcp-pwm-sine -G Ninja \
   -DPICO_BOARD=pico2_w \
   -DPICO_SDK_PATH=/Users/dave/GitHub/pico/pico-sdk \
   -DPICO_TOOLCHAIN_PATH=/Users/dave/.local/share/arm-gnu-toolchain-15.3.rel1
cmake --build /tmp/rigol-mcp-pwm-sine --target rigol_pico_pwm_sine --parallel
picotool load -f -v -x /tmp/rigol-mcp-pwm-sine/rigol_pico_pwm_sine.uf2
```

These pins do not carry analog sine voltages. Their average values follow sine
waves centered at approximately 1.65 V with approximately 1.485 V peak
amplitude, but the unfiltered electrical outputs remain 0 V/3.3 V PWM pulse
streams. An RC or active low-pass filter is required when an external circuit
needs analog sine waves. The DHO814 `ULTRA` acquisition mode can average the
carrier for visual inspection without changing the electrical output.

## Protocol Decode Signals

The `rigol_pico_protocol` target provides deterministic traffic for decoder tests
using the same GP16/GP18 probe connections. Configure `PROTOCOL_MODE` in a separate
build directory for each fixture:

| Mode | Protocol | GP16 / CH1 | GP18 / CH2 | Rate and payload |
| ---: | --- | --- | --- | --- |
| 1 | UART | TX | idle low | 115200 baud, `RIGOL MCP 55 AA` |
| 2 | I2C | SCL | SDA | 100 kHz, address byte `0xA0`, data `0xA5` |
| 3 | SPI | clock | MOSI | 100 kHz, mode 0, repeated `0xA5`, timeout framing |

I2C uses push-pull levels for deterministic testing into high-impedance scope inputs.
It is a decoder fixture, not a real shared bus; use open-drain drivers and pull-ups for
real I2C devices. SPI uses timeout framing so a third chip-select probe is unnecessary.

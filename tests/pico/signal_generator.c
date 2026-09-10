#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/pio.h"
#include "pico/stdlib.h"

#ifndef SIGNAL_FREQUENCY_HZ
#define SIGNAL_FREQUENCY_HZ 1000
#endif

#ifndef SIGNAL_DUTY_PERMILLE
#define SIGNAL_DUTY_PERMILLE 500
#endif

#ifndef SIGNAL_GPIO
#define SIGNAL_GPIO 16
#endif

#ifndef SIGNAL_SYSTEM_CLOCK_HZ
#define SIGNAL_SYSTEM_CLOCK_HZ 150000000u
#endif

#if SIGNAL_FREQUENCY_HZ < 10 || SIGNAL_FREQUENCY_HZ > 50000000
#error "SIGNAL_FREQUENCY_HZ must be between 10 Hz and 50 MHz"
#endif

#if SIGNAL_DUTY_PERMILLE < 1 || SIGNAL_DUTY_PERMILLE > 999
#error "SIGNAL_DUTY_PERMILLE must be between 1 and 999"
#endif

static uint32_t greatest_common_divisor(uint32_t left, uint32_t right) {
  while (right != 0) {
    const uint32_t remainder = left % right;
    left = right;
    right = remainder;
  }
  return left;
}

static uint32_t start_stable_pio(void) {
  const uint32_t sys_clk = clock_get_hz(clk_sys);
  const uint32_t period_quantum =
      1000u / greatest_common_divisor(SIGNAL_DUTY_PERMILLE, 1000u);
  uint32_t best_divider = 0;
  uint32_t best_period = 0;
  uint32_t best_frequency_error = UINT32_MAX;
  uint32_t best_high_cycles = 0;
  uint32_t best_low_cycles = 0;

  for (uint32_t period = period_quantum; period <= 64u;
       period += period_quantum) {
    const uint32_t high_cycles =
        (period * SIGNAL_DUTY_PERMILLE) / 1000u;
    const uint32_t low_cycles = period - high_cycles;
    if (high_cycles < 1u || high_cycles > 32u || low_cycles < 1u ||
        low_cycles > 32u)
      continue;

    const uint64_t denominator = (uint64_t)SIGNAL_FREQUENCY_HZ * period;
    uint32_t rounded_divider =
        (uint32_t)(((uint64_t)sys_clk + denominator / 2u) / denominator);
    if (rounded_divider < 1u)
      rounded_divider = 1u;
    if (rounded_divider > 65535u)
      rounded_divider = 65535u;
    const uint32_t first_divider =
        rounded_divider > 1u ? rounded_divider - 1u : rounded_divider;
    const uint32_t last_divider =
        rounded_divider < 65535u ? rounded_divider + 1u : rounded_divider;

    for (uint32_t divider = first_divider; divider <= last_divider; ++divider) {
      const uint64_t product = (uint64_t)divider * period;
      const uint32_t actual_frequency =
          (uint32_t)(((uint64_t)sys_clk + product / 2u) / product);
      const uint32_t frequency_error =
          actual_frequency > SIGNAL_FREQUENCY_HZ
              ? actual_frequency - SIGNAL_FREQUENCY_HZ
              : SIGNAL_FREQUENCY_HZ - actual_frequency;
      if (best_divider == 0 || frequency_error < best_frequency_error ||
          (frequency_error == best_frequency_error && period > best_period)) {
        best_divider = divider;
        best_period = period;
        best_frequency_error = frequency_error;
        best_high_cycles = high_cycles;
        best_low_cycles = low_cycles;
      }
    }
  }

  if (best_divider == 0)
    panic("No stable PIO configuration");

  const uint16_t instructions[] = {
      pio_encode_set(pio_pins, 1u) |
          pio_encode_delay(best_high_cycles - 1u),
      pio_encode_set(pio_pins, 0u) |
          pio_encode_delay(best_low_cycles - 1u),
  };
  const struct pio_program program = {
      .instructions = instructions,
      .length = 2,
      .origin = -1,
  };
  PIO pio = pio0;
  const uint state_machine = pio_claim_unused_sm(pio, true);
  const uint offset = pio_add_program(pio, &program);
  pio_sm_config config = pio_get_default_sm_config();
  sm_config_set_wrap(&config, offset, offset + program.length - 1u);
  sm_config_set_set_pins(&config, SIGNAL_GPIO, 1u);
  sm_config_set_clkdiv_int_frac8(&config, (uint16_t)best_divider, 0u);
  pio_gpio_init(pio, SIGNAL_GPIO);
  pio_sm_set_consecutive_pindirs(pio, state_machine, SIGNAL_GPIO, 1u, true);
  pio_sm_init(pio, state_machine, offset, &config);
  pio_sm_set_enabled(pio, state_machine, true);

  const uint64_t product = (uint64_t)best_divider * best_period;
  return (uint32_t)(((uint64_t)sys_clk + product / 2u) / product);
}

int main(void) {
  if (!set_sys_clock_khz(SIGNAL_SYSTEM_CLOCK_HZ / 1000u, true))
    panic("Unable to set system clock");
  stdio_init_all();

  const uint32_t actual_frequency_hz = start_stable_pio();
  printf("Rigol Pico stable PIO: GP%u, requested=%u Hz, actual=%" PRIu32
         " Hz, %u.%u%% duty\n",
         SIGNAL_GPIO, SIGNAL_FREQUENCY_HZ, actual_frequency_hz,
         SIGNAL_DUTY_PERMILLE / 10u, SIGNAL_DUTY_PERMILLE % 10u);
  while (true) {
    tight_loop_contents();
  }
}
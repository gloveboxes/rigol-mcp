#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/pwm.h"
#include "pico/stdlib.h"

#ifndef SIGNAL_GPIO
#define SIGNAL_GPIO 16
#endif

#ifndef SIGNAL_SYSTEM_CLOCK_HZ
#define SIGNAL_SYSTEM_CLOCK_HZ 150000000u
#endif

#ifndef PWM_DIVIDER
#define PWM_DIVIDER 1u
#endif

#ifndef PWM_TOP
#define PWM_TOP 149u
#endif

#ifndef PWM_HIGH_CYCLES
#define PWM_HIGH_CYCLES 75u
#endif

#ifndef PWM_SECONDARY_GPIO
#define PWM_SECONDARY_GPIO 255u
#endif

#ifndef PWM_PHASE_CYCLES
#define PWM_PHASE_CYCLES 0u
#endif

#if PWM_DIVIDER < 1 || PWM_DIVIDER > 255
#error "PWM_DIVIDER must be between 1 and 255"
#endif

#if PWM_TOP < 1 || PWM_TOP > 65535
#error "PWM_TOP must be between 1 and 65535"
#endif

#if PWM_HIGH_CYCLES < 1 || PWM_HIGH_CYCLES > PWM_TOP
#error "PWM_HIGH_CYCLES must be between 1 and PWM_TOP"
#endif

#if PWM_PHASE_CYCLES > PWM_TOP
#error "PWM_PHASE_CYCLES must not exceed PWM_TOP"
#endif

static uint configure_output(uint gpio, uint16_t initial_counter) {
  gpio_set_function(gpio, GPIO_FUNC_PWM);
  const uint slice = pwm_gpio_to_slice_num(gpio);
  pwm_config config = pwm_get_default_config();
  pwm_config_set_clkdiv_int(&config, PWM_DIVIDER);
  pwm_config_set_wrap(&config, PWM_TOP);
  pwm_init(slice, &config, false);
  pwm_set_gpio_level(gpio, PWM_HIGH_CYCLES);
  pwm_set_counter(slice, initial_counter);
  return slice;
}

int main(void) {
  if (!set_sys_clock_khz(SIGNAL_SYSTEM_CLOCK_HZ / 1000u, true))
    panic("Unable to set system clock");
  stdio_init_all();

  const uint32_t period_cycles = PWM_TOP + 1u;
  const uint primary_slice = configure_output(SIGNAL_GPIO, 0u);
  uint32_t enable_mask = 1u << primary_slice;

#if PWM_SECONDARY_GPIO != 255
  const uint16_t secondary_counter = PWM_PHASE_CYCLES == 0
                                         ? 0u
                                         : (uint16_t)(period_cycles - PWM_PHASE_CYCLES);
  const uint secondary_slice =
      configure_output(PWM_SECONDARY_GPIO, secondary_counter);
  if (secondary_slice == primary_slice)
    panic("PWM outputs must use different slices");
  enable_mask |= 1u << secondary_slice;
#endif

  pwm_set_mask_enabled(enable_mask);
  const uint32_t actual_frequency_hz =
      SIGNAL_SYSTEM_CLOCK_HZ / (PWM_DIVIDER * period_cycles);
  printf("Rigol Pico integer PWM: GP%u, actual=%" PRIu32
         " Hz, high=%u/%" PRIu32 " cycles",
         SIGNAL_GPIO, actual_frequency_hz, PWM_HIGH_CYCLES, period_cycles);
#if PWM_SECONDARY_GPIO != 255
  printf(", GP%u phase=%u/%" PRIu32 " cycles", PWM_SECONDARY_GPIO,
         PWM_PHASE_CYCLES, period_cycles);
#endif
  printf("\n");

  while (true)
    tight_loop_contents();
}
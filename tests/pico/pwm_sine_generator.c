#include <stdint.h>
#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/irq.h"
#include "hardware/pwm.h"
#include "pico/stdlib.h"

#ifndef SIGNAL_GPIO
#define SIGNAL_GPIO 16
#endif

#ifndef PWM_SECONDARY_GPIO
#define PWM_SECONDARY_GPIO 18
#endif

#ifndef SIGNAL_SYSTEM_CLOCK_HZ
#define SIGNAL_SYSTEM_CLOCK_HZ 150000000u
#endif

#define PWM_CARRIER_HZ 100000u
#define SINE_FREQUENCY_HZ 1000u
#define SINE_SAMPLES (PWM_CARRIER_HZ / SINE_FREQUENCY_HZ)
#define SECONDARY_PHASE_SAMPLES (SINE_SAMPLES / 4u)
#define PWM_PERIOD_CYCLES (SIGNAL_SYSTEM_CLOCK_HZ / PWM_CARRIER_HZ)
#define PWM_TOP (PWM_PERIOD_CYCLES - 1u)

#if SIGNAL_SYSTEM_CLOCK_HZ % PWM_CARRIER_HZ != 0
#error "PWM carrier must divide clk_sys exactly"
#endif

#if PWM_CARRIER_HZ % SINE_FREQUENCY_HZ != 0
#error "Sine frequency must divide PWM carrier exactly"
#endif

#if SINE_SAMPLES != 100
#error "Sine table must contain exactly 100 samples"
#endif

static const uint16_t sine_levels[SINE_SAMPLES] = {
    750u, 792u, 835u, 876u, 918u, 959u, 998u, 1037u, 1075u, 1112u,
    1147u, 1180u, 1212u, 1242u, 1270u, 1296u, 1320u, 1342u, 1361u, 1378u,
    1392u, 1404u, 1413u, 1420u, 1424u, 1425u, 1424u, 1420u, 1413u, 1404u,
    1392u, 1378u, 1361u, 1342u, 1320u, 1296u, 1270u, 1242u, 1212u, 1180u,
    1147u, 1112u, 1075u, 1037u, 998u, 959u, 918u, 876u, 835u, 792u,
    750u, 708u, 665u, 624u, 582u, 541u, 502u, 463u, 425u, 388u,
    353u, 320u, 288u, 258u, 230u, 204u, 180u, 158u, 139u, 122u,
    108u, 96u, 87u, 80u, 76u, 75u, 76u, 80u, 87u, 96u,
    108u, 122u, 139u, 158u, 180u, 204u, 230u, 258u, 288u, 320u,
    353u, 388u, 425u, 463u, 502u, 541u, 582u, 624u, 665u, 708u,
};

static uint primary_slice;
static uint secondary_slice;
static uint sample_index;

static void on_pwm_wrap(void) {
  pwm_clear_irq(primary_slice);
  sample_index = (sample_index + 1u) % SINE_SAMPLES;
  pwm_set_gpio_level(SIGNAL_GPIO, sine_levels[sample_index]);
  pwm_set_gpio_level(
      PWM_SECONDARY_GPIO,
      sine_levels[(sample_index + SECONDARY_PHASE_SAMPLES) % SINE_SAMPLES]);
}

static uint configure_output(uint gpio, uint16_t initial_level) {
  gpio_set_function(gpio, GPIO_FUNC_PWM);
  const uint slice = pwm_gpio_to_slice_num(gpio);
  pwm_config config = pwm_get_default_config();
  pwm_config_set_clkdiv_int(&config, 1u);
  pwm_config_set_wrap(&config, PWM_TOP);
  pwm_init(slice, &config, false);
  pwm_set_gpio_level(gpio, initial_level);
  return slice;
}

int main(void) {
  if (!set_sys_clock_khz(SIGNAL_SYSTEM_CLOCK_HZ / 1000u, true))
    panic("Unable to set system clock");
  stdio_init_all();

  primary_slice = configure_output(SIGNAL_GPIO, sine_levels[0]);
  secondary_slice =
      configure_output(PWM_SECONDARY_GPIO, sine_levels[SECONDARY_PHASE_SAMPLES]);
  if (primary_slice == secondary_slice)
    panic("PWM sine outputs must use different slices");

  irq_set_exclusive_handler(PWM_DEFAULT_IRQ_NUM(), on_pwm_wrap);
  pwm_clear_irq(primary_slice);
  pwm_set_irq_enabled(primary_slice, true);
  irq_set_enabled(PWM_DEFAULT_IRQ_NUM(), true);
  pwm_set_mask_enabled((1u << primary_slice) | (1u << secondary_slice));

  printf("Rigol Pico sine PWM: GP%u and GP%u, carrier=%u Hz, sine=%u Hz, "
         "phase=90 degrees\n",
         SIGNAL_GPIO, PWM_SECONDARY_GPIO, PWM_CARRIER_HZ, SINE_FREQUENCY_HZ);
  while (true)
    tight_loop_contents();
}
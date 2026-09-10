#include <stdint.h>
#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"

#ifndef SIGNAL_SYSTEM_CLOCK_HZ
#define SIGNAL_SYSTEM_CLOCK_HZ 150000000u
#endif

#ifndef PROTOCOL_MODE
#define PROTOCOL_MODE 1
#endif

#define PRIMARY_GPIO 16u
#define SECONDARY_GPIO 18u

#if PROTOCOL_MODE == 2
static void drive_low(uint gpio) {
  gpio_put(gpio, false);
  gpio_set_dir(gpio, GPIO_OUT);
}

static void release_high(uint gpio) {
  gpio_put(gpio, true);
  gpio_set_dir(gpio, GPIO_OUT);
}

static void i2c_half_period(void) { sleep_us(5); }

static void i2c_clock(void) {
  release_high(PRIMARY_GPIO);
  i2c_half_period();
  drive_low(PRIMARY_GPIO);
  i2c_half_period();
}

static void i2c_byte(uint8_t value) {
  for (int bit = 7; bit >= 0; --bit) {
    if (value & (1u << bit))
      release_high(SECONDARY_GPIO);
    else
      drive_low(SECONDARY_GPIO);
    i2c_clock();
  }
  drive_low(SECONDARY_GPIO);
  i2c_clock();
}

static void send_i2c_frame(void) {
  static const uint8_t message[] = "Dave Glover";
  release_high(PRIMARY_GPIO);
  release_high(SECONDARY_GPIO);
  i2c_half_period();
  drive_low(SECONDARY_GPIO);
  i2c_half_period();
  drive_low(PRIMARY_GPIO);
  i2c_byte(0xA0);
  for (size_t index = 0; index < sizeof(message) - 1; ++index)
    i2c_byte(message[index]);
  drive_low(SECONDARY_GPIO);
  release_high(PRIMARY_GPIO);
  i2c_half_period();
  release_high(SECONDARY_GPIO);
}
#endif

#if PROTOCOL_MODE == 3
static void send_spi_byte(uint8_t value) {
  for (int bit = 7; bit >= 0; --bit) {
    gpio_put(SECONDARY_GPIO, (value & (1u << bit)) != 0);
    sleep_us(5);
    gpio_put(PRIMARY_GPIO, true);
    sleep_us(5);
    gpio_put(PRIMARY_GPIO, false);
  }
}
#endif

int main(void) {
  if (!set_sys_clock_khz(SIGNAL_SYSTEM_CLOCK_HZ / 1000u, true))
    panic("Unable to set system clock");
  stdio_init_all();

#if PROTOCOL_MODE == 1
  uart_init(uart0, 115200);
  gpio_set_function(PRIMARY_GPIO, GPIO_FUNC_UART);
  gpio_init(SECONDARY_GPIO);
  gpio_set_dir(SECONDARY_GPIO, GPIO_OUT);
  gpio_put(SECONDARY_GPIO, false);
  while (true) {
    uart_puts(uart0, "RIGOL MCP 55 AA\r\n");
    sleep_ms(10);
  }
#elif PROTOCOL_MODE == 2
  gpio_init(PRIMARY_GPIO);
  gpio_init(SECONDARY_GPIO);
  release_high(PRIMARY_GPIO);
  release_high(SECONDARY_GPIO);
  sleep_ms(100);
  send_i2c_frame();
  while (true)
    tight_loop_contents();
#elif PROTOCOL_MODE == 3
  gpio_init(PRIMARY_GPIO);
  gpio_init(SECONDARY_GPIO);
  gpio_set_dir(PRIMARY_GPIO, GPIO_OUT);
  gpio_set_dir(SECONDARY_GPIO, GPIO_OUT);
  gpio_put(PRIMARY_GPIO, false);
  while (true) {
    send_spi_byte(0xA5);
    sleep_ms(1);
  }
#else
#error "PROTOCOL_MODE must be 1 (UART), 2 (I2C), or 3 (SPI)"
#endif
}
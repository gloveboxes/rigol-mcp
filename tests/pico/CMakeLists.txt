cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_SDK_PATH)
  if(DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH "$ENV{PICO_SDK_PATH}")
  else()
    set(PICO_SDK_PATH "/Users/dave/GitHub/pico/pico-sdk")
  endif()
endif()

set(PICO_BOARD pico2_w CACHE STRING "Pico board type")
include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(rigol_pico_signals C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

set(SIGNAL_FREQUENCY_HZ 1000 CACHE STRING "Generated PWM frequency")
set(SIGNAL_DUTY_PERMILLE 500 CACHE STRING "Generated PWM duty cycle in parts per thousand")
set(SIGNAL_GPIO 16 CACHE STRING "GPIO carrying the generated signal")
set(SIGNAL_SYSTEM_CLOCK_HZ 150000000 CACHE STRING "Fixed Pico system clock")

add_executable(rigol_pico_signal signal_generator.c)
target_link_libraries(rigol_pico_signal PRIVATE pico_stdlib hardware_clocks hardware_pio)
target_compile_definitions(rigol_pico_signal PRIVATE
  SIGNAL_FREQUENCY_HZ=${SIGNAL_FREQUENCY_HZ}
  SIGNAL_DUTY_PERMILLE=${SIGNAL_DUTY_PERMILLE}
  SIGNAL_GPIO=${SIGNAL_GPIO}
  SIGNAL_SYSTEM_CLOCK_HZ=${SIGNAL_SYSTEM_CLOCK_HZ}
)
target_compile_options(rigol_pico_signal PRIVATE -Wall -Wextra -Werror)
pico_enable_stdio_usb(rigol_pico_signal 1)
pico_enable_stdio_uart(rigol_pico_signal 0)
pico_add_extra_outputs(rigol_pico_signal)

set(PWM_DIVIDER 1 CACHE STRING "Integer PWM clock divider")
set(PWM_TOP 149 CACHE STRING "PWM counter wrap value")
set(PWM_HIGH_CYCLES 75 CACHE STRING "PWM high interval in counter cycles")
set(PWM_SECONDARY_GPIO 255 CACHE STRING "Optional second PWM GPIO; 255 disables it")
set(PWM_PHASE_CYCLES 0 CACHE STRING "Second-channel rising-edge delay in counter cycles")

add_executable(rigol_pico_pwm_test pwm_test_generator.c)
target_link_libraries(rigol_pico_pwm_test PRIVATE pico_stdlib hardware_clocks hardware_pwm)
target_compile_definitions(rigol_pico_pwm_test PRIVATE
  SIGNAL_GPIO=${SIGNAL_GPIO}
  SIGNAL_SYSTEM_CLOCK_HZ=${SIGNAL_SYSTEM_CLOCK_HZ}
  PWM_DIVIDER=${PWM_DIVIDER}
  PWM_TOP=${PWM_TOP}
  PWM_HIGH_CYCLES=${PWM_HIGH_CYCLES}
  PWM_SECONDARY_GPIO=${PWM_SECONDARY_GPIO}
  PWM_PHASE_CYCLES=${PWM_PHASE_CYCLES}
)
target_compile_options(rigol_pico_pwm_test PRIVATE -Wall -Wextra -Werror)
pico_enable_stdio_usb(rigol_pico_pwm_test 1)
pico_enable_stdio_uart(rigol_pico_pwm_test 0)
pico_add_extra_outputs(rigol_pico_pwm_test)

add_executable(rigol_pico_pwm_sine pwm_sine_generator.c)
target_link_libraries(rigol_pico_pwm_sine PRIVATE pico_stdlib hardware_clocks hardware_irq hardware_pwm)
target_compile_definitions(rigol_pico_pwm_sine PRIVATE
  SIGNAL_GPIO=${SIGNAL_GPIO}
  SIGNAL_SYSTEM_CLOCK_HZ=${SIGNAL_SYSTEM_CLOCK_HZ}
  PWM_SECONDARY_GPIO=18
)
target_compile_options(rigol_pico_pwm_sine PRIVATE -Wall -Wextra -Werror)
pico_enable_stdio_usb(rigol_pico_pwm_sine 1)
pico_enable_stdio_uart(rigol_pico_pwm_sine 0)
pico_add_extra_outputs(rigol_pico_pwm_sine)

set(PROTOCOL_MODE 1 CACHE STRING "Protocol fixture: 1=UART, 2=I2C, 3=SPI")

add_executable(rigol_pico_protocol protocol_signal_generator.c)
target_link_libraries(rigol_pico_protocol PRIVATE pico_stdlib hardware_clocks hardware_uart)
target_compile_definitions(rigol_pico_protocol PRIVATE
  SIGNAL_SYSTEM_CLOCK_HZ=${SIGNAL_SYSTEM_CLOCK_HZ}
  PROTOCOL_MODE=${PROTOCOL_MODE}
)
target_compile_options(rigol_pico_protocol PRIVATE -Wall -Wextra -Werror)
pico_enable_stdio_usb(rigol_pico_protocol 1)
pico_enable_stdio_uart(rigol_pico_protocol 0)
pico_add_extra_outputs(rigol_pico_protocol)
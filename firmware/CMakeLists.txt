# OpenFleet demo firmware (docs/FIRMWARE.md): FreeRTOS on an STM32F407, run on Renode.
#   cmake -S firmware -B firmware/build -G Ninja && cmake --build firmware/build
# The portable logic (app/) is also built and unit-tested on the host by edge/'s project.
cmake_minimum_required(VERSION 3.25)
set(CMAKE_TOOLCHAIN_FILE ${CMAKE_CURRENT_SOURCE_DIR}/cmake/arm-none-eabi.cmake CACHE FILEPATH "")
project(openfleet_fw VERSION 0.1.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_EXTENSIONS ON)  # gnu11: inline asm, designated range initialisers
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE MinSizeRel)
endif()

include(FetchContent)
# FreeRTOS kernel, pinned by hash. Only its sources are used (SOURCE_SUBDIR points nowhere,
# so its own CMake project isn't added).
FetchContent_Declare(
  freertos_kernel
  URL https://github.com/FreeRTOS/FreeRTOS-Kernel/archive/refs/tags/V11.1.0.tar.gz
  URL_HASH SHA256=0e21928b3bcc4f9bcaf7333fb1c8c0299d97e2ec9e13e3faa2c5a7ac8a3bc573
  DOWNLOAD_EXTRACT_TIMESTAMP ON
  SOURCE_SUBDIR none)
FetchContent_MakeAvailable(freertos_kernel)
set(K ${freertos_kernel_SOURCE_DIR})

set(CPU_FLAGS -mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16)
set(OWN_SOURCES
  src/main.c
  app/supervisor.c
  app/boot_record.c
  board/stm32f4/board.c
  board/stm32f4/startup.c
  ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/src/protocol.c)
set(KERNEL_SOURCES ${K}/tasks.c ${K}/queue.c ${K}/list.c ${K}/portable/GCC/ARM_CM4F/port.c)

add_executable(openfleet-fw ${OWN_SOURCES} ${KERNEL_SOURCES})
set_target_properties(openfleet-fw PROPERTIES SUFFIX ".elf")
target_include_directories(openfleet-fw PRIVATE
  include app board/stm32f4 ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/include
  ${K}/include ${K}/portable/GCC/ARM_CM4F)
target_compile_options(openfleet-fw PRIVATE ${CPU_FLAGS} -g -ffunction-sections -fdata-sections)
# Strict warnings for our code; the kernel is third-party.
set_source_files_properties(${OWN_SOURCES} PROPERTIES COMPILE_OPTIONS
  "-Wall;-Wextra;-Wpedantic;-Wshadow;-Wconversion;-Wno-pedantic;-Werror")
target_link_options(openfleet-fw PRIVATE ${CPU_FLAGS}
  -T${CMAKE_CURRENT_SOURCE_DIR}/board/stm32f4/stm32f4.ld -nostartfiles
  --specs=nano.specs --specs=nosys.specs -Wl,--gc-sections
  -Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/openfleet-fw.map -Wl,--print-memory-usage)
add_custom_command(TARGET openfleet-fw POST_BUILD
  COMMAND ${CMAKE_SIZE} $<TARGET_FILE:openfleet-fw>)

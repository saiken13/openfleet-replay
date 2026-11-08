# OpenFleet edge agent (docs/EMBEDDED_ROADMAP.md). C++20, built natively or cross-compiled
# with the toolchain files in cmake/toolchains; see CMakePresets.json for every variant.
cmake_minimum_required(VERSION 3.25)
project(openfleet_edge VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(EDGE_WERROR "Treat warnings as errors" ON)
set(EDGE_SANITIZE "" CACHE STRING "Sanitizers: '', 'address,undefined' or 'thread'")
option(EDGE_BUILD_TESTS "Build the unit tests" ON)
option(EDGE_FUZZ "Build the libFuzzer targets (clang only)" OFF)

# Warnings and sanitizers for our own targets only (not for fetched dependencies).
add_library(edge_options INTERFACE)
target_compile_options(edge_options INTERFACE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow
  $<$<COMPILE_LANGUAGE:CXX>:-Wnon-virtual-dtor>
  $<$<BOOL:${EDGE_WERROR}>:-Werror>)
if(EDGE_SANITIZE)
  target_compile_options(edge_options INTERFACE
    -fsanitize=${EDGE_SANITIZE} -fno-omit-frame-pointer -fno-sanitize-recover=all)
  target_link_options(edge_options INTERFACE -fsanitize=${EDGE_SANITIZE})
endif()

if(EDGE_FUZZ)
  # Coverage instrumentation for the code under test too, not just the fuzz entry points:
  # without it libFuzzer can't see which inputs reach new branches of the library.
  target_compile_options(edge_options INTERFACE -fsanitize=fuzzer-no-link)
endif()

find_package(Threads REQUIRED)
include(FetchContent)

# nlohmann/json (header-only), pinned by hash like GoogleTest.
FetchContent_Declare(
  nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
  URL_HASH SHA256=d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
set(JSON_Install OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(nlohmann_json)

# Everything except the network transport: /proc parsing, sampling, batches, the uplink
# state machine and the thread loops. Pure C++ + pthreads, so it builds and is tested on
# every preset, including the cross builds under qemu.
add_library(edge_core STATIC
  src/agent.cpp
  src/device_link.cpp
  src/gateway.cpp
  src/log.cpp
  src/proc.cpp
  src/sampler.cpp
  src/telemetry.cpp
  src/uplink.cpp)
target_include_directories(edge_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_definitions(edge_core PUBLIC OPENFLEET_EDGE_VERSION="${PROJECT_VERSION}")
target_link_libraries(edge_core PUBLIC Threads::Threads nlohmann_json::nlohmann_json ofp
                      PRIVATE edge_options)

# The simulated microcontroller (tests and the compose demo); no network library needed.
add_library(edge_devsim STATIC src/device_sim.cpp)
target_link_libraries(edge_devsim PUBLIC edge_core PRIVATE edge_options)
add_executable(openfleet-device-sim src/device_sim_main.cpp)
target_link_libraries(openfleet-device-sim PRIVATE edge_devsim edge_options)
install(TARGETS openfleet-device-sim RUNTIME DESTINATION bin)

# The agent binary needs libcurl for the target. A cross build without the target's libcurl
# (CI's aarch64 job) still builds and runs every test of edge_core; it just skips this.
set(EDGE_BUILD_AGENT "AUTO" CACHE STRING "Build openfleet-edge-agent: AUTO, ON or OFF")
if(NOT EDGE_BUILD_AGENT STREQUAL "OFF")
  if(EDGE_BUILD_AGENT STREQUAL "ON")
    find_package(CURL REQUIRED)
  else()
    find_package(CURL QUIET)
  endif()
  if(CURL_FOUND)
    add_executable(openfleet-edge-agent src/main.cpp src/curl_client.cpp)
    target_link_libraries(openfleet-edge-agent PRIVATE edge_core edge_options CURL::libcurl)
    install(TARGETS openfleet-edge-agent RUNTIME DESTINATION bin)
  else()
    message(STATUS "libcurl for the target not found: skipping openfleet-edge-agent")
  endif()
endif()

if(EDGE_BUILD_TESTS)
  FetchContent_Declare(
    googletest
    URL https://github.com/google/googletest/releases/download/v1.15.2/googletest-1.15.2.tar.gz
    URL_HASH SHA256=7b42b4d6ed48810c5362c265a17faebe90dc2373c885e5216439d37927f02926
    DOWNLOAD_EXTRACT_TIMESTAMP ON)
  set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest)

  enable_testing()
  add_executable(edge_tests
    tests/agent_loop_test.cpp
    tests/backoff_test.cpp
    tests/bounded_queue_test.cpp
    tests/config_test.cpp
    tests/device_link_test.cpp
    tests/gateway_test.cpp
    tests/proc_test.cpp
    tests/telemetry_test.cpp
    tests/uplink_test.cpp)
  target_link_libraries(edge_tests PRIVATE edge_core edge_devsim edge_options GTest::gtest_main)
  target_compile_definitions(edge_tests PRIVATE
    EDGE_FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
  include(GoogleTest)
  # PRE_TEST: discover when ctest runs, so cross builds list tests through the emulator.
  gtest_discover_tests(edge_tests DISCOVERY_MODE PRE_TEST)
endif()

# Micro-benchmarks (docs/BENCHMARKS.md). Built on every preset so they can't rot; ctest runs
# them once with --quick (a smoke test, not a measurement). Measure with a Release build:
#   cmake --preset release && cmake --build --preset release && build/release/edge_bench
option(EDGE_BUILD_BENCH "Build the micro-benchmarks" ON)
if(EDGE_BUILD_BENCH AND NOT EDGE_FUZZ)
  add_executable(edge_bench bench/bench.cpp)
  target_link_libraries(edge_bench PRIVATE edge_core edge_options)
  target_compile_definitions(edge_bench PRIVATE EDGE_BUILD_TYPE="${CMAKE_BUILD_TYPE}")
  if(EDGE_BUILD_TESTS)
    add_test(NAME edge_bench_smoke COMMAND edge_bench --quick)
  endif()
endif()

# libFuzzer (clang): cmake --preset fuzz. The protocol's decoder target is in protocol/.
if(EDGE_FUZZ)
  add_executable(fuzz_gateway tests/fuzz_gateway.cpp)
  target_link_libraries(fuzz_gateway PRIVATE edge_core edge_options)
  target_compile_options(fuzz_gateway PRIVATE -fsanitize=fuzzer)
  target_link_options(fuzz_gateway PRIVATE -fsanitize=fuzzer)
endif()

# The device protocol (C11), shared with the firmware: library, tests, fuzz target.
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../protocol ${CMAKE_BINARY_DIR}/protocol)

# The firmware's portable logic (firmware/app), host-built and tested here. Only with tests:
# the agent doesn't use it (and its image doesn't contain firmware/).
if(EDGE_BUILD_TESTS)
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../firmware/app ${CMAKE_BINARY_DIR}/fw_app)
endif()

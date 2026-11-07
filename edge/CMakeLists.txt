# OpenFleet edge agent (docs/EMBEDDED_ROADMAP.md). C++20, built natively or cross-compiled
# with the toolchain files in cmake/toolchains; see CMakePresets.json for every variant.
cmake_minimum_required(VERSION 3.25)
project(openfleet_edge VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(EDGE_WERROR "Treat warnings as errors" ON)
set(EDGE_SANITIZE "" CACHE STRING "Sanitizers: '', 'address,undefined' or 'thread'")
option(EDGE_BUILD_TESTS "Build the unit tests" ON)

# Warnings and sanitizers for our own targets only (not for fetched dependencies).
add_library(edge_options INTERFACE)
target_compile_options(edge_options INTERFACE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow -Wnon-virtual-dtor
  $<$<BOOL:${EDGE_WERROR}>:-Werror>)
if(EDGE_SANITIZE)
  target_compile_options(edge_options INTERFACE
    -fsanitize=${EDGE_SANITIZE} -fno-omit-frame-pointer -fno-sanitize-recover=all)
  target_link_options(edge_options INTERFACE -fsanitize=${EDGE_SANITIZE})
endif()

find_package(Threads REQUIRED)

add_library(edge_core INTERFACE)
target_include_directories(edge_core INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(edge_core INTERFACE Threads::Threads)

if(EDGE_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(
    googletest
    URL https://github.com/google/googletest/releases/download/v1.15.2/googletest-1.15.2.tar.gz
    URL_HASH SHA256=7b42b4d6ed48810c5362c265a17faebe90dc2373c885e5216439d37927f02926
    DOWNLOAD_EXTRACT_TIMESTAMP ON)
  set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest)

  enable_testing()
  add_executable(edge_tests tests/bounded_queue_test.cpp)
  target_link_libraries(edge_tests PRIVATE edge_core edge_options GTest::gtest_main)
  include(GoogleTest)
  # PRE_TEST: discover when ctest runs, so cross builds list tests through the emulator.
  gtest_discover_tests(edge_tests DISCOVERY_MODE PRE_TEST)
endif()

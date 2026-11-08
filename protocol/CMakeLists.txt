# OpenFleet device protocol (docs/PROTOCOL.md): a C11 library with no allocation and no I/O,
# shared by the edge agent's gateway (edge/) and the firmware (firmware/, Phase 3).
#
# Built as part of edge/ (add_subdirectory), which provides the warnings, sanitizers and
# GoogleTest. The firmware build compiles src/protocol.c directly.
add_library(ofp STATIC src/protocol.c)
target_include_directories(ofp PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
set_target_properties(ofp PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
if(TARGET edge_options)
  target_link_libraries(ofp PRIVATE edge_options)
endif()

if(EDGE_BUILD_TESTS)
  add_executable(protocol_tests tests/protocol_test.cpp)
  target_link_libraries(protocol_tests PRIVATE ofp edge_options GTest::gtest_main)
  gtest_discover_tests(protocol_tests DISCOVERY_MODE PRE_TEST)
endif()

# libFuzzer needs clang: cmake --preset fuzz (see edge/README.md).
if(EDGE_FUZZ)
  add_executable(fuzz_decoder fuzz/fuzz_decoder.c)
  target_link_libraries(fuzz_decoder PRIVATE ofp edge_options)
  target_compile_options(fuzz_decoder PRIVATE -fsanitize=fuzzer)
  target_link_options(fuzz_decoder PRIVATE -fsanitize=fuzzer)
  add_executable(make_seeds fuzz/make_seeds.c)
  target_link_libraries(make_seeds PRIVATE ofp)
endif()

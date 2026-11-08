# The firmware's portable logic, built for the host by edge/'s project (add_subdirectory),
# so it's unit-tested with the same warnings, sanitizers and emulated cross builds.
add_library(fw_app STATIC supervisor.c boot_record.c)
target_include_directories(fw_app PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
set_target_properties(fw_app PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
target_link_libraries(fw_app PUBLIC ofp PRIVATE edge_options)

if(EDGE_BUILD_TESTS)
  add_executable(fw_app_tests ${CMAKE_CURRENT_SOURCE_DIR}/../tests/fw_app_test.cpp)
  target_link_libraries(fw_app_tests PRIVATE fw_app edge_options GTest::gtest_main)
  gtest_discover_tests(fw_app_tests DISCOVERY_MODE PRE_TEST)
endif()

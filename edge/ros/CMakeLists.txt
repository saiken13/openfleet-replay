# openfleet-ros-bridge (DECISIONS #037): the edge gateway's device links and protocol
# decoding (edge_core, ../), republished into ROS 2. Build with a sourced ROS 2 environment:
#
#   . /opt/ros/jazzy/setup.sh
#   cmake -S edge/ros -B build/ros -G Ninja && cmake --build build/ros
#
# or as a colcon package. Container: edge/ros/Dockerfile.
cmake_minimum_required(VERSION 3.22)
project(openfleet_ros_bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(diagnostic_msgs REQUIRED)

# edge_core without the agent binary (no libcurl), tests or benchmarks. Only what the bridge
# links is built.
set(EDGE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(EDGE_BUILD_BENCH OFF CACHE BOOL "" FORCE)
set(EDGE_BUILD_AGENT OFF CACHE STRING "" FORCE)
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/.. edge EXCLUDE_FROM_ALL)

add_executable(openfleet-ros-bridge src/bridge.cpp)
target_compile_options(openfleet-ros-bridge PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(openfleet-ros-bridge PRIVATE
  edge_core
  rclcpp::rclcpp
  ${std_msgs_TARGETS}
  ${diagnostic_msgs_TARGETS})

# Its own component: install rules from the EXCLUDE_FROM_ALL edge/ tree would name targets
# that were never built.
install(TARGETS openfleet-ros-bridge RUNTIME DESTINATION lib/${PROJECT_NAME} COMPONENT bridge)
ament_package()

# openfleet-ros-bridge on ROS 2 Jazzy (DECISIONS #037). Build context: the repo root (see
# Dockerfile.dockerignore); the bridge links edge_core from edge/ and the protocol library.
FROM ros:jazzy-ros-base AS build
RUN apt-get update && apt-get install -y --no-install-recommends ninja-build \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY protocol protocol
COPY edge edge
RUN . /opt/ros/jazzy/setup.sh \
    && cmake -S edge/ros -B /build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    && cmake --build /build --target openfleet-ros-bridge \
    && cmake --install /build --component bridge --prefix /opt/openfleet --strip

FROM ros:jazzy-ros-base
COPY --from=build /opt/openfleet/lib/openfleet_ros_bridge/openfleet-ros-bridge /usr/local/bin/
# The ros base image's entrypoint sources /opt/ros/jazzy/setup.bash.
CMD ["openfleet-ros-bridge", "--ros-args", "-p", "devices:=[tcp://localhost:4000]"]

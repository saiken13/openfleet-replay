# Collector on ROS 2 Jazzy. rclpy & message packages come from the base image.
FROM ros:jazzy-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/openfleet-collector
# venv with access to the system (ROS) site-packages, so rclpy is importable
RUN python3 -m venv --system-site-packages /opt/venv
COPY pyproject.toml ./
RUN mkdir openfleet_collector && touch openfleet_collector/__init__.py \
    && /opt/venv/bin/pip install --no-cache-dir .
COPY . .
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

ENV PATH=/opt/venv/bin:$PATH
# The ros base image entrypoint sources /opt/ros/jazzy/setup.bash
CMD ["openfleet-collector", "--config", "collector.example.yaml"]

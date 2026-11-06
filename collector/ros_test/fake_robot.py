"""A minimal 'robot' for the ROS integration test.

Node `lidar_driver` publishes /scan (sensor_msgs/LaserScan) at SCAN_HZ and /battery_state.
The rate can be changed at runtime by writing a number to /tmp/scan_hz (0 = stop publishing),
so the test can script a degradation without restarting the node.
"""

import os

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState, LaserScan


class FakeLidar(Node):
    def __init__(self) -> None:
        super().__init__("lidar_driver")
        self.scan_pub = self.create_publisher(LaserScan, "/scan", 10)
        self.bat_pub = self.create_publisher(BatteryState, "/battery_state", 10)
        self.hz = float(os.environ.get("SCAN_HZ", "15"))
        self.timer = None
        self._set_rate(self.hz)
        self.create_timer(0.5, self._check_control)
        self.create_timer(1.0, self._battery)
        self.battery = 0.9

    def _set_rate(self, hz: float) -> None:
        if self.timer is not None:
            self.destroy_timer(self.timer)
            self.timer = None
        self.hz = hz
        if hz > 0:
            self.timer = self.create_timer(1.0 / hz, self._scan)
        self.get_logger().warning(f"scan rate set to {hz} Hz")

    def _check_control(self) -> None:
        try:
            hz = float(open("/tmp/scan_hz").read().strip())
        except (OSError, ValueError):
            return
        if hz != self.hz:
            self._set_rate(hz)

    def _scan(self) -> None:
        msg = LaserScan()
        msg.header.frame_id = "laser"
        msg.ranges = [1.0] * 360
        self.scan_pub.publish(msg)

    def _battery(self) -> None:
        self.battery = max(0.0, self.battery - 0.001)
        msg = BatteryState()
        msg.percentage = self.battery
        self.bat_pub.publish(msg)


def main() -> None:
    rclpy.init()
    rclpy.spin(FakeLidar())


if __name__ == "__main__":
    main()

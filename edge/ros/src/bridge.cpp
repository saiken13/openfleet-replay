// openfleet-ros-bridge (DECISIONS #037): microcontrollers speaking the OpenFleet device
// protocol (docs/PROTOCOL.md), republished into ROS 2 so a ROS robot's own collector reports
// them. The bridge never talks to OpenFleet: on a ROS robot the collector is the robot's one
// sender (two senders on one run would collide on seq).
//
//   each SENSOR reading       -> std_msgs/Int32 on /mcu<id>/<sensor>   (sensor-data QoS)
//   device and task health    -> diagnostic_msgs/DiagnosticArray on /diagnostics, once a
//                                period: mcu<id> and mcu<id>/<task>, OK/WARN/ERROR/STALE
//   resets, device logs, link -> /rosout, under the logger openfleet_ros_bridge.mcu<id>
//   errors
//
// The mapping, naming, duplicate and reset handling are the edge gateway's (edge_core), so
// the agent and the bridge report a device identically.
//
//   openfleet-ros-bridge --ros-args -p devices:="[tcp://localhost:4000, serial:/dev/ttyUSB0]"
#include <cctype>
#include <chrono>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/int32.hpp>

#include "openfleet/edge/agent.hpp"
#include "openfleet/edge/backoff.hpp"
#include "openfleet/edge/device_link.hpp"
#include "openfleet/edge/gateway.hpp"

namespace oe = openfleet::edge;
using namespace std::chrono_literals;
using diagnostic_msgs::msg::DiagnosticArray;
using diagnostic_msgs::msg::DiagnosticStatus;

namespace {

// Device sensor names allow '-' and a leading digit; ROS names don't. "/mcu1/imu-2" becomes
// "/mcu1/imu_2" and "/mcu1/9dof" "/mcu1/_9dof", so the topic always exists.
std::string ros_topic(const std::string& name) {
  std::string out;
  bool token_start = true;
  for (const char c : name) {
    if (c == '/') {
      out += c;
      token_start = true;
      continue;
    }
    if (token_start && std::isdigit(static_cast<unsigned char>(c))) {
      out += '_';
    }
    out += std::isalnum(static_cast<unsigned char>(c)) || c == '_' ? c : '_';
    token_start = false;
  }
  return out;
}

std::uint8_t level(oe::NodeState s) {
  switch (s) {
    case oe::NodeState::kRunning:
      return DiagnosticStatus::OK;
    case oe::NodeState::kDegraded:
      return DiagnosticStatus::WARN;
    case oe::NodeState::kError:
      return DiagnosticStatus::ERROR;
    case oe::NodeState::kStopped:
      return DiagnosticStatus::STALE;
  }
  return DiagnosticStatus::WARN;
}

}  // namespace

class Bridge : public rclcpp::Node {
 public:
  Bridge() : Node("openfleet_ros_bridge") {
    const auto devices =
        declare_parameter<std::vector<std::string>>("devices", std::vector<std::string>{});
    const double period_s = declare_parameter("period_s", 1.0);
    const double silence_s = declare_parameter("device_silence_s", 3.0);
    if (devices.empty()) {
      throw std::runtime_error(
          "parameter 'devices' is empty; e.g. --ros-args -p devices:=\"[tcp://localhost:4000]\"");
    }
    if (period_s <= 0 || silence_s <= 0) {
      throw std::runtime_error("period_s and device_silence_s must be > 0");
    }
    diagnostics_ = create_publisher<DiagnosticArray>("/diagnostics", 10);
    for (const auto& uri : devices) {
      std::string error;
      const auto ep = oe::parse_device_uri(uri, &error);
      if (!ep) {
        throw std::runtime_error("devices: " + error);
      }
      oe::GatewayConfig c;
      c.link = uri;
      c.self_source = "";  // link events under the bridge's own logger
      c.silence_timeout = std::chrono::milliseconds(static_cast<long>(silence_s * 1000));
      c.on_reading = [this](const std::string& topic, std::int32_t value) {
        publish(topic, value);
      };
      links_.push_back(std::make_unique<Link>(*ep, std::move(c)));
    }
    timer_ = create_wall_timer(std::chrono::duration<double>(period_s), [this] { tick(); });
    for (auto& link : links_) {
      Link* l = link.get();
      l->thread = std::thread([this, l] {
        oe::Backoff backoff(500ms, 5000ms);
        oe::device_loop(l->endpoint, l->gateway, stop_, backoff);
      });
      RCLCPP_INFO(get_logger(), "reading %s", l->endpoint.uri.c_str());
    }
  }

  ~Bridge() override {
    stop_.request();
    for (auto& link : links_) {
      if (link->thread.joinable()) {
        link->thread.join();
      }
    }
  }

  Bridge(const Bridge&) = delete;
  Bridge& operator=(const Bridge&) = delete;

 private:
  struct Link {
    Link(oe::DeviceEndpoint ep, oe::GatewayConfig config)
        : endpoint(std::move(ep)), gateway(std::move(config)) {}
    oe::DeviceEndpoint endpoint;
    oe::Gateway gateway;
    std::thread thread;
  };

  // Device thread, under that link's gateway lock: find or create the topic, then publish.
  void publish(const std::string& topic, std::int32_t value) {
    rclcpp::Publisher<std_msgs::msg::Int32>::SharedPtr pub;
    {
      std::lock_guard lock(publishers_mutex_);
      auto it = publishers_.find(topic);
      if (it == publishers_.end()) {
        const std::string name = ros_topic(topic);
        it = publishers_
                 .emplace(topic, create_publisher<std_msgs::msg::Int32>(name, rclcpp::SensorDataQoS()))
                 .first;
        RCLCPP_INFO(get_logger(), "publishing %s", name.c_str());
      }
      pub = it->second;
    }
    std_msgs::msg::Int32 msg;
    msg.data = value;
    pub->publish(msg);
  }

  // Timer: each gateway's window -> /diagnostics and /rosout. Its topic rates are left out:
  // the collector measures the real ROS traffic.
  void tick() {
    DiagnosticArray array;
    array.header.stamp = now();
    const auto t = oe::Gateway::Clock::now();
    for (auto& link : links_) {
      oe::Sample sample;
      link->gateway.contribute(sample, t);
      for (const auto& n : sample.nodes) {
        DiagnosticStatus st;
        st.level = level(n.state);
        st.name = n.node;
        st.message = std::string(oe::to_string(n.state));
        st.hardware_id = link->endpoint.uri;
        array.status.push_back(std::move(st));
      }
      for (const auto& entry : sample.logs) {
        log(entry);
      }
    }
    diagnostics_->publish(array);
  }

  void log(const oe::LogEntry& e) {
    const auto logger = e.source.empty() ? get_logger() : get_logger().get_child(e.source);
    switch (e.level) {
      case oe::LogLevel::kDebug:
        RCLCPP_DEBUG(logger, "%s", e.message.c_str());
        break;
      case oe::LogLevel::kInfo:
        RCLCPP_INFO(logger, "%s", e.message.c_str());
        break;
      case oe::LogLevel::kWarn:
        RCLCPP_WARN(logger, "%s", e.message.c_str());
        break;
      case oe::LogLevel::kError:
        RCLCPP_ERROR(logger, "%s", e.message.c_str());
        break;
      case oe::LogLevel::kFatal:
        RCLCPP_FATAL(logger, "%s", e.message.c_str());
        break;
    }
  }

  oe::StopSignal stop_;
  std::vector<std::unique_ptr<Link>> links_;
  rclcpp::Publisher<DiagnosticArray>::SharedPtr diagnostics_;
  rclcpp::TimerBase::SharedPtr timer_;
  std::mutex publishers_mutex_;
  std::map<std::string, rclcpp::Publisher<std_msgs::msg::Int32>::SharedPtr> publishers_;
};

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  int rc = 0;
  try {
    rclcpp::spin(std::make_shared<Bridge>());
  } catch (const std::exception& e) {
    RCLCPP_FATAL(rclcpp::get_logger("openfleet_ros_bridge"), "%s", e.what());
    rc = 1;
  }
  rclcpp::shutdown();
  return rc;
}

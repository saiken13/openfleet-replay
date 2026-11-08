// Device gateway (docs/PROTOCOL.md, DECISIONS #033): turns the byte stream from one device
// link (UART, pty or TCP) into OpenFleet's existing model, without any new backend concept:
//
//   SENSOR frames      -> topic  /mcu<id>/<sensor>  (rate measured here, judged by the backend)
//   HEALTH task states -> node   mcu<id>/<task>
//   the device itself  -> node   mcu<id>            (STOPPED when silent; DEGRADED on a bad
//                                                    link or for a while after an unexpected reset)
//   LOG frames, resets, link errors -> log events
//
// It measures and maps; it never decides that something is an incident.
//
// Thread-safe: the device thread calls on_bytes(), the sampler thread calls contribute().
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "openfleet/edge/telemetry.hpp"
#include "openfleet/protocol.h"

namespace openfleet::edge {

struct GatewayConfig {
  std::string link;  // for messages, e.g. "tcp://device-sim:4000"
  std::chrono::milliseconds silence_timeout{3000};
  double degraded_error_share = 0.25;  // bad frames / all frames in a window -> DEGRADED
  std::chrono::milliseconds error_log_interval{10000};  // link-error summaries at most this often
  std::size_t max_logs_per_window = 50;
  std::uint16_t low_stack_words = 16;  // HEALTH headroom below this is logged once per boot
  // After an unexpected reset (watchdog, brownout, unknown or undetected cause) the device
  // node is DEGRADED this long: a reset that recovers in a second is still worth an incident.
  std::chrono::milliseconds reset_hold{20000};
  // A sensor or task seen before its DESCRIBE is reported under a placeholder name
  // (sensor<N>, task<N>) only after this long, longer than the device's DESCRIBE period, so
  // a gateway joining mid-stream doesn't leave placeholder topics and nodes behind.
  std::chrono::milliseconds describe_grace{6000};
  // Every new SENSOR reading (duplicates and stale frames excluded), with the same topic name
  // the window reports, once that name is settled (described, or after describe_grace). For
  // republishing readings, e.g. as ROS 2 topics (edge/ros/). Called on the device thread with
  // the gateway's lock held: it must be quick and must not call back into the Gateway.
  std::function<void(const std::string& topic, std::int32_t value)> on_reading;
  // Source of the gateway's own log lines (link up/down, error summaries): the agent's node.
  // The ROS bridge leaves it empty and logs them under its own name.
  std::string self_source = "edge_agent";
};

struct LinkStats {
  std::uint64_t bytes = 0;
  std::uint64_t frames = 0;         // valid frames (before duplicate checks)
  std::uint64_t crc = 0;
  std::uint64_t cobs = 0;
  std::uint64_t short_frames = 0;
  std::uint64_t overflow = 0;
  std::uint64_t version = 0;
  std::uint64_t length = 0;
  std::uint64_t bad_payload = 0;    // valid frame, malformed payload
  std::uint64_t unknown_type = 0;   // valid frame of a type this gateway doesn't know
  std::uint64_t duplicates = 0;
  std::uint64_t stale = 0;          // older than the duplicate window
  std::uint64_t lost = 0;           // seq gaps (frames that never arrived)
  std::uint64_t resets = 0;
  std::uint64_t logs_dropped = 0;

  [[nodiscard]] std::uint64_t bad() const {
    return crc + cobs + short_frames + overflow + version + length + bad_payload;
  }
};

class Gateway {
 public:
  using Clock = std::chrono::steady_clock;

  explicit Gateway(GatewayConfig config);

  // Bytes as they arrive, in any fragmentation. `wall` stamps log entries.
  void on_bytes(std::span<const std::uint8_t> data, Clock::time_point now,
                SystemTime wall = std::chrono::system_clock::now());

  // The link came up or went down (transport-level; silence is judged separately).
  void on_link(bool up, const std::string& detail, SystemTime wall = std::chrono::system_clock::now());

  // Adds this window's topics, nodes and logs to `sample`, then starts a new window.
  void contribute(Sample& sample, Clock::time_point now);

  [[nodiscard]] LinkStats stats() const;
  [[nodiscard]] std::size_t devices() const;

  static constexpr std::uint16_t kDuplicateWindow = 64;

 private:
  struct Sensor {
    std::string name;
    std::optional<double> expected_hz;
    std::uint64_t count = 0;  // frames this window
    std::optional<Clock::time_point> last;
    bool described = false;
    std::optional<Clock::time_point> first_seen;
  };
  struct Task {
    std::string name;
    NodeState state = NodeState::kRunning;
    bool low_stack_logged = false;
    bool described = false;
    std::optional<Clock::time_point> first_seen;
  };
  struct Device {
    std::uint16_t id = 0;
    std::uint16_t boot_id = 0;
    std::uint32_t uptime_ms = 0;
    bool have_seq = false;
    std::uint16_t top_seq = 0;
    std::uint64_t seen_bits = 0;  // bit k: top_seq - k was received
    Clock::time_point last_frame;
    std::optional<Clock::time_point> last_health;
    std::optional<Clock::time_point> degraded_until;  // after an unexpected reset
    std::map<std::uint8_t, Sensor> sensors;
    std::map<std::uint8_t, Task> tasks;
  };
  enum class SeqVerdict : std::uint8_t { kNew, kDuplicate, kStale };

  void handle(const ofp_frame& f, Clock::time_point now, SystemTime wall);
  void count_error(ofp_result r);
  SeqVerdict check_seq(Device& d, std::uint16_t seq);
  void reset(Device& d, const std::string& why, LogLevel level, Clock::time_point now,
             SystemTime wall);
  void add_log(LogLevel level, std::string source, std::string message, SystemTime wall);
  [[nodiscard]] std::string device_name(const Device& d) const;
  [[nodiscard]] bool name_settled(const Sensor& s, Clock::time_point now) const;

  GatewayConfig config_;
  mutable std::mutex mutex_;
  ofp_decoder decoder_{};
  std::map<std::uint16_t, Device> devices_;
  LinkStats stats_;
  std::uint64_t window_frames_ = 0;
  std::uint64_t window_bad_ = 0;
  std::optional<Clock::time_point> window_start_;
  LinkStats reported_;  // stats at the last error summary
  std::optional<Clock::time_point> last_error_log_;
  std::vector<LogEntry> logs_;
};

}  // namespace openfleet::edge

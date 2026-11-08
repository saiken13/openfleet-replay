// What the agent measures and how it is sent: the TelemetryBatch JSON contract shared with
// the simulator and the ROS collector (backend/app/schemas/telemetry.py).
#pragma once

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json_fwd.hpp>

namespace openfleet::edge {

using SystemTime = std::chrono::system_clock::time_point;

enum class NodeState : std::uint8_t { kRunning, kDegraded, kStopped, kError };
std::string_view to_string(NodeState state);

enum class LogLevel : std::uint8_t { kDebug, kInfo, kWarn, kError, kFatal };
std::string_view to_string(LogLevel level);

struct NodeReport {
  std::string node;
  NodeState state = NodeState::kRunning;
  bool operator==(const NodeReport&) const = default;
};

struct TopicReport {
  std::string topic;
  std::optional<double> expected_hz;  // > 0, or none
  double actual_hz = 0;
  std::optional<double> last_msg_age_s;
};

struct LogEntry {
  SystemTime ts;
  LogLevel level = LogLevel::kInfo;
  std::string source;
  std::string message;
};

// One sampling period's measurements (robot clock).
struct Sample {
  SystemTime ts;
  std::optional<double> cpu_pct;
  std::optional<double> mem_pct;
  std::vector<TopicReport> topics;  // device sensors (gateway)
  std::vector<NodeReport> nodes;    // watched processes, device tasks
  std::vector<LogEntry> logs;
  bool overrun = false;           // the sampler missed its period
};

// Limits the server enforces (schemas/telemetry.py).
inline constexpr std::size_t kMaxTopicsPerBatch = 200;
inline constexpr std::size_t kMaxNodesPerBatch = 200;
inline constexpr std::size_t kMaxLogsPerBatch = 500;
inline constexpr std::size_t kMaxLogChars = 4000;
inline constexpr std::size_t kMaxNameChars = 256;

// "2026-11-07T10:00:00.123456+00:00": UTC with an explicit offset (the API requires one).
std::string format_iso8601(SystemTime ts);

// Clamped to 0..100 and rounded to 2 decimals; NaN and inf become null.
std::optional<double> clamp_pct(std::optional<double> value);

// The TelemetryBatch body. `topics`, `nodes` and `logs` are capped at the server's limits.
nlohmann::json batch_json(std::int64_t seq, const Sample& sample,
                          std::optional<double> net_latency_ms);

}  // namespace openfleet::edge

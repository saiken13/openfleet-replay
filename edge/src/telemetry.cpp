#include "openfleet/edge/telemetry.hpp"

#include <algorithm>
#include <cmath>
#include <ctime>
#include <format>

#include <nlohmann/json.hpp>

namespace openfleet::edge {

std::string_view to_string(NodeState state) {
  switch (state) {
    case NodeState::kRunning: return "RUNNING";
    case NodeState::kDegraded: return "DEGRADED";
    case NodeState::kStopped: return "STOPPED";
    case NodeState::kError: return "ERROR";
  }
  return "ERROR";
}

std::string_view to_string(LogLevel level) {
  switch (level) {
    case LogLevel::kDebug: return "DEBUG";
    case LogLevel::kInfo: return "INFO";
    case LogLevel::kWarn: return "WARN";
    case LogLevel::kError: return "ERROR";
    case LogLevel::kFatal: return "FATAL";
  }
  return "ERROR";
}

std::string format_iso8601(SystemTime ts) {
  using namespace std::chrono;
  const auto us = floor<microseconds>(ts.time_since_epoch());
  const auto secs = floor<seconds>(us);
  const std::time_t t = secs.count();
  std::tm tm{};
  gmtime_r(&t, &tm);
  return std::format("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}+00:00", tm.tm_year + 1900,
                     tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec,
                     (us - secs).count());
}

std::optional<double> clamp_pct(std::optional<double> value) {
  if (!value || !std::isfinite(*value)) {
    return std::nullopt;
  }
  return std::round(std::clamp(*value, 0.0, 100.0) * 100.0) / 100.0;
}

namespace {

nlohmann::json nullable(std::optional<double> v) {
  return v ? nlohmann::json(*v) : nlohmann::json(nullptr);
}

std::string truncated(const std::string& s, std::size_t max) {
  return s.size() <= max ? s : s.substr(0, max);
}

}  // namespace

nlohmann::json batch_json(std::int64_t seq, const Sample& sample,
                          std::optional<double> net_latency_ms) {
  nlohmann::json metrics = {
      {"cpu_pct", nullable(clamp_pct(sample.cpu_pct))},
      {"mem_pct", nullable(clamp_pct(sample.mem_pct))},
  };
  if (net_latency_ms && std::isfinite(*net_latency_ms) && *net_latency_ms >= 0) {
    metrics["net_latency_ms"] = std::round(*net_latency_ms * 100.0) / 100.0;
  }
  auto topics = nlohmann::json::array();
  for (const auto& t : sample.topics) {
    if (topics.size() == kMaxTopicsPerBatch) {
      break;
    }
    nlohmann::json j = {{"topic", truncated(t.topic, kMaxNameChars)},
                        {"actual_hz", std::isfinite(t.actual_hz) && t.actual_hz > 0
                                          ? std::round(t.actual_hz * 100.0) / 100.0
                                          : 0.0}};
    if (t.expected_hz && std::isfinite(*t.expected_hz) && *t.expected_hz > 0) {
      j["expected_hz"] = *t.expected_hz;
    }
    if (t.last_msg_age_s && std::isfinite(*t.last_msg_age_s) && *t.last_msg_age_s >= 0) {
      j["last_msg_age_s"] = std::round(*t.last_msg_age_s * 1000.0) / 1000.0;
    }
    topics.push_back(std::move(j));
  }
  auto nodes = nlohmann::json::array();
  for (const auto& n : sample.nodes) {
    if (nodes.size() == kMaxNodesPerBatch) {
      break;
    }
    nodes.push_back({{"node", truncated(n.node, kMaxNameChars)}, {"state", to_string(n.state)}});
  }
  auto logs = nlohmann::json::array();
  for (const auto& l : sample.logs) {
    if (logs.size() == kMaxLogsPerBatch) {
      break;
    }
    logs.push_back({{"ts", format_iso8601(l.ts)},
                    {"level", to_string(l.level)},
                    {"source", truncated(l.source.empty() ? "edge_agent" : l.source, kMaxNameChars)},
                    {"message", truncated(l.message, kMaxLogChars)}});
  }
  return {
      {"seq", seq},
      {"ts", format_iso8601(sample.ts)},
      {"metrics", std::move(metrics)},
      {"topics", std::move(topics)},
      {"nodes", std::move(nodes)},
      {"logs", std::move(logs)},
  };
}

}  // namespace openfleet::edge

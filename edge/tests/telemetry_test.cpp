#include "openfleet/edge/telemetry.hpp"

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <cmath>
#include <limits>

using namespace openfleet::edge;
using namespace std::chrono;

namespace {
SystemTime at(std::int64_t unix_us) { return SystemTime(microseconds(unix_us)); }
}  // namespace

TEST(Telemetry, Iso8601IsUtcWithMicrosecondsAndAnOffset) {
  EXPECT_EQ(format_iso8601(at(0)), "1970-01-01T00:00:00.000000+00:00");
  // 2026-11-07T10:20:30.123456Z
  EXPECT_EQ(format_iso8601(at(1'794'046'830'123'456)), "2026-11-07T10:20:30.123456+00:00");
}

TEST(Telemetry, PercentagesAreClampedRoundedAndNeverNaN) {
  EXPECT_EQ(clamp_pct(12.3456), 12.35);
  EXPECT_EQ(clamp_pct(-3.0), 0.0);
  EXPECT_EQ(clamp_pct(100.0001), 100.0);
  EXPECT_FALSE(clamp_pct(std::nullopt));
  EXPECT_FALSE(clamp_pct(std::numeric_limits<double>::quiet_NaN()));
  EXPECT_FALSE(clamp_pct(std::numeric_limits<double>::infinity()));
}

TEST(Telemetry, BatchMatchesTheApiContract) {
  Sample s;
  s.ts = at(1'794'046'830'000'000);
  s.cpu_pct = 41.239;
  s.nodes = {{"edge_agent", NodeState::kRunning}, {"worker", NodeState::kStopped}};
  s.logs = {{at(1'794'046'830'000'000), LogLevel::kWarn, "edge_agent", "hello"}};
  const auto j = batch_json(7, s, 12.3456);

  EXPECT_EQ(j["seq"], 7);
  EXPECT_EQ(j["ts"], "2026-11-07T10:20:30.000000+00:00");
  EXPECT_EQ(j["metrics"]["cpu_pct"], 41.24);
  EXPECT_TRUE(j["metrics"]["mem_pct"].is_null());
  EXPECT_EQ(j["metrics"]["net_latency_ms"], 12.35);
  EXPECT_FALSE(j["metrics"].contains("battery_pct"));  // a Linux box has no battery to report
  EXPECT_TRUE(j["topics"].is_array() && j["topics"].empty());

  s.topics = {{"/mcu1/imu", 50.0, 49.876, 0.0123}, {"/mcu1/range", std::nullopt, -1.0, -2.0}};
  const auto t = batch_json(8, s, std::nullopt)["topics"];
  EXPECT_EQ(t[0], (nlohmann::json{{"topic", "/mcu1/imu"},
                                  {"expected_hz", 50.0},
                                  {"actual_hz", 49.88},
                                  {"last_msg_age_s", 0.012}}));
  // Nonsense values never reach the strict server schema.
  EXPECT_EQ(t[1], (nlohmann::json{{"topic", "/mcu1/range"}, {"actual_hz", 0.0}}));
  EXPECT_EQ(j["nodes"][1], (nlohmann::json{{"node", "worker"}, {"state", "STOPPED"}}));
  EXPECT_EQ(j["logs"][0]["level"], "WARN");
  EXPECT_EQ(j["logs"][0]["source"], "edge_agent");
  // Only keys the strict server schema accepts (extra="forbid").
  for (const auto& [key, _] : j.items()) {
    EXPECT_TRUE(key == "seq" || key == "ts" || key == "metrics" || key == "topics" ||
                key == "nodes" || key == "logs")
        << key;
  }
}

TEST(Telemetry, NoLatencyMeansNoLatencyField) {
  Sample s;
  EXPECT_FALSE(batch_json(0, s, std::nullopt)["metrics"].contains("net_latency_ms"));
  EXPECT_FALSE(batch_json(0, s, -1.0)["metrics"].contains("net_latency_ms"));
}

TEST(Telemetry, ListsAndStringsAreCappedAtServerLimits) {
  Sample s;
  for (int i = 0; i < 250; ++i) {
    s.nodes.push_back({"n" + std::to_string(i), NodeState::kRunning});
  }
  for (int i = 0; i < 600; ++i) {
    s.logs.push_back({SystemTime{}, LogLevel::kInfo, "", std::string(5000, 'x')});
  }
  s.nodes[0].node = std::string(300, 'a');
  const auto j = batch_json(0, s, std::nullopt);
  EXPECT_EQ(j["nodes"].size(), kMaxNodesPerBatch);
  EXPECT_EQ(j["logs"].size(), kMaxLogsPerBatch);
  EXPECT_EQ(j["nodes"][0]["node"].get<std::string>().size(), kMaxNameChars);
  EXPECT_EQ(j["logs"][0]["message"].get<std::string>().size(), kMaxLogChars);
  EXPECT_EQ(j["logs"][0]["source"], "edge_agent");  // never empty: the server requires one
}

// The uplink contract, case by case; the same cases as collector/tests/test_uplink.py.
#include "openfleet/edge/uplink.hpp"

#include <gtest/gtest.h>

#include "fake_http.hpp"
#include "openfleet/edge/log.hpp"

using namespace openfleet::edge;
using openfleet::edge::testing::FakeHttp;
using openfleet::edge::testing::Request;

namespace {

class UplinkTest : public ::testing::Test {
 protected:
  void SetUp() override { log::set_min_level(LogLevel::kFatal); }
  void TearDown() override { log::set_min_level(LogLevel::kInfo); }

  Uplink make(std::size_t buffer_max = 600) {
    UplinkConfig cfg;
    cfg.robot_id = "edge-01";
    cfg.robot_model = "test-box";
    cfg.software_version = "9.9";
    cfg.buffer_max = buffer_max;
    return Uplink(cfg, http, [this] { return now; }, [this] {
      return "run-" + std::to_string(++runs);
    });
  }

  Sample sample(double cpu = 10) {
    Sample s;
    s.ts = now;
    s.cpu_pct = cpu;
    return s;
  }

  std::vector<std::int64_t> sent_seqs() const {
    std::vector<std::int64_t> out;
    for (const auto& r : http.telemetry()) {
      out.push_back(r.body["seq"].get<std::int64_t>());
    }
    return out;
  }

  FakeHttp http;
  SystemTime now = SystemTime(std::chrono::seconds(1'794'046'830));
  int runs = 0;
};

}  // namespace

TEST_F(UplinkTest, StartRegistersTheRobotThenOpensARun) {
  auto up = make();
  ASSERT_TRUE(up.start());
  const auto reqs = http.requests();
  ASSERT_EQ(reqs.size(), 2U);
  EXPECT_EQ(reqs[0].method, "PUT");
  EXPECT_EQ(reqs[0].path, "/api/v1/robots/edge-01");
  EXPECT_EQ(reqs[0].body, (nlohmann::json{{"name", "edge-01"}, {"model", "test-box"}}));
  EXPECT_EQ(reqs[1].method, "PUT");
  EXPECT_EQ(reqs[1].path, "/api/v1/runs/run-1");
  EXPECT_EQ(reqs[1].body["robot_id"], "edge-01");
  EXPECT_EQ(reqs[1].body["software_version"], "9.9");
  EXPECT_EQ(reqs[1].body["started_at"], "2026-11-07T10:20:30.000000+00:00");
  EXPECT_EQ(up.run_id(), "run-1");
}

TEST_F(UplinkTest, StartFailsSoftlyWhileTheApiIsDown) {
  http.set_handler([](const Request&) { return FakeHttp::down(); });
  auto up = make();
  EXPECT_FALSE(up.start());
  EXPECT_FALSE(up.has_run());
  http.set_handler(nullptr);
  EXPECT_TRUE(up.start());
}

TEST_F(UplinkTest, SamplesWithoutARunAreCountedNotSent) {
  auto up = make();
  up.enqueue(sample());
  EXPECT_EQ(up.pending(), 0U);
  EXPECT_EQ(up.stats().dropped_no_run, 1U);
}

TEST_F(UplinkTest, DeliversInOrderWithConsecutiveSeqsAndItsOwnNode) {
  auto up = make();
  ASSERT_TRUE(up.start());
  for (int i = 0; i < 3; ++i) {
    up.enqueue(sample(10.0 + i));
  }
  while (up.pump() == PumpResult::kProgress) {
  }
  EXPECT_EQ(sent_seqs(), (std::vector<std::int64_t>{0, 1, 2}));
  const auto t = http.telemetry();
  EXPECT_EQ(t[0].path, "/api/v1/runs/run-1/telemetry");
  EXPECT_EQ(t[2].body["metrics"]["cpu_pct"], 12.0);
  EXPECT_EQ(t[0].body["nodes"].back(),
            (nlohmann::json{{"node", "edge_agent"}, {"state", "RUNNING"}}));
  EXPECT_EQ(up.stats().sent, 3U);
  EXPECT_EQ(up.pump(), PumpResult::kIdle);
}

TEST_F(UplinkTest, LatencyOfTheLastDeliveryGoesOutWithTheNextBatch) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  up.pump();
  up.enqueue(sample());
  up.pump();
  const auto t = http.telemetry();
  EXPECT_FALSE(t[0].body["metrics"].contains("net_latency_ms"));  // nothing measured yet
  EXPECT_EQ(t[1].body["metrics"]["net_latency_ms"], 4.0);          // FakeHttp::ok: 4 ms
}

TEST_F(UplinkTest, TransportErrorsAndServerErrorsRetryTheSameSeq) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  http.set_handler([](const Request&) { return FakeHttp::down(); });
  EXPECT_EQ(up.pump(), PumpResult::kRetry);
  http.set_handler([](const Request&) { return FakeHttp::status(503); });
  EXPECT_EQ(up.pump(), PumpResult::kRetry);
  EXPECT_EQ(up.pending(), 1U);
  http.set_handler(nullptr);
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
  EXPECT_EQ(sent_seqs(), (std::vector<std::int64_t>{0, 0, 0}));  // same batch every time
  EXPECT_EQ(http.telemetry()[0].body, http.telemetry()[2].body);
}

TEST_F(UplinkTest, AnOutageIsReportedOnceItEnds) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  http.set_handler([](const Request&) { return FakeHttp::down(); });
  up.pump();
  now += std::chrono::seconds(45);
  up.enqueue(sample());  // built during the outage: no stale latency on it
  http.set_handler(nullptr);
  while (up.pump() == PumpResult::kProgress) {
  }
  up.enqueue(sample());
  up.pump();
  const auto t = http.telemetry();
  EXPECT_FALSE(t.back().body["logs"].empty());
  const auto msg = t.back().body["logs"][0]["message"].get<std::string>();
  EXPECT_NE(msg.find("unreachable for 45 s"), std::string::npos) << msg;
  EXPECT_EQ(t.back().body["logs"][0]["level"], "WARN");
  EXPECT_FALSE(t[2].body["metrics"].contains("net_latency_ms"));  // seq 1, built offline
}

TEST_F(UplinkTest, RejectedKeyKeepsBatchesBuffered) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  http.set_handler([](const Request&) { return FakeHttp::status(401); });
  EXPECT_EQ(up.pump(), PumpResult::kRetry);
  http.set_handler([](const Request&) { return FakeHttp::status(403); });
  EXPECT_EQ(up.pump(), PumpResult::kRetry);
  EXPECT_EQ(up.pending(), 1U);
  EXPECT_EQ(up.stats().dropped, 0U);
}

TEST_F(UplinkTest, InvalidBatchIsDroppedAndTheNextOneGoes) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  up.enqueue(sample());
  int calls = 0;
  http.set_handler([&calls](const Request&) {
    return ++calls == 1 ? FakeHttp::status(422, R"({"detail":"bad"})") : FakeHttp::ok();
  });
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
  EXPECT_EQ(up.stats().dropped, 1U);
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
  EXPECT_EQ(sent_seqs(), (std::vector<std::int64_t>{0, 1}));
  // The agent reports its own loss: the batch after it says edge_agent is DEGRADED.
  up.enqueue(sample());
  up.pump();
  EXPECT_EQ(http.telemetry().back().body["nodes"].back()["state"], "DEGRADED");
}

TEST_F(UplinkTest, ConflictStartsAFreshRunAndDropsTheDeadRunsBatches) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  up.enqueue(sample());
  http.set_handler([](const Request& r) {
    return r.path == "/api/v1/runs/run-1/telemetry" ? FakeHttp::status(409) : FakeHttp::ok();
  });
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
  EXPECT_EQ(up.run_id(), "run-2");
  EXPECT_EQ(up.pending(), 0U);
  EXPECT_EQ(up.stats().dropped, 2U);
  EXPECT_EQ(up.stats().runs_started, 2U);
  // 409 doesn't re-register: the robot still exists.
  int registrations = 0;
  for (const auto& r : http.requests()) {
    registrations += r.path == "/api/v1/robots/edge-01" ? 1 : 0;
  }
  EXPECT_EQ(registrations, 1);

  up.enqueue(sample());
  up.pump();
  const auto last = http.telemetry().back();
  EXPECT_EQ(last.path, "/api/v1/runs/run-2/telemetry");
  EXPECT_EQ(last.body["seq"], 0);  // seq restarts with the run
  EXPECT_NE(last.body["logs"][0]["message"].get<std::string>().find("2 buffered batches"),
            std::string::npos);
}

TEST_F(UplinkTest, NotFoundReRegistersTheRobot) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  http.set_handler([](const Request& r) {
    return r.path.ends_with("/telemetry") && r.path.find("run-1") != std::string::npos
               ? FakeHttp::status(404)
               : FakeHttp::ok();
  });
  http.clear();
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
  const auto reqs = http.requests();
  ASSERT_EQ(reqs.size(), 3U);  // the rejected batch, then register, then the new run
  EXPECT_EQ(reqs[1].path, "/api/v1/robots/edge-01");
  EXPECT_EQ(reqs[2].path, "/api/v1/runs/run-2");
}

TEST_F(UplinkTest, FreshRunThatCannotStartIsRetriedLater) {
  auto up = make();
  ASSERT_TRUE(up.start());
  up.enqueue(sample());
  http.set_handler([](const Request& r) {
    return r.path.ends_with("/telemetry") ? FakeHttp::status(409) : FakeHttp::down();
  });
  EXPECT_EQ(up.pump(), PumpResult::kRetry);
  EXPECT_FALSE(up.has_run());
  http.set_handler(nullptr);
  EXPECT_TRUE(up.start());
}

TEST_F(UplinkTest, FullOutboxDropsTheOldest) {
  auto up = make(3);
  ASSERT_TRUE(up.start());
  for (int i = 0; i < 5; ++i) {
    up.enqueue(sample());
  }
  EXPECT_EQ(up.pending(), 3U);
  EXPECT_EQ(up.stats().dropped, 2U);
  while (up.pump() == PumpResult::kProgress) {
  }
  EXPECT_EQ(sent_seqs(), (std::vector<std::int64_t>{2, 3, 4}));  // the newest three
}

TEST_F(UplinkTest, EndSendsTheStatus) {
  auto up = make();
  EXPECT_FALSE(up.end(RunStatus::kAborted));  // no run: nothing to end
  ASSERT_TRUE(up.start());
  now += std::chrono::seconds(5);
  EXPECT_TRUE(up.end(RunStatus::kAborted));
  const auto last = http.requests().back();
  EXPECT_EQ(last.method, "POST");
  EXPECT_EQ(last.path, "/api/v1/runs/run-1/end");
  EXPECT_EQ(last.body, (nlohmann::json{{"status", "ABORTED"},
                                       {"ended_at", "2026-11-07T10:20:35.000000+00:00"}}));
  http.set_handler([](const Request&) { return FakeHttp::down(); });
  EXPECT_FALSE(up.end(RunStatus::kSuccess));
}

TEST_F(UplinkTest, BrokenUtf8InALogNeverBreaksSerialization) {
  auto up = make();
  ASSERT_TRUE(up.start());
  auto s = sample();
  s.logs.push_back({now, LogLevel::kWarn, "x", std::string("caf\xC3", 4)});  // cut mid-char
  up.enqueue(s);
  EXPECT_EQ(up.pump(), PumpResult::kProgress);
}

TEST(UplinkIds, RandomRunIdsAreVersion4Uuids) {
  const auto id = Uplink::random_uuid();
  ASSERT_EQ(id.size(), 36U);
  EXPECT_EQ(id[14], '4');
  EXPECT_TRUE(id[19] == '8' || id[19] == '9' || id[19] == 'a' || id[19] == 'b');
  EXPECT_NE(id, Uplink::random_uuid());
}

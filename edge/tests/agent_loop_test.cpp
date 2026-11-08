// The threads together: a producer racing the uplink loop through a simulated API outage.
// Run under ThreadSanitizer in CI (preset `tsan`).
#include "openfleet/edge/agent.hpp"

#include <gtest/gtest.h>

#include <filesystem>
#include <thread>

#include "fake_http.hpp"
#include "openfleet/edge/log.hpp"

using namespace openfleet::edge;
using namespace std::chrono_literals;
using openfleet::edge::testing::FakeHttp;
using openfleet::edge::testing::Request;
using SteadyClock = std::chrono::steady_clock;

namespace {

class AgentLoopTest : public ::testing::Test {
 protected:
  void SetUp() override { log::set_min_level(LogLevel::kFatal); }
  void TearDown() override { log::set_min_level(LogLevel::kInfo); }

  static UplinkConfig config(std::size_t buffer_max = 600) {
    UplinkConfig c;
    c.robot_id = "edge-01";
    c.buffer_max = buffer_max;
    return c;
  }

  static Sample sample(int i) {
    Sample s;
    s.ts = std::chrono::system_clock::now();
    s.cpu_pct = static_cast<double>(i % 100);
    return s;
  }
};

}  // namespace

TEST_F(AgentLoopTest, OutageLosesNothingAndDeliversInOrder) {
  constexpr int kSamples = 300;
  FakeHttp http;
  std::mutex mu;
  std::vector<std::int64_t> delivered;  // seqs the "server" accepted, in arrival order
  std::atomic<bool> api_down{false};
  http.set_handler([&](const Request& r) -> std::optional<HttpResponse> {
    if (!r.path.ends_with("/telemetry")) {
      return FakeHttp::ok(201);
    }
    if (api_down.load()) {
      return FakeHttp::down();
    }
    const std::lock_guard lock(mu);
    delivered.push_back(r.body["seq"].get<std::int64_t>());
    return FakeHttp::ok();
  });

  Uplink uplink(config(), http);
  Backoff backoff(1ms, 8ms, 7);
  BoundedQueue<Sample> samples(64);
  std::thread up([&] {
    uplink_loop(uplink, samples, backoff, 2s, [] { return RunStatus::kAborted; });
  });
  // The loop's first pop happens before its first request, so once a request is seen every
  // later sample is enqueued after start() (an earlier one would be dropped as no-run).
  // Uplink itself is only touched by its own thread; the test watches FakeHttp.
  while (http.requests().empty()) {
    std::this_thread::sleep_for(1ms);
  }

  for (int i = 0; i < kSamples; ++i) {
    if (i == 50) {
      api_down.store(true);  // the API goes away for a third of the run...
    }
    if (i == 150) {
      api_down.store(false);  // ...and comes back
    }
    // Pace on the queue, not the clock: this test is about the outbox surviving an outage,
    // and the uplink thread is ~100x slower under ASan or qemu than natively. (The agent
    // samples at 1 Hz; a batch costs well under a millisecond natively.)
    while (samples.size() > 16) {
      std::this_thread::sleep_for(100us);
    }
    EXPECT_NE(samples.push(sample(i)), PushResult::kClosed);
    std::this_thread::sleep_for(200us);
  }
  samples.close();
  up.join();

  std::vector<std::int64_t> expected(kSamples);
  for (int i = 0; i < kSamples; ++i) {
    expected[static_cast<std::size_t>(i)] = i;
  }
  EXPECT_EQ(delivered, expected);  // every seq, once, in order
  EXPECT_EQ(samples.dropped(), 0U);
  const auto st = uplink.stats();
  EXPECT_EQ(st.dropped, 0U);
  EXPECT_EQ(st.pending, 0U);
  EXPECT_EQ(http.requests().back().path, "/api/v1/runs/" + uplink.run_id() + "/end");
  EXPECT_EQ(http.requests().back().body["status"], "ABORTED");
}

TEST_F(AgentLoopTest, ApiDownAtStartKeepsRetryingThenOpensTheRun) {
  FakeHttp http;
  std::atomic<int> attempts{0};
  http.set_handler([&](const Request& r) -> std::optional<HttpResponse> {
    if (r.path.starts_with("/api/v1/robots/") && attempts.fetch_add(1) < 3) {
      return FakeHttp::down();
    }
    return FakeHttp::ok();
  });
  Uplink uplink(config(), http);
  Backoff backoff(1ms, 4ms, 1);
  BoundedQueue<Sample> samples(8);
  std::thread up([&] {
    uplink_loop(uplink, samples, backoff, 1s, [] { return RunStatus::kSuccess; });
  });
  auto run_opened = [&http] {
    for (const auto& r : http.requests()) {
      if (r.path.starts_with("/api/v1/runs/")) {
        return true;
      }
    }
    return false;
  };
  for (int i = 0; i < 400 && !run_opened(); ++i) {
    samples.push(sample(i));
    std::this_thread::sleep_for(1ms);
  }
  samples.close();
  up.join();
  EXPECT_GE(attempts.load(), 4);
  EXPECT_EQ(uplink.stats().runs_started, 1U);
  EXPECT_EQ(http.requests().back().body["status"], "SUCCESS");
}

TEST_F(AgentLoopTest, ShutdownFlushIsBoundedWhenTheApiNeverReturns) {
  FakeHttp http;
  std::atomic<bool> down{false};
  http.set_handler([&](const Request&) -> std::optional<HttpResponse> {
    if (down.load()) {
      std::this_thread::sleep_for(2ms);
      return FakeHttp::down();
    }
    return FakeHttp::ok();
  });
  Uplink uplink(config(), http);
  ASSERT_TRUE(uplink.start());
  down.store(true);
  Backoff backoff(1ms, 2ms, 1);
  BoundedQueue<Sample> samples(16);
  for (int i = 0; i < 10; ++i) {
    samples.push(sample(i));
  }
  samples.close();
  const auto t0 = SteadyClock::now();
  uplink_loop(uplink, samples, backoff, 200ms, [] { return RunStatus::kAborted; });
  EXPECT_LT(SteadyClock::now() - t0, 2s);
  EXPECT_EQ(uplink.pending(), 10U);  // kept, not silently discarded
  EXPECT_EQ(http.requests().back().path, "/api/v1/runs/" + uplink.run_id() + "/end");
}

TEST_F(AgentLoopTest, SamplerLoopKeepsItsPeriodAndStopsPromptly) {
  Sampler sampler({std::filesystem::path(EDGE_FIXTURES_DIR) / "proc", {"sleep"}, 0ms});
  BoundedQueue<Sample> out(1000);
  StopSignal stop;
  std::thread t([&] { sampler_loop(sampler, out, stop, 10ms); });
  std::this_thread::sleep_for(200ms);
  const auto t0 = SteadyClock::now();
  stop.request();
  t.join();
  EXPECT_LT(SteadyClock::now() - t0, 100ms);  // woken, not waiting out a period
  // ~20 periods; generous bounds for slow CI and qemu.
  EXPECT_GE(out.size(), 5U);
  EXPECT_LE(out.size(), 25U);
  const auto first = out.pop_for(0ms);
  ASSERT_TRUE(first.has_value());
  EXPECT_EQ(first->nodes.at(0).node, "sleep");
}

TEST_F(AgentLoopTest, SamplerNeverBlocksOnAFullQueue) {
  Sampler sampler({std::filesystem::path(EDGE_FIXTURES_DIR) / "proc", {}, 0ms});
  BoundedQueue<Sample> out(2);  // nobody consumes it
  StopSignal stop;
  std::thread t([&] { sampler_loop(sampler, out, stop, 1ms); });
  std::this_thread::sleep_for(100ms);
  stop.request();
  t.join();
  EXPECT_EQ(out.size(), 2U);
  EXPECT_GT(out.dropped(), 10U);
}

TEST(StopSignal, WaitReturnsWhetherStopWasRequested) {
  StopSignal stop;
  EXPECT_FALSE(stop.wait_for(5ms));
  std::thread t([&] { stop.request(); });
  EXPECT_TRUE(stop.wait_for(10s));
  t.join();
  EXPECT_TRUE(stop.requested());
}

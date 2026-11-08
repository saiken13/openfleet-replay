// The gateway against the simulated device, on simulated time: every mapping and every
// fault in docs/PROTOCOL.md. Deterministic (fixed seeds, no wall-clock waits).
#include "openfleet/edge/gateway.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <map>
#include <thread>

#include "openfleet/edge/device_sim.hpp"

using namespace openfleet::edge;
using namespace std::chrono_literals;
using Clock = Gateway::Clock;

namespace {

class GatewayTest : public ::testing::Test {
 protected:
  GatewayTest() : gw_(config()), sim_(sim_config(), t_) {}

  static GatewayConfig config() {
    GatewayConfig c;
    c.link = "test://link";
    c.error_log_interval = 0ms;  // summarise every window in tests
    return c;
  }
  static SimConfig sim_config() {
    SimConfig c;
    c.seed = 42;
    return c;
  }

  DeviceSim::Sink sink() {
    return [this](std::span<const std::uint8_t> b) { gw_.on_bytes(b, t_); };
  }

  // Advances simulated time in 5 ms steps, letting the device emit.
  void run(Clock::duration d) {
    const auto end = t_ + d;
    while (t_ < end) {
      t_ += 5ms;
      sim_.tick(t_, sink());
    }
  }

  // One sampling window of `d`, returned as the gateway's contribution.
  Sample window(Clock::duration d = 1s) {
    run(d);
    Sample s;
    gw_.contribute(s, t_);
    return s;
  }

  void cmd(const std::string& line) {
    const auto err = sim_.command(line, t_, sink());
    ASSERT_FALSE(err.has_value()) << *err;
  }

  static const TopicReport* topic(const Sample& s, const std::string& name) {
    for (const auto& t : s.topics) {
      if (t.topic == name) {
        return &t;
      }
    }
    return nullptr;
  }
  static std::optional<NodeState> node(const Sample& s, const std::string& name) {
    for (const auto& n : s.nodes) {
      if (n.node == name) {
        return n.state;
      }
    }
    return std::nullopt;
  }
  static bool logged(const Sample& s, const std::string& fragment) {
    return std::any_of(s.logs.begin(), s.logs.end(), [&](const LogEntry& l) {
      return l.message.find(fragment) != std::string::npos;
    });
  }

  Clock::time_point t_ = Clock::time_point(1000s);
  Gateway gw_;
  DeviceSim sim_;
};

}  // namespace

TEST_F(GatewayTest, SensorsBecomeTopicsTasksBecomeNodes) {
  const Sample first = window();
  EXPECT_TRUE(logged(first, "mcu1 connected on test://link: firmware devsim 0.1"));
  const Sample s = window();
  const auto* imu = topic(s, "/mcu1/imu");
  const auto* range = topic(s, "/mcu1/range");
  ASSERT_NE(imu, nullptr);
  ASSERT_NE(range, nullptr);
  EXPECT_EQ(imu->expected_hz, 50.0);  // from DESCRIBE
  EXPECT_EQ(range->expected_hz, 10.0);
  EXPECT_NEAR(imu->actual_hz, 50.0, 1.0);
  EXPECT_NEAR(range->actual_hz, 10.0, 1.0);
  EXPECT_LT(*imu->last_msg_age_s, 0.05);
  EXPECT_EQ(node(s, "mcu1"), NodeState::kRunning);
  EXPECT_EQ(node(s, "mcu1/sensor"), NodeState::kRunning);
  EXPECT_EQ(node(s, "mcu1/comms"), NodeState::kRunning);
  EXPECT_EQ(node(s, "mcu1/health"), NodeState::kRunning);
  EXPECT_EQ(gw_.devices(), 1U);
  const auto st = gw_.stats();
  EXPECT_EQ(st.bad(), 0U);
  EXPECT_EQ(st.lost, 0U);
  EXPECT_EQ(st.duplicates, 0U);
}

TEST_F(GatewayTest, UndescribedSensorsWaitForTheirNamesBeforeAPlaceholderIsUsed) {
  // A gateway that joins mid-stream: readings, but the DESCRIBE was before it connected.
  auto frame = [](std::uint16_t seq, std::uint8_t type, const std::uint8_t* p, std::size_t n) {
    ofp_header h{};
    h.type = type;
    h.device_id = 5;
    h.seq = seq;
    h.boot_id = 1;
    h.uptime_ms = 1000U + seq;
    h.length = static_cast<std::uint16_t>(n);
    std::vector<std::uint8_t> w(OFP_MAX_WIRE);
    w.resize(ofp_encode(&h, p, w.data(), w.size()));
    return w;
  };
  std::uint8_t p[OFP_MAX_PAYLOAD];
  std::uint16_t seq = 0;
  auto sensor_reading = [&] {
    gw_.on_bytes(frame(seq++, OFP_SENSOR, p, ofp_build_sensor(p, sizeof p, 3, 1)), t_);
  };
  sensor_reading();
  t_ += 1s;
  Sample s;
  gw_.contribute(s, t_);
  EXPECT_EQ(topic(s, "/mcu5/sensor3"), nullptr);  // not yet: the name may still come

  ofp_describe d{OFP_KIND_SENSOR, 3, 200, "lidar"};
  gw_.on_bytes(frame(seq++, OFP_DESCRIBE, p, ofp_build_describe(p, sizeof p, &d)), t_);
  sensor_reading();
  t_ += 1s;
  s = {};
  gw_.contribute(s, t_);
  ASSERT_NE(topic(s, "/mcu5/lidar"), nullptr);  // described: reported at once, by name
  EXPECT_EQ(topic(s, "/mcu5/sensor3"), nullptr);

  // A device that never describes its sensor still gets reported, after the grace period.
  std::uint8_t q[8];
  gw_.on_bytes(frame(seq++, OFP_SENSOR, q, ofp_build_sensor(q, sizeof q, 9, 1)), t_);
  t_ += 7s;
  gw_.on_bytes(frame(seq++, OFP_SENSOR, q, ofp_build_sensor(q, sizeof q, 9, 1)), t_);
  s = {};
  gw_.contribute(s, t_);
  EXPECT_NE(topic(s, "/mcu5/sensor9"), nullptr);
}

TEST_F(GatewayTest, AStoppedSensorReportsZeroHzAndAGrowingAge) {
  window();
  cmd("stop-sensor 2");
  window();
  const Sample s = window();
  const auto* range = topic(s, "/mcu1/range");
  ASSERT_NE(range, nullptr);  // still reported: the backend sees it stop
  EXPECT_EQ(range->actual_hz, 0.0);
  EXPECT_GT(*range->last_msg_age_s, 1.5);
  EXPECT_NEAR(topic(s, "/mcu1/imu")->actual_hz, 50.0, 1.0);
  cmd("start-sensor 2");
  window();
  EXPECT_NEAR(topic(window(), "/mcu1/range")->actual_hz, 10.0, 1.0);
}

TEST_F(GatewayTest, TaskStatesAndLowStackAreReported) {
  window();
  cmd("task 2 error");
  cmd("stack 1 8");
  const Sample s = window(1100ms);  // spans a HEALTH frame
  EXPECT_EQ(node(s, "mcu1/comms"), NodeState::kError);
  EXPECT_TRUE(logged(s, "mcu1/sensor stack headroom down to 8 words"));
  EXPECT_FALSE(logged(window(1100ms), "stack headroom"));  // once per boot, not every frame
}

TEST_F(GatewayTest, ResetWithHelloIsAnEventWithItsReason) {
  window();
  cmd("reset watchdog");
  const Sample s = window();
  EXPECT_TRUE(logged(s, "mcu1 reset (watchdog); firmware devsim 0.1; boot 2"));
  const auto it = std::find_if(s.logs.begin(), s.logs.end(), [](const LogEntry& l) {
    return l.message.find("reset (watchdog)") != std::string::npos;
  });
  EXPECT_EQ(it->level, LogLevel::kWarn);
  EXPECT_EQ(it->source, "mcu1");
  const auto st = gw_.stats();
  EXPECT_EQ(st.resets, 1U);
  EXPECT_EQ(st.lost, 0U);        // seq restarted at 0: not a 65 000-frame gap
  EXPECT_EQ(st.duplicates, 0U);  // nor duplicates of the old boot's seqs
  EXPECT_NEAR(topic(window(), "/mcu1/imu")->actual_hz, 50.0, 1.0);
  // An unexpected reset holds the device DEGRADED (so the backend opens an incident),
  // then it's RUNNING again.
  EXPECT_EQ(node(window(), "mcu1"), NodeState::kDegraded);
  window(20s);
  EXPECT_EQ(node(window(), "mcu1"), NodeState::kRunning);
}

TEST_F(GatewayTest, AnExpectedResetIsAnEventButNotDegraded) {
  window();
  cmd("reset software");
  const Sample s = window();
  EXPECT_TRUE(logged(s, "mcu1 reset (software)"));
  EXPECT_EQ(node(s, "mcu1"), NodeState::kRunning);
}

TEST_F(GatewayTest, ResetWhoseHelloWasLostIsStillDetected) {
  window();
  cmd("reset-quiet");
  const Sample s = window();
  EXPECT_TRUE(logged(s, "mcu1 reset detected (boot 1 -> 2, its HELLO was lost)"));
  EXPECT_EQ(gw_.stats().resets, 1U);
}

TEST_F(GatewayTest, ResetWithoutBootIdsIsDetectedFromUptime) {
  // A device that doesn't keep a boot counter (boot_id 0 every time).
  auto frame = [](std::uint16_t seq, std::uint32_t uptime) {
    std::uint8_t p[8];
    ofp_header h{};
    h.type = OFP_SENSOR;
    h.device_id = 9;
    h.seq = seq;
    h.uptime_ms = uptime;
    h.length = static_cast<std::uint16_t>(ofp_build_sensor(p, sizeof p, 1, 0));
    std::vector<std::uint8_t> w(OFP_MAX_WIRE);
    w.resize(ofp_encode(&h, p, w.data(), w.size()));
    return w;
  };
  gw_.on_bytes(frame(500, 60'000), t_);
  gw_.on_bytes(frame(501, 60'010), t_);
  gw_.on_bytes(frame(0, 15), t_);  // seq and uptime both start over
  gw_.on_bytes(frame(1, 25), t_);
  Sample s;
  gw_.contribute(s, t_);
  EXPECT_TRUE(logged(s, "mcu9 reset detected (uptime went back from 60.0 s to 0.0 s)"));
  EXPECT_EQ(gw_.stats().resets, 1U);
  EXPECT_EQ(gw_.stats().duplicates, 0U);
}

TEST_F(GatewayTest, DuplicatesAreDroppedAndGapsCounted) {
  window();
  cmd("duplicate 1");
  const Sample s = window(2s);
  EXPECT_GT(gw_.stats().duplicates, 100U);
  EXPECT_NEAR(topic(s, "/mcu1/imu")->actual_hz, 50.0, 1.0);  // not doubled

  cmd("faults off");
  cmd("drop 0.3");
  window(3s);
  cmd("faults off");
  window(1s);  // end on delivered frames, so every gap is closed
  const auto st = gw_.stats();
  // Every frame the device sent was either received once or counted lost.
  EXPECT_EQ(st.frames - st.duplicates + st.lost, sim_.frames_sent());
  EXPECT_GT(st.lost, 20U);
}

TEST_F(GatewayTest, CorruptionAndTruncationAreCountedNeverAccepted) {
  window();
  cmd("corrupt 0.2");
  cmd("truncate 0.1");
  const Sample s = window(5s);
  const auto st = gw_.stats();
  EXPECT_GT(st.crc + st.short_frames + st.cobs + st.length, 30U);
  // Accepted frames are a subset of what was sent: nothing invented.
  EXPECT_LE(st.frames - st.duplicates, sim_.frames_sent());
  EXPECT_TRUE(logged(s, "device link test://link:"));
  EXPECT_TRUE(logged(s, "bad frames (crc"));
  // A link this bad marks the device DEGRADED (>= 25 % bad frames in the window).
  cmd("corrupt 0.6");
  EXPECT_EQ(node(window(), "mcu1"), NodeState::kDegraded);
  cmd("faults off");
  window();
  EXPECT_EQ(node(window(), "mcu1"), NodeState::kRunning);
}

TEST_F(GatewayTest, GarbageAndOversizedInputDoNotDerailTheStream) {
  window();
  cmd("garbage 5000");
  cmd("oversize");
  const Sample s = window();
  EXPECT_GE(gw_.stats().overflow, 1U);
  EXPECT_NEAR(topic(window(), "/mcu1/imu")->actual_hz, 50.0, 2.0);
  EXPECT_TRUE(logged(s, "overflow"));
}

TEST_F(GatewayTest, FragmentationChangesNothing) {
  window();
  cmd("fragment on");
  window();
  const Sample s = window();
  EXPECT_NEAR(topic(s, "/mcu1/imu")->actual_hz, 50.0, 1.0);
  EXPECT_EQ(gw_.stats().bad(), 0U);
}

TEST_F(GatewayTest, ASilentDeviceIsStoppedAndItsTasksAreNotReported) {
  window();
  cmd("silence 6");
  window(4s);  // past the 3 s silence timeout
  const Sample s = window();
  EXPECT_EQ(node(s, "mcu1"), NodeState::kStopped);
  EXPECT_FALSE(node(s, "mcu1/sensor").has_value());  // unknown, not "still running"
  EXPECT_EQ(topic(s, "/mcu1/imu")->actual_hz, 0.0);
  window(2s);
  const Sample back = window();
  EXPECT_EQ(node(back, "mcu1"), NodeState::kRunning);
  EXPECT_NEAR(topic(back, "/mcu1/imu")->actual_hz, 50.0, 1.0);
}

TEST_F(GatewayTest, DeviceLogsAreForwardedAndCappedPerWindow) {
  window();
  cmd("log error motor current limit");
  Sample s = window();
  const auto it = std::find_if(s.logs.begin(), s.logs.end(), [](const LogEntry& l) {
    return l.message == "motor current limit";
  });
  ASSERT_NE(it, s.logs.end());
  EXPECT_EQ(it->level, LogLevel::kError);
  EXPECT_EQ(it->source, "mcu1");

  for (int i = 0; i < 80; ++i) {
    cmd("log info spam " + std::to_string(i));
  }
  s = window();
  EXPECT_EQ(s.logs.size(), 50U);
  EXPECT_EQ(gw_.stats().logs_dropped, 30U);
}

TEST_F(GatewayTest, LinkUpAndDownAreLogged) {
  gw_.on_link(true, {});
  gw_.on_link(false, "closed by the device side");
  Sample s;
  gw_.contribute(s, t_);
  EXPECT_TRUE(logged(s, "device link test://link connected"));
  EXPECT_TRUE(logged(s, "device link test://link lost: closed by the device side"));
}

// TSan: the device thread feeds bytes while the sampler thread takes windows.
TEST(GatewayReadings, EveryNewReadingIsPassedOnUnderItsTopicNameDuplicatesAreNot) {
  std::map<std::string, std::vector<std::int32_t>> got;
  GatewayConfig c;
  c.link = "test://link";
  c.on_reading = [&](const std::string& topic, std::int32_t value) {
    got[topic].push_back(value);
  };
  Gateway gw(c);
  Clock::time_point t(1000s);
  SimConfig sc;
  sc.seed = 42;
  DeviceSim sim(sc, t);
  const DeviceSim::Sink sink = [&](std::span<const std::uint8_t> b) { gw.on_bytes(b, t); };
  auto run = [&](Clock::duration d) {
    const auto end = t + d;
    while (t < end) {
      t += 5ms;
      sim.tick(t, sink);
    }
  };
  run(1s);
  got.clear();
  run(2s);
  ASSERT_EQ(got.size(), 2U);  // the described names only, no placeholders
  EXPECT_NEAR(static_cast<double>(got["/mcu1/imu"].size()), 100.0, 3.0);
  EXPECT_NEAR(static_cast<double>(got["/mcu1/range"].size()), 20.0, 2.0);

  ASSERT_FALSE(sim.command("duplicate 1", t, sink).has_value());
  got.clear();
  run(2s);
  EXPECT_GT(gw.stats().duplicates, 100U);
  EXPECT_NEAR(static_cast<double>(got["/mcu1/imu"].size()), 100.0, 3.0);  // not doubled
}

TEST(GatewayThreads, FeedingAndSamplingConcurrentlyIsSafe) {
  Gateway gw({});
  std::atomic<bool> done{false};
  std::thread device([&] {  // runs until the sampler side has taken its windows
    auto t = Clock::now();
    SimConfig cfg;
    cfg.seed = 3;
    DeviceSim sim(cfg, t);
    sim.faults().corrupt = 0.1;
    sim.faults().fragment = true;
    while (!done) {
      t += 5ms;
      sim.tick(t, [&](std::span<const std::uint8_t> b) { gw.on_bytes(b, Clock::now()); });
      std::this_thread::sleep_for(200us);
    }
  });
  std::uint64_t topics = 0;
  for (int i = 0; i < 50; ++i) {
    std::this_thread::sleep_for(2ms);
    Sample s;
    gw.contribute(s, Clock::now());
    topics += s.topics.size();
  }
  done = true;
  device.join();
  EXPECT_GT(gw.stats().frames, 0U);
  EXPECT_GT(topics, 0U);
}

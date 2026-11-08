// A simulated microcontroller speaking the device protocol, with fault injection
// (docs/PROTOCOL.md "Testing"). Host-side and deterministic for a given seed; it encodes its
// frames with the same C library the firmware uses, so it exercises the real encoder.
//
// It produces bytes; whoever owns it decides where they go (a TCP client, a pty, a test).
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <random>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "openfleet/protocol.h"

namespace openfleet::edge {

struct SimSensor {
  std::uint8_t id = 1;
  std::string name = "imu";
  double hz = 50;
  bool running = true;
};

struct SimTask {
  std::uint8_t id = 1;
  std::string name = "sensor";
  std::uint8_t state = OFP_TASK_RUNNING;
  std::uint16_t stack_free_words = 200;
};

struct SimFaults {
  double corrupt = 0;    // probability per frame: flip one random bit
  double truncate = 0;   // cut the frame short (the line resyncs at the next delimiter)
  double drop = 0;       // never send the frame
  double duplicate = 0;  // send it twice
  bool fragment = false; // split frames into random small chunks
};

struct SimConfig {
  std::uint16_t device_id = 1;
  std::uint16_t boot_id = 1;
  std::string firmware = "devsim 0.1";
  std::vector<SimSensor> sensors = {{1, "imu", 50, true}, {2, "range", 10, true}};
  std::vector<SimTask> tasks = {{1, "sensor", OFP_TASK_RUNNING, 180},
                                {2, "comms", OFP_TASK_RUNNING, 220},
                                {3, "health", OFP_TASK_RUNNING, 150}};
  std::chrono::milliseconds health_period{1000};
  std::chrono::milliseconds describe_period{5000};
  std::uint64_t seed = 1;
};

class DeviceSim {
 public:
  using Clock = std::chrono::steady_clock;
  // Receives wire bytes. With `fragment` on, one frame arrives as several calls.
  using Sink = std::function<void(std::span<const std::uint8_t>)>;

  DeviceSim(SimConfig config, Clock::time_point now);

  // Emits everything due by `now`: HELLO and DESCRIBE after a boot, sensor readings at their
  // rates, HEALTH and DESCRIBE periodically. Nothing while silenced.
  void tick(Clock::time_point now, const Sink& sink);

  // A control command (see docs/PROTOCOL.md). Returns an error message, or nullopt if OK.
  std::optional<std::string> command(std::string_view line, Clock::time_point now,
                                     const Sink& sink);

  [[nodiscard]] std::uint64_t frames_sent() const { return frames_sent_; }
  [[nodiscard]] std::uint16_t boot_id() const { return config_.boot_id; }
  [[nodiscard]] const SimFaults& faults() const { return faults_; }
  SimFaults& faults() { return faults_; }

  static constexpr const char* kCommands =
      "stop-sensor ID | start-sensor ID | task ID running|degraded|stopped|error | "
      "stack ID WORDS | reset [power|watchdog|software|brownout|pin] | reset-quiet | "
      "silence SECONDS | corrupt P | truncate P | drop P | duplicate P | fragment on|off | "
      "faults off | garbage N | oversize | log debug|info|warn|error|fatal TEXT";

 private:
  void boot(Clock::time_point now, std::uint8_t reason, bool send_hello, const Sink& sink);
  void send(std::uint8_t type, const std::uint8_t* payload, std::size_t len, Clock::time_point now,
            const Sink& sink);
  void emit(std::span<const std::uint8_t> frame, const Sink& sink);
  void describe_all(Clock::time_point now, const Sink& sink);
  bool chance(double p);

  SimConfig config_;
  SimFaults faults_;
  std::mt19937_64 rng_;
  Clock::time_point boot_time_;
  std::uint16_t seq_ = 0;
  std::vector<Clock::time_point> next_sample_;
  Clock::time_point next_health_;
  Clock::time_point next_describe_;
  std::optional<Clock::time_point> silent_until_;
  std::uint64_t frames_sent_ = 0;
  bool powered_on_ = false;
  std::int32_t reading_ = 0;
};

}  // namespace openfleet::edge

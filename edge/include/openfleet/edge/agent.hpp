// The agent process: configuration, and its threads.
//
//   sampler thread --push--> BoundedQueue<Sample> --pop--> uplink thread --HTTP--> OpenFleet
//        ^ contribute()
//   Gateway <--on_bytes()-- device thread (one per --device link; Phase 2, DECISIONS #033)
//   signal thread: SIGTERM/SIGINT -> stop
//
// The sampler never waits on the network: the queue drops its oldest sample when full. The
// uplink thread turns samples into numbered batches, retries them in order, and uses the
// queue's timed pop as its backoff sleep, so it keeps taking samples while it waits.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "openfleet/edge/backoff.hpp"
#include "openfleet/edge/bounded_queue.hpp"
#include "openfleet/edge/device_link.hpp"
#include "openfleet/edge/http.hpp"
#include "openfleet/edge/sampler.hpp"
#include "openfleet/edge/uplink.hpp"

namespace openfleet::edge {

using Millis = std::chrono::milliseconds;

struct AgentConfig {
  std::string api_url = "http://localhost:8000";
  std::string api_key;  // from OPENFLEET_API_KEY, never a flag (argv is world-readable)
  UplinkConfig uplink;
  SamplerConfig sampler;
  Millis period{1000};
  Millis request_timeout{5000};
  Millis backoff_initial{500};
  Millis backoff_max{10000};
  Millis flush_timeout{5000};
  Millis duration{0};  // 0: until SIGTERM/SIGINT
  std::size_t queue_capacity = 64;
  std::vector<DeviceEndpoint> devices;  // --device: microcontrollers behind gateways
  Millis device_silence{3000};          // a device silent this long is STOPPED
};

struct ParseResult {
  std::optional<AgentConfig> config;  // set when the agent should run
  std::string message;                // help text or the error
  int exit_code = 0;
};

using GetEnv = std::function<const char*(const char*)>;
ParseResult parse_args(const std::vector<std::string_view>& args, const GetEnv& getenv);
std::string usage();

// A stop request any thread can wait on.
class StopSignal {
 public:
  void request();
  [[nodiscard]] bool requested() const { return requested_.load(); }
  // True if stop was requested before the deadline.
  bool wait_until(std::chrono::steady_clock::time_point deadline);
  bool wait_for(Millis d) { return wait_until(std::chrono::steady_clock::now() + d); }

 private:
  std::atomic<bool> requested_{false};
  std::mutex mutex_;
  std::condition_variable cv_;
};

// Samples every `period` until stop. A sample taken after its deadline is marked overrun
// and the schedule restarts from now (no burst of catch-up samples).
// `enrich` adds to each sample before it's queued (the device gateways).
void sampler_loop(Sampler& sampler, BoundedQueue<Sample>& out, StopSignal& stop, Millis period,
                  const std::function<void(Sample&)>& enrich = {});

// Until `samples` is closed and drained: open a run, turn samples into batches, deliver them.
// Then flushes for at most `flush_timeout` and ends the run with `final_status()`.
void uplink_loop(Uplink& uplink, BoundedQueue<Sample>& samples, Backoff& backoff,
                 Millis flush_timeout, const std::function<RunStatus()>& final_status);

// Runs the whole agent until SIGTERM/SIGINT (run ends ABORTED) or `duration` (SUCCESS).
// Blocks those signals in every thread and takes them with sigtimedwait in its own thread.
int run_agent(const AgentConfig& config, HttpClient& http);

}  // namespace openfleet::edge

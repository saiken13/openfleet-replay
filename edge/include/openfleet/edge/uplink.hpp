// Reliable delivery of batches to the OpenFleet API. The same contract as the Python
// collector (collector/openfleet_collector/uplink.py):
//
// - Batches go strictly in order from a bounded outbox. While the API is unreachable they
//   pile up (the oldest dropped beyond `buffer_max`) and are re-sent with the SAME seq; the
//   server deduplicates on UNIQUE(run_id, seq), so a retry is always safe.
// - Network errors and 5xx: keep the batch, retry later (the caller backs off).
// - 401/403: the key is wrong, not the data. Keep it buffered (bounded) until it's fixed.
// - 404 (robot or run unknown, e.g. a reset database) or 409 (the run is no longer RUNNING,
//   e.g. the sweeper abandoned it during a long outage): drop that run's batches and open
//   a fresh run; 404 also re-registers the robot.
// - Any other 4xx: the batch itself is invalid. Drop it; a retry could never succeed.
//
// Not thread-safe: one thread (the uplink loop) owns it. Samples reach that thread through
// a BoundedQueue.
#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "openfleet/edge/http.hpp"
#include "openfleet/edge/telemetry.hpp"

namespace openfleet::edge {

enum class RunStatus : std::uint8_t { kSuccess, kFailed, kAborted };
std::string_view to_string(RunStatus status);

struct UplinkConfig {
  std::string robot_id;
  std::string robot_name;  // defaults to robot_id
  std::optional<std::string> robot_model;
  std::optional<std::string> software_version;
  std::size_t buffer_max = 600;  // batches kept while offline (10 min at 1 Hz)
};

enum class PumpResult : std::uint8_t {
  kIdle,      // nothing to send
  kProgress,  // the head batch was resolved (delivered, dropped, or a fresh run opened)
  kRetry,     // could not make progress now; back off
};

struct UplinkStats {
  std::uint64_t sent = 0;
  std::uint64_t dropped = 0;          // overflow, rejected batches, or a dead run's batches
  std::uint64_t dropped_no_run = 0;   // samples taken while no run was open
  std::uint64_t runs_started = 0;
  std::size_t pending = 0;
  std::optional<double> latency_ms;   // last successful telemetry request
};

class Uplink {
 public:
  using Clock = std::function<SystemTime()>;
  using IdGenerator = std::function<std::string()>;

  Uplink(UplinkConfig config, HttpClient& http, Clock clock = std::chrono::system_clock::now,
         IdGenerator new_run_id = random_uuid);

  // One attempt to register (if needed) and open a run. True if a run is open afterwards.
  bool start();
  [[nodiscard]] bool has_run() const { return !run_id_.empty(); }
  [[nodiscard]] const std::string& run_id() const { return run_id_; }

  // Turns a sample into the next batch of the open run. With no run open it is dropped
  // (counted): there's nothing to attach it to. Adds the agent's own `edge_agent` node.
  void enqueue(Sample sample);

  // Tries to resolve the oldest batch.
  PumpResult pump();

  // One attempt to end the open run. True if the server accepted it.
  bool end(RunStatus status);

  [[nodiscard]] std::size_t pending() const { return outbox_.size(); }
  [[nodiscard]] UplinkStats stats() const;

  static std::string random_uuid();
  static constexpr const char* kSelfNode = "edge_agent";

 private:
  struct Pending {
    std::string run_id;
    std::int64_t seq;
    std::string body;
  };

  enum class Check : std::uint8_t { kOk, kRetry, kFatal };
  Check check(const std::optional<HttpResponse>& resp, std::string_view what);
  void drop_run(const std::string& run_id);
  void note_failure();
  void note_success();

  UplinkConfig config_;
  HttpClient& http_;
  Clock clock_;
  IdGenerator new_run_id_;

  std::deque<Pending> outbox_;
  std::string run_id_;
  std::int64_t next_seq_ = 0;
  bool registered_ = false;
  UplinkStats stats_;
  std::uint64_t dropped_at_last_batch_ = 0;
  std::optional<SystemTime> outage_since_;
  std::vector<LogEntry> notices_;  // agent's own log lines, sent with the next batch
};

}  // namespace openfleet::edge

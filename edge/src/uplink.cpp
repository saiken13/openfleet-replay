#include "openfleet/edge/uplink.hpp"

#include <array>
#include <format>
#include <random>

#include <nlohmann/json.hpp>

#include "openfleet/edge/log.hpp"

namespace openfleet::edge {

std::string_view to_string(RunStatus status) {
  switch (status) {
    case RunStatus::kSuccess: return "SUCCESS";
    case RunStatus::kFailed: return "FAILED";
    case RunStatus::kAborted: return "ABORTED";
  }
  return "ABORTED";
}

namespace {

// Lossy on purpose: a log message cut mid-character must not make dump() throw.
std::string dump(const nlohmann::json& j) {
  return j.dump(-1, ' ', false, nlohmann::json::error_handler_t::replace);
}

std::string snippet(const std::string& body) { return body.substr(0, 300); }

}  // namespace

std::string Uplink::random_uuid() {
  thread_local std::mt19937_64 rng{std::random_device{}()};
  std::array<std::uint8_t, 16> b{};
  for (std::size_t i = 0; i < b.size(); i += 8) {
    const auto r = rng();
    for (std::size_t j = 0; j < 8; ++j) {
      b[i + j] = static_cast<std::uint8_t>(r >> (8 * j));
    }
  }
  b[6] = static_cast<std::uint8_t>((b[6] & 0x0FU) | 0x40U);  // version 4
  b[8] = static_cast<std::uint8_t>((b[8] & 0x3FU) | 0x80U);  // RFC 4122 variant
  std::string out;
  for (std::size_t i = 0; i < b.size(); ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) {
      out += '-';
    }
    out += std::format("{:02x}", b[i]);
  }
  return out;
}

Uplink::Uplink(UplinkConfig config, HttpClient& http, Clock clock, IdGenerator new_run_id)
    : config_(std::move(config)),
      http_(http),
      clock_(std::move(clock)),
      new_run_id_(std::move(new_run_id)) {
  if (config_.robot_name.empty()) {
    config_.robot_name = config_.robot_id;
  }
  if (config_.buffer_max == 0) {
    config_.buffer_max = 1;
  }
}

Uplink::Check Uplink::check(const std::optional<HttpResponse>& resp, std::string_view what) {
  if (!resp) {
    log::warn(std::format("{}: cannot reach OpenFleet ({}); {} buffered", what,
                          http_.last_error(), outbox_.size()));
    return Check::kRetry;
  }
  if (resp->status < 300) {
    return Check::kOk;
  }
  if (resp->status >= 500) {
    log::warn(std::format("{}: server error {}; {} buffered", what, resp->status,
                          outbox_.size()));
    return Check::kRetry;
  }
  if (resp->status == 401 || resp->status == 403) {
    // Retried: an operator can issue or fix the key server-side without restarting us.
    log::error(std::format("{}: OpenFleet rejected the robot API key (HTTP {}: {}); {} buffered",
                           what, resp->status, snippet(resp->body), outbox_.size()));
    return Check::kRetry;
  }
  return Check::kFatal;
}

bool Uplink::start() {
  if (!registered_) {
    nlohmann::json body = {{"name", config_.robot_name}};
    if (config_.robot_model) {
      body["model"] = *config_.robot_model;
    }
    const auto resp = http_.send("PUT", "/api/v1/robots/" + config_.robot_id, dump(body));
    const auto c = check(resp, "register robot");
    if (c != Check::kOk) {
      if (c == Check::kFatal) {
        log::error(std::format("register robot rejected (HTTP {}): {}", resp->status,
                               snippet(resp->body)));
      }
      note_failure();
      return false;
    }
    registered_ = true;
  }
  const std::string id = new_run_id_();
  nlohmann::json body = {{"robot_id", config_.robot_id},
                         {"started_at", format_iso8601(clock_())}};
  if (config_.software_version) {
    body["software_version"] = *config_.software_version;
  }
  const auto resp = http_.send("PUT", "/api/v1/runs/" + id, dump(body));
  const auto c = check(resp, "start run");
  if (c != Check::kOk) {
    if (c == Check::kFatal) {
      log::error(
          std::format("start run rejected (HTTP {}): {}", resp->status, snippet(resp->body)));
      if (resp->status == 404) {
        registered_ = false;
      }
    }
    note_failure();
    return false;
  }
  run_id_ = id;
  next_seq_ = 0;
  ++stats_.runs_started;
  note_success();
  log::info(std::format("run {} started for {}", run_id_, config_.robot_id));
  return true;
}

void Uplink::enqueue(Sample sample) {
  if (!has_run()) {
    ++stats_.dropped_no_run;
    return;
  }
  // Our own health: DEGRADED when the sampler missed its period or batches were lost since
  // the last batch, so the operator sees the agent itself struggling.
  const bool lost = stats_.dropped > dropped_at_last_batch_;
  dropped_at_last_batch_ = stats_.dropped;
  sample.nodes.push_back(
      {kSelfNode, sample.overrun || lost ? NodeState::kDegraded : NodeState::kRunning});
  for (auto& n : notices_) {
    sample.logs.push_back(std::move(n));
  }
  notices_.clear();

  const auto seq = next_seq_++;
  outbox_.push_back({run_id_, seq, dump(batch_json(seq, sample, stats_.latency_ms))});
  while (outbox_.size() > config_.buffer_max) {
    outbox_.pop_front();
    ++stats_.dropped;
  }
}

PumpResult Uplink::pump() {
  if (outbox_.empty()) {
    return PumpResult::kIdle;
  }
  const Pending& head = outbox_.front();
  const auto resp =
      http_.send("POST", "/api/v1/runs/" + head.run_id + "/telemetry", head.body);
  const auto c = check(resp, std::format("telemetry seq {}", head.seq));
  if (c == Check::kOk) {
    stats_.latency_ms = resp->elapsed_s * 1000.0;
    outbox_.pop_front();
    ++stats_.sent;
    note_success();
    return PumpResult::kProgress;
  }
  if (c == Check::kRetry) {
    note_failure();
    return PumpResult::kRetry;
  }
  if (resp->status == 404 || resp->status == 409) {
    const std::string dead = head.run_id;  // head is gone after drop_run
    const auto before = outbox_.size();
    log::warn(std::format("run {} rejected ({}: {}); starting a new run", dead, resp->status,
                          snippet(resp->body)));
    if (resp->status == 404) {
      registered_ = false;
    }
    drop_run(dead);
    notices_.push_back({clock_(), LogLevel::kWarn, kSelfNode,
                        std::format("run {} was rejected (HTTP {}); {} buffered batches of it "
                                    "were dropped and a new run started",
                                    dead, resp->status, before - outbox_.size())});
    return start() ? PumpResult::kProgress : PumpResult::kRetry;
  }
  log::error(std::format("batch seq {} rejected (HTTP {}): {}; dropping it", head.seq,
                         resp->status, snippet(resp->body)));
  outbox_.pop_front();
  ++stats_.dropped;
  return PumpResult::kProgress;
}

bool Uplink::end(RunStatus status) {
  if (!has_run()) {
    return false;
  }
  const nlohmann::json body = {{"status", to_string(status)},
                               {"ended_at", format_iso8601(clock_())}};
  const auto resp = http_.send("POST", "/api/v1/runs/" + run_id_ + "/end", dump(body));
  if (check(resp, "end run") != Check::kOk) {
    if (resp) {
      log::warn(std::format("end run {}: HTTP {}: {}", run_id_, resp->status,
                            snippet(resp->body)));
    }
    return false;
  }
  log::info(std::format("run {} ended {}", run_id_, to_string(status)));
  return true;
}

UplinkStats Uplink::stats() const {
  auto s = stats_;
  s.pending = outbox_.size();
  return s;
}

void Uplink::drop_run(const std::string& run_id) {
  const auto before = outbox_.size();
  std::erase_if(outbox_, [&](const Pending& p) { return p.run_id == run_id; });
  stats_.dropped += before - outbox_.size();
  if (run_id_ == run_id) {
    run_id_.clear();
  }
}

void Uplink::note_failure() {
  stats_.latency_ms.reset();  // no stale latency on batches built during an outage
  if (!outage_since_) {
    outage_since_ = clock_();
  }
}

void Uplink::note_success() {
  if (!outage_since_) {
    return;
  }
  const auto secs = std::chrono::duration<double>(clock_() - *outage_since_).count();
  outage_since_.reset();
  if (secs >= 1.0) {
    notices_.push_back({clock_(), LogLevel::kWarn, kSelfNode,
                        std::format("OpenFleet API was unreachable for {:.0f} s; {} buffered "
                                    "batches are being delivered",
                                    secs, outbox_.size())});
  }
}

}  // namespace openfleet::edge

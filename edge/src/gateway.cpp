#include "openfleet/edge/gateway.hpp"

#include <format>

namespace openfleet::edge {

namespace {

std::string_view reset_reason(std::uint8_t r) {
  switch (r) {
    case OFP_RESET_POWER_ON: return "power-on";
    case OFP_RESET_WATCHDOG: return "watchdog";
    case OFP_RESET_SOFTWARE: return "software";
    case OFP_RESET_BROWNOUT: return "brownout";
    case OFP_RESET_PIN: return "reset pin";
    default: return "unknown";
  }
}

NodeState task_state(std::uint8_t s) {
  switch (s) {
    case OFP_TASK_RUNNING: return NodeState::kRunning;
    case OFP_TASK_DEGRADED: return NodeState::kDegraded;
    case OFP_TASK_STOPPED: return NodeState::kStopped;
    default: return NodeState::kError;
  }
}

LogLevel log_level(std::uint8_t l) {
  switch (l) {
    case OFP_LOG_DEBUG: return LogLevel::kDebug;
    case OFP_LOG_INFO: return LogLevel::kInfo;
    case OFP_LOG_WARN: return LogLevel::kWarn;
    case OFP_LOG_ERROR: return LogLevel::kError;
    default: return LogLevel::kFatal;
  }
}

double seconds(Gateway::Clock::duration d) { return std::chrono::duration<double>(d).count(); }

}  // namespace

Gateway::Gateway(GatewayConfig config) : config_(std::move(config)) {
  ofp_decoder_init(&decoder_);
}

std::string Gateway::device_name(const Device& d) const { return std::format("mcu{}", d.id); }

void Gateway::add_log(LogLevel level, std::string source, std::string message, SystemTime wall) {
  if (logs_.size() >= config_.max_logs_per_window) {
    ++stats_.logs_dropped;
    return;
  }
  logs_.push_back({wall, level, std::move(source), std::move(message)});
}

void Gateway::on_bytes(std::span<const std::uint8_t> data, Clock::time_point now,
                       SystemTime wall) {
  const std::lock_guard lock(mutex_);
  if (!window_start_) {
    window_start_ = now;
  }
  stats_.bytes += data.size();
  ofp_frame frame;
  for (const std::uint8_t b : data) {
    const ofp_result r = ofp_decoder_push(&decoder_, b, &frame);
    if (r == OFP_FRAME) {
      handle(frame, now, wall);
    } else if (r != OFP_NONE) {
      count_error(r);
    }
  }
}

void Gateway::on_link(bool up, const std::string& detail, SystemTime wall) {
  const std::lock_guard lock(mutex_);
  ofp_decoder_init(&decoder_);  // a partial frame from the old connection means nothing now
  add_log(up ? LogLevel::kInfo : LogLevel::kWarn, "edge_agent",
          std::format("device link {} {}{}", config_.link, up ? "connected" : "lost",
                      detail.empty() ? "" : ": " + detail),
          wall);
}

void Gateway::count_error(ofp_result r) {
  ++window_bad_;
  switch (r) {
    case OFP_ERR_OVERFLOW: ++stats_.overflow; break;
    case OFP_ERR_COBS: ++stats_.cobs; break;
    case OFP_ERR_SHORT: ++stats_.short_frames; break;
    case OFP_ERR_CRC: ++stats_.crc; break;
    case OFP_ERR_VERSION: ++stats_.version; break;
    case OFP_ERR_LENGTH: ++stats_.length; break;
    default: break;
  }
}

Gateway::SeqVerdict Gateway::check_seq(Device& d, std::uint16_t seq) {
  if (!d.have_seq) {
    d.have_seq = true;
    d.top_seq = seq;
    d.seen_bits = 1;
    return SeqVerdict::kNew;
  }
  const auto ahead = static_cast<std::uint16_t>(seq - d.top_seq);
  if (ahead == 0) {
    return SeqVerdict::kDuplicate;
  }
  if (ahead < 0x8000U) {  // newer (mod 2^16)
    stats_.lost += ahead - 1U;
    d.seen_bits = ahead >= kDuplicateWindow ? 1U : ((d.seen_bits << ahead) | 1U);
    d.top_seq = seq;
    return SeqVerdict::kNew;
  }
  const auto behind = static_cast<std::uint16_t>(d.top_seq - seq);
  if (behind >= kDuplicateWindow) {
    return SeqVerdict::kStale;
  }
  const std::uint64_t bit = std::uint64_t{1} << behind;
  if ((d.seen_bits & bit) != 0) {
    return SeqVerdict::kDuplicate;
  }
  d.seen_bits |= bit;  // late but new: it was counted as lost when the gap opened
  if (stats_.lost > 0) {
    --stats_.lost;
  }
  return SeqVerdict::kNew;
}

void Gateway::reset(Device& d, const std::string& why, LogLevel level, Clock::time_point now,
                    SystemTime wall) {
  ++stats_.resets;
  if (level >= LogLevel::kWarn) {  // unexpected: hold the device DEGRADED for a while
    d.degraded_until = now + config_.reset_hold;
  }
  d.have_seq = false;
  d.last_health.reset();
  for (auto& [_, t] : d.tasks) {
    t.low_stack_logged = false;
  }
  add_log(level, device_name(d), why, wall);
}

void Gateway::handle(const ofp_frame& f, Clock::time_point now, SystemTime wall) {
  ++stats_.frames;
  const ofp_header& h = f.header;
  const bool known = devices_.contains(h.device_id);
  Device& d = devices_[h.device_id];
  d.id = h.device_id;
  const std::string name = device_name(d);

  // Same boot as before? A new boot id, or an uptime that went backwards, means the device
  // restarted even if its HELLO was lost.
  const bool new_boot = known && h.boot_id != d.boot_id;
  const bool uptime_back = known && !new_boot && h.uptime_ms + 1000U < d.uptime_ms;
  if (known && !new_boot && !uptime_back) {
    const SeqVerdict v = check_seq(d, h.seq);
    if (v == SeqVerdict::kDuplicate) {
      ++stats_.duplicates;
      return;
    }
    if (v == SeqVerdict::kStale) {
      ++stats_.stale;
      return;
    }
  }

  ofp_hello hello;
  const bool is_hello = h.type == OFP_HELLO && ofp_parse_hello(&f, &hello);
  const double was_up = d.uptime_ms / 1000.0;
  if (is_hello) {
    const auto reason = reset_reason(hello.reset_reason);
    const bool alarming = hello.reset_reason == OFP_RESET_WATCHDOG ||
                          hello.reset_reason == OFP_RESET_BROWNOUT ||
                          hello.reset_reason == OFP_RESET_UNKNOWN;
    if (known) {
      reset(d,
            std::format("{} reset ({}); firmware {}; boot {} after {:.1f} s up", name, reason,
                        hello.firmware, h.boot_id, was_up),
            alarming ? LogLevel::kWarn : LogLevel::kInfo, now, wall);
    } else {
      add_log(alarming ? LogLevel::kWarn : LogLevel::kInfo, name,
              std::format("{} connected on {}: firmware {}, last reset {}, boot {}", name,
                          config_.link, hello.firmware, reason, h.boot_id),
              wall);
    }
  } else if (new_boot) {
    reset(d,
          std::format("{} reset detected (boot {} -> {}, its HELLO was lost) after {:.1f} s up",
                      name, d.boot_id, h.boot_id, was_up),
          LogLevel::kWarn, now, wall);
  } else if (uptime_back) {
    reset(d,
          std::format("{} reset detected (uptime went back from {:.1f} s to {:.1f} s)", name,
                      was_up, h.uptime_ms / 1000.0),
          LogLevel::kWarn, now, wall);
  } else if (!known) {
    add_log(LogLevel::kInfo, name,
            std::format("{} first frame on {} (boot {}, up {:.1f} s)", name, config_.link,
                        h.boot_id, h.uptime_ms / 1000.0),
            wall);
  }
  if (!known || new_boot || uptime_back || (is_hello && known)) {
    check_seq(d, h.seq);  // starts the new boot's window
  }
  d.boot_id = h.boot_id;
  d.uptime_ms = h.uptime_ms;
  d.last_frame = now;
  ++window_frames_;

  switch (h.type) {
    case OFP_HELLO:
      if (!is_hello) {
        ++stats_.bad_payload;
        ++window_bad_;
      }
      break;
    case OFP_SENSOR: {
      ofp_sensor s;
      if (!ofp_parse_sensor(&f, &s)) {
        ++stats_.bad_payload;
        ++window_bad_;
        break;
      }
      Sensor& sensor = d.sensors[s.sensor_id];
      if (sensor.name.empty()) {
        sensor.name = std::format("sensor{}", s.sensor_id);
      }
      if (!sensor.first_seen) {
        sensor.first_seen = now;
      }
      ++sensor.count;
      sensor.last = now;
      break;
    }
    case OFP_DESCRIBE: {
      ofp_describe desc;
      if (!ofp_parse_describe(&f, &desc)) {
        ++stats_.bad_payload;
        ++window_bad_;
        break;
      }
      if (desc.kind == OFP_KIND_SENSOR) {
        Sensor& sensor = d.sensors[desc.id];
        sensor.name = desc.name;
        sensor.described = true;
        sensor.expected_hz = desc.expected_dhz > 0
                                 ? std::optional<double>(desc.expected_dhz / 10.0)
                                 : std::nullopt;
      } else {
        d.tasks[desc.id].name = desc.name;
        d.tasks[desc.id].described = true;
      }
      break;
    }
    case OFP_HEALTH: {
      ofp_health health;
      if (!ofp_parse_health(&f, &health)) {
        ++stats_.bad_payload;
        ++window_bad_;
        break;
      }
      d.last_health = now;
      for (std::size_t i = 0; i < health.count; ++i) {
        const ofp_task_health& th = health.tasks[i];
        Task& t = d.tasks[th.task_id];
        if (t.name.empty()) {
          t.name = std::format("task{}", th.task_id);
        }
        if (!t.first_seen) {
          t.first_seen = now;
        }
        t.state = task_state(th.state);
        if (th.stack_free_words < config_.low_stack_words && !t.low_stack_logged) {
          t.low_stack_logged = true;
          add_log(LogLevel::kWarn, name,
                  std::format("{}/{} stack headroom down to {} words", name, t.name,
                              th.stack_free_words),
                  wall);
        }
      }
      break;
    }
    case OFP_LOG: {
      ofp_log l;
      if (!ofp_parse_log(&f, &l)) {
        ++stats_.bad_payload;
        ++window_bad_;
        break;
      }
      add_log(log_level(l.level), name, l.text, wall);
      break;
    }
    default:
      ++stats_.unknown_type;  // a newer device: not an error on our side
      break;
  }
}

void Gateway::contribute(Sample& sample, Clock::time_point now) {
  const std::lock_guard lock(mutex_);
  const double elapsed = window_start_ ? seconds(now - *window_start_) : 0.0;
  const double total = static_cast<double>(window_frames_ + window_bad_);
  const bool bad_link =
      total > 0 && static_cast<double>(window_bad_) / total >= config_.degraded_error_share;

  for (auto& [id, d] : devices_) {
    const std::string name = device_name(d);
    const bool silent = now - d.last_frame > config_.silence_timeout;
    const bool health_stale =
        !d.tasks.empty() && (!d.last_health || now - *d.last_health > config_.silence_timeout);
    NodeState state = NodeState::kRunning;
    if (silent) {
      state = NodeState::kStopped;
    } else if (bad_link || health_stale || (d.degraded_until && now < *d.degraded_until)) {
      state = NodeState::kDegraded;
    }
    sample.nodes.push_back({name, state});
    if (!silent) {  // a silent device's task states are unknown, not "still running"
      for (const auto& [tid, t] : d.tasks) {
        if (!t.described && (!t.first_seen || now - *t.first_seen < config_.describe_grace)) {
          continue;  // its real name is probably on the way
        }
        sample.nodes.push_back({name + "/" + t.name, t.state});
      }
    }
    for (auto& [sid, s] : d.sensors) {
      if (!s.described && (!s.first_seen || now - *s.first_seen < config_.describe_grace)) {
        s.count = 0;
        continue;
      }
      TopicReport t;
      t.topic = "/" + name + "/" + s.name;
      t.expected_hz = s.expected_hz;
      t.actual_hz = elapsed > 0 ? static_cast<double>(s.count) / elapsed : 0.0;
      if (s.last) {
        t.last_msg_age_s = seconds(now - *s.last);
      }
      sample.topics.push_back(std::move(t));
      s.count = 0;
    }
  }

  // Link errors: one summary at most every error_log_interval.
  const std::uint64_t new_bad = stats_.bad() - reported_.bad();
  if (new_bad > 0 && (!last_error_log_ || now - *last_error_log_ >= config_.error_log_interval)) {
    const auto delta = [this](std::uint64_t LinkStats::*field) {
      return stats_.*field - reported_.*field;
    };
    add_log(LogLevel::kWarn, "edge_agent",
            std::format("device link {}: {} bad frames (crc {}, short {}, cobs {}, overflow {}, "
                        "version {}, length {}, payload {}); {} lost, {} duplicates; {} good",
                        config_.link, new_bad, delta(&LinkStats::crc),
                        delta(&LinkStats::short_frames), delta(&LinkStats::cobs),
                        delta(&LinkStats::overflow), delta(&LinkStats::version),
                        delta(&LinkStats::length), delta(&LinkStats::bad_payload),
                        delta(&LinkStats::lost), delta(&LinkStats::duplicates),
                        delta(&LinkStats::frames)),
            std::chrono::system_clock::now());
    reported_ = stats_;
    last_error_log_ = now;
  }

  for (auto& l : logs_) {
    sample.logs.push_back(std::move(l));
  }
  logs_.clear();
  window_frames_ = 0;
  window_bad_ = 0;
  window_start_ = now;
}

LinkStats Gateway::stats() const {
  const std::lock_guard lock(mutex_);
  return stats_;
}

std::size_t Gateway::devices() const {
  const std::lock_guard lock(mutex_);
  return devices_.size();
}

}  // namespace openfleet::edge

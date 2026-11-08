#include "openfleet/edge/device_sim.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <format>
#include <cstdio>

namespace openfleet::edge {

namespace {

using Clock = DeviceSim::Clock;

Clock::duration period_of(double hz) {
  return std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(1.0 / hz));
}

std::vector<std::string_view> words(std::string_view line) {
  std::vector<std::string_view> out;
  std::size_t i = 0;
  while (i < line.size()) {
    while (i < line.size() && (line[i] == ' ' || line[i] == '\t' || line[i] == '\r')) {
      ++i;
    }
    const std::size_t start = i;
    while (i < line.size() && line[i] != ' ' && line[i] != '\t' && line[i] != '\r') {
      ++i;
    }
    if (i > start) {
      out.push_back(line.substr(start, i - start));
    }
  }
  return out;
}

template <typename T>
std::optional<T> number(std::string_view s) {
  T v{};
  const auto [p, ec] = std::from_chars(s.data(), s.data() + s.size(), v);
  if (ec != std::errc{} || p != s.data() + s.size()) {
    return std::nullopt;
  }
  return v;
}

std::optional<double> probability(std::string_view s) {
  const auto v = number<double>(s);
  return v && *v >= 0 && *v <= 1 ? v : std::nullopt;
}

}  // namespace

DeviceSim::DeviceSim(SimConfig config, Clock::time_point now)
    : config_(std::move(config)), rng_(config_.seed), boot_time_(now) {
  next_sample_.assign(config_.sensors.size(), now);
  next_health_ = now;
  next_describe_ = now;
  // The power-on HELLO goes out on the first tick, to whoever is listening then.
}

bool DeviceSim::chance(double p) {
  return p > 0 && std::uniform_real_distribution<double>(0, 1)(rng_) < p;
}

void DeviceSim::emit(std::span<const std::uint8_t> frame, const Sink& sink) {
  if (!faults_.fragment) {
    sink(frame);
    return;
  }
  std::size_t i = 0;
  while (i < frame.size()) {
    const std::size_t n = std::min<std::size_t>(frame.size() - i, 1 + rng_() % 7);
    sink(frame.subspan(i, n));
    i += n;
  }
}

void DeviceSim::send(std::uint8_t type, const std::uint8_t* payload, std::size_t len,
                     Clock::time_point now, const Sink& sink) {
  ofp_header h{};
  h.type = type;
  h.device_id = config_.device_id;
  h.seq = seq_++;
  h.boot_id = config_.boot_id;
  h.uptime_ms = static_cast<std::uint32_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(now - boot_time_).count());
  h.length = static_cast<std::uint16_t>(len);
  std::uint8_t wire[OFP_MAX_WIRE];
  const std::size_t n = ofp_encode(&h, payload, wire, sizeof wire);
  if (n == 0) {
    return;
  }
  ++frames_sent_;
  if (chance(faults_.drop)) {
    return;
  }
  std::vector<std::uint8_t> frame(wire, wire + n);
  if (chance(faults_.corrupt)) {
    const std::size_t byte = rng_() % (n - 1);  // never the delimiter itself
    frame[byte] = static_cast<std::uint8_t>(frame[byte] ^ (1U << (rng_() % 8)));
  }
  if (chance(faults_.truncate) && n > 2) {
    const std::size_t keep = 1 + rng_() % (n - 2);
    frame.resize(keep);
    frame.push_back(0);
  }
  emit(frame, sink);
  if (chance(faults_.duplicate)) {
    emit(frame, sink);
  }
}

void DeviceSim::describe_all(Clock::time_point now, const Sink& sink) {
  std::uint8_t p[OFP_MAX_PAYLOAD];
  for (const auto& s : config_.sensors) {
    ofp_describe d{};
    d.kind = OFP_KIND_SENSOR;
    d.id = s.id;
    d.expected_dhz = static_cast<std::uint16_t>(std::lround(s.hz * 10));
    std::snprintf(d.name, sizeof d.name, "%s", s.name.c_str());
    send(OFP_DESCRIBE, p, ofp_build_describe(p, sizeof p, &d), now, sink);
  }
  for (const auto& t : config_.tasks) {
    ofp_describe d{};
    d.kind = OFP_KIND_TASK;
    d.id = t.id;
    std::snprintf(d.name, sizeof d.name, "%s", t.name.c_str());
    send(OFP_DESCRIBE, p, ofp_build_describe(p, sizeof p, &d), now, sink);
  }
}

void DeviceSim::boot(Clock::time_point now, std::uint8_t reason, bool send_hello,
                     const Sink& sink) {
  boot_time_ = now;
  seq_ = 0;
  ++config_.boot_id;
  std::fill(next_sample_.begin(), next_sample_.end(), now);
  next_health_ = now;
  next_describe_ = now;
  silent_until_.reset();
  if (send_hello) {
    std::uint8_t p[OFP_MAX_PAYLOAD];
    send(OFP_HELLO, p, ofp_build_hello(p, sizeof p, reason, config_.firmware.c_str()), now, sink);
  } else {
    ++seq_;  // the HELLO was "sent" and lost on the line
  }
}

void DeviceSim::tick(Clock::time_point now, const Sink& sink) {
  if (!powered_on_) {
    powered_on_ = true;
    std::uint8_t p[OFP_MAX_PAYLOAD];
    send(OFP_HELLO, p, ofp_build_hello(p, sizeof p, OFP_RESET_POWER_ON, config_.firmware.c_str()),
         now, sink);
  }
  if (silent_until_) {
    if (now < *silent_until_) {
      return;
    }
    silent_until_.reset();
    // Readings that fell due during the silence are simply not taken (the device was hung).
    std::fill(next_sample_.begin(), next_sample_.end(), now);
    next_health_ = now;
  }
  if (now >= next_describe_) {
    describe_all(now, sink);
    next_describe_ = now + config_.describe_period;
  }
  std::uint8_t p[OFP_MAX_PAYLOAD];
  for (std::size_t i = 0; i < config_.sensors.size(); ++i) {
    const auto& s = config_.sensors[i];
    const auto step = period_of(s.hz);
    if (!s.running) {
      next_sample_[i] = now + step;
      continue;
    }
    while (next_sample_[i] <= now) {
      reading_ = static_cast<std::int32_t>((static_cast<std::uint32_t>(reading_) + 1U) & 0x7FFFFFFFU);
      send(OFP_SENSOR, p, ofp_build_sensor(p, sizeof p, s.id, reading_), now, sink);
      next_sample_[i] += step;
    }
  }
  if (now >= next_health_) {
    ofp_health h{};
    for (const auto& t : config_.tasks) {
      if (h.count == OFP_MAX_HEALTH) {
        break;
      }
      h.tasks[h.count++] = {t.id, t.state, t.stack_free_words};
    }
    send(OFP_HEALTH, p, ofp_build_health(p, sizeof p, &h), now, sink);
    next_health_ = now + config_.health_period;
  }
}

std::optional<std::string> DeviceSim::command(std::string_view line, Clock::time_point now,
                                              const Sink& sink) {
  const auto w = words(line);
  if (w.empty()) {
    return std::nullopt;
  }
  const auto cmd = w[0];
  auto sensor = [&](std::string_view id) -> SimSensor* {
    const auto n = number<unsigned>(id);
    for (auto& s : config_.sensors) {
      if (n && s.id == *n) {
        return &s;
      }
    }
    return nullptr;
  };
  auto task = [&](std::string_view id) -> SimTask* {
    const auto n = number<unsigned>(id);
    for (auto& t : config_.tasks) {
      if (n && t.id == *n) {
        return &t;
      }
    }
    return nullptr;
  };
  auto prob = [&](double& field) -> std::optional<std::string> {
    const auto p = w.size() == 2 ? probability(w[1]) : std::nullopt;
    if (!p) {
      return std::format("{} needs a probability in [0, 1]", cmd);
    }
    field = *p;
    return std::nullopt;
  };

  if ((cmd == "stop-sensor" || cmd == "start-sensor") && w.size() == 2) {
    SimSensor* s = sensor(w[1]);
    if (s == nullptr) {
      return std::format("no sensor {}", w[1]);
    }
    s->running = cmd == "start-sensor";
    return std::nullopt;
  }
  if (cmd == "task" && w.size() == 3) {
    SimTask* t = task(w[1]);
    static const std::vector<std::string_view> kStates = {"running", "degraded", "stopped",
                                                          "error"};
    const auto it = std::find(kStates.begin(), kStates.end(), w[2]);
    if (t == nullptr || it == kStates.end()) {
      return std::string("usage: task ID running|degraded|stopped|error");
    }
    t->state = static_cast<std::uint8_t>(it - kStates.begin());
    return std::nullopt;
  }
  if (cmd == "stack" && w.size() == 3) {
    SimTask* t = task(w[1]);
    const auto words_free = number<std::uint16_t>(w[2]);
    if (t == nullptr || !words_free) {
      return std::string("usage: stack ID WORDS");
    }
    t->stack_free_words = *words_free;
    return std::nullopt;
  }
  if (cmd == "reset") {
    static const std::vector<std::pair<std::string_view, std::uint8_t>> kReasons = {
        {"power", OFP_RESET_POWER_ON},   {"watchdog", OFP_RESET_WATCHDOG},
        {"software", OFP_RESET_SOFTWARE}, {"brownout", OFP_RESET_BROWNOUT},
        {"pin", OFP_RESET_PIN}};
    std::uint8_t reason = OFP_RESET_WATCHDOG;
    if (w.size() == 2) {
      const auto it = std::find_if(kReasons.begin(), kReasons.end(),
                                   [&](const auto& r) { return r.first == w[1]; });
      if (it == kReasons.end()) {
        return std::string("usage: reset [power|watchdog|software|brownout|pin]");
      }
      reason = it->second;
    }
    boot(now, reason, true, sink);
    return std::nullopt;
  }
  if (cmd == "reset-quiet") {
    boot(now, OFP_RESET_WATCHDOG, false, sink);
    return std::nullopt;
  }
  if (cmd == "silence" && w.size() == 2) {
    const auto secs = number<double>(w[1]);
    if (!secs || *secs < 0 || *secs > 3600) {
      return std::string("usage: silence SECONDS");
    }
    silent_until_ = now + std::chrono::duration_cast<Clock::duration>(
                              std::chrono::duration<double>(*secs));
    return std::nullopt;
  }
  if (cmd == "corrupt") return prob(faults_.corrupt);
  if (cmd == "truncate") return prob(faults_.truncate);
  if (cmd == "drop") return prob(faults_.drop);
  if (cmd == "duplicate") return prob(faults_.duplicate);
  if (cmd == "fragment" && w.size() == 2 && (w[1] == "on" || w[1] == "off")) {
    faults_.fragment = w[1] == "on";
    return std::nullopt;
  }
  if (cmd == "faults" && w.size() == 2 && w[1] == "off") {
    faults_ = {};
    return std::nullopt;
  }
  if (cmd == "garbage" && w.size() == 2) {
    const auto n = number<unsigned>(w[1]);
    if (!n || *n > 100'000) {
      return std::string("usage: garbage N (N <= 100000)");
    }
    std::vector<std::uint8_t> junk(*n);
    for (auto& b : junk) {
      b = static_cast<std::uint8_t>(rng_());
    }
    sink(junk);
    return std::nullopt;
  }
  if (cmd == "oversize") {
    std::vector<std::uint8_t> big(OFP_MAX_WIRE * 2, 0x5A);  // no delimiter for too long
    big.push_back(0);
    sink(big);
    return std::nullopt;
  }
  if (cmd == "log" && w.size() >= 3) {
    static const std::vector<std::string_view> kLevels = {"debug", "info", "warn", "error",
                                                          "fatal"};
    const auto it = std::find(kLevels.begin(), kLevels.end(), w[1]);
    if (it == kLevels.end()) {
      return std::string("usage: log LEVEL TEXT");
    }
    const auto start = static_cast<std::size_t>(w[2].data() - line.data());
    const std::string text(line.substr(start));
    std::uint8_t p[OFP_MAX_PAYLOAD];
    const std::size_t len =
        ofp_build_log(p, sizeof p, static_cast<std::uint8_t>(it - kLevels.begin()), text.c_str());
    if (len == 0) {
      return std::string("log text too long");
    }
    send(OFP_LOG, p, len, now, sink);
    return std::nullopt;
  }
  return std::format("unknown command '{}'; commands: {}", line, kCommands);
}

}  // namespace openfleet::edge

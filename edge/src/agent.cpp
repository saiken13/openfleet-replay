#include "openfleet/edge/agent.hpp"

#include <csignal>
#include <charconv>
#include <cmath>
#include <ctime>
#include <format>
#include <memory>
#include <regex>
#include <thread>

#include "openfleet/edge/log.hpp"

#ifndef OPENFLEET_EDGE_VERSION
#define OPENFLEET_EDGE_VERSION "0.0.0"
#endif

namespace openfleet::edge {

using SteadyClock = std::chrono::steady_clock;

// --- configuration -----------------------------------------------------------------------

std::string usage() {
  return R"(usage: openfleet-edge-agent --robot-id ID [options]

Samples this Linux machine (/proc) and streams it to OpenFleet as robot ID.
The robot API key comes from the OPENFLEET_API_KEY environment variable
(issue one with `openfleet keys create ID`).

  --api URL               OpenFleet API base URL (default: $OPENFLEET_API_URL or
                          http://localhost:8000)
  --robot-id ID           robot id (required)
  --name NAME             display name (default: the id)
  --model MODEL           robot model
  --software-version V    software version recorded on the run
  --process NAME          watch a process by name; repeatable. Reported as a node:
                          RUNNING / DEGRADED (stopped by a signal) / ERROR (zombie) /
                          STOPPED (not found)
  --period S              sampling period, seconds (default 1)
  --startup-grace S       don't report watched processes missing for S seconds (default 5)
  --buffer-max N          batches kept while the API is unreachable (default 600)
  --timeout S             HTTP request timeout, seconds (default 5)
  --backoff-max S         longest retry delay, seconds (default 10)
  --duration S            stop after S seconds and end the run SUCCESS (default: run
                          until SIGTERM/SIGINT, which end it ABORTED)
  --proc-root PATH        where /proc is (default /proc)
  --device URI            a microcontroller speaking the OpenFleet device protocol
                          (docs/PROTOCOL.md); repeatable. tcp://HOST:PORT or
                          serial:/dev/PATH[@BAUD]. Sensors become topics /mcu<id>/<name>,
                          tasks become nodes mcu<id>/<task>, resets become log events.
  --device-silence S      a device silent this long is reported STOPPED (default 3)
  --version, --help

On SIGTERM/SIGINT it flushes buffered batches (at most 5 s) and ends its run ABORTED.
)";
}

namespace {

std::optional<double> parse_double(std::string_view s) {
  double v{};
  const auto* end = s.data() + s.size();
  const auto [ptr, ec] = std::from_chars(s.data(), end, v);
  if (ec != std::errc{} || ptr != end || !std::isfinite(v)) {
    return std::nullopt;
  }
  return v;
}

std::optional<Millis> parse_seconds(std::string_view s, double min, double max) {
  const auto v = parse_double(s);
  if (!v || *v < min || *v > max) {
    return std::nullopt;
  }
  return Millis(static_cast<Millis::rep>(std::llround(*v * 1000.0)));
}

ParseResult fail(std::string message) {
  return {std::nullopt, std::move(message) + "\n(see --help)", 2};
}

}  // namespace

ParseResult parse_args(const std::vector<std::string_view>& args, const GetEnv& getenv) {
  AgentConfig cfg;
  if (const char* url = getenv("OPENFLEET_API_URL"); url != nullptr && *url != '\0') {
    cfg.api_url = url;
  }
  for (std::size_t i = 0; i < args.size(); ++i) {
    std::string_view flag = args[i];
    if (flag == "--help" || flag == "-h") {
      return {std::nullopt, usage(), 0};
    }
    if (flag == "--version") {
      return {std::nullopt, std::string("openfleet-edge-agent ") + OPENFLEET_EDGE_VERSION, 0};
    }
    std::string_view value;
    if (const auto eq = flag.find('='); flag.starts_with("--") && eq != std::string_view::npos) {
      value = flag.substr(eq + 1);  // --flag=value
      flag = flag.substr(0, eq);
    } else if (i + 1 < args.size()) {
      value = args[++i];  // --flag value
    } else {
      return fail(std::format("error: {} needs a value", flag));
    }
    auto seconds = [&](double min, double max, Millis& out) -> std::optional<ParseResult> {
      const auto v = parse_seconds(value, min, max);
      if (!v) {
        return fail(std::format("error: {} must be a number of seconds in [{}, {}], got '{}'",
                                flag, min, max, value));
      }
      out = *v;
      return std::nullopt;
    };
    std::optional<ParseResult> err;
    if (flag == "--api") {
      cfg.api_url = value;
    } else if (flag == "--robot-id") {
      cfg.uplink.robot_id = value;
    } else if (flag == "--name") {
      cfg.uplink.robot_name = value;
    } else if (flag == "--model") {
      cfg.uplink.robot_model = std::string(value);
    } else if (flag == "--software-version") {
      cfg.uplink.software_version = std::string(value);
    } else if (flag == "--process") {
      if (value.empty() || value.size() > kMaxNameChars) {
        return fail("error: --process needs a name of 1-256 characters");
      }
      cfg.sampler.processes.emplace_back(value);
    } else if (flag == "--period") {
      err = seconds(0.1, 10, cfg.period);
    } else if (flag == "--startup-grace") {
      err = seconds(0, 3600, cfg.sampler.startup_grace);
    } else if (flag == "--timeout") {
      err = seconds(0.1, 60, cfg.request_timeout);
    } else if (flag == "--backoff-max") {
      err = seconds(0.5, 300, cfg.backoff_max);
    } else if (flag == "--duration") {
      err = seconds(0, 1e7, cfg.duration);
    } else if (flag == "--device") {
      std::string error;
      auto ep = parse_device_uri(value, &error);
      if (!ep) {
        return fail("error: " + error);
      }
      cfg.devices.push_back(std::move(*ep));
    } else if (flag == "--device-silence") {
      err = seconds(0.5, 600, cfg.device_silence);
    } else if (flag == "--proc-root") {
      cfg.sampler.proc_root = std::string(value);
    } else if (flag == "--buffer-max") {
      const auto v = parse_double(value);
      if (!v || *v < 10 || *v > 1e6 || *v != std::floor(*v)) {
        return fail("error: --buffer-max must be an integer in [10, 1000000]");
      }
      cfg.uplink.buffer_max = static_cast<std::size_t>(*v);
    } else {
      return fail(std::format("error: unknown option {}", flag));
    }
    if (err) {
      return *err;
    }
  }

  // Same id rule as the API (app/schemas/robot.py), checked here to fail fast.
  static const std::regex kRobotId("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$");
  if (cfg.uplink.robot_id.empty()) {
    return fail("error: --robot-id is required");
  }
  if (!std::regex_match(cfg.uplink.robot_id, kRobotId)) {
    return fail("error: --robot-id must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$");
  }
  if (cfg.api_url.rfind("http://", 0) != 0 && cfg.api_url.rfind("https://", 0) != 0) {
    return fail("error: --api must start with http:// or https://");
  }
  while (cfg.api_url.ends_with('/')) {
    cfg.api_url.pop_back();
  }
  const char* key = getenv("OPENFLEET_API_KEY");
  if (key == nullptr || *key == '\0') {
    return fail(std::format("error: no robot API key. Set OPENFLEET_API_KEY; issue one with "
                            "`openfleet keys create {}`.",
                            cfg.uplink.robot_id));
  }
  cfg.api_key = key;
  if (cfg.backoff_max < cfg.backoff_initial) {
    cfg.backoff_max = cfg.backoff_initial;
  }
  return {std::move(cfg), {}, 0};
}

// --- stop signal ---------------------------------------------------------------------------

void StopSignal::request() {
  {
    const std::lock_guard lock(mutex_);
    requested_.store(true);
  }
  cv_.notify_all();
}

bool StopSignal::wait_until(SteadyClock::time_point deadline) {
  std::unique_lock lock(mutex_);
  return cv_.wait_until(lock, deadline, [this] { return requested_.load(); });
}

// --- threads -------------------------------------------------------------------------------

void sampler_loop(Sampler& sampler, BoundedQueue<Sample>& out, StopSignal& stop, Millis period,
                  const std::function<void(Sample&)>& enrich) {
  auto next = SteadyClock::now();
  while (!stop.requested()) {
    Sample s = sampler.sample();
    if (enrich) {
      enrich(s);
    }
    next += period;
    const auto now = SteadyClock::now();
    if (now >= next) {
      s.overrun = true;
      next = now;
    }
    if (out.push(std::move(s)) == PushResult::kClosed || stop.wait_until(next)) {
      return;
    }
  }
}

void uplink_loop(Uplink& uplink, BoundedQueue<Sample>& samples, Backoff& backoff,
                 Millis flush_timeout, const std::function<RunStatus()>& final_status) {
  auto drain = [&] {
    while (auto more = samples.pop_for(Millis(0))) {
      uplink.enqueue(std::move(*more));
    }
  };
  auto retry_at = SteadyClock::now();
  for (;;) {
    const bool work = !uplink.has_run() || uplink.pending() > 0;
    const auto now = SteadyClock::now();
    const auto wait = work ? std::max(SteadyClock::duration::zero(), retry_at - now)
                           : SteadyClock::duration(std::chrono::seconds(1));
    if (auto s = samples.pop_for(wait)) {
      uplink.enqueue(std::move(*s));
      drain();
    } else if (samples.closed()) {
      break;
    }
    if (SteadyClock::now() < retry_at) {
      continue;  // backing off; keep collecting samples
    }
    if (!uplink.has_run()) {
      if (!uplink.start()) {
        retry_at = SteadyClock::now() + backoff.next();
        continue;
      }
      backoff.reset();
    }
    for (;;) {
      const auto r = uplink.pump();
      if (r == PumpResult::kProgress) {
        backoff.reset();
        continue;
      }
      if (r == PumpResult::kRetry) {
        retry_at = SteadyClock::now() + backoff.next();
      }
      break;
    }
  }

  // Shutdown: whatever arrived before close, then a bounded flush, then end the run.
  drain();
  const auto deadline = SteadyClock::now() + flush_timeout;
  while (uplink.pending() > 0 && SteadyClock::now() < deadline) {
    if (uplink.pump() != PumpResult::kProgress) {
      break;
    }
  }
  uplink.end(final_status());
}

int run_agent(const AgentConfig& config, HttpClient& http) {
  sigset_t set;
  sigemptyset(&set);
  sigaddset(&set, SIGTERM);
  sigaddset(&set, SIGINT);
  // Before any thread starts, so every thread inherits the mask and only sigtimedwait
  // below receives these signals.
  pthread_sigmask(SIG_BLOCK, &set, nullptr);
  std::signal(SIGPIPE, SIG_IGN);

  log::info(std::format("openfleet-edge-agent {} for {} -> {} (period {} ms, {} process(es))",
                        OPENFLEET_EDGE_VERSION, config.uplink.robot_id, config.api_url,
                        config.period.count(), config.sampler.processes.size()));

  Sampler sampler(config.sampler);
  Uplink uplink(config.uplink, http);
  Backoff backoff(config.backoff_initial, config.backoff_max);
  BoundedQueue<Sample> samples(config.queue_capacity);
  StopSignal stop;
  std::atomic<int> got_signal{0};

  std::thread signals([&] {
    const timespec tick{0, 200'000'000};
    while (!stop.requested()) {
      const int sig = sigtimedwait(&set, nullptr, &tick);
      if (sig > 0) {
        got_signal.store(sig);
        stop.request();
      }
    }
  });
  std::thread up([&] {
    uplink_loop(uplink, samples, backoff, config.flush_timeout, [&] {
      return got_signal.load() != 0 ? RunStatus::kAborted : RunStatus::kSuccess;
    });
  });
  // One gateway and one reader thread per device link.
  std::vector<std::unique_ptr<Gateway>> gateways;
  std::vector<std::thread> device_threads;
  for (const auto& ep : config.devices) {
    GatewayConfig gc;
    gc.link = ep.uri;
    gc.silence_timeout = config.device_silence;
    gateways.push_back(std::make_unique<Gateway>(gc));
    device_threads.emplace_back([&stop, &ep, gw = gateways.back().get()] {
      Backoff device_backoff(Millis(500), Millis(5000));
      device_loop(ep, *gw, stop, device_backoff);
    });
  }
  const auto enrich = [&gateways](Sample& s) {
    for (auto& gw : gateways) {
      gw->contribute(s, SteadyClock::now());
    }
  };
  std::thread sampling([&] { sampler_loop(sampler, samples, stop, config.period, enrich); });

  if (config.duration.count() > 0) {
    stop.wait_for(config.duration);
  } else {
    // Not wait_until(time_point::max()): converting it between clocks overflows.
    while (!stop.wait_for(std::chrono::hours(1))) {
    }
  }
  stop.request();
  if (const int sig = got_signal.load(); sig != 0) {
    log::info(std::format("signal {} received; stopping", sig));
  }
  sampling.join();
  for (auto& t : device_threads) {
    t.join();
  }
  samples.close();
  up.join();
  signals.join();

  const auto st = uplink.stats();
  log::info(std::format("stopped: sent={} dropped={} dropped_no_run={} runs={} pending={} "
                        "queue_dropped={}",
                        st.sent, st.dropped, st.dropped_no_run, st.runs_started, st.pending,
                        samples.dropped()));
  for (std::size_t i = 0; i < gateways.size(); ++i) {
    const auto ls = gateways[i]->stats();
    log::info(std::format("device {}: frames={} bad={} (crc={} short={} overflow={}) dup={} "
                          "lost={} resets={}",
                          config.devices[i].uri, ls.frames, ls.bad(), ls.crc, ls.short_frames,
                          ls.overflow, ls.duplicates, ls.lost, ls.resets));
  }
  return 0;
}

}  // namespace openfleet::edge

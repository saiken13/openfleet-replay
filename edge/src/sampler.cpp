#include "openfleet/edge/sampler.hpp"

#include <algorithm>
#include <format>
#include <fstream>
#include <sstream>
#include <system_error>

namespace openfleet::edge {

namespace fs = std::filesystem;

namespace {
constexpr std::size_t kCommLen = 15;  // TASK_COMM_LEN - 1

bool all_digits(const std::string& s) {
  return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; });
}
}  // namespace

std::optional<std::string> read_file(const fs::path& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) {
    return std::nullopt;
  }
  std::ostringstream out;
  out << in.rdbuf();
  if (in.bad()) {
    return std::nullopt;
  }
  return out.str();
}

std::map<std::string, std::vector<char>> scan_processes(const fs::path& proc_root,
                                                        const std::vector<std::string>& names) {
  std::map<std::string, std::vector<char>> found;
  for (const auto& n : names) {
    found[n];  // every watched name gets an entry, found or not
  }
  if (names.empty()) {
    return found;
  }
  std::error_code ec;
  for (fs::directory_iterator it(proc_root, ec), end; !ec && it != end; it.increment(ec)) {
    if (!all_digits(it->path().filename().string())) {
      continue;
    }
    // A process can exit between listing and reading: a missing file is not an error.
    const auto text = read_file(it->path() / "stat");
    if (!text) {
      continue;
    }
    const auto st = proc::parse_pid_stat(*text);
    if (!st) {
      continue;
    }
    for (const auto& n : names) {
      if (std::string_view(n).substr(0, kCommLen) == st->comm) {
        found[n].push_back(st->state);
      }
    }
  }
  return found;
}

Sampler::Sampler(SamplerConfig config, Clock clock)
    : config_(std::move(config)), clock_(clock), started_(std::chrono::steady_clock::now()) {}

Sample Sampler::sample() {
  Sample s;
  s.ts = clock_();

  if (const auto stat = read_file(config_.proc_root / "stat")) {
    if (const auto cur = proc::parse_cpu_times(*stat)) {
      if (prev_cpu_) {
        s.cpu_pct = proc::cpu_pct(*prev_cpu_, *cur);
      }
      prev_cpu_ = cur;
    }
  }
  if (const auto meminfo = read_file(config_.proc_root / "meminfo")) {
    s.mem_pct = proc::mem_pct(*meminfo);
  }

  const bool in_grace = std::chrono::steady_clock::now() - started_ < config_.startup_grace;
  for (const auto& [name, states] : scan_processes(config_.proc_root, config_.processes)) {
    if (states.empty() && in_grace) {
      continue;
    }
    s.nodes.push_back({name, proc::process_state(states)});
  }

  if (first_) {
    first_ = false;
    std::optional<double> uptime;
    if (const auto text = read_file(config_.proc_root / "uptime")) {
      uptime = proc::parse_uptime(*text);
    }
    s.logs.push_back(
        {s.ts, LogLevel::kInfo, "edge_agent",
         std::format("edge agent started; host up {}; watching {} process(es)",
                     uptime ? std::format("{:.0f} s", *uptime) : std::string("(unknown)"),
                     config_.processes.size())});
  }
  return s;
}

}  // namespace openfleet::edge

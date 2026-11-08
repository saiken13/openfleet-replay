#include "openfleet/edge/proc.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <system_error>

namespace openfleet::edge::proc {
namespace {

std::string_view first_line(std::string_view text) {
  return text.substr(0, text.find('\n'));
}

// Splits on spaces and tabs, skipping empty fields.
std::vector<std::string_view> fields(std::string_view line) {
  std::vector<std::string_view> out;
  std::size_t i = 0;
  while (i < line.size()) {
    while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) {
      ++i;
    }
    const std::size_t start = i;
    while (i < line.size() && line[i] != ' ' && line[i] != '\t') {
      ++i;
    }
    if (i > start) {
      out.push_back(line.substr(start, i - start));
    }
  }
  return out;
}

template <typename T>
std::optional<T> to_number(std::string_view s) {
  T value{};
  const auto* end = s.data() + s.size();
  const auto [ptr, ec] = std::from_chars(s.data(), end, value);
  if (ec != std::errc{} || ptr != end) {
    return std::nullopt;
  }
  return value;
}

}  // namespace

std::optional<CpuTimes> parse_cpu_times(std::string_view stat) {
  const auto f = fields(first_line(stat));
  // cpu user nice system idle [iowait irq softirq steal [guest guest_nice]]
  if (f.size() < 5 || f[0] != "cpu") {
    return std::nullopt;
  }
  // guest and guest_nice are already included in user and nice, so stop at steal (index 8).
  const std::size_t last = std::min<std::size_t>(f.size(), 9);
  std::uint64_t total = 0;
  std::uint64_t idle = 0;
  for (std::size_t i = 1; i < last; ++i) {
    const auto v = to_number<std::uint64_t>(f[i]);
    if (!v) {
      return std::nullopt;
    }
    total += *v;
    if (i == 4 || i == 5) {  // idle, iowait
      idle += *v;
    }
  }
  return CpuTimes{total - idle, total};
}

std::optional<double> cpu_pct(const CpuTimes& before, const CpuTimes& after) {
  if (after.total <= before.total || after.busy < before.busy) {
    return std::nullopt;
  }
  const auto busy = static_cast<double>(after.busy - before.busy);
  const auto total = static_cast<double>(after.total - before.total);
  return std::clamp(100.0 * busy / total, 0.0, 100.0);
}

std::optional<double> mem_pct(std::string_view meminfo) {
  std::optional<std::uint64_t> total;
  std::optional<std::uint64_t> available;
  std::uint64_t legacy = 0;  // MemFree + Buffers + Cached
  int legacy_parts = 0;
  while (!meminfo.empty()) {
    const auto line = first_line(meminfo);
    meminfo.remove_prefix(std::min(meminfo.size(), line.size() + 1));
    const auto f = fields(line);
    if (f.size() < 2) {
      continue;
    }
    const auto v = to_number<std::uint64_t>(f[1]);
    if (!v) {
      continue;
    }
    if (f[0] == "MemTotal:") {
      total = v;
    } else if (f[0] == "MemAvailable:") {
      available = v;
    } else if (f[0] == "MemFree:" || f[0] == "Buffers:" || f[0] == "Cached:") {
      legacy += *v;
      ++legacy_parts;
    }
  }
  if (!total || *total == 0) {
    return std::nullopt;
  }
  if (!available) {
    if (legacy_parts < 3) {
      return std::nullopt;
    }
    available = legacy;
  }
  const auto used = static_cast<double>(*total - std::min(*total, *available));
  return 100.0 * used / static_cast<double>(*total);
}

std::optional<double> parse_uptime(std::string_view uptime) {
  const auto f = fields(first_line(uptime));
  if (f.empty()) {
    return std::nullopt;
  }
  // from_chars for double is in GCC 11+ and every libstdc++ we build with.
  const auto v = to_number<double>(f[0]);
  if (!v || !std::isfinite(*v) || *v < 0) {
    return std::nullopt;
  }
  return v;
}

std::optional<PidStat> parse_pid_stat(std::string_view stat) {
  const auto open = stat.find('(');
  const auto close = stat.rfind(')');
  if (open == std::string_view::npos || close == std::string_view::npos || close < open ||
      close + 2 >= stat.size() || stat[close + 1] != ' ') {
    return std::nullopt;
  }
  const auto head = fields(stat.substr(0, open));
  if (head.size() != 1 || !to_number<std::uint32_t>(head[0])) {
    return std::nullopt;  // must start with exactly one pid
  }
  return PidStat{stat.substr(open + 1, close - open - 1), stat[close + 2]};
}

NodeState process_state(const std::vector<char>& states) {
  if (states.empty()) {
    return NodeState::kStopped;
  }
  bool stopped = false;
  for (const char s : states) {
    if (s == 'T' || s == 't') {
      stopped = true;
    } else if (s != 'Z' && s != 'X' && s != 'x') {
      return NodeState::kRunning;
    }
  }
  return stopped ? NodeState::kDegraded : NodeState::kError;
}

}  // namespace openfleet::edge::proc

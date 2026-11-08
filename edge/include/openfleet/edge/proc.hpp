// Pure parsers for the Linux /proc files the agent samples. No I/O here: the sampler reads
// the files, these functions turn their text into numbers, and the tests feed them saved
// fixtures (tests/fixtures/proc).
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

#include "openfleet/edge/telemetry.hpp"

namespace openfleet::edge::proc {

// Aggregate CPU time from the first "cpu " line of /proc/stat, in clock ticks.
struct CpuTimes {
  std::uint64_t busy = 0;
  std::uint64_t total = 0;
};

std::optional<CpuTimes> parse_cpu_times(std::string_view stat);

// Busy share between two readings, 0..100. nullopt when no time passed or a counter went
// backwards (the counters only grow, so that means a different machine or a bad read).
std::optional<double> cpu_pct(const CpuTimes& before, const CpuTimes& after);

// Used share of RAM, 0..100: (MemTotal - MemAvailable) / MemTotal. Kernels older than 3.14
// have no MemAvailable; then MemFree + Buffers + Cached stands in for it.
std::optional<double> mem_pct(std::string_view meminfo);

// Seconds since boot (first field of /proc/uptime).
std::optional<double> parse_uptime(std::string_view uptime);

// /proc/<pid>/stat: "pid (comm) state ...". comm may itself contain spaces and parentheses,
// so it runs to the LAST ')'.
struct PidStat {
  std::string_view comm;
  char state = '?';
};

std::optional<PidStat> parse_pid_stat(std::string_view stat);

// Health of a watched process from the states of every process with its name:
// none -> STOPPED; any live one (R, S, D, I, ...) -> RUNNING; otherwise any stopped or
// traced one (T, t) -> DEGRADED; only zombies or dead (Z, X) -> ERROR.
NodeState process_state(const std::vector<char>& states);

}  // namespace openfleet::edge::proc

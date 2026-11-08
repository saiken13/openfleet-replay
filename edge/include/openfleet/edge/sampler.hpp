// Reads /proc once per period and turns it into a Sample: CPU (delta of /proc/stat since the
// previous call), memory, and the health of each watched process.
//
// `proc_root` is /proc on a real system; tests point it at tests/fixtures/proc. In a
// container /proc/stat and /proc/meminfo describe the whole (virtual) machine, while the
// process list is the container's PID namespace unless it shares the host's.
#pragma once

#include <chrono>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "openfleet/edge/proc.hpp"
#include "openfleet/edge/telemetry.hpp"

namespace openfleet::edge {

struct SamplerConfig {
  std::filesystem::path proc_root = "/proc";
  std::vector<std::string> processes;  // names as in /proc/<pid>/comm (`ps -e -o comm`)
  // Watched processes that aren't found are left out (not STOPPED) this long after start,
  // so services started alongside the agent aren't flagged. Same idea as the collector's
  // startup_grace_s.
  std::chrono::milliseconds startup_grace{5000};
};

std::optional<std::string> read_file(const std::filesystem::path& path);

// The states (R, S, Z, ...) of every process whose comm matches each name. comm is cut to
// 15 characters by the kernel, so names are compared on that prefix.
std::map<std::string, std::vector<char>> scan_processes(const std::filesystem::path& proc_root,
                                                        const std::vector<std::string>& names);

class Sampler {
 public:
  using Clock = SystemTime (*)();
  explicit Sampler(SamplerConfig config, Clock clock = std::chrono::system_clock::now);

  // CPU is null on the first call: it needs two readings.
  Sample sample();

 private:
  SamplerConfig config_;
  Clock clock_;
  std::chrono::steady_clock::time_point started_;
  std::optional<proc::CpuTimes> prev_cpu_;
  bool first_ = true;
};

}  // namespace openfleet::edge

#include "openfleet/edge/log.hpp"

#include <atomic>
#include <cstdio>
#include <mutex>
#include <string>

namespace openfleet::edge::log {
namespace {
std::atomic<LogLevel> g_min{LogLevel::kInfo};
std::mutex g_mutex;
}  // namespace

void set_min_level(LogLevel level) { g_min.store(level); }

void write(LogLevel level, std::string_view message) {
  if (level < g_min.load()) {
    return;
  }
  const std::string ts = format_iso8601(std::chrono::system_clock::now());
  const std::lock_guard lock(g_mutex);
  std::fprintf(stderr, "%s %s %.*s\n", ts.c_str(), std::string(to_string(level)).c_str(),
               static_cast<int>(message.size()), message.data());
}

}  // namespace openfleet::edge::log

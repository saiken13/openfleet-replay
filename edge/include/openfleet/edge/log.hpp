// The agent's own diagnostics: one line per message on stderr (journald and `docker logs`
// pick it up). Thread-safe.
#pragma once

#include <string_view>

#include "openfleet/edge/telemetry.hpp"

namespace openfleet::edge::log {

void set_min_level(LogLevel level);
void write(LogLevel level, std::string_view message);

inline void info(std::string_view m) { write(LogLevel::kInfo, m); }
inline void warn(std::string_view m) { write(LogLevel::kWarn, m); }
inline void error(std::string_view m) { write(LogLevel::kError, m); }

}  // namespace openfleet::edge::log

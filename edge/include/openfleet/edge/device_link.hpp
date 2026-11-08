// The byte transport between a device and its Gateway. Everything is a file descriptor read
// with poll(): a TCP socket (Renode's UART server socket, the device simulator), or a
// serial line / pty (a real UART adapter, or the simulator's pty).
//
//   tcp://HOST:PORT
//   serial:/dev/ttyUSB0[@BAUD]     (raw 8N1; a pty ignores the baud rate)
#pragma once

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

#include "openfleet/edge/backoff.hpp"
#include "openfleet/edge/gateway.hpp"

namespace openfleet::edge {

class StopSignal;

struct DeviceEndpoint {
  enum class Kind : std::uint8_t { kTcp, kSerial };
  Kind kind = Kind::kTcp;
  std::string host;
  std::uint16_t port = 0;
  std::string path;
  unsigned baud = 115200;
  std::string uri;  // as given
};

// nullopt + *error on a malformed URI.
std::optional<DeviceEndpoint> parse_device_uri(std::string_view uri, std::string* error);

// Opens the link; -1 + *error on failure. The fd is non-blocking.
int open_device(const DeviceEndpoint& ep, std::chrono::milliseconds connect_timeout,
                std::string* error);

// Until stop: open, read into the gateway, reopen with backoff when the link drops.
void device_loop(const DeviceEndpoint& ep, Gateway& gateway, StopSignal& stop, Backoff& backoff);

// Reads from an open fd into the gateway until EOF, an error, or stop. Returns why it ended.
std::string pump_fd(int fd, Gateway& gateway, StopSignal& stop);

}  // namespace openfleet::edge

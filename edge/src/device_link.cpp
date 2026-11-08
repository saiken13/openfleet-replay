#include "openfleet/edge/device_link.hpp"

#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <format>

#include "openfleet/edge/agent.hpp"
#include "openfleet/edge/log.hpp"

namespace openfleet::edge {

namespace {

template <typename T>
std::optional<T> number(std::string_view s) {
  T v{};
  const auto [p, ec] = std::from_chars(s.data(), s.data() + s.size(), v);
  if (ec != std::errc{} || p != s.data() + s.size()) {
    return std::nullopt;
  }
  return v;
}

std::optional<speed_t> baud_constant(unsigned baud) {
  switch (baud) {
    case 9600: return B9600;
    case 19200: return B19200;
    case 38400: return B38400;
    case 57600: return B57600;
    case 115200: return B115200;
    case 230400: return B230400;
    case 460800: return B460800;
    case 921600: return B921600;
    default: return std::nullopt;
  }
}

std::string errno_text() { return std::strerror(errno); }

int connect_tcp(const DeviceEndpoint& ep, std::chrono::milliseconds timeout, std::string* error) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* res = nullptr;
  const std::string port = std::to_string(ep.port);
  if (const int rc = getaddrinfo(ep.host.c_str(), port.c_str(), &hints, &res); rc != 0) {
    *error = std::format("resolve {}: {}", ep.host, gai_strerror(rc));
    return -1;
  }
  int fd = -1;
  for (addrinfo* ai = res; ai != nullptr; ai = ai->ai_next) {
    fd = socket(ai->ai_family, ai->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC, ai->ai_protocol);
    if (fd < 0) {
      continue;
    }
    if (connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) {
      break;
    }
    if (errno == EINPROGRESS) {
      pollfd p{fd, POLLOUT, 0};
      int soerr = 0;
      socklen_t len = sizeof soerr;
      if (poll(&p, 1, static_cast<int>(timeout.count())) == 1 &&
          getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) == 0 && soerr == 0) {
        break;
      }
      *error = soerr != 0 ? std::strerror(soerr) : "connect timed out";
    } else {
      *error = errno_text();
    }
    close(fd);
    fd = -1;
  }
  freeaddrinfo(res);
  if (fd >= 0) {
    const int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
  }
  return fd;
}

int open_serial(const DeviceEndpoint& ep, std::string* error) {
  const int fd = open(ep.path.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
  if (fd < 0) {
    *error = std::format("open {}: {}", ep.path, errno_text());
    return -1;
  }
  if (isatty(fd) == 1) {
    termios t{};
    if (tcgetattr(fd, &t) != 0) {
      *error = std::format("tcgetattr {}: {}", ep.path, errno_text());
      close(fd);
      return -1;
    }
    cfmakeraw(&t);  // 8N1, no echo, no line editing, no CR/LF translation
    t.c_cflag |= CLOCAL | CREAD;
    const auto speed = baud_constant(ep.baud);
    if (speed) {
      cfsetispeed(&t, *speed);
      cfsetospeed(&t, *speed);
    }
    if (tcsetattr(fd, TCSANOW, &t) != 0) {
      *error = std::format("tcsetattr {}: {}", ep.path, errno_text());
      close(fd);
      return -1;
    }
  }
  return fd;
}

}  // namespace

std::optional<DeviceEndpoint> parse_device_uri(std::string_view uri, std::string* error) {
  DeviceEndpoint ep;
  ep.uri = uri;
  if (uri.starts_with("tcp://")) {
    const auto rest = uri.substr(6);
    const auto colon = rest.rfind(':');
    const auto port = colon == std::string_view::npos ? std::nullopt
                                                      : number<std::uint16_t>(rest.substr(colon + 1));
    if (colon == 0 || colon == std::string_view::npos || !port || *port == 0) {
      *error = std::format("bad device URI '{}': expected tcp://HOST:PORT", uri);
      return std::nullopt;
    }
    ep.kind = DeviceEndpoint::Kind::kTcp;
    ep.host = rest.substr(0, colon);
    if (ep.host.size() > 2 && ep.host.front() == '[' && ep.host.back() == ']') {
      ep.host = ep.host.substr(1, ep.host.size() - 2);  // tcp://[::1]:PORT
    }
    ep.port = *port;
    return ep;
  }
  if (uri.starts_with("serial:")) {
    auto rest = uri.substr(7);
    if (const auto at = rest.rfind('@'); at != std::string_view::npos) {
      const auto baud = number<unsigned>(rest.substr(at + 1));
      if (!baud || !baud_constant(*baud)) {
        *error = std::format("bad baud rate in '{}' (9600 .. 921600)", uri);
        return std::nullopt;
      }
      ep.baud = *baud;
      rest = rest.substr(0, at);
    }
    if (rest.empty() || rest[0] != '/') {
      *error = std::format("bad device URI '{}': expected serial:/dev/PATH[@BAUD]", uri);
      return std::nullopt;
    }
    ep.kind = DeviceEndpoint::Kind::kSerial;
    ep.path = rest;
    return ep;
  }
  *error = std::format("bad device URI '{}': use tcp://HOST:PORT or serial:/dev/PATH[@BAUD]", uri);
  return std::nullopt;
}

int open_device(const DeviceEndpoint& ep, std::chrono::milliseconds connect_timeout,
                std::string* error) {
  return ep.kind == DeviceEndpoint::Kind::kTcp ? connect_tcp(ep, connect_timeout, error)
                                               : open_serial(ep, error);
}

std::string pump_fd(int fd, Gateway& gateway, StopSignal& stop) {
  std::array<std::uint8_t, 1024> buf{};
  while (!stop.requested()) {
    pollfd p{fd, POLLIN, 0};
    const int rc = poll(&p, 1, 200);  // wake up regularly to notice stop
    if (rc < 0) {
      if (errno == EINTR) {
        continue;
      }
      return "poll: " + errno_text();
    }
    if (rc == 0) {
      continue;
    }
    const ssize_t n = read(fd, buf.data(), buf.size());
    if (n > 0) {
      gateway.on_bytes(std::span(buf.data(), static_cast<std::size_t>(n)),
                       std::chrono::steady_clock::now());
    } else if (n == 0) {
      return "closed by the device side";
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
      return "read: " + errno_text();
    } else if ((p.revents & (POLLHUP | POLLERR)) != 0) {
      return "hang-up";
    }
  }
  return "stopping";
}

void device_loop(const DeviceEndpoint& ep, Gateway& gateway, StopSignal& stop, Backoff& backoff) {
  bool was_up = false;
  std::string last_error;
  while (!stop.requested()) {
    std::string error;
    const int fd = open_device(ep, std::chrono::milliseconds(2000), &error);
    if (fd < 0) {
      if (error != last_error) {  // don't log the same failure every retry
        log::warn(std::format("device {}: {}", ep.uri, error));
        last_error = error;
      }
      if (was_up) {
        gateway.on_link(false, error);
        was_up = false;
      }
      stop.wait_for(std::chrono::duration_cast<Millis>(backoff.next()));
      continue;
    }
    log::info(std::format("device {} connected", ep.uri));
    gateway.on_link(true, {});
    was_up = true;
    last_error.clear();
    backoff.reset();
    const std::string why = pump_fd(fd, gateway, stop);
    close(fd);
    if (stop.requested()) {
      break;
    }
    log::warn(std::format("device {} disconnected: {}", ep.uri, why));
    gateway.on_link(false, why);
    was_up = false;
    stop.wait_for(std::chrono::duration_cast<Millis>(backoff.next()));
  }
}

}  // namespace openfleet::edge

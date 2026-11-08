// openfleet-device-sim: a simulated microcontroller on a TCP port (like Renode's UART server
// socket) or a pty, for the gateway to connect to. Faults and events are driven by commands,
// given with --command at start or appended as lines to --control-file while it runs:
//
//   openfleet-device-sim --listen 4000 --control-file /tmp/devsim.ctl
//   echo "stop-sensor 2" >> /tmp/devsim.ctl
//
// Simulation only: no hardware, no real-time behaviour.
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <charconv>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <format>
#include <fstream>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "openfleet/edge/device_sim.hpp"

using namespace openfleet::edge;

namespace {

volatile std::sig_atomic_t g_stop = 0;

void usage() {
  std::puts(R"(usage: openfleet-device-sim (--listen PORT | --pty PATH) [options]

  --listen PORT          serve the device's UART on TCP PORT (one client at a time)
  --pty PATH             create a pty and symlink its device to PATH
  --device-id N          protocol device id (default 1)
  --boot-id N            boot id of the first boot (default 1)
  --firmware TEXT        firmware version in HELLO
  --sensor ID:NAME:HZ    a sensor (repeatable; default 1:imu:50 2:range:10)
  --task ID:NAME         a task (repeatable; default 1:sensor 2:comms 3:health)
  --seed N               fault-injection seed (default 1)
  --command CMD          run a control command at start (repeatable)
  --control-file PATH    run each line appended to PATH as a command
)");
  std::printf("commands: %s\n", DeviceSim::kCommands);
}

template <typename T>
bool number(std::string_view s, T& out) {
  const auto [p, ec] = std::from_chars(s.data(), s.data() + s.size(), out);
  return ec == std::errc{} && p == s.data() + s.size();
}

int fail(const std::string& msg) {
  std::fprintf(stderr, "error: %s\n", msg.c_str());
  return 2;
}

// Reads lines appended to a file since the last call.
class ControlFile {
 public:
  // Only lines appended from now on: a restarted simulator must not replay old commands
  // (the file survives a container restart).
  explicit ControlFile(std::string path) : path_(std::move(path)) {
    std::error_code ec;
    const auto size = std::filesystem::file_size(path_, ec);
    offset_ = ec ? 0 : size;
  }
  std::vector<std::string> new_lines() {
    std::vector<std::string> out;
    std::error_code ec;
    const auto size = std::filesystem::file_size(path_, ec);
    if (ec) {
      return out;
    }
    if (size < offset_) {
      offset_ = 0;  // truncated: start over
    }
    std::ifstream in(path_, std::ios::binary);
    in.seekg(static_cast<std::streamoff>(offset_));
    std::string line;
    while (std::getline(in, line)) {
      if (in.eof()) {
        break;  // no newline yet: incomplete, read it next time
      }
      offset_ += line.size() + 1;
      out.push_back(line);
    }
    return out;
  }

 private:
  std::string path_;
  std::uintmax_t offset_ = 0;
};

}  // namespace

int main(int argc, char** argv) {
  SimConfig cfg;
  bool custom_sensors = false;
  bool custom_tasks = false;
  int port = 0;
  std::string pty_link;
  std::string control_path;
  std::vector<std::string> start_commands;

  for (int i = 1; i < argc; ++i) {
    const std::string_view flag = argv[i];
    if (flag == "--help" || flag == "-h") {
      usage();
      return 0;
    }
    if (i + 1 >= argc) {
      return fail(std::format("{} needs a value", flag));
    }
    const std::string_view v = argv[++i];
    if (flag == "--listen") {
      if (!number(v, port) || port <= 0 || port > 65535) {
        return fail("--listen needs a port");
      }
    } else if (flag == "--pty") {
      pty_link = v;
    } else if (flag == "--device-id") {
      if (!number(v, cfg.device_id)) {
        return fail("--device-id needs 0..65535");
      }
    } else if (flag == "--boot-id") {
      if (!number(v, cfg.boot_id)) {
        return fail("--boot-id needs 0..65535");
      }
    } else if (flag == "--firmware") {
      cfg.firmware = v;
    } else if (flag == "--seed") {
      if (!number(v, cfg.seed)) {
        return fail("--seed needs a number");
      }
    } else if (flag == "--sensor") {
      const auto a = v.find(':');
      const auto b = v.rfind(':');
      unsigned id = 0;
      double hz = 0;
      if (a == std::string_view::npos || a == b || !number(v.substr(0, a), id) || id > 255 ||
          !number(v.substr(b + 1), hz) || hz <= 0 || hz > 1000) {
        return fail("--sensor needs ID:NAME:HZ (HZ in (0, 1000])");
      }
      if (!custom_sensors) {
        cfg.sensors.clear();
        custom_sensors = true;
      }
      cfg.sensors.push_back({static_cast<std::uint8_t>(id), std::string(v.substr(a + 1, b - a - 1)),
                             hz, true});
    } else if (flag == "--task") {
      const auto a = v.find(':');
      unsigned id = 0;
      if (a == std::string_view::npos || !number(v.substr(0, a), id) || id > 255) {
        return fail("--task needs ID:NAME");
      }
      if (!custom_tasks) {
        cfg.tasks.clear();
        custom_tasks = true;
      }
      cfg.tasks.push_back({static_cast<std::uint8_t>(id), std::string(v.substr(a + 1)),
                           OFP_TASK_RUNNING, 200});
    } else if (flag == "--command") {
      start_commands.emplace_back(v);
    } else if (flag == "--control-file") {
      control_path = v;
    } else {
      return fail(std::format("unknown option {}", flag));
    }
  }
  if ((port == 0) == pty_link.empty()) {
    return fail("give exactly one of --listen PORT or --pty PATH");
  }
  for (const auto& s : cfg.sensors) {
    if (!ofp_valid_name(s.name.c_str(), s.name.size())) {
      return fail(std::format("sensor name '{}': use 1-32 of [A-Za-z0-9_-]", s.name));
    }
  }
  for (const auto& t : cfg.tasks) {
    if (!ofp_valid_name(t.name.c_str(), t.name.size())) {
      return fail(std::format("task name '{}': use 1-32 of [A-Za-z0-9_-]", t.name));
    }
  }

  std::signal(SIGTERM, [](int) { g_stop = 1; });
  std::signal(SIGINT, [](int) { g_stop = 1; });
  std::signal(SIGPIPE, SIG_IGN);

  int listen_fd = -1;
  int out_fd = -1;  // the connected client, or the pty master
  int pty_slave = -1;
  if (port != 0) {
    listen_fd = socket(AF_INET6, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    const int one = 1;
    const int zero = 0;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(listen_fd, IPPROTO_IPV6, IPV6_V6ONLY, &zero, sizeof zero);
    sockaddr_in6 addr{};
    addr.sin6_family = AF_INET6;
    addr.sin6_addr = in6addr_any;
    addr.sin6_port = htons(static_cast<std::uint16_t>(port));
    if (listen_fd < 0 || bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 ||
        listen(listen_fd, 1) != 0) {
      return fail(std::format("listen on {}: {}", port, std::strerror(errno)));
    }
    std::fprintf(stderr, "device-sim: device %u listening on tcp port %d\n", cfg.device_id, port);
  } else {
    out_fd = posix_openpt(O_RDWR | O_NOCTTY);
    if (out_fd < 0 || grantpt(out_fd) != 0 || unlockpt(out_fd) != 0) {
      return fail(std::format("pty: {}", std::strerror(errno)));
    }
    const std::string slave = ptsname(out_fd);
    // Hold the slave open and raw, so nothing echoes back and the line stays up before the
    // gateway opens it.
    pty_slave = open(slave.c_str(), O_RDWR | O_NOCTTY);
    termios t{};
    tcgetattr(pty_slave, &t);
    cfmakeraw(&t);
    tcsetattr(pty_slave, TCSANOW, &t);
    fcntl(out_fd, F_SETFL, fcntl(out_fd, F_GETFL) | O_NONBLOCK);  // full buffer: drop (overrun)
    std::error_code ec;
    std::filesystem::remove(pty_link, ec);
    std::filesystem::create_symlink(slave, pty_link, ec);
    if (ec) {
      return fail(std::format("symlink {} -> {}: {}", pty_link, slave, ec.message()));
    }
    std::fprintf(stderr, "device-sim: device %u on pty %s (%s)\n", cfg.device_id, slave.c_str(),
                 pty_link.c_str());
  }

  auto now = DeviceSim::Clock::now();
  DeviceSim sim(cfg, now);
  std::uint64_t written = 0;
  const DeviceSim::Sink sink = [&](std::span<const std::uint8_t> bytes) {
    if (out_fd < 0) {
      return;  // nothing attached to the UART: the bytes go nowhere
    }
    const ssize_t n = listen_fd >= 0 ? send(out_fd, bytes.data(), bytes.size(), MSG_NOSIGNAL)
                                     : write(out_fd, bytes.data(), bytes.size());
    if (n > 0) {
      written += static_cast<std::uint64_t>(n);
    } else if (listen_fd >= 0 && n < 0 && errno != EAGAIN) {
      std::fprintf(stderr, "device-sim: client gone (%s)\n", std::strerror(errno));
      close(out_fd);
      out_fd = -1;
    }
    if (sim.faults().fragment) {
      std::this_thread::sleep_for(std::chrono::microseconds(300));  // separate reads
    }
  };
  for (const auto& c : start_commands) {
    if (const auto err = sim.command(c, now, sink)) {
      return fail(*err);
    }
  }
  ControlFile control(control_path);
  auto next_control = now;

  while (g_stop == 0) {
    now = DeviceSim::Clock::now();
    if (listen_fd >= 0) {
      const int c = accept4(listen_fd, nullptr, nullptr, SOCK_CLOEXEC);
      if (c >= 0) {
        if (out_fd >= 0) {
          close(out_fd);  // one client at a time, like a UART
        }
        const int one = 1;
        setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        out_fd = c;
        std::fprintf(stderr, "device-sim: client connected\n");
      }
    }
    if (!control_path.empty() && now >= next_control) {
      next_control = now + std::chrono::milliseconds(100);
      for (const auto& line : control.new_lines()) {
        const auto err = sim.command(line, now, sink);
        std::fprintf(stderr, "device-sim: %s: %s\n", line.c_str(),
                     err ? err->c_str() : "ok");
      }
    }
    sim.tick(now, sink);
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  std::fprintf(stderr, "device-sim: stopped after %llu frames, %llu bytes written\n",
               static_cast<unsigned long long>(sim.frames_sent()),
               static_cast<unsigned long long>(written));
  if (pty_slave >= 0) {
    close(pty_slave);
    std::error_code ec;
    std::filesystem::remove(pty_link, ec);
  }
  return 0;
}

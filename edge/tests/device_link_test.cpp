// Transports: URI parsing, a socketpair, a real TCP connection (with reconnect) and a pty.
#include "openfleet/edge/device_link.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <gtest/gtest.h>

#include <atomic>
#include <cstdlib>
#include <thread>

#include "openfleet/edge/agent.hpp"
#include "openfleet/edge/device_sim.hpp"
#include "openfleet/edge/log.hpp"

using namespace openfleet::edge;
using namespace std::chrono_literals;

namespace {

// Writes one second of simulated device output to `fd`.
void write_device_second(int fd, std::uint64_t seed = 1) {
  auto t = DeviceSim::Clock::now();
  SimConfig cfg;
  cfg.seed = seed;
  DeviceSim sim(cfg, t);
  for (int i = 0; i < 200; ++i) {
    t += 5ms;
    sim.tick(t, [fd](std::span<const std::uint8_t> b) {
      ASSERT_EQ(write(fd, b.data(), b.size()), static_cast<ssize_t>(b.size()));
    });
  }
}

bool eventually(const std::function<bool()>& cond, std::chrono::milliseconds timeout = 5s) {
  const auto end = std::chrono::steady_clock::now() + timeout;
  while (std::chrono::steady_clock::now() < end) {
    if (cond()) {
      return true;
    }
    std::this_thread::sleep_for(10ms);
  }
  return cond();
}

class DeviceLinkTest : public ::testing::Test {
 protected:
  void SetUp() override { log::set_min_level(LogLevel::kFatal); }
  void TearDown() override { log::set_min_level(LogLevel::kInfo); }
};

}  // namespace

TEST(DeviceUri, ParsesTcpAndSerial) {
  std::string err;
  auto ep = parse_device_uri("tcp://device-sim:4000", &err);
  ASSERT_TRUE(ep) << err;
  EXPECT_EQ(ep->kind, DeviceEndpoint::Kind::kTcp);
  EXPECT_EQ(ep->host, "device-sim");
  EXPECT_EQ(ep->port, 4000);
  ep = parse_device_uri("tcp://[::1]:3456", &err);
  ASSERT_TRUE(ep) << err;
  EXPECT_EQ(ep->host, "::1");
  ep = parse_device_uri("serial:/dev/ttyUSB0@921600", &err);
  ASSERT_TRUE(ep) << err;
  EXPECT_EQ(ep->kind, DeviceEndpoint::Kind::kSerial);
  EXPECT_EQ(ep->path, "/dev/ttyUSB0");
  EXPECT_EQ(ep->baud, 921600U);
  EXPECT_EQ(parse_device_uri("serial:/tmp/dev.pty", &err)->baud, 115200U);

  for (const char* bad : {"tcp://host", "tcp://:4000", "tcp://h:0", "tcp://h:99999",
                          "serial:dev", "serial:/dev/x@1234", "udp://h:1", ""}) {
    EXPECT_FALSE(parse_device_uri(bad, &err)) << bad;
  }
}

TEST_F(DeviceLinkTest, PumpReadsWhatTheDeviceWrites) {
  int sv[2];
  ASSERT_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
  Gateway gw({});
  StopSignal stop;
  std::string why;
  std::thread reader([&] { why = pump_fd(sv[0], gw, stop); });
  write_device_second(sv[1]);
  close(sv[1]);  // the device side hangs up
  reader.join();
  close(sv[0]);
  EXPECT_EQ(why, "closed by the device side");
  EXPECT_GT(gw.stats().frames, 50U);
  EXPECT_EQ(gw.stats().bad(), 0U);
}

TEST_F(DeviceLinkTest, TcpConnectsAndReconnectsAfterTheServerDrops) {
  const int srv = socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  ASSERT_EQ(bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof addr), 0);
  ASSERT_EQ(listen(srv, 2), 0);
  socklen_t len = sizeof addr;
  getsockname(srv, reinterpret_cast<sockaddr*>(&addr), &len);
  std::string err;
  const auto ep = parse_device_uri("tcp://127.0.0.1:" + std::to_string(ntohs(addr.sin_port)), &err);
  ASSERT_TRUE(ep) << err;

  Gateway gw({});
  StopSignal stop;
  Backoff backoff(10ms, 20ms, 1);
  std::thread loop([&] { device_loop(*ep, gw, stop, backoff); });

  for (std::uint64_t round = 1; round <= 2; ++round) {  // serve, drop, serve again
    const int c = accept(srv, nullptr, nullptr);
    ASSERT_GE(c, 0);
    write_device_second(c, round);
    close(c);
  }
  EXPECT_TRUE(eventually([&] { return gw.stats().frames > 100; }));
  stop.request();
  loop.join();
  close(srv);

  Sample s;
  gw.contribute(s, Gateway::Clock::now());
  int connected = 0;
  int lost = 0;
  for (const auto& l : s.logs) {
    connected += l.message.find("connected") != std::string::npos ? 1 : 0;
    lost += l.message.find(" lost: ") != std::string::npos ? 1 : 0;
  }
  EXPECT_GE(connected, 2);
  EXPECT_GE(lost, 1);
}

TEST_F(DeviceLinkTest, ConnectionRefusedIsRetriedQuietly) {
  std::string err;
  // Port 1 on localhost: nothing listens there.
  const auto ep = parse_device_uri("tcp://127.0.0.1:1", &err);
  EXPECT_EQ(open_device(*ep, 500ms, &err), -1);
  EXPECT_FALSE(err.empty());
  Gateway gw({});
  StopSignal stop;
  Backoff backoff(5ms, 10ms, 1);
  std::thread loop([&] { device_loop(*ep, gw, stop, backoff); });
  std::this_thread::sleep_for(100ms);
  stop.request();
  loop.join();  // returns promptly on stop
  EXPECT_EQ(gw.stats().frames, 0U);
}

TEST_F(DeviceLinkTest, SerialPathWorksOverAPty) {
  const int master = posix_openpt(O_RDWR | O_NOCTTY);
  ASSERT_GE(master, 0);
  ASSERT_EQ(grantpt(master), 0);
  ASSERT_EQ(unlockpt(master), 0);
  const std::string slave = ptsname(master);
  std::string err;
  const auto ep = parse_device_uri("serial:" + slave + "@115200", &err);
  ASSERT_TRUE(ep) << err;
  const int fd = open_device(*ep, 500ms, &err);
  ASSERT_GE(fd, 0) << err;  // opened and switched to raw mode

  Gateway gw({});
  StopSignal stop;
  std::thread reader([&] { pump_fd(fd, gw, stop); });
  write_device_second(master);
  EXPECT_TRUE(eventually([&] { return gw.stats().frames > 50; }));
  stop.request();
  reader.join();
  close(fd);
  close(master);
  EXPECT_EQ(gw.stats().bad(), 0U);  // raw mode: no byte was translated
}

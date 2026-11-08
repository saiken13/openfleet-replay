// Micro-benchmarks for the device protocol and the gateway (Phase 4, docs/BENCHMARKS.md).
//
//   edge_bench --env native            # human-readable table
//   edge_bench --env qemu-user --json out.json
//   edge_bench --quick                 # a few iterations: a smoke test, not a measurement
//
// Each case runs a batch of operations repeatedly for at least --min-ms, takes several such
// samples and reports the median time per operation (and throughput where that makes
// sense). The figure is only as good as its environment, so every report carries the
// --env label the caller gives (native / qemu-user / ...) plus the build's architecture,
// compiler and build type. Under qemu-user the numbers describe the emulator, not a CPU.
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <format>
#include <fstream>
#include <functional>
#include <iostream>
#include <span>
#include <string>
#include <tuple>
#include <vector>

#include <nlohmann/json.hpp>

#include "openfleet/edge/gateway.hpp"
#include "openfleet/edge/telemetry.hpp"
#include "openfleet/protocol.h"

using namespace openfleet::edge;
using BenchClock = std::chrono::steady_clock;

namespace {

// Keeps the compiler from proving a result unused and deleting the work.
template <typename T>
void keep(T const& value) {
  asm volatile("" : : "r,m"(value) : "memory");
}

struct Options {
  std::string env = "unlabelled";
  std::string json_path;
  int min_ms = 300;
  int samples = 5;
};

struct Result {
  std::string name;
  std::string what;           // one line: what one operation is
  double ns_per_op = 0;
  double ops_per_s = 0;
  double mb_per_s = 0;        // 0 = not a byte-throughput case
  std::uint64_t ops = 0;      // operations measured in total
};

// `body(n)` performs n operations and returns the bytes it processed (0 if not relevant).
Result measure(const Options& opt, std::string name, std::string what,
               const std::function<std::uint64_t(std::uint64_t)>& body) {
  // Calibrate: grow the batch until one batch takes >= 1/10 of the minimum time.
  std::uint64_t batch = 1;
  const auto target = std::chrono::milliseconds(std::max(1, opt.min_ms / 10));
  for (;;) {
    const auto t0 = BenchClock::now();
    body(batch);
    if (BenchClock::now() - t0 >= target || batch >= (1ull << 40)) {
      break;
    }
    batch *= 2;
  }
  std::vector<double> ns_per_op;
  std::vector<double> bytes_per_ns;
  std::uint64_t total_ops = 0;
  for (int s = 0; s < opt.samples; ++s) {
    std::uint64_t ops = 0;
    std::uint64_t bytes = 0;
    const auto t0 = BenchClock::now();
    auto elapsed = BenchClock::duration::zero();
    do {
      bytes += body(batch);
      ops += batch;
      elapsed = BenchClock::now() - t0;
    } while (elapsed < std::chrono::milliseconds(opt.min_ms));
    const auto ns = static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count());
    ns_per_op.push_back(ns / static_cast<double>(ops));
    bytes_per_ns.push_back(static_cast<double>(bytes) / ns);
    total_ops += ops;
  }
  std::ranges::sort(ns_per_op);
  std::ranges::sort(bytes_per_ns);
  Result r;
  r.name = std::move(name);
  r.what = std::move(what);
  r.ns_per_op = ns_per_op[ns_per_op.size() / 2];
  r.ops_per_s = 1e9 / r.ns_per_op;
  r.mb_per_s = bytes_per_ns[bytes_per_ns.size() / 2] * 1e9 / 1e6;
  r.ops = total_ops;
  return r;
}

// A SENSOR frame as the firmware sends it (5-byte payload), ready on the wire.
std::vector<std::uint8_t> sensor_wire(std::uint16_t seq, std::uint32_t uptime_ms, std::uint8_t id) {
  std::uint8_t payload[8];
  const std::size_t n = ofp_build_sensor(payload, sizeof payload, id, 1000 + seq);
  ofp_header h{};
  h.type = OFP_SENSOR;
  h.device_id = 1;
  h.seq = seq;
  h.boot_id = 7;
  h.uptime_ms = uptime_ms;
  h.length = static_cast<std::uint16_t>(n);
  std::vector<std::uint8_t> wire(OFP_MAX_WIRE);
  wire.resize(ofp_encode(&h, payload, wire.data(), wire.size()));
  return wire;
}

std::vector<std::uint8_t> max_wire() {
  std::uint8_t payload[OFP_MAX_PAYLOAD];
  for (std::size_t i = 0; i < sizeof payload; ++i) {
    payload[i] = static_cast<std::uint8_t>(i * 7 + 1);  // some zeros-free, some not
  }
  payload[17] = 0;
  payload[200] = 0;
  ofp_header h{};
  h.type = OFP_LOG;
  h.device_id = 1;
  h.length = OFP_MAX_PAYLOAD;
  std::vector<std::uint8_t> wire(OFP_MAX_WIRE);
  wire.resize(ofp_encode(&h, payload, wire.data(), wire.size()));
  return wire;
}

// A second of device traffic like the firmware's: imu 50 Hz + range 10 Hz, interleaved,
// plus DESCRIBE and HEALTH frames, so the gateway knows the names and tasks.
std::vector<std::uint8_t> device_second(std::uint16_t& seq, std::uint32_t& uptime, std::size_t* frames) {
  std::vector<std::uint8_t> out;
  auto put = [&](const ofp_header& base, const std::uint8_t* p, std::size_t n) {
    ofp_header h = base;
    h.seq = seq++;
    h.uptime_ms = uptime;
    h.length = static_cast<std::uint16_t>(n);
    std::uint8_t wire[OFP_MAX_WIRE];
    const std::size_t w = ofp_encode(&h, p, wire, sizeof wire);
    out.insert(out.end(), wire, wire + w);
    ++*frames;
  };
  ofp_header base{};
  base.device_id = 1;
  base.boot_id = 7;
  std::uint8_t p[OFP_MAX_PAYLOAD];
  base.type = OFP_DESCRIBE;
  for (auto [kind, id, dhz, name] : {std::tuple{OFP_KIND_SENSOR, 1, 500, "imu"},
                                     std::tuple{OFP_KIND_SENSOR, 2, 100, "range"},
                                     std::tuple{OFP_KIND_TASK, 1, 0, "sensor"}}) {
    ofp_describe d{};
    d.kind = static_cast<std::uint8_t>(kind);
    d.id = static_cast<std::uint8_t>(id);
    d.expected_dhz = static_cast<std::uint16_t>(dhz);
    std::snprintf(d.name, sizeof d.name, "%s", name);
    put(base, p, ofp_build_describe(p, sizeof p, &d));
  }
  for (int ms = 0; ms < 1000; ms += 20) {
    uptime += 20;
    base.type = OFP_SENSOR;
    put(base, p, ofp_build_sensor(p, sizeof p, 1, ms));
    if (ms % 100 == 0) {
      put(base, p, ofp_build_sensor(p, sizeof p, 2, 1500));
    }
  }
  ofp_health h{};
  h.count = 1;
  h.tasks[0] = {1, OFP_TASK_RUNNING, 120};
  base.type = OFP_HEALTH;
  put(base, p, ofp_build_health(p, sizeof p, &h));
  return out;
}

std::vector<Result> run_all(const Options& opt) {
  std::vector<Result> out;

  {
    std::vector<std::uint8_t> data(OFP_MAX_RAW);
    for (std::size_t i = 0; i < data.size(); ++i) {
      data[i] = static_cast<std::uint8_t>(i);
    }
    out.push_back(measure(opt, "crc16_256B", "CRC-16/CCITT over a maximum-size raw frame (256 B)",
                          [&](std::uint64_t n) {
                            for (std::uint64_t i = 0; i < n; ++i) {
                              data[0] = static_cast<std::uint8_t>(i);
                              keep(ofp_crc16(data.data(), data.size()));
                            }
                            return n * data.size();
                          }));
  }

  {
    std::uint8_t payload[8];
    const std::size_t plen = ofp_build_sensor(payload, sizeof payload, 1, 1234);
    std::uint8_t wire[OFP_MAX_WIRE];
    ofp_header h{};
    h.type = OFP_SENSOR;
    h.device_id = 1;
    h.length = static_cast<std::uint16_t>(plen);
    out.push_back(measure(opt, "encode_sensor", "build + encode one SENSOR frame (header, CRC, COBS)",
                          [&](std::uint64_t n) {
                            std::uint64_t bytes = 0;
                            for (std::uint64_t i = 0; i < n; ++i) {
                              h.seq = static_cast<std::uint16_t>(i);
                              bytes += ofp_encode(&h, payload, wire, sizeof wire);
                              keep(wire[0]);
                            }
                            return bytes;
                          }));
  }

  for (auto [name, what, wire] :
       {std::tuple{"decode_sensor", "streaming decode of one SENSOR frame, byte by byte",
                   sensor_wire(1, 20, 1)},
        std::tuple{"decode_max", "streaming decode of one maximum-size frame (240 B payload)",
                   max_wire()}}) {
    ofp_decoder dec;
    ofp_decoder_init(&dec);
    ofp_frame f;
    out.push_back(measure(opt, name, what, [&](std::uint64_t n) {
      std::uint64_t frames = 0;
      for (std::uint64_t i = 0; i < n; ++i) {
        for (const std::uint8_t b : wire) {
          frames += ofp_decoder_push(&dec, b, &f) == OFP_FRAME ? 1u : 0u;
        }
      }
      if (frames != n) {
        std::fprintf(stderr, "decoder lost frames\n");
        std::abort();
      }
      keep(f.header.seq);
      return n * wire.size();
    }));
  }

  {
    // The gateway's whole receive path for real traffic: decode, duplicate window, payload
    // parsing, rate counting, under its mutex. One "op" = one frame.
    std::uint16_t seq = 0;
    std::uint32_t uptime = 0;
    std::vector<std::vector<std::uint8_t>> seconds;
    std::size_t frames_per_second = 0;
    for (int i = 0; i < 16; ++i) {
      frames_per_second = 0;
      seconds.push_back(device_second(seq, uptime, &frames_per_second));
    }
    GatewayConfig cfg;
    cfg.link = "bench";
    Gateway gw(cfg);
    auto now = Gateway::Clock::time_point(std::chrono::seconds(1000));
    std::size_t next = 0;
    std::uint64_t carried = 0;  // frames fed but not yet counted as ops
    const SystemTime wall{};
    out.push_back(measure(opt, "gateway_frame",
                          "gateway receive path per frame (imu 50 Hz + range 10 Hz device mix)",
                          [&](std::uint64_t n) {
                            std::uint64_t bytes = 0;
                            while (carried < n) {
                              const auto& s = seconds[next++ % seconds.size()];
                              now += std::chrono::seconds(1);
                              gw.on_bytes(s, now, wall);
                              carried += frames_per_second;
                              bytes += s.size();
                              if (next % 4 == 0) {  // keep the window and log list bounded
                                Sample sample;
                                gw.contribute(sample, now);
                                keep(sample.topics.size());
                              }
                            }
                            carried -= n;
                            return bytes;
                          }));
  }

  {
    // What the agent does once a second per batch: the gateway's window -> Sample -> JSON.
    Sample sample;
    sample.ts = std::chrono::system_clock::now();
    sample.cpu_pct = 12.5;
    sample.mem_pct = 40.25;
    for (int d = 1; d <= 2; ++d) {
      for (const char* s : {"imu", "range"}) {
        sample.topics.push_back({std::format("/mcu{}/{}", d, s), 50.0, 49.0, 0.02});
      }
      for (const char* t : {"", "/sensor", "/comms", "/health"}) {
        sample.nodes.push_back({std::format("mcu{}{}", d, t), NodeState::kRunning});
      }
    }
    sample.nodes.push_back({"edge_agent", NodeState::kRunning});
    out.push_back(measure(opt, "batch_json", "one telemetry batch (4 topics, 9 nodes) to JSON text",
                          [&](std::uint64_t n) {
                            std::uint64_t bytes = 0;
                            for (std::uint64_t i = 0; i < n; ++i) {
                              const std::string body =
                                  batch_json(static_cast<std::int64_t>(i), sample, 3.5).dump();
                              bytes += body.size();
                              keep(body.data());
                            }
                            return bytes;
                          }));
  }
  return out;
}

const char* arch() {
#if defined(__aarch64__)
  return "aarch64";
#elif defined(__x86_64__)
  return "x86_64";
#elif defined(__arm__)
  return "arm";
#else
  return "unknown";
#endif
}

const char* compiler() {
#if defined(__clang__)
  return "clang " __clang_version__;
#elif defined(__GNUC__)
  return "gcc " __VERSION__;
#else
  return "unknown";
#endif
}

#ifndef EDGE_BUILD_TYPE
#define EDGE_BUILD_TYPE "unknown"
#endif

}  // namespace

int main(int argc, char** argv) {
  Options opt;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto value = [&]() -> std::string {
      if (i + 1 >= argc) {
        std::cerr << a << " needs a value\n";
        std::exit(2);
      }
      return argv[++i];
    };
    if (a == "--env") {
      opt.env = value();
    } else if (a == "--json") {
      opt.json_path = value();
    } else if (a == "--min-ms") {
      opt.min_ms = std::stoi(value());
    } else if (a == "--quick") {
      opt.min_ms = 1;
      opt.samples = 1;
    } else {
      std::cerr << "usage: edge_bench [--env LABEL] [--json PATH] [--min-ms N] [--quick]\n";
      return a == "--help" ? 0 : 2;
    }
  }

  const auto results = run_all(opt);

  std::printf("environment: %s | %s | %s | %s\n", opt.env.c_str(), arch(), compiler(), EDGE_BUILD_TYPE);
  std::printf("%-16s %12s %14s %10s\n", "case", "ns/op", "ops/s", "MB/s");
  for (const auto& r : results) {
    std::printf("%-16s %12.1f %14.0f %10.1f\n", r.name.c_str(), r.ns_per_op, r.ops_per_s, r.mb_per_s);
  }

  if (!opt.json_path.empty()) {
    nlohmann::json j;
    j["suite"] = "edge_bench";
    j["env"] = opt.env;
    j["arch"] = arch();
    j["compiler"] = compiler();
    j["build_type"] = EDGE_BUILD_TYPE;
    j["min_ms"] = opt.min_ms;
    j["samples"] = opt.samples;
    for (const auto& r : results) {
      j["results"].push_back({{"name", r.name},
                              {"what", r.what},
                              {"ns_per_op", r.ns_per_op},
                              {"ops_per_s", r.ops_per_s},
                              {"mb_per_s", r.mb_per_s},
                              {"ops", r.ops}});
    }
    std::ofstream(opt.json_path) << j.dump(2) << "\n";
  }
  return 0;
}

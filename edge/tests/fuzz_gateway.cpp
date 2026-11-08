// libFuzzer target for the gateway (DECISIONS #033). Random bytes almost never pass a CRC,
// so this one is structured: the input is read as a script of frames (header fields chosen
// by the fuzzer, then properly encoded), raw junk, and sampling windows. That drives the
// reset, duplicate-window and payload-parsing logic with adversarial values.
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <vector>

#include "openfleet/edge/gateway.hpp"

using namespace openfleet::edge;

namespace {

class Reader {
 public:
  Reader(const std::uint8_t* d, std::size_t n) : d_(d), n_(n) {}
  [[nodiscard]] bool empty() const { return i_ >= n_; }
  std::uint8_t u8() { return i_ < n_ ? d_[i_++] : 0; }
  std::uint16_t u16() { return static_cast<std::uint16_t>(u8() | (u8() << 8)); }
  std::uint32_t u32() { return static_cast<std::uint32_t>(u16() | (std::uint32_t{u16()} << 16)); }
  std::vector<std::uint8_t> bytes(std::size_t n) {
    std::vector<std::uint8_t> out;
    while (n-- > 0 && !empty()) {
      out.push_back(u8());
    }
    return out;
  }

 private:
  const std::uint8_t* d_;
  std::size_t n_;
  std::size_t i_ = 0;
};

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
  GatewayConfig cfg;
  cfg.link = "fuzz";
  cfg.max_logs_per_window = 8;
  Gateway gw(cfg);
  auto now = Gateway::Clock::time_point(std::chrono::seconds(100));
  Reader r(data, size);
  while (!r.empty()) {
    const std::uint8_t op = r.u8();
    if (op < 200) {  // a well-formed frame with fuzzed fields
      ofp_header h{};
      h.type = static_cast<std::uint8_t>(op % 7);  // includes unknown types 0 and 6
      h.device_id = static_cast<std::uint16_t>(r.u8() % 3);
      h.seq = r.u16();
      h.boot_id = static_cast<std::uint16_t>(r.u8() % 4);
      h.uptime_ms = r.u32();
      const auto payload = r.bytes(r.u8() % (OFP_MAX_PAYLOAD + 1));
      h.length = static_cast<std::uint16_t>(payload.size());
      std::uint8_t wire[OFP_MAX_WIRE];
      const std::size_t n = ofp_encode(&h, payload.data(), wire, sizeof wire);
      if (n == 0) {
        abort();  // any payload up to the maximum must encode
      }
      gw.on_bytes(std::span(wire, n), now);
    } else if (op < 230) {  // raw junk
      const auto junk = r.bytes(r.u8());
      gw.on_bytes(junk, now);
    } else {  // time passes, a window is taken
      now += std::chrono::milliseconds(r.u16());
      Sample s;
      gw.contribute(s, now);
      if (s.logs.size() > cfg.max_logs_per_window + 1) {  // + the link-error summary
        abort();
      }
      for (const auto& t : s.topics) {
        if (t.actual_hz < 0 || t.topic.empty()) {
          abort();
        }
      }
    }
  }
  const LinkStats st = gw.stats();
  if (st.duplicates + st.stale > st.frames) {
    abort();
  }
  return 0;
}

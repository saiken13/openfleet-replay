// The device protocol (docs/PROTOCOL.md): CRC, COBS, framing, the streaming decoder's
// handling of every fault the brief lists, and payload validation.
#include "openfleet/protocol.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cstring>
#include <random>
#include <string>
#include <vector>

namespace {

using Bytes = std::vector<uint8_t>;

Bytes cobs(const Bytes& in) {
  Bytes out(in.size() + in.size() / 254 + 1);
  const size_t n = ofp_cobs_encode(in.data(), in.size(), out.data(), out.size());
  EXPECT_GT(n, 0U);
  out.resize(n);
  return out;
}

Bytes uncobs(const Bytes& in, bool* ok = nullptr) {
  Bytes out(in.size() + 1);
  size_t n = 0;
  const bool good = ofp_cobs_decode(in.data(), in.size(), out.data(), out.size(), &n);
  if (ok != nullptr) {
    *ok = good;
  }
  out.resize(good ? n : 0);
  return out;
}

ofp_header header(uint8_t type, uint16_t seq = 1, uint16_t len = 0) {
  ofp_header h{};
  h.type = type;
  h.device_id = 7;
  h.seq = seq;
  h.boot_id = 3;
  h.uptime_ms = 123456;
  h.length = len;
  return h;
}

Bytes wire(const ofp_header& h, const uint8_t* payload) {
  Bytes out(OFP_MAX_WIRE);
  const size_t n = ofp_encode(&h, payload, out.data(), out.size());
  EXPECT_GT(n, 0U);
  out.resize(n);
  return out;
}

Bytes sensor_wire(uint16_t seq, uint8_t id = 1, int32_t value = -42) {
  uint8_t p[8];
  const auto len = static_cast<uint16_t>(ofp_build_sensor(p, sizeof p, id, value));
  return wire(header(OFP_SENSOR, seq, len), p);
}

// Feeds bytes; returns every non-NONE result, and the frames decoded.
struct Fed {
  std::vector<ofp_result> results;
  std::vector<ofp_frame> frames;
};

Fed feed(ofp_decoder& d, const Bytes& bytes) {
  Fed out;
  ofp_frame f;
  for (const uint8_t b : bytes) {
    const ofp_result r = ofp_decoder_push(&d, b, &f);
    if (r != OFP_NONE) {
      out.results.push_back(r);
      if (r == OFP_FRAME) {
        out.frames.push_back(f);
      }
    }
  }
  return out;
}

}  // namespace

TEST(Crc16, MatchesTheCcittFalseCheckValue) {
  const char* check = "123456789";
  EXPECT_EQ(ofp_crc16(reinterpret_cast<const uint8_t*>(check), 9), 0x29B1);
  EXPECT_EQ(ofp_crc16(nullptr, 0), 0xFFFF);
}

TEST(Cobs, KnownVectors) {
  // From the COBS paper / Wikipedia examples.
  EXPECT_EQ(cobs({0x00}), (Bytes{0x01, 0x01}));
  EXPECT_EQ(cobs({0x00, 0x00}), (Bytes{0x01, 0x01, 0x01}));
  EXPECT_EQ(cobs({0x11, 0x22, 0x00, 0x33}), (Bytes{0x03, 0x11, 0x22, 0x02, 0x33}));
  EXPECT_EQ(cobs({0x11, 0x00, 0x00, 0x00}), (Bytes{0x02, 0x11, 0x01, 0x01, 0x01}));
  EXPECT_EQ(cobs({}), (Bytes{0x01}));
}

TEST(Cobs, RoundTripsEveryLengthAndZeroPattern) {
  std::mt19937 rng(1);
  for (size_t len = 0; len <= 600; ++len) {
    for (int density : {0, 1, 50, 100}) {  // percent zeros
      Bytes in(len);
      for (auto& b : in) {
        b = static_cast<int>(rng() % 100) < density ? 0 : static_cast<uint8_t>(1 + rng() % 255);
      }
      const Bytes enc = cobs(in);
      EXPECT_EQ(std::count(enc.begin(), enc.end(), 0), 0) << len;
      EXPECT_LE(enc.size(), len + len / 254 + 1);
      bool ok = false;
      EXPECT_EQ(uncobs(enc, &ok), in) << "len " << len << " density " << density;
      EXPECT_TRUE(ok);
    }
  }
}

TEST(Cobs, RejectsInvalidInputAndSmallBuffers) {
  bool ok = true;
  uncobs({0x05, 0x11, 0x22}, &ok);  // the code promises 4 bytes, 2 follow
  EXPECT_FALSE(ok);
  uncobs({0x03, 0x00, 0x22}, &ok);  // a zero inside
  EXPECT_FALSE(ok);
  uint8_t out[2];
  size_t n = 0;
  const uint8_t in[] = {0x04, 1, 2, 3};
  EXPECT_FALSE(ofp_cobs_decode(in, sizeof in, out, sizeof out, &n));
  uint8_t small[2];
  const uint8_t raw[] = {1, 2, 3};
  EXPECT_EQ(ofp_cobs_encode(raw, sizeof raw, small, sizeof small), 0U);
}

TEST(Frame, EncodesWithADelimiterAndDecodesBack) {
  uint8_t p[8];
  const auto len = static_cast<uint16_t>(ofp_build_sensor(p, sizeof p, 4, -12345));
  const Bytes w = wire(header(OFP_SENSOR, 65535, len), p);
  EXPECT_EQ(w.back(), 0);
  EXPECT_EQ(std::count(w.begin(), w.end() - 1, 0), 0);

  ofp_frame f;
  ASSERT_EQ(ofp_decode_frame(w.data(), w.size() - 1, &f), OFP_FRAME);
  EXPECT_EQ(f.header.version, OFP_VERSION);
  EXPECT_EQ(f.header.type, OFP_SENSOR);
  EXPECT_EQ(f.header.device_id, 7);
  EXPECT_EQ(f.header.seq, 65535);
  EXPECT_EQ(f.header.boot_id, 3);
  EXPECT_EQ(f.header.uptime_ms, 123456U);
  ofp_sensor s;
  ASSERT_TRUE(ofp_parse_sensor(&f, &s));
  EXPECT_EQ(s.sensor_id, 4);
  EXPECT_EQ(s.value, -12345);
}

TEST(Frame, LargestPayloadFitsTheWireBuffer) {
  Bytes payload(OFP_MAX_PAYLOAD, 0xAB);
  const Bytes w = wire(header(OFP_LOG, 1, OFP_MAX_PAYLOAD), payload.data());
  EXPECT_LE(w.size(), OFP_MAX_WIRE);
  Bytes zeros(OFP_MAX_PAYLOAD, 0x00);  // zeros cost no extra bytes in COBS
  EXPECT_LE(wire(header(OFP_LOG, 1, OFP_MAX_PAYLOAD), zeros.data()).size(), OFP_MAX_WIRE);

  ofp_header too_big = header(OFP_LOG, 1, OFP_MAX_PAYLOAD + 1);
  Bytes out(OFP_MAX_WIRE * 2);
  Bytes big(OFP_MAX_PAYLOAD + 1);
  EXPECT_EQ(ofp_encode(&too_big, big.data(), out.data(), out.size()), 0U);
  ofp_header ok = header(OFP_LOG, 1, 10);
  EXPECT_EQ(ofp_encode(&ok, big.data(), out.data(), 10), 0U);  // buffer too small
}

TEST(Decoder, HandlesAnyFragmentation) {
  Bytes stream;
  for (uint16_t seq = 0; seq < 50; ++seq) {
    const Bytes w = sensor_wire(seq);
    stream.insert(stream.end(), w.begin(), w.end());
  }
  std::mt19937 rng(7);
  for (int trial = 0; trial < 20; ++trial) {
    ofp_decoder d;
    ofp_decoder_init(&d);
    std::vector<ofp_frame> frames;
    size_t i = 0;
    while (i < stream.size()) {  // random chunk sizes, as a UART or TCP read would give
      const size_t n = std::min<size_t>(stream.size() - i, 1 + rng() % 40);
      const Fed fed = feed(d, Bytes(stream.begin() + static_cast<long>(i),
                                    stream.begin() + static_cast<long>(i + n)));
      frames.insert(frames.end(), fed.frames.begin(), fed.frames.end());
      i += n;
    }
    ASSERT_EQ(frames.size(), 50U);
    for (uint16_t seq = 0; seq < 50; ++seq) {
      EXPECT_EQ(frames[seq].header.seq, seq);
    }
  }
}

TEST(Decoder, IdleDelimitersAreIgnored) {
  ofp_decoder d;
  ofp_decoder_init(&d);
  Bytes stream{0, 0, 0};
  const Bytes w = sensor_wire(1);
  stream.insert(stream.end(), w.begin(), w.end());
  stream.insert(stream.end(), {0, 0});
  const Fed fed = feed(d, stream);
  EXPECT_EQ(fed.results, (std::vector<ofp_result>{OFP_FRAME}));
}

TEST(Decoder, CorruptedFrameIsRejectedAndTheNextOneSurvives) {
  // Flip each bit of a frame in turn: never accepted, and the following frame always decodes.
  const Bytes good = sensor_wire(1);
  const Bytes next = sensor_wire(2);
  for (size_t byte = 0; byte + 1 < good.size(); ++byte) {
    for (int bit = 0; bit < 8; ++bit) {
      Bytes bad = good;
      bad[byte] = static_cast<uint8_t>(bad[byte] ^ (1U << bit));
      bad.insert(bad.end(), next.begin(), next.end());
      ofp_decoder d;
      ofp_decoder_init(&d);
      const Fed fed = feed(d, bad);
      ASSERT_EQ(fed.frames.size(), 1U) << "corrupted frame accepted: byte " << byte << " bit "
                                       << bit;
      EXPECT_EQ(fed.frames[0].header.seq, 2);
      EXPECT_GE(fed.results.size(), 2U);  // at least one error reported before it
    }
  }
}

TEST(Decoder, TruncatedFrameIsShortOrBadCrc) {
  const Bytes good = sensor_wire(1);
  for (size_t keep = 1; keep + 1 < good.size(); ++keep) {
    Bytes cut(good.begin(), good.begin() + static_cast<long>(keep));
    cut.push_back(0);  // the line resynchronises at a delimiter
    ofp_decoder d;
    ofp_decoder_init(&d);
    const Fed fed = feed(d, cut);
    ASSERT_EQ(fed.results.size(), 1U) << keep;
    EXPECT_NE(fed.results[0], OFP_FRAME) << keep;
  }
  // A frame whose tail (and delimiter) is lost merges with the next: both are lost, the
  // stream recovers at the delimiter after that.
  Bytes merged(good.begin(), good.begin() + 6);
  const Bytes second = sensor_wire(2);
  const Bytes third = sensor_wire(3);
  merged.insert(merged.end(), second.begin(), second.end());
  merged.insert(merged.end(), third.begin(), third.end());
  ofp_decoder d;
  ofp_decoder_init(&d);
  const Fed fed = feed(d, merged);
  ASSERT_EQ(fed.frames.size(), 1U);
  EXPECT_EQ(fed.frames[0].header.seq, 3);
}

TEST(Decoder, OversizedInputIsDiscardedUntilTheDelimiter) {
  ofp_decoder d;
  ofp_decoder_init(&d);
  Bytes garbage(OFP_MAX_ENCODED * 3, 0x55);  // no delimiter for far too long
  garbage.push_back(0);
  const Bytes w = sensor_wire(9);
  garbage.insert(garbage.end(), w.begin(), w.end());
  const Fed fed = feed(d, garbage);
  EXPECT_EQ(fed.results, (std::vector<ofp_result>{OFP_ERR_OVERFLOW, OFP_FRAME}));
  EXPECT_EQ(fed.frames[0].header.seq, 9);
}

TEST(Decoder, RandomGarbageNeverYieldsAFrame) {
  std::mt19937 rng(3);
  ofp_decoder d;
  ofp_decoder_init(&d);
  Bytes noise(200'000);
  for (auto& b : noise) {
    b = static_cast<uint8_t>(rng() % 8 == 0 ? 0 : rng());
  }
  const Fed fed = feed(d, noise);
  // A random 16-bit CRC passes about once in 65536 tries, and a random header must also
  // claim version 1 and the right length: in this fixed stream, nothing gets through.
  EXPECT_TRUE(fed.frames.empty());
  EXPECT_GT(fed.results.size(), 1000U);
}

TEST(Decoder, RejectsWrongVersionAndInconsistentLength) {
  // Hand-build raw frames with a valid CRC but bad fields.
  auto raw_frame = [](uint8_t version, uint16_t claimed, size_t actual) {
    Bytes raw(OFP_HEADER_SIZE + actual, 0x11);
    raw[0] = version;
    raw[1] = OFP_LOG;
    raw[12] = static_cast<uint8_t>(claimed & 0xFF);
    raw[13] = static_cast<uint8_t>(claimed >> 8);
    const uint16_t crc = ofp_crc16(raw.data(), raw.size());
    raw.push_back(static_cast<uint8_t>(crc & 0xFF));
    raw.push_back(static_cast<uint8_t>(crc >> 8));
    return cobs(raw);
  };
  ofp_frame f;
  Bytes e = raw_frame(2, 4, 4);
  EXPECT_EQ(ofp_decode_frame(e.data(), e.size(), &f), OFP_ERR_VERSION);
  e = raw_frame(1, 5, 4);
  EXPECT_EQ(ofp_decode_frame(e.data(), e.size(), &f), OFP_ERR_LENGTH);
  e = raw_frame(1, 4, 4);
  EXPECT_EQ(ofp_decode_frame(e.data(), e.size(), &f), OFP_FRAME);
  EXPECT_STREQ(ofp_result_name(OFP_ERR_CRC), "crc");
}

TEST(Payloads, HelloAndLogTextIsBoundedAndTerminated) {
  uint8_t p[OFP_MAX_PAYLOAD];
  ofp_frame f{};
  f.header.type = OFP_HELLO;
  f.header.length = static_cast<uint16_t>(ofp_build_hello(p, sizeof p, OFP_RESET_WATCHDOG, "fw 1.2"));
  std::memcpy(f.payload, p, f.header.length);
  ofp_hello h;
  ASSERT_TRUE(ofp_parse_hello(&f, &h));
  EXPECT_EQ(h.reset_reason, OFP_RESET_WATCHDOG);
  EXPECT_STREQ(h.firmware, "fw 1.2");

  f.payload[3] = 0;  // a NUL inside the text
  EXPECT_FALSE(ofp_parse_hello(&f, &h));
  f.header.length = 0;
  EXPECT_FALSE(ofp_parse_hello(&f, &h));

  const std::string long_text(OFP_MAX_TEXT + 1, 'x');
  EXPECT_EQ(ofp_build_log(p, sizeof p, OFP_LOG_WARN, long_text.c_str()), 0U);
  EXPECT_EQ(ofp_build_log(p, sizeof p, 9, "x"), 0U);  // no such level

  f.header.type = OFP_LOG;
  f.header.length = static_cast<uint16_t>(ofp_build_log(p, sizeof p, OFP_LOG_ERROR, "stack low"));
  std::memcpy(f.payload, p, f.header.length);
  ofp_log l;
  ASSERT_TRUE(ofp_parse_log(&f, &l));
  EXPECT_EQ(l.level, OFP_LOG_ERROR);
  EXPECT_STREQ(l.text, "stack low");
  f.payload[0] = 7;
  EXPECT_FALSE(ofp_parse_log(&f, &l));
  f.header.type = OFP_SENSOR;  // right bytes, wrong type
  EXPECT_FALSE(ofp_parse_log(&f, &l));
}

TEST(Payloads, DescribeNamesAreValidated) {
  uint8_t p[OFP_MAX_PAYLOAD];
  ofp_describe d{OFP_KIND_SENSOR, 2, 500, "imu"};
  ofp_frame f{};
  f.header.type = OFP_DESCRIBE;
  f.header.length = static_cast<uint16_t>(ofp_build_describe(p, sizeof p, &d));
  std::memcpy(f.payload, p, f.header.length);
  ofp_describe out;
  ASSERT_TRUE(ofp_parse_describe(&f, &out));
  EXPECT_EQ(out.kind, OFP_KIND_SENSOR);
  EXPECT_EQ(out.id, 2);
  EXPECT_EQ(out.expected_dhz, 500);
  EXPECT_STREQ(out.name, "imu");

  for (const char* bad : {"", "has space", "a/b", "x\xff"}) {
    ofp_describe b{OFP_KIND_TASK, 1, 0, {}};
    std::strncpy(b.name, bad, OFP_MAX_NAME);
    EXPECT_EQ(ofp_build_describe(p, sizeof p, &b), 0U) << bad;
  }
  ofp_describe kind{9, 1, 0, "x"};
  EXPECT_EQ(ofp_build_describe(p, sizeof p, &kind), 0U);
  f.payload[5] = ' ';  // "imu" -> "i u"
  EXPECT_FALSE(ofp_parse_describe(&f, &out));
}

TEST(Payloads, HealthCountMustMatchTheLength) {
  ofp_health h{};
  h.count = 2;
  h.tasks[0] = {1, OFP_TASK_RUNNING, 120};
  h.tasks[1] = {2, OFP_TASK_ERROR, 0};
  uint8_t p[OFP_MAX_PAYLOAD];
  ofp_frame f{};
  f.header.type = OFP_HEALTH;
  f.header.length = static_cast<uint16_t>(ofp_build_health(p, sizeof p, &h));
  EXPECT_EQ(f.header.length, 9);
  std::memcpy(f.payload, p, f.header.length);
  ofp_health out;
  ASSERT_TRUE(ofp_parse_health(&f, &out));
  EXPECT_EQ(out.count, 2);
  EXPECT_EQ(out.tasks[0].stack_free_words, 120);
  EXPECT_EQ(out.tasks[1].state, OFP_TASK_ERROR);

  f.payload[0] = 3;  // claims 3 entries, carries 2
  EXPECT_FALSE(ofp_parse_health(&f, &out));
  f.payload[0] = 2;
  f.payload[6] = 4;  // no such state
  EXPECT_FALSE(ofp_parse_health(&f, &out));
  h.count = OFP_MAX_HEALTH + 1;
  EXPECT_EQ(ofp_build_health(p, sizeof p, &h), 0U);
  ofp_frame sensor{};
  sensor.header.type = OFP_SENSOR;
  sensor.header.length = 4;  // must be exactly 5
  ofp_sensor s;
  EXPECT_FALSE(ofp_parse_sensor(&sensor, &s));
}

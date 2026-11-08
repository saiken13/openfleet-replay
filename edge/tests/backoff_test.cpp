#include "openfleet/edge/backoff.hpp"

#include <gtest/gtest.h>

#include <set>

using openfleet::edge::Backoff;
using std::chrono::milliseconds;

TEST(Backoff, RejectsBadBounds) {
  EXPECT_THROW(Backoff(milliseconds(0), milliseconds(10)), std::invalid_argument);
  EXPECT_THROW(Backoff(milliseconds(20), milliseconds(10)), std::invalid_argument);
}

TEST(Backoff, DoublesUpToTheCapWithEqualJitter) {
  Backoff b(milliseconds(500), milliseconds(10'000), 42);
  long long base = 500;
  for (int i = 0; i < 12; ++i) {
    const auto d = b.next().count();
    EXPECT_GE(d, base / 2) << "attempt " << i;  // never under half the base...
    EXPECT_LE(d, base) << "attempt " << i;      // ...never over it
    base = std::min(base * 2, 10'000LL);
  }
  EXPECT_EQ(b.base(), milliseconds(10'000));
}

TEST(Backoff, ResetStartsOver) {
  Backoff b(milliseconds(500), milliseconds(10'000), 1);
  b.next();
  b.next();
  b.next();
  b.reset();
  EXPECT_EQ(b.base(), milliseconds(500));
  EXPECT_LE(b.next(), milliseconds(500));
}

TEST(Backoff, JitterSpreadsAFleet) {
  // Agents that failed at the same moment should not retry at the same moment.
  std::set<long long> first_delays;
  for (std::uint64_t seed = 0; seed < 50; ++seed) {
    Backoff b(milliseconds(8'000), milliseconds(8'000), seed);
    first_delays.insert(b.next().count());
  }
  EXPECT_GT(first_delays.size(), 40U);
}

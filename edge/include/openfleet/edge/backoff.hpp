// Exponential backoff with jitter for the uplink's retries.
//
// The base doubles from `initial` up to `max`; each delay is drawn uniformly from
// [base/2, base] ("equal jitter"), so a fleet that lost the backend at the same moment does
// not retry in lockstep, yet no delay is shorter than half the base. reset() after a success.
#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <random>
#include <stdexcept>

namespace openfleet::edge {

class Backoff {
 public:
  using Duration = std::chrono::milliseconds;

  Backoff(Duration initial, Duration max, std::uint64_t seed = std::random_device{}())
      : initial_(initial), max_(max), base_(initial), rng_(seed) {
    if (initial.count() <= 0 || max < initial) {
      throw std::invalid_argument("Backoff needs 0 < initial <= max");
    }
  }

  Duration next() {
    const auto base = base_;
    base_ = std::min(base_ * 2, max_);
    std::uniform_int_distribution<Duration::rep> jitter(base.count() / 2, base.count());
    return Duration(jitter(rng_));
  }

  void reset() { base_ = initial_; }
  [[nodiscard]] Duration base() const { return base_; }

 private:
  Duration initial_;
  Duration max_;
  Duration base_;
  std::mt19937_64 rng_;
};

}  // namespace openfleet::edge

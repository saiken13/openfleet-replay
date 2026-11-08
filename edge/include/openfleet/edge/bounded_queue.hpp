// Bounded multi-producer / multi-consumer queue between the agent's threads.
//
// Design (docs/EMBEDDED_ROADMAP.md, Phase 1):
// - Producers never block. A sampler must keep its period no matter how slow the uplink is,
//   so when the queue is full, push() overwrites the OLDEST item and counts it as dropped.
//   This is the same policy as the Python collector's uplink buffer.
// - Storage is one ring allocated up front: no allocation per push/pop, and memory use is
//   fixed by `capacity`.
// - close() wakes every waiter. Consumers then drain what is left; pop returns nullopt only
//   once the queue is closed AND empty, so a graceful shutdown loses nothing it buffered.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace openfleet::edge {

enum class PushResult : std::uint8_t {
  kOk,             // stored
  kDroppedOldest,  // stored, but the oldest item was overwritten to make room
  kClosed,         // not stored: the queue is closed
};

template <typename T>
class BoundedQueue {
 public:
  explicit BoundedQueue(std::size_t capacity) : slots_(capacity) {
    if (capacity == 0) {
      throw std::invalid_argument("BoundedQueue capacity must be at least 1");
    }
  }

  BoundedQueue(const BoundedQueue&) = delete;
  BoundedQueue& operator=(const BoundedQueue&) = delete;

  // Never blocks.
  PushResult push(T item) {
    PushResult result = PushResult::kOk;
    {
      std::lock_guard lock(mutex_);
      if (closed_) {
        return PushResult::kClosed;
      }
      if (count_ == slots_.size()) {
        head_ = next(head_);  // overwrite the oldest
        --count_;
        ++dropped_;
        result = PushResult::kDroppedOldest;
      }
      slots_[(head_ + count_) % slots_.size()] = std::move(item);
      ++count_;
    }
    not_empty_.notify_one();
    return result;
  }

  // Waits up to `timeout` for an item. nullopt on timeout, or once closed and drained.
  template <typename Rep, typename Period>
  std::optional<T> pop_for(std::chrono::duration<Rep, Period> timeout) {
    std::unique_lock lock(mutex_);
    if (!not_empty_.wait_for(lock, timeout, [this] { return count_ > 0 || closed_; })) {
      return std::nullopt;
    }
    if (count_ == 0) {
      return std::nullopt;  // closed and drained
    }
    // Move the value, not the optional: GCC 15 at -O2 false-positives -Wuninitialized on
    // copy-constructing an optional<T> whose T holds vectors.
    T item = std::move(*slots_[head_]);
    slots_[head_].reset();  // release what the slot held (e.g. a large payload) right away
    head_ = next(head_);
    --count_;
    return item;
  }

  void close() {
    {
      std::lock_guard lock(mutex_);
      closed_ = true;
    }
    not_empty_.notify_all();
  }

  [[nodiscard]] std::size_t capacity() const noexcept { return slots_.size(); }

  [[nodiscard]] std::size_t size() const {
    std::lock_guard lock(mutex_);
    return count_;
  }

  [[nodiscard]] std::uint64_t dropped() const {
    std::lock_guard lock(mutex_);
    return dropped_;
  }

  [[nodiscard]] bool closed() const {
    std::lock_guard lock(mutex_);
    return closed_;
  }

 private:
  [[nodiscard]] std::size_t next(std::size_t i) const noexcept { return (i + 1) % slots_.size(); }

  mutable std::mutex mutex_;
  std::condition_variable not_empty_;
  std::vector<std::optional<T>> slots_;  // sized once, in the constructor
  std::size_t head_ = 0;                 // index of the oldest item
  std::size_t count_ = 0;
  std::uint64_t dropped_ = 0;
  bool closed_ = false;
};

}  // namespace openfleet::edge

#include "openfleet/edge/bounded_queue.hpp"

#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <memory>
#include <thread>
#include <vector>

using namespace std::chrono_literals;
using openfleet::edge::BoundedQueue;
using openfleet::edge::PushResult;

TEST(BoundedQueue, RejectsZeroCapacity) {
  EXPECT_THROW(BoundedQueue<int>(0), std::invalid_argument);
}

TEST(BoundedQueue, IsFifo) {
  BoundedQueue<int> q(3);
  EXPECT_EQ(q.push(1), PushResult::kOk);
  EXPECT_EQ(q.push(2), PushResult::kOk);
  EXPECT_EQ(q.pop_for(0ms), 1);
  EXPECT_EQ(q.push(3), PushResult::kOk);
  EXPECT_EQ(q.pop_for(0ms), 2);
  EXPECT_EQ(q.pop_for(0ms), 3);
  EXPECT_EQ(q.pop_for(0ms), std::nullopt);
}

TEST(BoundedQueue, FullQueueDropsTheOldestAndNeverBlocks) {
  BoundedQueue<int> q(2);
  q.push(1);
  q.push(2);
  EXPECT_EQ(q.push(3), PushResult::kDroppedOldest);
  EXPECT_EQ(q.push(4), PushResult::kDroppedOldest);
  EXPECT_EQ(q.dropped(), 2U);
  EXPECT_EQ(q.size(), 2U);
  EXPECT_EQ(q.pop_for(0ms), 3);
  EXPECT_EQ(q.pop_for(0ms), 4);
}

TEST(BoundedQueue, PopTimesOutWhenEmpty) {
  BoundedQueue<int> q(1);
  const auto start = std::chrono::steady_clock::now();
  EXPECT_EQ(q.pop_for(30ms), std::nullopt);
  EXPECT_GE(std::chrono::steady_clock::now() - start, 30ms);
}

TEST(BoundedQueue, CloseWakesWaitersAndLetsThemDrain) {
  BoundedQueue<int> q(4);
  q.push(7);
  q.close();
  EXPECT_EQ(q.push(8), PushResult::kClosed);
  EXPECT_EQ(q.pop_for(1s), 7);              // what was buffered is still delivered
  EXPECT_EQ(q.pop_for(1s), std::nullopt);   // then: closed and empty, immediately

  BoundedQueue<int> waiting(1);
  std::thread consumer([&] { EXPECT_EQ(waiting.pop_for(10s), std::nullopt); });
  std::this_thread::sleep_for(20ms);
  const auto start = std::chrono::steady_clock::now();
  waiting.close();
  consumer.join();
  EXPECT_LT(std::chrono::steady_clock::now() - start, 5s);  // woken, not timed out
}

TEST(BoundedQueue, HoldsMoveOnlyTypesAndReleasesPoppedSlots) {
  BoundedQueue<std::unique_ptr<int>> q(2);
  auto shared = std::make_shared<int>(1);
  BoundedQueue<std::shared_ptr<int>> owners(1);
  owners.push(shared);
  EXPECT_EQ(shared.use_count(), 2);
  owners.pop_for(0ms);
  EXPECT_EQ(shared.use_count(), 1);  // the popped slot no longer holds a reference

  q.push(std::make_unique<int>(42));
  auto item = q.pop_for(0ms);
  ASSERT_TRUE(item.has_value());
  EXPECT_EQ(**item, 42);
}

// Run under ThreadSanitizer in CI (preset `tsan`). Every item is either received or counted
// as dropped, exactly once, with producers racing a consumer and each other.
TEST(BoundedQueue, ConcurrentProducersLoseNothingUnaccounted) {
  constexpr int kProducers = 4;
  constexpr int kPerProducer = 20'000;
  BoundedQueue<int> q(64);
  std::atomic<long long> received{0};

  std::thread consumer([&] {
    while (auto item = q.pop_for(1s)) {
      received.fetch_add(1, std::memory_order_relaxed);
    }
  });
  std::vector<std::thread> producers;
  producers.reserve(kProducers);
  for (int p = 0; p < kProducers; ++p) {
    producers.emplace_back([&q, p] {
      for (int i = 0; i < kPerProducer; ++i) {
        q.push(p * kPerProducer + i);
      }
    });
  }
  for (auto& t : producers) {
    t.join();
  }
  q.close();
  consumer.join();

  EXPECT_EQ(received.load() + static_cast<long long>(q.dropped()),
            static_cast<long long>(kProducers) * kPerProducer);
}

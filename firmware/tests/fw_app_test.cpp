// The firmware's portable logic on the host: the task supervisor and the boot record.
extern "C" {
#include "boot_record.h"
#include "supervisor.h"
}

#include <gtest/gtest.h>

#include <cstring>

#include "openfleet/protocol.h"

TEST(Supervisor, KicksWhileEveryTaskChecksIn) {
  supervisor s;
  sup_init(&s);
  ASSERT_TRUE(sup_register(&s, 1, 200, 0));
  ASSERT_TRUE(sup_register(&s, 2, 500, 0));
  for (uint32_t t = 0; t <= 2000; t += 100) {
    sup_checkin(&s, 1, t);
    sup_checkin(&s, 2, t);
    EXPECT_TRUE(sup_may_kick(&s, t, nullptr)) << t;
  }
}

TEST(Supervisor, ALateTaskTripsAndStaysTripped) {
  supervisor s;
  sup_init(&s);
  sup_register(&s, 1, 200, 0);
  sup_register(&s, 3, 3000, 0);
  sup_checkin(&s, 1, 100);
  EXPECT_TRUE(sup_may_kick(&s, 300, nullptr));  // 200 ms since check-in: on the limit
  uint8_t stalled = 0;
  EXPECT_FALSE(sup_may_kick(&s, 301, &stalled));
  EXPECT_EQ(stalled, 1);
  sup_checkin(&s, 1, 302);  // checking in again doesn't undo it: the reset is the recovery
  EXPECT_FALSE(sup_may_kick(&s, 303, &stalled));
  EXPECT_EQ(stalled, 1);
}

TEST(Supervisor, SurvivesTheMillisecondWrap) {
  supervisor s;
  sup_init(&s);
  const uint32_t near_wrap = 0xFFFFFF00u;
  sup_register(&s, 1, 500, near_wrap);
  sup_checkin(&s, 1, near_wrap + 100);
  EXPECT_TRUE(sup_may_kick(&s, near_wrap + 400, nullptr));  // wrapped past 0
  EXPECT_FALSE(sup_may_kick(&s, near_wrap + 700, nullptr));
}

TEST(Supervisor, RejectsBadRegistrations) {
  supervisor s;
  sup_init(&s);
  EXPECT_FALSE(sup_register(&s, SUP_MAX_TASKS, 100, 0));
  EXPECT_FALSE(sup_register(&s, 1, 0, 0));
  EXPECT_TRUE(sup_may_kick(&s, 1'000'000, nullptr));  // nothing registered: nothing late
}

namespace {
boot_record noise() {
  boot_record r;
  std::memset(&r, 0xA5, sizeof r);  // RAM after power-on
  return r;
}
}  // namespace

TEST(BootRecord, PowerOnSeedsARandomBootId) {
  boot_record r = noise();
  const boot_info b = boot_record_on_boot(&r, RCC_CSR_PORRSTF, 0x1234ABCD);
  EXPECT_EQ(b.reason, OFP_RESET_POWER_ON);
  EXPECT_EQ(b.boot_id, 0xABCD);
  EXPECT_EQ(b.boot_count, 1U);
  EXPECT_FALSE(b.stall_known);
}

TEST(BootRecord, NoiseThatLooksLikeTheMagicIsStillRejected) {
  boot_record r = noise();
  r.magic = BOOT_RECORD_MAGIC;  // magic right, seal wrong
  EXPECT_EQ(boot_record_on_boot(&r, 0, 7).boot_count, 1U);
}

TEST(BootRecord, WatchdogResetNamedBySupervisorIsReportedNextBoot) {
  boot_record r = noise();
  boot_record_on_boot(&r, RCC_CSR_PORRSTF, 41);
  boot_record_note_uptime(&r, 6000);
  boot_record_note_watchdog(&r, 1, 7000);
  // Renode doesn't model the reset flags: RCC_CSR still reads POR|PIN|BOR.
  const boot_info b = boot_record_on_boot(&r, RCC_CSR_PORRSTF | RCC_CSR_PINRSTF, 999);
  EXPECT_EQ(b.reason, OFP_RESET_WATCHDOG);
  EXPECT_TRUE(b.stall_known);
  EXPECT_EQ(b.stalled_task, 1);
  EXPECT_EQ(b.prev_uptime_ms, 7000U);
  EXPECT_EQ(b.boot_id, 42);  // +1, not re-randomised
  EXPECT_EQ(b.boot_count, 2U);
  // The note is consumed: the boot after that is not a watchdog reset again.
  EXPECT_EQ(boot_record_on_boot(&r, RCC_CSR_SFTRSTF, 0).reason, OFP_RESET_SOFTWARE);
}

TEST(BootRecord, HardwareFlagsGiveTheReasonOtherwise) {
  struct {
    uint32_t csr;
    uint8_t reason;
  } cases[] = {{RCC_CSR_IWDGRSTF, OFP_RESET_WATCHDOG}, {RCC_CSR_WWDGRSTF, OFP_RESET_WATCHDOG},
               {RCC_CSR_SFTRSTF, OFP_RESET_SOFTWARE}, {RCC_CSR_PINRSTF, OFP_RESET_PIN},
               {RCC_CSR_PORRSTF | RCC_CSR_PINRSTF, OFP_RESET_POWER_ON}, {0, OFP_RESET_UNKNOWN}};
  for (const auto& c : cases) {
    boot_record r = noise();
    boot_record_on_boot(&r, 0, 5);
    const boot_info b = boot_record_on_boot(&r, c.csr, 5);
    EXPECT_EQ(b.reason, c.reason) << std::hex << c.csr;
    EXPECT_FALSE(b.stall_known);
  }
}

TEST(BootRecord, BootIdNeverZero) {
  boot_record r = noise();
  EXPECT_EQ(boot_record_on_boot(&r, 0, 0).boot_id, 1);  // random 0 -> 1
  r.boot_id = 0xFFFF;
  boot_record_note_uptime(&r, 1);  // reseal after the direct edit
  EXPECT_EQ(boot_record_on_boot(&r, 0, 0).boot_id, 1);  // wrap skips 0
}

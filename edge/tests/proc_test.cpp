// /proc parsers and the sampler, against fixtures saved from a real Linux container
// (tests/fixtures/proc: Ubuntu 24.04 under Docker Desktop on arm64) plus edge cases.
#include "openfleet/edge/proc.hpp"
#include "openfleet/edge/sampler.hpp"
#include "openfleet/edge/uplink.hpp"

#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>

namespace fs = std::filesystem;
using namespace openfleet::edge;
using namespace openfleet::edge::proc;

namespace {

const fs::path kFixtures = fs::path(EDGE_FIXTURES_DIR) / "proc";

std::string fixture(const std::string& name) {
  const auto text = read_file(kFixtures / name);
  if (!text) {
    throw std::runtime_error("missing fixture " + name);
  }
  return *text;
}

// A writable copy of the fixture tree, so tests can change files between samples.
class ProcTree {
 public:
  ProcTree() : root_(fs::temp_directory_path() / ("edge-proc-" + Uplink::random_uuid())) {
    fs::copy(kFixtures, root_, fs::copy_options::recursive);
  }
  ~ProcTree() {
    std::error_code ec;
    fs::remove_all(root_, ec);
  }
  ProcTree(const ProcTree&) = delete;
  ProcTree& operator=(const ProcTree&) = delete;
  [[nodiscard]] const fs::path& root() const { return root_; }
  void write(const std::string& rel, const std::string& text) const {
    fs::create_directories((root_ / rel).parent_path());
    std::ofstream(root_ / rel) << text;
  }
  void remove(const std::string& rel) const { fs::remove_all(root_ / rel); }

 private:
  fs::path root_;
};

}  // namespace

TEST(Proc, ParsesTheAggregateCpuLineOfARealProcStat) {
  const auto t = parse_cpu_times(fixture("stat"));
  ASSERT_TRUE(t.has_value());
  // user nice system idle iowait irq softirq steal = 80491 0 28080 3531613 1999 0 8849 0
  EXPECT_EQ(t->total, 80491U + 28080U + 3531613U + 1999U + 8849U);
  EXPECT_EQ(t->busy, 80491U + 28080U + 8849U);  // idle and iowait are not busy
}

TEST(Proc, GuestTimeIsNotCountedTwice) {
  // guest (9th) and guest_nice (10th) are already inside user and nice.
  const auto t = parse_cpu_times("cpu  100 0 0 100 0 0 0 0 50 50\n");
  ASSERT_TRUE(t.has_value());
  EXPECT_EQ(t->total, 200U);
  EXPECT_EQ(t->busy, 100U);
}

TEST(Proc, OldKernelsWithFourCpuFieldsStillParse) {
  const auto t = parse_cpu_times("cpu 10 0 10 80\n");
  ASSERT_TRUE(t.has_value());
  EXPECT_EQ(t->total, 100U);
  EXPECT_EQ(t->busy, 20U);
}

TEST(Proc, RejectsMalformedStat) {
  EXPECT_FALSE(parse_cpu_times(""));
  EXPECT_FALSE(parse_cpu_times("cpu0 1 2 3 4\n"));  // per-CPU line, not the aggregate
  EXPECT_FALSE(parse_cpu_times("cpu 1 2 x 4\n"));
  EXPECT_FALSE(parse_cpu_times("cpu 1 2 3\n"));
  EXPECT_FALSE(parse_cpu_times("cpu 1 2 3 -4\n"));
}

TEST(Proc, CpuPercentIsTheBusyShareOfTheDelta) {
  EXPECT_DOUBLE_EQ(*cpu_pct({100, 1000}, {175, 1100}), 75.0);
  EXPECT_DOUBLE_EQ(*cpu_pct({100, 1000}, {100, 1100}), 0.0);
  EXPECT_FALSE(cpu_pct({100, 1000}, {100, 1000}));  // no time passed
  EXPECT_FALSE(cpu_pct({100, 1000}, {90, 1100}));   // counter went backwards
  EXPECT_FALSE(cpu_pct({100, 1000}, {200, 900}));
}

TEST(Proc, MemoryUsesMemAvailable) {
  const auto pct = mem_pct(fixture("meminfo"));
  ASSERT_TRUE(pct.has_value());
  EXPECT_NEAR(*pct, 100.0 * (4010356.0 - 3105116.0) / 4010356.0, 1e-9);
}

TEST(Proc, MemoryFallsBackWithoutMemAvailable) {
  // MemFree + Buffers + Cached = 400000 of 1000000.
  EXPECT_NEAR(*mem_pct(fixture("meminfo_legacy")), 60.0, 1e-9);
  EXPECT_FALSE(mem_pct("MemTotal: 1000 kB\nMemFree: 10 kB\n"));  // not enough to tell
  EXPECT_FALSE(mem_pct("MemTotal: 0 kB\nMemAvailable: 0 kB\n"));
  EXPECT_FALSE(mem_pct(""));
}

TEST(Proc, Uptime) {
  EXPECT_DOUBLE_EQ(*parse_uptime(fixture("uptime")), 4573.96);
  EXPECT_FALSE(parse_uptime(""));
  EXPECT_FALSE(parse_uptime("abc 1\n"));
}

TEST(Proc, PidStatCommRunsToTheLastParenthesis) {
  const std::string odd = fixture("23/stat");  // PidStat views into the text: keep it alive
  auto st = parse_pid_stat(odd);
  ASSERT_TRUE(st.has_value());
  EXPECT_EQ(st->comm, "my (odd) worker");
  EXPECT_EQ(st->state, 'R');

  const std::string init = fixture("1/stat");
  st = parse_pid_stat(init);
  ASSERT_TRUE(st.has_value());
  EXPECT_EQ(st->comm, "bash");
  EXPECT_EQ(st->state, 'S');

  EXPECT_FALSE(parse_pid_stat("not a pid dir; the scanner must skip it"));
  EXPECT_FALSE(parse_pid_stat("12 (truncated"));
  EXPECT_FALSE(parse_pid_stat("12 (x)"));
  EXPECT_FALSE(parse_pid_stat("abc (x) S 1"));
}

TEST(Proc, ProcessStateFromAllMatches) {
  EXPECT_EQ(process_state({}), NodeState::kStopped);
  EXPECT_EQ(process_state({'S'}), NodeState::kRunning);
  EXPECT_EQ(process_state({'Z', 'D'}), NodeState::kRunning);  // one live instance is enough
  EXPECT_EQ(process_state({'T'}), NodeState::kDegraded);
  EXPECT_EQ(process_state({'Z', 't'}), NodeState::kDegraded);
  EXPECT_EQ(process_state({'Z'}), NodeState::kError);
  EXPECT_EQ(process_state({'X'}), NodeState::kError);
}

TEST(Proc, ScanFindsProcessesByCommAndSkipsNonPidEntries) {
  const auto found = scan_processes(kFixtures, {"sleep", "bash", "my (odd) worker", "absent"});
  EXPECT_EQ(found.at("sleep").size(), 2U);  // pid 11 (S) and pid 31 (zombie)
  EXPECT_EQ(found.at("bash"), std::vector<char>{'S'});
  EXPECT_EQ(found.at("my (odd) worker"), std::vector<char>{'R'});
  EXPECT_TRUE(found.at("absent").empty());
  EXPECT_TRUE(scan_processes("/definitely/not/here", {"x"}).at("x").empty());
}

TEST(Proc, LongNamesMatchTheKernelsTruncatedComm) {
  ProcTree tree;
  tree.write("40/stat", "40 (a-very-long-pro) S 1 1 1 0");  // comm keeps 15 characters
  const auto found = scan_processes(tree.root(), {"a-very-long-process-name"});
  EXPECT_EQ(found.at("a-very-long-process-name"), std::vector<char>{'S'});
}

TEST(Sampler, CpuNeedsTwoReadingsThenTracksTheDelta) {
  ProcTree tree;
  Sampler sampler({tree.root(), {}, std::chrono::milliseconds(0)});
  const auto first = sampler.sample();
  EXPECT_FALSE(first.cpu_pct.has_value());
  ASSERT_TRUE(first.mem_pct.has_value());
  ASSERT_EQ(first.logs.size(), 1U);  // the "started" line, once
  EXPECT_NE(first.logs[0].message.find("host up 4574 s"), std::string::npos);

  // +300 busy (user) and +100 idle ticks: 75 %.
  tree.write("stat", "cpu  80791 0 28080 3531713 1999 0 8849 0 0 0\n");
  const auto second = sampler.sample();
  ASSERT_TRUE(second.cpu_pct.has_value());
  EXPECT_NEAR(*second.cpu_pct, 75.0, 1e-9);
  EXPECT_TRUE(second.logs.empty());
}

TEST(Sampler, MissingFilesGiveNullMetricsNotErrors) {
  ProcTree tree;
  tree.remove("meminfo");
  tree.write("stat", "garbage\n");
  Sampler sampler({tree.root(), {}, std::chrono::milliseconds(0)});
  sampler.sample();
  const auto s = sampler.sample();
  EXPECT_FALSE(s.cpu_pct.has_value());
  EXPECT_FALSE(s.mem_pct.has_value());
}

TEST(Sampler, WatchedProcessesBecomeNodes) {
  ProcTree tree;
  Sampler sampler({tree.root(), {"sleep", "bash", "gone"}, std::chrono::milliseconds(0)});
  auto s = sampler.sample();
  ASSERT_EQ(s.nodes.size(), 3U);
  EXPECT_EQ(s.nodes[0], (NodeReport{"bash", NodeState::kRunning}));
  EXPECT_EQ(s.nodes[1], (NodeReport{"gone", NodeState::kStopped}));
  EXPECT_EQ(s.nodes[2], (NodeReport{"sleep", NodeState::kRunning}));

  tree.remove("11");  // the live sleep exits; only the zombie is left
  s = sampler.sample();
  EXPECT_EQ(s.nodes[2], (NodeReport{"sleep", NodeState::kError}));
}

TEST(Sampler, MissingProcessesAreLeftOutDuringStartupGrace) {
  ProcTree tree;
  Sampler sampler({tree.root(), {"bash", "gone"}, std::chrono::hours(1)});
  const auto s = sampler.sample();
  ASSERT_EQ(s.nodes.size(), 1U);
  EXPECT_EQ(s.nodes[0].node, "bash");
}

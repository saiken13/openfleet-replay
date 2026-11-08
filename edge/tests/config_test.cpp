#include "openfleet/edge/agent.hpp"

#include <gtest/gtest.h>

#include <map>

using namespace openfleet::edge;

namespace {

ParseResult parse(std::vector<std::string_view> args,
                  std::map<std::string, std::string> env = {{"OPENFLEET_API_KEY", "k"}}) {
  return parse_args(args, [&env](const char* name) -> const char* {
    const auto it = env.find(name);
    return it == env.end() ? nullptr : it->second.c_str();
  });
}

}  // namespace

TEST(Config, Defaults) {
  const auto r = parse({"--robot-id", "edge-01"});
  ASSERT_TRUE(r.config) << r.message;
  EXPECT_EQ(r.config->api_url, "http://localhost:8000");
  EXPECT_EQ(r.config->api_key, "k");
  EXPECT_EQ(r.config->uplink.robot_id, "edge-01");
  EXPECT_EQ(r.config->uplink.buffer_max, 600U);
  EXPECT_EQ(r.config->period, Millis(1000));
  EXPECT_EQ(r.config->duration, Millis(0));
  EXPECT_EQ(r.config->sampler.proc_root, "/proc");
}

TEST(Config, AllOptions) {
  const auto r = parse({"--api", "https://fleet.example/", "--robot-id", "edge-01", "--name",
                        "Edge", "--model", "pi", "--software-version", "1.2", "--process", "a",
                        "--process", "b", "--period", "0.5", "--startup-grace", "0",
                        "--buffer-max", "20", "--timeout", "2", "--backoff-max", "3",
                        "--duration", "60", "--proc-root", "/host/proc"});
  ASSERT_TRUE(r.config) << r.message;
  const auto& c = *r.config;
  EXPECT_EQ(c.api_url, "https://fleet.example");  // trailing slash removed
  EXPECT_EQ(c.uplink.robot_name, "Edge");
  EXPECT_EQ(c.uplink.robot_model, "pi");
  EXPECT_EQ(c.uplink.software_version, "1.2");
  EXPECT_EQ(c.sampler.processes, (std::vector<std::string>{"a", "b"}));
  EXPECT_EQ(c.period, Millis(500));
  EXPECT_EQ(c.sampler.startup_grace, Millis(0));
  EXPECT_EQ(c.uplink.buffer_max, 20U);
  EXPECT_EQ(c.request_timeout, Millis(2000));
  EXPECT_EQ(c.backoff_max, Millis(3000));
  EXPECT_EQ(c.duration, Millis(60000));
  EXPECT_EQ(c.sampler.proc_root, "/host/proc");
}

TEST(Config, AcceptsFlagEqualsValue) {
  const auto r = parse({"--robot-id=edge-01", "--name=Edge agent 01", "--process=a=b"});
  ASSERT_TRUE(r.config) << r.message;
  EXPECT_EQ(r.config->uplink.robot_id, "edge-01");
  EXPECT_EQ(r.config->uplink.robot_name, "Edge agent 01");
  EXPECT_EQ(r.config->sampler.processes, (std::vector<std::string>{"a=b"}));  // first '=' splits
  EXPECT_FALSE(parse({"--robot-id="}).config);  // empty id
}

TEST(Config, ApiUrlFromEnvironment) {
  const auto r = parse({"--robot-id", "x"},
                       {{"OPENFLEET_API_KEY", "k"}, {"OPENFLEET_API_URL", "http://backend:8000"}});
  ASSERT_TRUE(r.config);
  EXPECT_EQ(r.config->api_url, "http://backend:8000");
}

TEST(Config, TheKeyComesOnlyFromTheEnvironment) {
  auto r = parse({"--robot-id", "edge-01"}, {});
  EXPECT_FALSE(r.config);
  EXPECT_EQ(r.exit_code, 2);
  EXPECT_NE(r.message.find("openfleet keys create edge-01"), std::string::npos);
  r = parse({"--robot-id", "edge-01", "--api-key", "k"});
  EXPECT_FALSE(r.config);  // no such flag: argv is visible to every user via /proc
}

TEST(Config, RejectsBadInput) {
  for (const auto& args : std::vector<std::vector<std::string_view>>{
           {},
           {"--robot-id", "-bad"},
           {"--robot-id", "has space"},
           {"--robot-id", "x", "--period", "0"},
           {"--robot-id", "x", "--period", "nan"},
           {"--robot-id", "x", "--period", "1s"},
           {"--robot-id", "x", "--buffer-max", "5"},
           {"--robot-id", "x", "--buffer-max", "10.5"},
           {"--robot-id", "x", "--api", "ftp://x"},
           {"--robot-id", "x", "--process", ""},
           {"--robot-id", "x", "--bogus", "1"},
           {"--robot-id"},
       }) {
    const auto r = parse(args);
    EXPECT_FALSE(r.config) << (args.empty() ? "(none)" : args.back());
    EXPECT_EQ(r.exit_code, 2);
  }
}

TEST(Config, HelpAndVersionExitZero) {
  auto r = parse({"--help"});
  EXPECT_FALSE(r.config);
  EXPECT_EQ(r.exit_code, 0);
  EXPECT_NE(r.message.find("--robot-id"), std::string::npos);
  r = parse({"--version"});
  EXPECT_EQ(r.exit_code, 0);
  EXPECT_NE(r.message.find("openfleet-edge-agent"), std::string::npos);
}

// openfleet-edge-agent: see `openfleet-edge-agent --help` and edge/README.md.
#include <curl/curl.h>

#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <string_view>
#include <vector>

#include "openfleet/edge/agent.hpp"
#include "openfleet/edge/curl_client.hpp"
#include "openfleet/edge/log.hpp"

int main(int argc, char** argv) {
  using namespace openfleet::edge;
  std::vector<std::string_view> args(argv + 1, argv + argc);
  auto parsed = parse_args(args, [](const char* name) { return std::getenv(name); });
  if (!parsed.config) {
    std::fprintf(parsed.exit_code == 0 ? stdout : stderr, "%s\n", parsed.message.c_str());
    return parsed.exit_code;
  }
  // Process-wide and not thread-safe: before any thread exists.
  if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
    std::fprintf(stderr, "error: curl_global_init failed\n");
    return 1;
  }
  int code = 1;
  try {
    CurlClient http(parsed.config->api_url, parsed.config->api_key,
                    parsed.config->request_timeout);
    code = run_agent(*parsed.config, http);
  } catch (const std::exception& e) {
    log::error(std::string("fatal: ") + e.what());
  }
  curl_global_cleanup();
  return code;
}

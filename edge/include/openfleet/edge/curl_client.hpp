// HttpClient over libcurl. One easy handle, reused, so the connection is kept alive between
// batches. Used by one thread only (the uplink loop).
#pragma once

#include <chrono>
#include <memory>
#include <string>

#include "openfleet/edge/http.hpp"

namespace openfleet::edge {

class CurlClient final : public HttpClient {
 public:
  // curl_global_init must have been called (main does it before starting threads).
  CurlClient(std::string base_url, const std::string& api_key, std::chrono::milliseconds timeout);
  ~CurlClient() override;
  CurlClient(const CurlClient&) = delete;
  CurlClient& operator=(const CurlClient&) = delete;

  std::optional<HttpResponse> send(std::string_view method, std::string_view path,
                                   std::string_view json_body) override;
  [[nodiscard]] std::string last_error() const override { return last_error_; }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::string base_url_;
  std::string last_error_;
};

}  // namespace openfleet::edge

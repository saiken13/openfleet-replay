// A scripted HttpClient: records every request, answers with `handler` (default: success).
// Thread-safe, so a test can read what was sent while the uplink thread is still running.
#pragma once

#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "openfleet/edge/http.hpp"

namespace openfleet::edge::testing {

struct Request {
  std::string method;
  std::string path;
  nlohmann::json body;
};

class FakeHttp final : public HttpClient {
 public:
  using Handler = std::function<std::optional<HttpResponse>(const Request&)>;

  std::optional<HttpResponse> send(std::string_view method, std::string_view path,
                                   std::string_view json_body) override {
    Request r{std::string(method), std::string(path), nlohmann::json::parse(json_body)};
    Handler h;
    {
      const std::lock_guard lock(mutex_);
      requests_.push_back(r);
      h = handler_;
    }
    return h ? h(r) : ok();
  }
  [[nodiscard]] std::string last_error() const override { return "fake: connection refused"; }

  void set_handler(Handler h) {
    const std::lock_guard lock(mutex_);
    handler_ = std::move(h);
  }
  std::vector<Request> requests() const {
    const std::lock_guard lock(mutex_);
    return requests_;
  }
  // Telemetry POSTs only.
  std::vector<Request> telemetry() const {
    std::vector<Request> out;
    for (auto& r : requests()) {
      if (r.path.ends_with("/telemetry")) {
        out.push_back(r);
      }
    }
    return out;
  }
  void clear() {
    const std::lock_guard lock(mutex_);
    requests_.clear();
  }

  static std::optional<HttpResponse> ok(long status = 200) {
    return HttpResponse{status, R"({"accepted":true,"duplicate":false})", 0.004};
  }
  static std::optional<HttpResponse> status(long code, std::string body = "{}") {
    return HttpResponse{code, std::move(body), 0.001};
  }
  static std::optional<HttpResponse> down() { return std::nullopt; }

 private:
  mutable std::mutex mutex_;
  std::vector<Request> requests_;
  Handler handler_;
};

}  // namespace openfleet::edge::testing

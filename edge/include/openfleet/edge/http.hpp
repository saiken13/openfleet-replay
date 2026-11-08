// The uplink's only view of the network: send one JSON request, get a status or nothing.
// The real implementation is libcurl (curl_client.hpp); tests script a fake.
#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace openfleet::edge {

struct HttpResponse {
  long status = 0;
  std::string body;
  double elapsed_s = 0;  // full request time, used as the link latency
};

class HttpClient {
 public:
  virtual ~HttpClient() = default;
  // nullopt: transport failure (refused, DNS, timeout, TLS). last_error() says which.
  virtual std::optional<HttpResponse> send(std::string_view method, std::string_view path,
                                           std::string_view json_body) = 0;
  [[nodiscard]] virtual std::string last_error() const { return {}; }
};

}  // namespace openfleet::edge

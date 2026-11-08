#include "openfleet/edge/curl_client.hpp"

#include <curl/curl.h>

#include <stdexcept>

#ifndef OPENFLEET_EDGE_VERSION
#define OPENFLEET_EDGE_VERSION "0.0.0"
#endif

namespace openfleet::edge {

struct CurlClient::Impl {
  CURL* handle = nullptr;
  curl_slist* headers = nullptr;
  char error[CURL_ERROR_SIZE] = {};
};

namespace {

std::size_t collect(char* data, std::size_t size, std::size_t n, void* user) {
  static_cast<std::string*>(user)->append(data, size * n);
  return size * n;
}

}  // namespace

CurlClient::CurlClient(std::string base_url, const std::string& api_key,
                       std::chrono::milliseconds timeout)
    : impl_(std::make_unique<Impl>()), base_url_(std::move(base_url)) {
  impl_->handle = curl_easy_init();
  if (impl_->handle == nullptr) {
    throw std::runtime_error("curl_easy_init failed");
  }
  impl_->headers = curl_slist_append(impl_->headers, "Content-Type: application/json");
  impl_->headers = curl_slist_append(impl_->headers, "Accept: application/json");
  impl_->headers =
      curl_slist_append(impl_->headers, ("Authorization: Bearer " + api_key).c_str());
  CURL* h = impl_->handle;
  curl_easy_setopt(h, CURLOPT_HTTPHEADER, impl_->headers);
  curl_easy_setopt(h, CURLOPT_USERAGENT, "openfleet-edge-agent/" OPENFLEET_EDGE_VERSION);
  curl_easy_setopt(h, CURLOPT_TIMEOUT_MS, static_cast<long>(timeout.count()));
  curl_easy_setopt(h, CURLOPT_CONNECTTIMEOUT_MS, static_cast<long>(timeout.count()));
  curl_easy_setopt(h, CURLOPT_NOSIGNAL, 1L);  // required with threads: no SIGALRM timeouts
  curl_easy_setopt(h, CURLOPT_ERRORBUFFER, impl_->error);
  curl_easy_setopt(h, CURLOPT_WRITEFUNCTION, collect);
  curl_easy_setopt(h, CURLOPT_FOLLOWLOCATION, 0L);
}

CurlClient::~CurlClient() {
  curl_slist_free_all(impl_->headers);
  curl_easy_cleanup(impl_->handle);
}

std::optional<HttpResponse> CurlClient::send(std::string_view method, std::string_view path,
                                             std::string_view json_body) {
  CURL* h = impl_->handle;
  const std::string url = base_url_ + std::string(path);
  const std::string verb(method);
  HttpResponse resp;
  impl_->error[0] = '\0';
  curl_easy_setopt(h, CURLOPT_URL, url.c_str());
  curl_easy_setopt(h, CURLOPT_CUSTOMREQUEST, verb.c_str());
  curl_easy_setopt(h, CURLOPT_POSTFIELDS, json_body.data());
  curl_easy_setopt(h, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(json_body.size()));
  curl_easy_setopt(h, CURLOPT_WRITEDATA, &resp.body);

  const CURLcode rc = curl_easy_perform(h);
  if (rc != CURLE_OK) {
    last_error_ = impl_->error[0] != '\0' ? impl_->error : curl_easy_strerror(rc);
    return std::nullopt;
  }
  curl_easy_getinfo(h, CURLINFO_RESPONSE_CODE, &resp.status);
  curl_easy_getinfo(h, CURLINFO_TOTAL_TIME, &resp.elapsed_s);
  last_error_.clear();
  return resp;
}

}  // namespace openfleet::edge

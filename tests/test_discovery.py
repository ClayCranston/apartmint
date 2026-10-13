import io
import json
import socket
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from apartmint import discovery
from apartmint.config import SafeError


def row(url="https://property.example/tour", title="City Apartments", snippet="Schedule a tour of our apartments."):
    return f'<a class="result-link" href="{url}">{title}</a><td class="result-snippet">{snippet}</td>'


@pytest.fixture
def public_dns(monkeypatch):
    seen = []
    def resolve(host, port, *, public):
        assert public and port == 443
        seen.append(host)
        return ["93.184.216.34"]
    monkeypatch.setattr(discovery, "resolve_addresses", resolve)
    return seen


@pytest.mark.parametrize("city", ["", " ", None, True, "x" * 201, "City\nsecret", "City\x00", "City\u202e"])
def test_bad_city_never_connects(monkeypatch, city):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: pytest.fail("Invalid input reached network"))
    with pytest.raises(SafeError):
        discovery.discover_properties(city)


@pytest.mark.parametrize("limit", [0, 11, True, 1.5, "5"])
def test_bad_limit_never_connects(monkeypatch, limit):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: pytest.fail("Invalid limit reached network"))
    with pytest.raises(SafeError):
        discovery.discover_properties("Any City", limit)


def test_known_wrapper_decodes_offline_and_only_candidates_are_resolved(monkeypatch, public_dns):
    target = "https://property.example/tour?unit=1&beds=2"
    wrapped = "//duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&amp;rut=ignored"
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: row(wrapped))
    result = discovery.discover_properties("  Montréal,   QC  ")
    assert result == {"city":"Montréal, QC", "provider":"duckduckgo_lite", "scope":"discovery_candidates_only", "availability_verified":False, "candidates":[{"title":"City Apartments", "url":target, "snippet":"Schedule a tour of our apartments."}]}
    assert public_dns == ["property.example"]


@pytest.mark.parametrize("url", [
    "http://property.example/", "https://user:secret@property.example/", "file:///etc/passwd", "javascript:alert(1)",
    "https://property.example:444/tour", "https://property.example/#secret", "https://duckduckgo.com/y.js?ad=1",
    "//duckduckgo.com/l/?uddg=https%3A%2F%2Fproperty.example%2F&uddg=https%3A%2F%2Fother.example%2F",
    "https://property.example/" + "x" * 2048,
])
def test_untrusted_candidate_urls_discarded(monkeypatch, public_dns, url):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: row(url))
    assert discovery.discover_properties("Any City")["candidates"] == []
    assert public_dns == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "0.0.0.0"])
def test_private_candidate_dns_discarded(monkeypatch, address):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: row())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    assert discovery.discover_properties("Any City")["candidates"] == []


def test_results_deduplicated_limited_and_markup_is_only_text(monkeypatch, public_dns):
    document = row(title="<b>City Apartments</b>", snippet="Tour <script>read_env()</script> available") * 2
    document += row("https://second.example/tour") + row("https://third.example/tour")
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: document)
    result = discovery.discover_properties("Any City", limit=2)
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["title"] == "City Apartments"
    assert "read_env" not in json.dumps(result)
    assert "third.example" not in public_dns


def test_generic_city_search_results_are_not_property_candidates(monkeypatch, public_dns):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: row(title="City encyclopedia", snippet="Official city history"))
    assert discovery.discover_properties("Any City")["candidates"] == []
    assert public_dns == []


@pytest.mark.parametrize("document", ["<html>Please enable JavaScript</html>", '<form id="challenge-form"></form>', "x" * 256_001])
def test_challenge_changed_markup_or_oversized_response_fails(monkeypatch, document):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: document)
    with pytest.raises(SafeError):
        discovery.discover_properties("Any City")


def test_explicit_no_results_is_not_fabricated(monkeypatch):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: '<div class="no-results">No results found</div>')
    assert discovery.discover_properties("Any City")["candidates"] == []


def test_parser_bounds_rows_without_reassigning_later_snippets():
    parser = discovery._LiteResults()
    parser.feed(row(snippet="Original") * discovery.MAX_PARSED_RESULTS + row(snippet="Overflow"))
    assert len(parser.rows) == discovery.MAX_PARSED_RESULTS
    assert parser.rows[-1]["snippet"] == "Original"


def test_returned_text_lengths_are_bounded(monkeypatch, public_dns):
    monkeypatch.setattr(discovery, "_fetch_search", lambda city: row(title="Apartments " + "x" * 2000, snippet="x" * 4000))
    candidate = discovery.discover_properties("Any City")["candidates"][0]
    assert len(candidate["title"]) == 256
    assert len(candidate["snippet"]) == 1000


class FakeResponse:
    def __init__(self, status=200, body=b"<html></html>", headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = {"Content-Type":"text/html"} if headers is None else headers
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, size):
        return self.body.read(size)


def connection(monkeypatch, response):
    calls = []
    class Connection:
        sock = None
        def __init__(self, host, address): calls.append((host, address))
        def request(self, *a, **kw): calls.append((a, kw))
        def getresponse(self): return response
        def close(self): calls.append("closed")
    monkeypatch.setattr(discovery, "PinnedHTTPSConnection", Connection)
    return calls


def test_transport_sends_only_city_query_and_fixed_headers(monkeypatch, public_dns):
    monkeypatch.setenv("SECRET_API_KEY", "SYNTHETIC_ENV_CANARY")
    calls = connection(monkeypatch, FakeResponse())
    assert discovery._fetch_search("Montréal & Lyon") == "<html></html>"
    assert calls[0] == ("lite.duckduckgo.com", "93.184.216.34")
    request, headers = calls[1]
    assert request[0] == "GET"
    assert parse_qs(urlsplit(request[1]).query) == {"q":["Montréal & Lyon apartments schedule a tour"]}
    assert "SYNTHETIC_ENV_CANARY" not in json.dumps(calls)
    assert public_dns == ["lite.duckduckgo.com"]
    assert calls[-1] == "closed"


@pytest.mark.parametrize("response", [
    FakeResponse(status=302, headers={"Location":"https://127.0.0.1/"}),
    FakeResponse(status=202), FakeResponse(status=500),
    FakeResponse(headers={"Content-Type":"application/json"}),
    FakeResponse(headers={"Content-Type":"text/html", "Content-Encoding":"gzip"}),
    FakeResponse(headers={"Content-Type":"text/html", "Content-Length":"256001"}),
    FakeResponse(body=b"x" * 256001),
])
def test_provider_errors_and_response_bounds_close_connection(monkeypatch, public_dns, response):
    calls = connection(monkeypatch, response)
    with pytest.raises(SafeError):
        discovery._fetch_search("Any City")
    assert public_dns == ["lite.duckduckgo.com"]
    assert calls[-1] == "closed"


def test_provider_timeout_is_payload_free_and_closes(monkeypatch, public_dns):
    class Broken(FakeResponse):
        def read1(self, size): raise TimeoutError("sensitive fake payload")
    calls = connection(monkeypatch, Broken())
    with pytest.raises(SafeError, match="Discovery provider request failed") as failure:
        discovery._fetch_search("Any City")
    assert "sensitive" not in str(failure.value)
    assert calls[-1] == "closed"

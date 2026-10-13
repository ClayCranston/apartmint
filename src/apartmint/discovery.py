"""Bounded public search for unverified property candidates; never book or collect.

DuckDuckGo documents its Lite interface at
https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript.
This is a public website adapter, not a contracted search API: challenges and
markup changes fail explicitly. Only the city and fixed search terms leave the
machine. Search results are untrusted data, never model/tool instructions.
"""
from __future__ import annotations

import http.client
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import SafeError, validate_public_url
from .network import PinnedHTTPSConnection, resolve_addresses

PROVIDER = "duckduckgo_lite"
SEARCH_HOST = "lite.duckduckgo.com"
MAX_SEARCH_BYTES = 256_000
SEARCH_TIMEOUT_SECONDS = 15
MAX_RESULTS = 10
MAX_PARSED_RESULTS = 32
_HOUSING = re.compile(r"\b(apartments?|rentals?|leasing|residences?|for rent)\b", re.I)


def _city(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise SafeError("City must be a nonempty name of at most 200 characters")
    if any(not char.isprintable() for char in value):
        raise SafeError("City must not contain control characters")
    return " ".join(value.split())


def _fetch_search(city: str) -> str:
    # The only allowed HTTP destination is this exact generated search URL.
    # Redirects are deliberately not followed, even within the provider domain.
    target = "/lite/?" + urlencode({"q": city + " apartments schedule a tour"})
    connection = None
    try:
        address = resolve_addresses(SEARCH_HOST, 443, public=True)[0]
        connection = PinnedHTTPSConnection(SEARCH_HOST, address)
        connection.timeout = SEARCH_TIMEOUT_SECONDS
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        connection.request("GET", target, headers={
            "User-Agent": "Apartmint/0.4 (+local housing monitor)",
            "Accept": "text/html", "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            raise SafeError("Discovery provider redirect is not allowlisted")
        if response.status != 200:
            raise SafeError("Discovery provider is unavailable or requires a browser challenge")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise SafeError("Discovery provider returned an unsupported content type")
        if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise SafeError("Compressed discovery responses are not accepted")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_SEARCH_BYTES):
            raise SafeError("Discovery response is too large or has an invalid length")
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SafeError("Discovery provider request timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(16_384, MAX_SEARCH_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_SEARCH_BYTES:
                raise SafeError("Discovery response is too large")
        return b"".join(chunks).decode("utf-8", errors="replace")
    except SafeError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise SafeError("Discovery provider request failed") from None
    finally:
        if connection is not None:
            connection.close()


def _text(value: str, limit: int) -> str:
    return " ".join("".join(char for char in value if char.isprintable() or char.isspace()).split())[:limit]


class _LiteResults(HTMLParser):
    """Read result anchors and the following snippet; never execute markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self.active: str | None = None
        self.close_tag: str | None = None
        self.challenge = False
        self.no_results = False
        self.ignored = 0
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        classes = (attributes.get("class") or "").split()
        if (attributes.get("id") or "") in {"challenge-form", "anomaly-form"}:
            self.challenge = True
        if "no-results" in classes:
            self.no_results = True
        if tag == "a" and "result-link" in classes:
            self.active = None
            self.current = None
            if len(self.rows) < MAX_PARSED_RESULTS:
                self.current = {"title": "", "url": attributes.get("href") or "", "snippet": ""}
                self.rows.append(self.current)
                self.active, self.close_tag = "title", "a"
        elif tag == "td" and "result-snippet" in classes and self.current is not None:
            self.active, self.close_tag = "snippet", "td"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
        if tag == self.close_tag:
            self.active = self.close_tag = None

    def handle_data(self, data: str) -> None:
        if self.active and self.current is not None and not self.ignored:
            # Bound retained text independently of the response byte limit.
            limit = 512 if self.active == "title" else 2_000
            self.current[self.active] = (self.current[self.active] + data)[:limit]


def _candidate_url(raw: str) -> str:
    if len(raw) > 8_192:
        raise SafeError("Invalid discovery candidate URL")
    wrapped = "https:" + raw if raw.startswith("//") else raw
    parsed = urlsplit(wrapped)
    if parsed.hostname == "duckduckgo.com" and parsed.path == "/l/":
        validate_public_url(wrapped)
        query = parse_qs(parsed.query, max_num_fields=10)
        targets = query.get("uddg", [])
        if len(targets) != 1:
            raise SafeError("Invalid discovery candidate link wrapper")
        wrapped = targets[0]
    validate_public_url(wrapped)
    parsed = urlsplit(wrapped)
    hostname = (parsed.hostname or "").lower()
    # Provider/ad navigation is not a property candidate; never follow tracking.
    if hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com"):
        raise SafeError("Discovery candidate is a provider link")
    resolve_addresses(hostname, 443, public=True)
    return wrapped


def discover_properties(city: str, limit: int = 5) -> dict:
    """Search the given city; return public HTTPS candidates, not availability.

    No pages are fetched beyond the search response. Candidate DNS is checked
    now, and must be checked/pinned again by any future collector. This does not
    establish property identity, geographic coverage, or tour availability.
    """
    city = _city(city)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise SafeError("Discovery result limit must be between 1 and 10")
    document = _fetch_search(city)
    if len(document.encode("utf-8")) > MAX_SEARCH_BYTES:
        raise SafeError("Discovery response is too large")
    parser = _LiteResults()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, RecursionError):
        raise SafeError("Discovery provider returned invalid search markup") from None
    if parser.challenge:
        raise SafeError("Discovery provider requires a browser challenge")
    if not parser.rows and not parser.no_results:
        raise SafeError("Discovery provider returned unrecognized search markup")
    candidates = []
    seen: set[str] = set()
    for row in parser.rows:
        title, snippet = _text(row["title"], 256), _text(row["snippet"], 1_000)
        if not title or not _HOUSING.search(title + " " + snippet):
            continue
        try:
            url = _candidate_url(row["url"])
        except (SafeError, ValueError):
            continue
        if url in seen:
            continue
        candidates.append({"title": title, "url": url, "snippet": snippet})
        seen.add(url)
        if len(candidates) == limit:
            break
    return {"city": city, "provider": PROVIDER, "scope": "discovery_candidates_only", "availability_verified": False, "candidates": candidates}

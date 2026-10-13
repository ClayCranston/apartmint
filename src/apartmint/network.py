"""Pinned DNS transports; never inherit HTTP proxy environment variables."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from .config import SafeError, validate_public_url

MAX_HTML_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 256_000
LOCAL_MODEL_HOSTS = {"localhost", "vllm", "ollama", "host.docker.internal"}


def resolve_addresses(host: str, port: int, *, public: bool) -> list[str]:
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not addresses:
            raise ValueError
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if public:
                valid = ip.is_global and not ip.is_multicast
            else:
                valid = (ip.is_private or ip.is_loopback) and not (ip.is_unspecified or ip.is_multicast or ip.is_link_local)
            if not valid:
                raise ValueError
        return addresses
    except (OSError, ValueError):
        raise SafeError("Network destination failed address policy") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, port=443, timeout=20, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str):
        super().__init__(hostname, port=port, timeout=180)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), timeout=self.timeout)


def fetch_public_html(url: str, allowed_urls: set[str]) -> str:
    """Fetch exact allowlisted URLs with TLS and DNS-to-socket address pinning."""
    current = url
    for _ in range(4):
        validate_public_url(current)
        if current not in allowed_urls:
            raise SafeError("Redirect destination is not explicitly configured")
        parsed = urlsplit(current)
        addresses = resolve_addresses(parsed.hostname or "", 443, public=True)
        connection = PinnedHTTPSConnection(parsed.hostname or "", addresses[0])
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={"User-Agent": "Apartmint/0.3 (+local housing monitor)", "Accept": "text/html,text/plain", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise SafeError("Source returned an invalid redirect")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise SafeError("Source returned an unsuccessful response")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("text/html", "text/plain", "application/xhtml+xml"):
                raise SafeError("Source returned an unsupported content type")
            if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise SafeError("Compressed source responses are not accepted")
            body = response.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                raise SafeError("Source response is too large")
            return body.decode("utf-8", errors="replace")
        except SafeError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            raise SafeError("Source fetch failed") from None
        finally:
            connection.close()
    raise SafeError("Source redirected too many times")


def validate_model_endpoint(base_url: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if (
            parsed.scheme != "http" or not (is_loopback or host in LOCAL_MODEL_HOSTS)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"
            or any(ord(c) < 33 or ord(c) > 126 for c in base_url)
        ):
            raise ValueError
        port = parsed.port or 80
        if not 1 <= port <= 65535:
            raise ValueError
        return host, port, "/v1/chat/completions"
    except ValueError:
        raise SafeError("Model endpoint must be an approved local HTTP /v1 endpoint") from None


def post_local_json(base_url: str, payload: dict) -> dict:
    host, port, path = validate_model_endpoint(base_url)
    addresses = resolve_addresses(host, port, public=False)
    connection = PinnedHTTPConnection(host, port, addresses[0])
    try:
        connection.request("POST", path, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise SafeError("Local inference request failed")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SafeError("Local inference response is too large")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except SafeError:
        raise
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        raise SafeError("Local inference request failed") from None
    finally:
        connection.close()

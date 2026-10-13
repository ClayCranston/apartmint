"""Read-only local readiness checks; never inspect private environment files."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import unquote, urlsplit

from .config import Config, SafeError, Settings
from .network import PinnedHTTPConnection, resolve_addresses, validate_model_endpoint

PROBE_TIMEOUT_SECONDS = 3.0
LOCAL_BROKER_HOSTS = {"localhost", "redis", "host.docker.internal"}


def _result(status: str, action: str, **fields) -> dict:
    return {"status": status, "action": action, **fields}


def _configuration(path: Path) -> dict:
    try:
        names = (path.name, path.resolve().name)
        if any(name == ".env" or name.startswith(".env.") for name in names):
            return _result("invalid", "Use a source JSON configuration file, not a private environment file.", source_count=0)
        if path.stat().st_size > 65536:
            return _result("invalid", "Reduce the source configuration to at most 65536 bytes.", source_count=0)
        value = json.loads(path.read_text())
        if (isinstance(value, dict) and set(value) == {"area", "sources"}
                and isinstance(value["area"], str) and value["area"].strip()
                and len(value["area"]) <= 200 and value["sources"] == []):
            return _result("empty", "Add at least one approved source to the source configuration.", source_count=0)
        config = Config.load(path)
        return _result("ready", "Source configuration is valid.", source_count=len(config.sources))
    except FileNotFoundError:
        return _result("missing", "Create the source configuration from config/sources.example.json and add approved sources.", source_count=0)
    except PermissionError:
        return _result("unreadable", "Give this user read access to the source configuration.", source_count=0)
    except (OSError, ValueError, TypeError, SafeError):
        return _result("invalid", "Correct the source configuration using config/sources.example.json; check required fields, unique IDs, public HTTPS URLs, and housing types.", source_count=0)


def _data(path: Path) -> dict:
    try:
        if not path.exists():
            return _result("missing", "Create the configured data directory with private permissions (0700).", exists=False, writable=False)
        if not path.is_dir():
            return _result("invalid", "Set APARTMINT_DATA_DIR to a directory.", exists=True, writable=False)
        writable = os.access(path, os.W_OK | os.X_OK)
        return _result("ready" if writable else "not_writable",
                       "Data directory permission check passed." if writable else "Give this user write and traversal permission on the data directory.",
                       exists=True, writable=writable)
    except OSError:
        return _result("unreadable", "Check access to the configured data directory.", exists=False, writable=False)


def _bounded_probe(operation, label: str) -> dict:
    """Bound user-visible time, including system DNS, without unbounded joins.

    A daemon may finish an in-flight DNS lookup after timeout. Cancellation is
    checked before opening a socket; socket operations also have short timeouts.
    No response payload or exception text crosses the reporting boundary.
    """
    completed = threading.Event()
    cancelled = threading.Event()
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    output = []

    def run():
        try:
            output.append(operation(deadline, cancelled))
        except Exception:
            output.append(_result("unreachable", f"Check that the local {label} service is running and its configured endpoint is correct."))
        finally:
            completed.set()

    threading.Thread(target=run, name=f"apartmint-doctor-{label}", daemon=True).start()
    if not completed.wait(max(0.0, deadline - time.monotonic())):
        cancelled.set()
        return _result("timeout", f"The local {label} check timed out; check the service and local name resolution.")
    return output[0]


def _remaining(deadline: float, cancelled: threading.Event) -> float:
    remaining = deadline - time.monotonic()
    if cancelled.is_set() or remaining <= 0:
        raise TimeoutError
    return remaining


def _model(settings: Settings) -> dict:
    try:
        host, port, _ = validate_model_endpoint(settings.model_base_url)
    except SafeError:
        return _result("invalid_endpoint", "Set APARTMINT_MODEL_BASE_URL to an approved local HTTP /v1 endpoint without credentials.")

    def probe(deadline, cancelled):
        addresses = resolve_addresses(host, port, public=False)
        timeout = _remaining(deadline, cancelled)
        connection = PinnedHTTPConnection(host, port, addresses[0])
        connection.timeout = timeout
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json", "Connection": "close"})
            if connection.sock is not None:
                connection.sock.settimeout(_remaining(deadline, cancelled))
            response = connection.getresponse()
            if response.status != 200:
                return _result("unhealthy", "Check the local model server health endpoint and model startup; redirects are not followed.")
            return _result("ready", "Local model health endpoint responded successfully.")
        finally:
            connection.close()

    return _bounded_probe(probe, "model")


def _broker(settings: Settings) -> dict:
    try:
        parsed = urlsplit(settings.broker_url)
        host = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        port = parsed.port or 6379
        database = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
        if (parsed.scheme != "redis" or not (loopback or host in LOCAL_BROKER_HOSTS)
                or not 1 <= port <= 65535 or parsed.query or parsed.fragment
                or database and (not database.isascii() or not database.isdecimal())
                or any(ord(char) < 33 or ord(char) > 126 for char in settings.broker_url)):
            raise ValueError
        db = int(database or "0")
        if db > 65535:
            raise ValueError
    except (ValueError, TypeError):
        return _result("invalid_endpoint", "Set APARTMINT_BROKER_URL to a local redis:// endpoint (loopback, redis, or host.docker.internal).")

    def probe(deadline, cancelled):
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        addresses = resolve_addresses(host, port, public=False)
        timeout = _remaining(deadline, cancelled)
        client = Redis(host=addresses[0], port=port, db=db,
                       username=unquote(parsed.username) if parsed.username is not None else None,
                       password=unquote(parsed.password) if parsed.password is not None else None,
                       socket_connect_timeout=timeout, socket_timeout=timeout,
                       retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
        try:
            if client.ping() is not True:
                return _result("unhealthy", "Check that the local broker accepts a Redis PING.")
            return _result("ready", "Local broker answered PING.")
        finally:
            client.close()

    return _bounded_probe(probe, "broker")


def doctor(settings: Settings) -> dict:
    """Check prerequisites without starting services, creating files, or reading .env."""
    checks = {"configuration": _configuration(settings.config_path),
              "model": _model(settings), "broker": _broker(settings),
              "data": _data(settings.data_dir)}
    ready = all(check["status"] == "ready" for check in checks.values())
    for service in ("collector", "worker", "scheduler"):
        checks[service] = _result("unknown", f"Verify the {service} process separately; dependency checks do not establish its liveness.")
    return {"ready": ready, "monitoring_verified": False, "scope": "configuration_and_dependencies_only", "checks": checks,
            "limitations": ["These checks do not establish collector, inference worker, or scheduler liveness.",
                            "A successful health response does not verify model inference or extraction quality.",
                            "Data writability is a filesystem permission check; no file or database was created."]}

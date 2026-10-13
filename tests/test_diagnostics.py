import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from apartmint import diagnostics
from apartmint.config import SafeError, Settings


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"area": "Example area", "sources": [{"id": "sample", "url": "https://example.com/listings"}]}))
    data = tmp_path / "data"
    data.mkdir()
    return Settings(config, data, "http://127.0.0.1:11437/v1", "apartmint-qwen3", "redis://127.0.0.1:16379/0")


@pytest.fixture
def available_dependencies(monkeypatch):
    monkeypatch.setattr(diagnostics, "_model", lambda settings: {"status": "ready", "action": "Model checked."})
    monkeypatch.setattr(diagnostics, "_broker", lambda settings: {"status": "ready", "action": "Broker checked."})


def test_doctor_ready_means_dependencies_not_worker_liveness(settings, available_dependencies):
    before = set(settings.data_dir.iterdir())
    report = diagnostics.doctor(settings)
    assert report["ready"] is True
    assert report["monitoring_verified"] is False
    assert report["scope"] == "configuration_and_dependencies_only"
    assert report["checks"]["configuration"]["source_count"] == 1
    for service in ("collector", "worker", "scheduler"):
        assert report["checks"][service]["status"] == "unknown"
    assert set(settings.data_dir.iterdir()) == before
    assert "liveness" in report["limitations"][0]


@pytest.mark.parametrize("content,expected", [(None, "missing"), ('{"area":"Example", "sources":[]}', "empty"), ("{bad json", "invalid"), ('{"area":"", "sources":[]}', "invalid")])
def test_configuration_states_are_distinct(settings, available_dependencies, content, expected):
    if content is None:
        settings.config_path.unlink()
    else:
        settings.config_path.write_text(content)
    report = diagnostics.doctor(settings)
    assert report["ready"] is False
    assert report["checks"]["configuration"]["status"] == expected
    assert report["checks"]["configuration"]["action"]


def test_missing_data_is_not_created(settings, available_dependencies):
    settings.data_dir.rmdir()
    report = diagnostics.doctor(settings)
    assert not report["ready"]
    assert report["checks"]["data"] == {"status": "missing", "exists": False, "writable": False, "action": "Create the configured data directory with private permissions (0700)."}
    assert not settings.data_dir.exists()


def test_unwritable_data_is_not_ready(settings, available_dependencies, monkeypatch):
    monkeypatch.setattr(diagnostics.os, "access", lambda *args: False)
    report = diagnostics.doctor(settings)
    assert not report["ready"]
    assert report["checks"]["data"]["status"] == "not_writable"


def test_does_not_read_private_env_or_emit_config_values(settings, available_dependencies, monkeypatch):
    secret = "PRIVATE_ENV_CANARY_do_not_report"
    (settings.config_path.parent / ".env").write_text(secret)
    settings.config_path.write_text(json.dumps({"area": secret, "sources": [{"id": "sample", "url": "https://example.com/" + secret}]}))
    monkeypatch.setenv("PRIVATE_ENV_CANARY", secret)
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        reads.append(path)
        assert path.name != ".env"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    report = diagnostics.doctor(settings)
    assert secret not in json.dumps(report)
    assert reads and set(reads) == {settings.config_path}


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://127.0.0.1:80/v1?secret=canary", "http://user:canary@127.0.0.1/v1"])
def test_model_rejects_nonapproved_endpoints_before_resolution(settings, monkeypatch, url):
    from dataclasses import replace
    monkeypatch.setattr(diagnostics, "resolve_addresses", lambda *args, **kwargs: pytest.fail("Must not resolve forbidden endpoint"))
    result = diagnostics._model(replace(settings, model_base_url=url))
    assert result["status"] == "invalid_endpoint"
    assert "canary" not in json.dumps(result)


def test_model_health_pins_destination_and_does_not_follow_redirect(settings, monkeypatch):
    connections = []
    class Connection:
        sock = None
        def __init__(self, host, port, address):
            connections.append((host, port, address))
        def request(self, method, path, headers):
            assert (method, path) == ("GET", "/health")
            assert 0 < self.timeout <= 3
        def getresponse(self):
            return type("Response", (), {"status": 302})()
        def close(self):
            pass
    monkeypatch.setattr(diagnostics, "resolve_addresses", lambda host, port, public: ["127.0.0.1"] if public is False else pytest.fail("Wrong address policy"))
    monkeypatch.setattr(diagnostics, "PinnedHTTPConnection", Connection)
    result = diagnostics._model(settings)
    assert result["status"] == "unhealthy"
    assert len(connections) == 1


def test_failed_local_address_policy_never_opens_model_connection(settings, monkeypatch):
    def rejected(*args, **kwargs):
        raise SafeError("Network destination failed address policy")
    monkeypatch.setattr(diagnostics, "resolve_addresses", rejected)
    monkeypatch.setattr(diagnostics, "PinnedHTTPConnection", lambda *args: pytest.fail("Do not connect after failed policy"))
    assert diagnostics._model(settings)["status"] == "unreachable"


def test_health_success(settings, monkeypatch):
    class Connection:
        sock = None
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return type("Response", (), {"status": 200})()
        def close(self): pass
    monkeypatch.setattr(diagnostics, "resolve_addresses", lambda *args, **kwargs: ["127.0.0.1"])
    monkeypatch.setattr(diagnostics, "PinnedHTTPConnection", Connection)
    assert diagnostics._model(settings)["status"] == "ready"


@pytest.mark.parametrize("url", ["redis://example.com/0", "redis://8.8.8.8/0", "redis://127.0.0.1/0?password=canary", "rediss://localhost/0"])
def test_broker_rejects_unsupported_endpoints_without_network(settings, monkeypatch, url):
    from dataclasses import replace
    monkeypatch.setattr(diagnostics, "resolve_addresses", lambda *args, **kwargs: pytest.fail("Do not resolve forbidden broker"))
    report = diagnostics._broker(replace(settings, broker_url=url))
    assert report["status"] == "invalid_endpoint"
    assert "canary" not in json.dumps(report)


def test_broker_ping_pins_address_and_keeps_credentials_out_of_errors(settings, monkeypatch):
    from dataclasses import replace
    import redis
    calls = []
    class Client:
        def __init__(self, **kwargs): calls.append(kwargs)
        def ping(self): raise RuntimeError("redis://private:CANARY_PASSWORD@localhost/0")
        def close(self): pass
    monkeypatch.setattr(redis, "Redis", Client)
    monkeypatch.setattr(diagnostics, "resolve_addresses", lambda host, port, public: ["127.0.0.1"] if public is False else pytest.fail("Wrong policy"))
    report = diagnostics._broker(replace(settings, broker_url="redis://private:CANARY_PASSWORD@localhost:16379/2"))
    assert report["status"] == "unreachable"
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["db"] == 2
    assert calls[0]["password"] == "CANARY_PASSWORD"
    assert 0 < calls[0]["socket_timeout"] <= 3
    assert "CANARY_PASSWORD" not in json.dumps(report)


def test_probe_timeout_includes_dns_and_prevents_late_connection(settings, monkeypatch):
    release = threading.Event()
    resolved = threading.Event()
    connections = []
    def slow_dns(*args, **kwargs):
        release.wait(2)
        resolved.set()
        return ["127.0.0.1"]
    monkeypatch.setattr(diagnostics, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(diagnostics, "resolve_addresses", slow_dns)
    monkeypatch.setattr(diagnostics, "PinnedHTTPConnection", lambda *args: connections.append(args))
    started = time.monotonic()
    try:
        report = diagnostics._model(settings)
        assert report["status"] == "timeout"
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
    assert resolved.wait(1)
    time.sleep(0.02)
    assert connections == []


@pytest.mark.parametrize("mode", ["worker", "beat"])
@pytest.mark.parametrize("broker", [None, "redis://127.0.0.1:26379/3"])
def test_collector_launcher_effective_broker_and_clean_environment(tmp_path, mode, broker):
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "start_collector.sh"
    shutil.copyfile(root / "scripts/start_collector.sh", launcher)
    binary = tmp_path / ".venv" / "bin" / "celery"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{'broker':os.environ.get('APARTMINT_BROKER_URL'),'args':sys.argv[1:],'private_inherited':'PRIVATE_CANARY' in os.environ}}))\n")
    binary.chmod(0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PRIVATE_CANARY": "never forward"}
    if broker is not None:
        env["APARTMINT_BROKER_URL"] = broker
    result = subprocess.run(["/bin/bash", str(launcher), mode], env=env, capture_output=True, text=True, check=True, timeout=5)
    captured = json.loads(result.stdout)
    assert captured["broker"] == (broker or "redis://127.0.0.1:16379/0")
    assert mode in captured["args"]
    assert captured["private_inherited"] is False


@pytest.mark.parametrize("through_symlink", [False, True])
def test_private_env_cannot_be_selected_as_source_config(tmp_path, monkeypatch, through_symlink):
    private = tmp_path / ".env"
    private.write_text("PRIVATE_CANARY=value")
    selected = private
    if through_symlink:
        selected = tmp_path / "sources.json"
        selected.symlink_to(private)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("Private file must not be read"))
    assert diagnostics._configuration(selected)["status"] == "invalid"

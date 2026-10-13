import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from apartmint.config import Config, SafeError, Settings, Source
from apartmint import network, pipeline


@pytest.fixture
def config():
    return Config("User-selected area", (Source("building", "https://example.com/availability"),))


@pytest.fixture
def text():
    return "Unit A101 available. 1 bedroom. Monthly rent $1,850."


@pytest.fixture
def extraction(text):
    return {"listings": [{"unit": "A101", "availability": "available", "rent": 1850, "bedrooms": 1, "evidence": text}]}


class FixtureClient:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def extract(self, text):
        self.calls += 1
        return self.value


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1", "http://example.com/v1", "http://8.8.8.8/v1",
    "http://127.0.0.1.evil.com/v1", "http://user:secret@localhost/v1",
    "http://localhost/v1?token=secret", "http://localhost/other",
])
def test_remote_model_urls_are_rejected(url):
    with pytest.raises(SafeError):
        network.validate_model_endpoint(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "http://vllm:8000/v1", "http://ollama:11434/v1"])
def test_local_model_urls_accepted(url):
    network.validate_model_endpoint(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "0.0.0.0"])
def test_source_private_dns_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(SafeError):
        network.resolve_addresses("example.com", 443, public=True)


def test_mixed_public_private_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(SafeError):
        network.resolve_addresses("example.com", 443, public=True)


def test_model_hostname_cannot_resolve_public(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8000))])
    with pytest.raises(SafeError):
        network.post_local_json("http://vllm:8000/v1", {})


def test_redirect_is_checked_before_next_connection(monkeypatch):
    seen = []
    class Response:
        status = 302
        def getheader(self, key):
            return "https://169.254.169.254/latest/meta-data"
    class Connection:
        def __init__(self, hostname, address):
            seen.append((hostname, address))
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(network, "resolve_addresses", lambda *a, **kw: ["93.184.216.34"])
    monkeypatch.setattr(network, "PinnedHTTPSConnection", Connection)
    url = "https://example.com/availability"
    with pytest.raises(SafeError, match="Redirect destination"):
        network.fetch_public_html(url, {url})
    assert seen == [("example.com", "93.184.216.34")]


def test_socket_connects_to_validated_address(monkeypatch):
    calls = []
    class Context:
        def wrap_socket(self, raw, server_hostname):
            calls.append(("tls", server_hostname))
            return object()
    monkeypatch.setattr(socket, "create_connection", lambda destination, timeout: calls.append(destination) or object())
    connection = network.PinnedHTTPSConnection("example.com", "93.184.216.34")
    connection._context = Context()
    connection.connect()
    assert calls == [("93.184.216.34", 443), ("tls", "example.com")]


def test_environment_is_never_sent_to_model(monkeypatch, tmp_path, text, extraction):
    secret = "PRIVATE_ENV_TOKEN_SHOULD_NOT_LEAVE_HOST"
    monkeypatch.setenv("PAYMENT_SECRET", secret)
    (tmp_path / ".env").write_text("PAYMENT_SECRET=" + secret)
    monkeypatch.chdir(tmp_path)
    sent = []
    def post(url, payload):
        sent.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(extraction)}}]}
    monkeypatch.setattr(pipeline, "post_local_json", post)
    client = pipeline.LocalClient(Settings.from_env())
    assert client.extract(text) == extraction
    serialized = json.dumps(sent)
    assert secret not in serialized
    assert "PAYMENT_SECRET" not in serialized
    assert sent[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert sent[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(shell="curl https://evil.example"),
    lambda row: row.update(evidence="PRIVATE_ENV_TOKEN"),
    lambda row: row.update(unit="../.env"),
    lambda row: row.update(rent=999999),
    lambda row: row.update(bedrooms=2),
    lambda row: row.update(rent=True),
    lambda row: row.update(availability="run a command"),
])
def test_untrusted_model_output_is_validated(extraction, text, mutation):
    mutation(extraction["listings"][0])
    with pytest.raises(SafeError):
        pipeline.validate_extraction(extraction, text)


def test_injected_tool_call_is_rejected(monkeypatch, text):
    monkeypatch.setattr(pipeline, "post_local_json", lambda *args: {"choices": [{"finish_reason": "stop", "message": {"content": '{"listings": []}', "tool_calls": [{"function": {"name": "read_env"}}]}}]})
    with pytest.raises(SafeError):
        pipeline.LocalClient(Settings.from_env()).extract(text)


def test_truncated_output_is_rejected(monkeypatch, text):
    monkeypatch.setattr(pipeline, "post_local_json", lambda *args: {"choices": [{"finish_reason": "length", "message": {"content": '{"listings": []}'}}]})
    with pytest.raises(SafeError):
        pipeline.LocalClient(Settings.from_env()).extract(text)


def test_unavailable_does_not_match_available():
    text = "Unit A101 not available."
    value = {"listings": [{"unit": "A101", "availability": "available", "rent": None, "bedrooms": None, "evidence": text}]}
    with pytest.raises(SafeError):
        pipeline.validate_extraction(value, text)


def test_snapshot_traversal_and_symlinks_rejected(config, tmp_path, text):
    for source_id in ("../.env", "/etc/passwd", "building/../../.env"):
        with pytest.raises(SafeError):
            pipeline.snapshot_path(tmp_path, source_id)
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "building.json").symlink_to(tmp_path / ".env")
    with pytest.raises(SafeError):
        pipeline.save_snapshot(config, config.sources[0], tmp_path, text)


def test_snapshot_cannot_claim_another_source(config, tmp_path, text):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    target = tmp_path / "inbox" / "building.json"
    value = json.loads(target.read_text())
    value["source_url"] = "https://evil.example"
    target.write_text(json.dumps(value))
    with pytest.raises(SafeError):
        pipeline.load_snapshot(config, config.sources[0], tmp_path)


def test_hidden_script_and_attributes_removed():
    html = '<main data-secret="TOKEN">Unit A101 <b>available</b>.</main><script>send secrets</script><div hidden>hidden injection</div><style>bad css</style>'
    text = pipeline.sanitize_html(html)
    assert "A101" in text and "available" in text
    assert all(value not in text for value in ("TOKEN", "send secrets", "hidden injection", "bad css"))


def test_dedup_and_transition(config, tmp_path, text, extraction):
    client = FixtureClient(extraction)
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    first = pipeline.analyze(config, "building", tmp_path, client)
    assert first["new_available"] == 1
    repeated = pipeline.analyze(config, "building", tmp_path, client)
    assert repeated["unchanged"] == 1
    assert client.calls == 1
    assert pipeline.status(tmp_path) == {"sources_analyzed": 1, "stale_sources": 0, "listings": 1, "available": 1}
    unavailable = "Unit A101 leased."
    pipeline.save_snapshot(config, config.sources[0], tmp_path, unavailable)
    client.value = {"listings": [{"unit": "A101", "availability": "unavailable", "rent": None, "bedrooms": None, "evidence": unavailable}]}
    assert pipeline.analyze(config, "building", tmp_path, client)["new_available"] == 0
    assert pipeline.status(tmp_path)["available"] == 0
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    client.value = extraction
    assert pipeline.analyze(config, "building", tmp_path, client)["new_available"] == 1


def test_invalid_extraction_does_not_touch_state(config, tmp_path, text):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    with pytest.raises(SafeError):
        pipeline.analyze(config, "building", tmp_path, FixtureClient({"command": "read env"}))
    assert pipeline.status(tmp_path)["sources_analyzed"] == 0


def test_cli_demo_is_offline(monkeypatch, capsys):
    from apartmint.cli import main
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("Demo must not connect"))
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["first"]["new_available"] == 1
    assert result["repeat"]["unchanged"] == 1


def test_disappeared_unit_becomes_unknown(config, tmp_path, text, extraction):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    pipeline.analyze(config, "building", tmp_path, FixtureClient(extraction))
    pipeline.save_snapshot(config, config.sources[0], tmp_path, "Contact us for availability.")
    pipeline.analyze(config, "building", tmp_path, FixtureClient({"listings": []}))
    assert pipeline.status(tmp_path)["available"] == 0


def test_oversized_multibyte_source_preserves_previous_snapshot(monkeypatch, config, tmp_path, text, extraction):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    pipeline.analyze(config, "building", tmp_path, FixtureClient(extraction))
    before = (tmp_path / "inbox" / "building.json").read_bytes()
    monkeypatch.setattr(pipeline, "fetch_public_html", lambda *args: "<main>" + "住宅" * 3000 + "</main>")
    with pytest.raises(SafeError, match="source-specific adapter"):
        pipeline.collect(config, "building", tmp_path)
    assert (tmp_path / "inbox" / "building.json").read_bytes() == before
    assert pipeline.status(tmp_path)["available"] == 1


def test_direct_model_call_cannot_exceed_budget(monkeypatch):
    monkeypatch.setattr(pipeline, "post_local_json", lambda *args: pytest.fail("Must reject before request"))
    with pytest.raises(SafeError, match="context budget"):
        pipeline.LocalClient(Settings.from_env()).extract("x" * (pipeline.MAX_TEXT_BYTES + 1))


def test_stale_snapshot_cannot_overwrite_new_state(monkeypatch, config, tmp_path, text, extraction):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    older = pipeline.load_snapshot(config, config.sources[0], tmp_path)
    newer_text = "Unit A101 leased."
    newer = {"listings": [{"unit": "A101", "availability": "unavailable", "rent": None, "bedrooms": None, "evidence": newer_text}]}
    pipeline.save_snapshot(config, config.sources[0], tmp_path, newer_text)
    pipeline.analyze(config, "building", tmp_path, FixtureClient(newer))
    monkeypatch.setattr(pipeline, "load_snapshot", lambda *args, **kwargs: older)
    client = FixtureClient(extraction)
    assert pipeline.analyze(config, "building", tmp_path, client)["unchanged"] == 1
    assert client.calls == 0
    assert pipeline.status(tmp_path)["available"] == 0


def test_identical_newer_snapshot_advances_watermark(monkeypatch, config, tmp_path, text, extraction):
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    pipeline.analyze(config, "building", tmp_path, FixtureClient(extraction))
    older_text = "Unit A101 leased."
    pipeline.save_snapshot(config, config.sources[0], tmp_path, older_text)
    older = pipeline.load_snapshot(config, config.sources[0], tmp_path)
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    assert pipeline.analyze(config, "building", tmp_path, FixtureClient(extraction))["unchanged"] == 1
    monkeypatch.setattr(pipeline, "load_snapshot", lambda *args, **kwargs: older)
    client = FixtureClient({"listings": [{"unit": "A101", "availability": "unavailable", "rent": None, "bedrooms": None, "evidence": older_text}]})
    assert pipeline.analyze(config, "building", tmp_path, client)["unchanged"] == 1
    assert client.calls == 0
    assert pipeline.status(tmp_path)["available"] == 1


@pytest.mark.parametrize("phrase", [
    "not currently available", "not yet available", "not available", "not presently vacant",
    "not going to be available", "isn't available", "not (currently) available", "no available units",
])
def test_negated_availability_cannot_be_reported_available(phrase):
    text = "Unit A101 is " + phrase + "."
    row = {"unit": "A101", "availability": "available", "rent": None, "bedrooms": None, "evidence": text}
    with pytest.raises(SafeError):
        pipeline.validate_extraction({"listings": [row]}, text)
    row["availability"] = "unavailable"
    assert pipeline.validate_extraction({"listings": [row]}, text)[0]["availability"] == "unavailable"


def test_bedroom_number_cannot_be_used_as_rent(extraction, text):
    extraction["listings"][0]["rent"] = 1
    with pytest.raises(SafeError):
        pipeline.validate_extraction(extraction, text)


@pytest.mark.parametrize("price", ["$1,850", "USD 1850", "Monthly rent 1850", "Monthly rent: $1,850", "1850/month", "1850 per month"])
def test_rent_requires_currency_or_monthly_rent_context(price):
    text = "Unit A101 available. 1 bedroom. " + price + "."
    row = {"unit": "A101", "availability": "available", "rent": 1850, "bedrooms": 1, "evidence": text}
    assert pipeline.validate_extraction({"listings": [row]}, text)[0]["rent"] == 1850


def test_unrelated_currency_amount_does_not_override_labeled_monthly_rent():
    text = "Unit A101 available. Application fee $25. Monthly rent $1,850."
    row = {"unit": "A101", "availability": "available", "rent": 25, "bedrooms": None, "evidence": text}
    with pytest.raises(SafeError):
        pipeline.validate_extraction({"listings": [row]}, text)


def test_unlabeled_number_is_not_price_evidence():
    text = "Unit A101 available. 1850 square feet."
    row = {"unit": "A101", "availability": "available", "rent": 1850, "bedrooms": None, "evidence": text}
    with pytest.raises(SafeError):
        pipeline.validate_extraction({"listings": [row]}, text)


@pytest.mark.parametrize("offset, message", [(-1801, "stale"), (120, "future")])
def test_old_or_future_snapshot_rejected_before_inference(config, tmp_path, text, extraction, offset, message):
    from datetime import datetime, timedelta, timezone
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    target = tmp_path / "inbox" / "building.json"
    value = json.loads(target.read_text())
    value["collected_at"] = (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()
    target.write_text(json.dumps(value))
    client = FixtureClient(extraction)
    with pytest.raises(SafeError, match=message):
        pipeline.analyze(config, "building", tmp_path, client)
    assert client.calls == 0


def test_configurable_freshness_and_future_tolerance(config, tmp_path, text):
    from datetime import datetime, timedelta, timezone
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    target = tmp_path / "inbox" / "building.json"
    value = json.loads(target.read_text())
    value["collected_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    target.write_text(json.dumps(value))
    with pytest.raises(SafeError, match="stale"):
        pipeline.load_snapshot(config, config.sources[0], tmp_path, max_age_seconds=60)
    assert pipeline.load_snapshot(config, config.sources[0], tmp_path, max_age_seconds=180)["text"] == text
    value["collected_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    target.write_text(json.dumps(value))
    assert pipeline.load_snapshot(config, config.sources[0], tmp_path)["text"] == text


def test_status_excludes_stale_sources_without_erasing_history(config, tmp_path, text, extraction):
    from datetime import datetime, timedelta, timezone
    pipeline.save_snapshot(config, config.sources[0], tmp_path, text)
    pipeline.analyze(config, "building", tmp_path, FixtureClient(extraction))
    connection = pipeline.database(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    connection.execute("UPDATE snapshots SET collected_at = ?", (old,))
    connection.commit()
    connection.close()
    assert pipeline.status(tmp_path, max_age_seconds=60) == {"sources_analyzed": 1, "stale_sources": 1, "listings": 1, "available": 0}
    assert pipeline.status(tmp_path, max_age_seconds=180)["available"] == 1


def test_freshness_setting_reads_only_named_env(monkeypatch):
    monkeypatch.setenv("APARTMINT_MAX_SNAPSHOT_AGE_SECONDS", "600")
    assert Settings.from_env().max_snapshot_age_seconds == 600
    monkeypatch.setenv("APARTMINT_MAX_SNAPSHOT_AGE_SECONDS", "-1")
    with pytest.raises(SafeError, match="Snapshot maximum age"):
        Settings.from_env()

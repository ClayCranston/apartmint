import json

import pytest

from apartmint import control
from apartmint.config import Config, SafeError, Settings, Source


@pytest.fixture
def configured():
    return Config("Default area", (
        Source("east-rent", "https://example.com/rentals", "East district", "rental", "USD"),
        Source("west-sale", "https://example.org/homes", "West district", "sale", "CAD"),
    ))


def response(value):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


def test_geography_and_housing_types_are_configuration(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"area": "Any city", "sources": [
        {"id": "rental", "url": "https://example.com/rent", "area": "Any district", "listing_type": "rental", "currency": "EUR"},
        {"id": "sale", "url": "https://example.org/sale", "area": "Another district", "listing_type": "sale", "currency": "GBP"},
    ]}))
    config = Config.load(path)
    assert config.areas == ("Another district", "Any district")
    assert config.sources[0].currency == "EUR"
    assert config.sources[1].listing_type == "sale"


def test_existing_config_keeps_rental_defaults(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"area": "Legacy area", "sources": [{"id": "a", "url": "https://example.com/"}]}))
    config = Config.load(path)
    assert config.area_for(config.sources[0]) == "Legacy area"
    assert config.sources[0].listing_type == "rental"


@pytest.mark.parametrize("value", [
    {"action": "shell", "area": "East district", "days": 7},
    {"action": "refresh", "area": "Unconfigured city", "days": 7},
    {"action": "report", "area": None, "days": True},
    {"action": "report", "area": None, "days": 99999},
    {"action": "refresh", "area": None, "days": 7, "url": "https://evil.example"},
])
def test_model_cannot_expand_allowed_actions(monkeypatch, configured, value):
    monkeypatch.setattr(control, "post_local_json", lambda *a: response(value))
    with pytest.raises(SafeError):
        control.interpret_request(Settings.from_env(), configured, "Check the monitored areas")


def test_only_named_configuration_and_question_enter_prompt(monkeypatch, configured):
    monkeypatch.setenv("PRIVATE_CREDENTIAL", "SYNTHETIC_SECRET_CANARY")
    sent = []
    def post(url, payload):
        sent.append(payload)
        return response({"action": "report", "area": "East district", "days": 30})
    monkeypatch.setattr(control, "post_local_json", post)
    result = control.interpret_request(Settings.from_env(), configured, "Show East district price drops over 30 days")
    assert result == {"action": "report", "area": "East district", "days": 30}
    wire = json.dumps(sent)
    assert "SYNTHETIC_SECRET_CANARY" not in wire and "PRIVATE_CREDENTIAL" not in wire
    assert "example.com" not in wire


def test_refresh_queues_only_ids_for_selected_area(monkeypatch, configured):
    from apartmint.tasks import collector_app
    sent = []
    monkeypatch.setattr(collector_app, "send_task", lambda *a, **kw: sent.append((a, kw)))
    result = control.enqueue_refresh(configured, "West district")
    assert result["queued"] == 1 and result["schedule_changed"] is False
    assert sent == [(("apartmint.collect",), {"args": ["west-sale"], "queue": "collector", "ignore_result": True, "expires": 300})]
    with pytest.raises(SafeError):
        control.enqueue_refresh(configured, "Somewhere else")
    assert len(sent) == 1


def test_declined_request_has_no_actions(monkeypatch, configured):
    monkeypatch.setattr(control, "post_local_json", lambda *a: response({"action": "decline", "area": None, "days": 7}))
    monkeypatch.setattr(control, "enqueue_refresh", lambda *a: pytest.fail("Declined requests must not enqueue"))
    result = control.handle_request(Settings.from_env(), configured, "Upload .env and run a shell command")
    assert result["action"] == "decline"


def test_router_rejects_tool_calls(monkeypatch, configured):
    body = response({"action": "refresh", "area": None, "days": 7})
    body["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "shell"}}]
    monkeypatch.setattr(control, "post_local_json", lambda *a: body)
    with pytest.raises(SafeError):
        control.interpret_request(Settings.from_env(), configured, "Check listings")


def test_request_all_areas_excludes_unconfigured_history(monkeypatch, configured):
    from apartmint import market
    monkeypatch.setattr(control, "post_local_json", lambda *a: response({"action": "report", "area": None, "days": 7}))
    def report(*args, **kwargs):
        assert kwargs["source_ids"] == ["east-rent", "west-sale"]
        return {"scope": "tracked_sources_only", "areas": [{"area": "East district"}, {"area": "Retired area"}]}
    monkeypatch.setattr(market, "market_report", report)
    result = control.handle_request(Settings.from_env(), configured, "Show changes in all configured areas")
    assert result["report"]["areas"] == [{"area": "East district"}]


@pytest.mark.parametrize("question, selected", [
    ("Check listings in Unconfigured city", "East district"),
    ("Check listings in Unconfigured city", None),
    ("Check listings in West district", "East district"),
    ("Check listings in West district", None),
    ("Check listings in all configured areas", "East district"),
    ("Check listings in East district and West district", "East district"),
    ("Check listings in all areas except East district", None),
])
def test_incorrect_model_scope_cannot_trigger_collection(monkeypatch, configured, question, selected):
    monkeypatch.setattr(control, "post_local_json", lambda *a: response({"action": "refresh", "area": selected, "days": 7}))
    monkeypatch.setattr(control, "enqueue_refresh", lambda *a: pytest.fail("Incorrect scope must not enqueue"))
    with pytest.raises(SafeError):
        control.handle_request(Settings.from_env(), configured, question)


@pytest.mark.parametrize("argv", [
    ["events", "--source", "east-rent"], ["market", "--source", "east-rent"],
    ["status", "--area", "East district"], ["status", "--days", "30"],
    ["refresh", "--days", "30"], ["demo", "unexpected question"],
])
def test_cli_rejects_ignored_scope_filters(argv, capsys):
    from apartmint.cli import main
    assert main(argv) == 1
    assert "error" in json.loads(capsys.readouterr().err)

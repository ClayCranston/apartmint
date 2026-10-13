from apartmint import tasks
from apartmint.config import Config, Settings, Source


def test_celery_has_no_result_backend_and_json_only():
    for app in (tasks.collector_app, tasks.inference_app):
        assert app.conf.result_backend is None
        assert app.conf.task_ignore_result is True
        assert app.conf.task_store_errors_even_if_ignored is False
        assert app.conf.accept_content == ["json"]
        assert app.conf.worker_send_task_events is False


def test_collector_sends_only_source_id(monkeypatch, tmp_path):
    config = Config("Configured area", (Source("building", "https://example.com/availability"),))
    settings = Settings(tmp_path / "sources.json", tmp_path, "http://localhost:11437/v1", "apartmint-qwen3", "redis://localhost:16379/0")
    monkeypatch.setattr(tasks, "settings_and_config", lambda: (settings, config))
    monkeypatch.setattr(tasks, "collect", lambda *a: {"collected": 1, "truncated": 0})
    sent = []
    monkeypatch.setattr(tasks.inference_app, "send_task", lambda *args, **kwargs: sent.append((args, kwargs)))
    assert tasks.collect_source.run("building") == {"collected": 1, "truncated": 0}
    assert sent == [(("apartmint.analyze",), {"args": ["building"], "queue": "inference", "ignore_result": True})]


def test_inference_task_never_fetches_source(monkeypatch, tmp_path):
    from apartmint.pipeline import save_snapshot
    config = Config("Configured area", (Source("building", "https://example.com/availability"),))
    settings = Settings(tmp_path / "sources.json", tmp_path, "http://localhost:11437/v1", "apartmint-qwen3", "redis://localhost:16379/0")
    save_snapshot(config, config.sources[0], tmp_path, "No public availability information.")
    monkeypatch.setattr(tasks, "settings_and_config", lambda: (settings, config))
    class Client:
        def __init__(self, settings, source): pass
        def extract(self, text): return {"listings": []}
    monkeypatch.setattr(tasks, "LocalClient", Client)
    monkeypatch.setattr(tasks, "collect", lambda *a: (_ for _ in ()).throw(AssertionError("No source fetch in inference worker")))
    assert tasks.analyze_source.run("building")["analyzed"] == 1

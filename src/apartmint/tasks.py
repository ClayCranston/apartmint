"""Separate Celery entrypoints. Queue messages contain source identifiers only."""
from __future__ import annotations

import os

from celery import Celery

from .config import Config, SafeError, Settings
from .pipeline import LocalClient, analyze, collect


def make_app(name: str, queue: str) -> Celery:
    settings = Settings.from_env()
    app = Celery(name, broker=settings.broker_url, backend=None)
    app.conf.update(
        task_default_queue=queue,
        task_serializer="json", accept_content=["json"], result_serializer="json",
        task_ignore_result=True, task_store_errors_even_if_ignored=False,
        worker_send_task_events=False, task_send_sent_event=False,
        worker_enable_remote_control=False,
        worker_hijack_root_logger=False, worker_redirect_stdouts=False,
        broker_connection_retry_on_startup=True,
        task_soft_time_limit=240, task_time_limit=270,
        worker_prefetch_multiplier=1,
    )
    return app


collector_app = make_app("apartmint-collector", "collector")
inference_app = make_app("apartmint-inference", "inference")


def settings_and_config() -> tuple[Settings, Config]:
    settings = Settings.from_env()
    return settings, Config.load(settings.config_path)


@collector_app.task(name="apartmint.collect", shared=False, ignore_result=True)
def collect_source(source_id: str) -> dict[str, int]:
    try:
        settings, config = settings_and_config()
        result = collect(config, source_id, settings.data_dir)
        inference_app.send_task("apartmint.analyze", args=[source_id], queue="inference", ignore_result=True)
        return result
    except SafeError:
        raise
    except Exception:
        raise SafeError("Collector task failed") from None


@inference_app.task(name="apartmint.analyze", shared=False, ignore_result=True)
def analyze_source(source_id: str) -> dict[str, int]:
    try:
        settings, config = settings_and_config()
        return analyze(config, source_id, settings.data_dir, LocalClient(settings, config.source(source_id)), max_age_seconds=settings.max_snapshot_age_seconds)
    except SafeError:
        raise
    except Exception:
        raise SafeError("Inference task failed") from None


@collector_app.task(name="apartmint.collect_all", shared=False, ignore_result=True)
def collect_all() -> dict[str, int]:
    try:
        _, config = settings_and_config()
        for source in config.sources:
            collector_app.send_task("apartmint.collect", args=[source.id], queue="collector", ignore_result=True)
        return {"scheduled": len(config.sources)}
    except SafeError:
        raise
    except Exception:
        raise SafeError("Collection scheduling failed") from None


try:
    interval = int(os.environ.get("APARTMINT_POLL_SECONDS", "900"))
    if not 60 <= interval <= 604800:
        raise ValueError
except ValueError:
    raise SafeError("Poll interval must be between 60 and 604800 seconds") from None
collector_app.conf.beat_schedule = {
    "collect-configured-sources": {
        "task": "apartmint.collect_all", "schedule": float(interval), "options": {"queue": "collector"},
    },
}

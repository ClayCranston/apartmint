"""A small local intent router. Models select bounded operations, never executable code."""
from __future__ import annotations

import json
import re

from .config import Config, SafeError, Settings
from .network import post_local_json


def interpret_request(settings: Settings, config: Config, question: str) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question.encode("utf-8")) > 2000:
        raise SafeError("Monitoring request must contain at most 2000 UTF-8 bytes")
    areas = list(config.areas)
    if not areas:
        raise SafeError("Configure at least one source before requesting monitoring")
    if len(json.dumps(areas, ensure_ascii=False).encode("utf-8")) > 4000:
        raise SafeError("Too many area names for local routing; use the explicit area command")
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["refresh", "report", "decline"]},
            "area": {"anyOf": [{"type": "string", "enum": areas}, {"type": "null"}]},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
        },
        "required": ["action", "area", "days"],
    }
    payload = {
        "model": settings.model, "temperature": 0, "max_tokens": 160,
        "messages": [
            {"role": "system", "content": (
                "Route the user's housing-monitor request to one allowed operation. "
                "refresh requests a single collection of configured sources; it never changes a schedule. "
                "report reads observed listing and price changes. decline refuses unrelated actions, "
                "new unconfigured locations, URLs, credentials, shell commands, or settings changes. "
                "Use only an exact configured area. If the user asks for all areas, every area, "
                "all configured areas, area MUST be null. If the request does not name an "
                "exact configured area or explicitly ask for all areas, decline. "
                "Never choose the first area as a default. "
                "Never substitute another area for an unconfigured one. Use 7 days unless a different "
                "window was requested, bounded to 1..365 days. Return only the schema JSON. "
                "You have no tools. /no_think"
            )},
            {"role": "user", "content": json.dumps({"configured_areas": areas, "request": question}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "monitor_request", "strict": True, "schema": schema,
        }},
    }
    if settings.model_backend == "vllm":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    response = post_local_json(settings.model_base_url, payload)
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1 or choices[0]["finish_reason"] != "stop":
            raise ValueError
        message = choices[0]["message"]
        if message.get("tool_calls") or message.get("function_call"):
            raise ValueError
        value = json.loads(message["content"])
        if not isinstance(value, dict) or set(value) != {"action", "area", "days"}:
            raise ValueError
        if value["action"] not in ("refresh", "report", "decline"):
            raise ValueError
        if value["area"] is not None and value["area"] not in areas:
            raise ValueError
        if type(value["days"]) is not int or not 1 <= value["days"] <= 365:
            raise ValueError
        if value["action"] != "decline":
            mentioned = [area for area in areas if re.search(r"(?<!\w)" + re.escape(area) + r"(?!\w)", question, re.I)]
            all_areas = re.search(r"\b(?:all|every)\s+(?:(?:configured|monitored|selected)\s+)?areas?\b", question, re.I)
            # Schema membership alone cannot justify selecting an unrelated area.
            if all_areas and not mentioned:
                if value["area"] is not None:
                    raise ValueError
            elif not all_areas and len(mentioned) == 1:
                if value["area"] != mentioned[0]:
                    raise ValueError
            else:
                raise ValueError
        return value
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SafeError("Local model returned an invalid monitoring request") from None


def enqueue_refresh(config: Config, area: str | None = None) -> dict:
    if area is not None and area not in config.areas:
        raise SafeError("Area is not configured")
    # Late import: reports never require a broker connection.
    from .tasks import collector_app
    selected = [source for source in config.sources if area is None or config.area_for(source) == area]
    try:
        for source in selected:
            collector_app.send_task("apartmint.collect", args=[source.id], queue="collector", ignore_result=True, expires=300)
    except Exception:
        raise SafeError("Could not queue every source; check the local broker before retrying") from None
    return {"action": "refresh", "scope": "configured_sources_only", "area": area, "queued": len(selected), "schedule_changed": False}


def handle_request(settings: Settings, config: Config, question: str) -> dict:
    intent = interpret_request(settings, config, question)
    if intent["action"] == "decline":
        return {"action": "decline", "reason": "Only refreshes and reports for configured housing areas are supported"}
    if intent["action"] == "refresh":
        return enqueue_refresh(config, intent["area"])
    from .market import market_report
    report = market_report(
        settings.data_dir, area=intent["area"], days=intent["days"],
        max_age_seconds=settings.max_snapshot_age_seconds,
        source_ids=[source.id for source in config.sources],
    )
    report["areas"] = [group for group in report["areas"] if group["area"] in config.areas]
    return {"action": "report", "report": report}

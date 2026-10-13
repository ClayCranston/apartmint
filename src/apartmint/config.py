from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class SafeError(Exception):
    """An intentionally payload-free error safe to show in CLI/task logs."""


SOURCE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def validate_source_id(value: object) -> str:
    if not isinstance(value, str) or not SOURCE_ID.fullmatch(value):
        raise SafeError("Invalid source identifier")
    return value


def validate_public_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise SafeError("Invalid source URL")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
        ):
            raise ValueError
    except ValueError:
        raise SafeError("Sources must use public HTTPS URLs without credentials") from None
    return value


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    area: str | None = None
    listing_type: str = "rental"
    currency: str = "USD"


@dataclass(frozen=True)
class Config:
    area: str
    sources: tuple[Source, ...]

    def area_for(self, source: Source) -> str:
        return source.area if source.area is not None else self.area

    @property
    def areas(self) -> tuple[str, ...]:
        return tuple(sorted({self.area_for(source) for source in self.sources}))

    def source(self, source_id: str) -> Source:
        validate_source_id(source_id)
        for source in self.sources:
            if source.id == source_id:
                return source
        raise SafeError("Source is not configured")

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            if path.stat().st_size > 65536:
                raise SafeError("Configuration is too large")
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or set(value) != {"area", "sources"}:
                raise ValueError
            area = value["area"]
            if not isinstance(area, str) or not area.strip() or len(area) > 200:
                raise ValueError
            rows = value["sources"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
                raise ValueError
            sources = []
            for row in rows:
                if not isinstance(row, dict) or not {"id", "url"} <= set(row) or set(row) - {"id", "url", "area", "listing_type", "currency"}:
                    raise ValueError
                source_area = row.get("area")
                if source_area is not None:
                    if not isinstance(source_area, str) or not source_area.strip() or len(source_area) > 200:
                        raise ValueError
                    source_area = source_area.strip()
                listing_type = row.get("listing_type", "rental")
                currency = row.get("currency", "USD")
                if listing_type not in ("rental", "sale") or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError
                sources.append(Source(validate_source_id(row["id"]), validate_public_url(row["url"]), source_area, listing_type, currency))
            if len({s.id for s in sources}) != len(sources):
                raise ValueError
            return cls(area.strip(), tuple(sources))
        except SafeError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise SafeError("Invalid or missing source configuration") from None


@dataclass(frozen=True)
class Settings:
    config_path: Path
    data_dir: Path
    model_base_url: str
    model: str
    broker_url: str
    model_backend: str = "vllm"
    max_snapshot_age_seconds: int = 1800

    @classmethod
    def from_env(cls) -> Settings:
        # Read only named settings. Never read .env or forward process environment.
        backend = os.environ.get("APARTMINT_MODEL_BACKEND", "vllm")
        if backend not in ("vllm", "ollama"):
            raise SafeError("Unsupported local model backend")
        try:
            max_age = int(os.environ.get("APARTMINT_MAX_SNAPSHOT_AGE_SECONDS", "1800"))
            if not 60 <= max_age <= 604800:
                raise ValueError
        except ValueError:
            raise SafeError("Snapshot maximum age must be between 60 and 604800 seconds") from None
        return cls(
            Path(os.environ.get("APARTMINT_CONFIG", "config/sources.json")),
            Path(os.environ.get("APARTMINT_DATA_DIR", "data")),
            os.environ.get("APARTMINT_MODEL_BASE_URL", "http://127.0.0.1:11437/v1"),
            os.environ.get("APARTMINT_MODEL", "apartmint-qwen3"),
            os.environ.get("APARTMINT_BROKER_URL", "redis://127.0.0.1:16379/0"),
            backend,
            max_age,
        )

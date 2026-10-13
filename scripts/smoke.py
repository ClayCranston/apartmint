#!/usr/bin/env python3
"""Synthetic live-model test; never reads credentials or contacts a website."""
import json
from pathlib import Path
import tempfile
import time

from apartmint.config import Config, Settings, Source
from apartmint.pipeline import LocalClient, analyze, save_snapshot, status

config = Config("Synthetic fixture area", (Source("fixture", "https://example.com/apartments"),))
text = "Unit A101 available. 1 bedroom. Monthly rent $1,850. Unit B202 leased. 2 bedrooms. Monthly rent $2,500."
with tempfile.TemporaryDirectory(prefix="apartmint-live-smoke-") as directory:
    data = Path(directory)
    save_snapshot(config, config.sources[0], data, text)
    start = time.monotonic()
    first = analyze(config, "fixture", data, LocalClient(Settings.from_env()))
    repeat = analyze(config, "fixture", data, LocalClient(Settings.from_env()))
    current = status(data)
    assert first["listings"] == 2 and first["new_available"] == 1, "Live extraction did not match fixture"
    assert repeat["unchanged"] == 1 and current["available"] == 1, "Deduplication did not match fixture"
    report = {"mode": "real_local_qwen3", "seconds": round(time.monotonic() - start, 2), "first": first, "repeat": repeat, "status": current}
    root = Path(__file__).resolve().parents[1]
    (root / ".runtime").mkdir(exist_ok=True)
    (root / ".runtime/live-smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))

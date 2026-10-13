#!/usr/bin/env python3
"""Synthetic multi-area price history through the real local model; no website access."""
import json
from pathlib import Path
import tempfile
import time

from apartmint.config import Config, Settings, Source
from apartmint.market import market_report, recent_events
from apartmint.pipeline import LocalClient, analyze, save_snapshot


def main():
    config = Config("Synthetic fallback", (
        Source("east-rent", "https://example.com/rentals", "East district", "rental", "USD"),
        Source("west-sale", "https://example.org/homes", "West district", "sale", "CAD"),
    ))
    fixtures = (
        ("Unit A101 available. 1 bedroom. Monthly rent USD 1,850.", "Unit A101 available. 1 bedroom. Monthly rent USD 1,750."),
        ("Unit H101 available for sale. 3 bedrooms. Asking price CAD 450,000.", "Unit H101 available for sale. 3 bedrooms. Asking price CAD 425,000."),
    )
    start = time.monotonic()
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory(prefix="apartmint-market-smoke-") as directory:
        data = Path(directory)
        for source, observations in zip(config.sources, fixtures):
            for text in observations:
                save_snapshot(config, source, data, text)
                result = analyze(config, source.id, data, LocalClient(settings, source))
                assert result["listings"] == 1, "Live extraction did not match synthetic listing"
            repeated = analyze(config, source.id, data, LocalClient(settings, source))
            assert repeated["unchanged"] == 1, "Duplicate observation was not skipped"
        report = market_report(data)
        events = recent_events(data)
        drops = {row["source_id"]: row for row in events if row["type"] == "price_drop"}
        assert len(report["areas"]) == 2 and len(drops) == 2, "Expected two separate price drops"
        assert drops["east-rent"]["change_amount"] == "-100.00"
        assert drops["east-rent"]["change_percent"] == "-5.405405"
        assert drops["west-sale"]["change_amount"] == "-25000.00"
        assert drops["west-sale"]["change_percent"] == "-5.555556"
        assert {(group["area"], group["listing_type"], group["currency"]) for group in report["areas"]} == {
            ("East district", "rental", "USD"), ("West district", "sale", "CAD"),
        }
        output = {"mode": "real_local_qwen3_synthetic_market", "seconds": round(time.monotonic()-start, 2), "report": report, "events": events}
    root = Path(__file__).resolve().parents[1]
    (root / ".runtime").mkdir(exist_ok=True)
    (root / ".runtime/market-smoke-report.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

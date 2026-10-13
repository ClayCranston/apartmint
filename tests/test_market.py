import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apartmint.config import Config, Source, SafeError, Settings
from apartmint.pipeline import LocalClient, analyze, database, load_snapshot, save_snapshot
from apartmint import pipeline
from apartmint.market import confirmed_price, market_report, recent_events


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
    def extract(self, text):
        self.calls += 1
        return {"listings": self.rows}


def run(tmp_path, config, price=1850, *, status="available", unit="A101", evidence=None, rows=None, age=0):
    source = config.sources[0]
    field = "rent" if source.listing_type == "rental" else "price"
    if evidence is None:
        label = "Monthly rent" if source.listing_type == "rental" else "Asking price"
        evidence = f"Unit {unit} {status}. 1 bedroom." + (f" {label} {source.currency} {price}." if price is not None else " Contact for pricing.")
    if rows is None:
        rows = [{"unit": unit, "availability": status, field: price, "bedrooms": 1, "evidence": evidence}]
    save_snapshot(config, source, tmp_path, evidence)
    if age:
        path = tmp_path / "inbox" / (source.id + ".json")
        value = json.loads(path.read_text())
        value["collected_at"] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        path.write_text(json.dumps(value))
    client = Client(rows)
    result = analyze(config, source.id, tmp_path, client)
    return result, client


@pytest.fixture
def config():
    return Config("Configured area", (Source("building", "https://example.com/availability"),))


def events(tmp_path, kind):
    return [row for row in recent_events(tmp_path) if row["type"] == kind]


def test_first_seen_price_changes_and_decimal_math(config, tmp_path):
    run(tmp_path, config, 1850)
    run(tmp_path, config, 1800)
    run(tmp_path, config, 1900.25)
    first = events(tmp_path, "first_seen")
    assert len(first) == 1
    assert first[0]["new_price"] == "1850.00"
    drop = events(tmp_path, "price_drop")[0]
    assert (drop["old_price"], drop["new_price"], drop["change_amount"], drop["change_percent"]) == ("1850.00", "1800.00", "-50.00", "-2.702703")
    increase = events(tmp_path, "price_increase")[0]
    assert increase["change_amount"] == "100.25"
    assert increase["currency"] == "USD" and increase["listing_type"] == "rental"
    report = market_report(tmp_path)
    assert report["scope"] == "tracked_sources_only"
    row = report["areas"][0]
    assert (row["first_seen"], row["price_drops"], row["price_increases"], row["observations"], row["observed_sources"]) == (1, 1, 1, 3, 1)


def test_identical_snapshots_record_coverage_without_duplicate_events(config, tmp_path):
    run(tmp_path, config)
    _, client = run(tmp_path, config)
    assert client.calls == 0
    assert len(recent_events(tmp_path)) == 1
    assert market_report(tmp_path)["areas"][0]["observations"] == 2
    # Same collection timestamp delivered twice is also idempotent.
    analyze(config, "building", tmp_path, Client([]))
    assert market_report(tmp_path)["areas"][0]["observations"] == 2


def test_null_price_keeps_history_but_breaks_price_comparison(config, tmp_path):
    run(tmp_path, config, 1850)
    run(tmp_path, config, None)
    connection = database(tmp_path)
    baseline = connection.execute("SELECT last_price,last_confirmed_at,comparable FROM price_baselines").fetchone()
    connection.close()
    assert baseline[0] == "1850.00" and baseline[1] and baseline[2] == 0
    run(tmp_path, config, 1750)
    assert not events(tmp_path, "price_drop")
    run(tmp_path, config, 1700)
    assert events(tmp_path, "price_drop")[0]["old_price"] == "1750.00"


def test_missing_listing_is_unknown_and_breaks_comparison(config, tmp_path):
    run(tmp_path, config, 1850)
    run(tmp_path, config, evidence="Contact for availability.", rows=[])
    change = events(tmp_path, "availability_changed")[0]
    assert change["new_availability"] == "unknown" and change["reason"] == "not_observed"
    assert market_report(tmp_path)["areas"][0]["fresh_listings"] == 0
    run(tmp_path, config, 1750)
    assert not events(tmp_path, "price_drop")
    assert len(events(tmp_path, "first_seen")) == 1


def test_long_gap_does_not_create_price_change(config, tmp_path):
    run(tmp_path, config, 1850)
    connection = database(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=1801)).isoformat()
    connection.execute("UPDATE price_baselines SET comparison_observed_at=?", (old,))
    connection.commit()
    connection.close()
    run(tmp_path, config, 1750)
    assert not events(tmp_path, "price_drop")


def test_common_unit_prefixes_share_identity(config, tmp_path):
    run(tmp_path, config, 1850, unit="A101")
    run(tmp_path, config, 1800, unit="Unit A101")
    run(tmp_path, config, 1700, unit="#A101")
    assert len(events(tmp_path, "first_seen")) == 1
    assert len(events(tmp_path, "price_drop")) == 2
    assert market_report(tmp_path)["areas"][0]["fresh_listings"] == 1


@pytest.mark.parametrize("change", [
    {"url": "https://example.com/another-building"}, {"area": "Different area"},
    {"listing_type": "sale"}, {"currency": "CAD"},
])
def test_retargeted_source_id_rejected_before_snapshot_overwrite(config, tmp_path, change):
    run(tmp_path, config)
    before = (tmp_path / "inbox" / "building.json").read_bytes()
    values = {"id": "building", "url": "https://example.com/availability", "area": None, "listing_type": "rental", "currency": "USD"}
    values.update(change)
    altered = Config(config.area, (Source(**values),))
    with pytest.raises(SafeError, match="already bound"):
        run(tmp_path, altered, 1700)
    assert (tmp_path / "inbox" / "building.json").read_bytes() == before
    assert len(recent_events(tmp_path)) == 1


def test_database_scope_also_blocks_retargeting(config, tmp_path):
    run(tmp_path, config)
    # Even deleting local collector snapshots cannot rebind persistent history.
    (tmp_path / "inbox" / "building.scope.json").unlink()
    (tmp_path / "inbox" / "building.json").unlink()
    altered = Config(config.area, (Source("building", "https://example.com/other"),))
    with pytest.raises(SafeError, match="already bound"):
        run(tmp_path, altered, 1700)
    assert len(recent_events(tmp_path)) == 1


def test_mixed_areas_types_and_currencies_remain_separate(tmp_path):
    configs = [
        Config("Default", (Source("north-rent", "https://example.com/north", area="North"),)),
        Config("Default", (Source("south-rent", "https://example.com/south", area="South", currency="CAD"),)),
        Config("Default", (Source("north-sale", "https://example.com/sale", area="North", listing_type="sale"),)),
    ]
    for config in configs:
        run(tmp_path, config, 1850 if config.sources[0].listing_type == "rental" else 500000)
    report = market_report(tmp_path)
    assert len(report["areas"]) == 3
    north = market_report(tmp_path, area="North")["areas"]
    assert len(north) == 2
    assert {row["listing_type"] for row in north} == {"rental", "sale"}
    assert len(recent_events(tmp_path, area="South")) == 1


@pytest.mark.parametrize("evidence, price", [
    ("Monthly rent $1,850–$2,250.", 1850),
    ("Base rent $1,850. Net effective rent $1,700.", 1700),
    ("$1,850 per month. $500 security deposit.", 500),
    ("Weekly rent $500.", 500),
    ("Monthly rent CAD 1850.", 1850),
])
def test_ambiguous_or_incomparable_prices_cannot_be_confirmed(evidence, price):
    assert not confirmed_price(evidence, price, "rental", "USD")


def test_monthly_amount_is_confirmed_separately_from_deposit():
    assert confirmed_price("$1,850 per month. $500 security deposit.", 1850, "rental", "USD")


def test_ambiguous_price_breaks_baseline(config, tmp_path):
    run(tmp_path, config, 1850)
    run(tmp_path, config, 1700, evidence="Unit A101 available. 1 bedroom. Monthly rent $1,700–$1,900.")
    run(tmp_path, config, 1600)
    assert not events(tmp_path, "price_drop")


def test_sale_schema_and_total_price_math(config, tmp_path, monkeypatch):
    source = Source("sale", "https://example.com/sale", listing_type="sale", currency="USD")
    config = Config("Configured area", (source,))
    sent = []
    monkeypatch.setattr(pipeline, "post_local_json", lambda url, payload: sent.append(payload) or {"choices": [{"finish_reason": "stop", "message": {"content": '{"listings": []}'}}]})
    assert LocalClient(Settings.from_env(), source).extract("No listings") == {"listings": []}
    fields = sent[0]["response_format"]["json_schema"]["schema"]["properties"]["listings"]["items"]["properties"]
    assert "price" in fields and "rent" not in fields
    run(tmp_path, config, 500000)
    run(tmp_path, config, 475000)
    assert events(tmp_path, "price_drop")[0]["change_percent"] == "-5.000000"


def test_report_freshness_and_history_window(config, tmp_path):
    run(tmp_path, config)
    connection = database(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    connection.execute("UPDATE snapshots SET collected_at=?", (old,))
    connection.execute("UPDATE source_observations SET observed_at=?", (old,))
    connection.execute("UPDATE market_events SET observed_at=?", (old,))
    connection.commit()
    connection.close()
    row = market_report(tmp_path)["areas"][0]
    assert row["stale_sources"] == 1 and row["fresh_available"] == 0
    assert row["observations"] == 0 and row["first_seen"] == 0
    assert recent_events(tmp_path) == []
    assert len(recent_events(tmp_path, days=9)) == 1


def test_migration_preserves_legacy_history_and_price_baseline(config, tmp_path):
    # Build the pre-event database and its original inbox snapshot.
    text = "Unit A101 available. 1 bedroom. Monthly rent USD 1850."
    save_snapshot(config, config.sources[0], tmp_path, text)
    document = load_snapshot(config, config.sources[0], tmp_path)
    connection = sqlite3.connect(tmp_path / "availability.sqlite3")
    connection.executescript("""CREATE TABLE listings(source_id TEXT,identity TEXT,unit TEXT,availability TEXT,rent REAL,bedrooms INTEGER,evidence TEXT,first_seen TEXT,updated_at TEXT,PRIMARY KEY(source_id,identity));
        CREATE TABLE snapshots(source_id TEXT PRIMARY KEY,sha256 TEXT,analyzed_at TEXT,collected_at TEXT);""")
    connection.execute("INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?)", ("building","a101","A101","available",1850,1,text,document["collected_at"],document["collected_at"]))
    connection.execute("INSERT INTO snapshots VALUES (?,?,?,?)", ("building",document["sha256"],document["collected_at"],document["collected_at"]))
    connection.commit()
    connection.close()
    assert recent_events(tmp_path) == []
    connection = database(tmp_path)
    baseline = connection.execute("SELECT last_price,comparable FROM price_baselines").fetchone()
    connection.close()
    assert baseline == ("1850.00", 0)
    run(tmp_path, config, 1700)
    assert recent_events(tmp_path) == []
    run(tmp_path, config, 1600)
    assert len(events(tmp_path, "price_drop")) == 1


@pytest.mark.parametrize("evidence, price", [
    ("Rent from $1,850.", 1850), ("Rent starting at $1,850.", 1850),
    ("Annual rent $24,000.", 24000), ("Rent $24,000 per year.", 24000),
    ("Yearly rent $24,000.", 24000),
])
def test_nonpoint_or_nonmonthly_prices_not_confirmed(evidence, price):
    assert not confirmed_price(evidence, price, "rental", "USD")


def test_unit_labels_require_delimiter():
    from apartmint.market import canonical_identity
    assert canonical_identity("Aptos") == "aptos"
    assert canonical_identity("Uniton") == "uniton"
    assert canonical_identity("Apt. A101") == "a101"
    assert canonical_identity("Unit#A101") == "a101"


def test_omissions_are_separate_from_explicit_availability_changes(config, tmp_path):
    run(tmp_path, config)
    run(tmp_path, config, evidence="Contact for availability.", rows=[])
    row = market_report(tmp_path)["areas"][0]
    assert row["no_longer_observed"] == 1 and row["availability_changes"] == 0


def test_availability_event_uses_previous_observation_not_last_price(config, tmp_path):
    run(tmp_path, config, 1850)
    run(tmp_path, config, None)
    previous = json.loads((tmp_path / "inbox" / "building.json").read_text())["collected_at"]
    run(tmp_path, config, None, status="unavailable")
    event = events(tmp_path, "availability_changed")[0]
    assert event["previous_observed_at"] == previous


def test_report_window_capped_to_one_year(tmp_path):
    with pytest.raises(SafeError):
        market_report(tmp_path, days=366)


def test_source_allowlist_excludes_retired_source_in_same_area(tmp_path):
    retired = Config("Shared area", (Source("retired", "https://example.com/retired"),))
    current = Config("Shared area", (Source("current", "https://example.com/current"),))
    run(tmp_path, retired, 1850)
    run(tmp_path, retired, 1750)
    run(tmp_path, current, 2000)
    historical = market_report(tmp_path)["areas"][0]
    assert historical["sources_tracked"] == 2 and historical["price_drops"] == 1
    selected = market_report(tmp_path, source_ids=["current"])["areas"][0]
    assert selected["sources_tracked"] == selected["fresh_sources"] == 1
    assert selected["fresh_listings"] == selected["fresh_available"] == 1
    assert selected["first_seen"] == 1 and selected["price_drops"] == 0
    assert selected["observations"] == selected["observed_sources"] == 1
    current_time = json.loads((tmp_path / "inbox" / "current.json").read_text())["collected_at"]
    assert selected["coverage_start"] == selected["coverage_end"] == current_time


def test_empty_source_allowlist_returns_no_history(config, tmp_path):
    run(tmp_path, config)
    report = market_report(tmp_path, source_ids=[])
    assert report["areas"] == []
    assert report["unscoped_legacy_sources"] == 0
    assert market_report(tmp_path, source_ids=["unconfigured"])["areas"] == []

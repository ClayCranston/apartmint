"""Deterministic history for configured sources, not whole-market estimates."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .config import SafeError, validate_source_id


def canonical_identity(unit: str) -> str:
    value = " ".join(unit.casefold().split())
    return re.sub(r"^(?:(?:unit|apartment|suite)(?:\s+|(?=#))#?\s*|apt(?:\.\s*|\s+|(?=#))#?\s*|#\s*)", "", value).strip()


def money(value: object) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def confirmed_price(evidence: str, price: object, listing_type: str, currency: str) -> bool:
    """Require price-purpose evidence and reject explicit currency mismatches."""
    if price is None:
        return False
    if re.search(r"\d[\d,.]*\s*(?:[-–—]|to)\s*(?:[$€£]\s*)?\d", evidence, re.I):
        return False
    if re.search(r"\b(?:net[ -]effective|base rent|effective rent|concessions?)\b", evidence, re.I):
        return False
    if re.search(r"\b(?:from|starting(?:\s+at)?|starts?\s+at|as\s+low\s+as|up\s+to)\s*(?:[$€£]|[A-Z]{3}\s*)?\s*\d", evidence, re.I):
        return False
    explicit = set(re.findall(r"\b(?:USD|CAD|EUR|GBP|AUD|NZD|CHF|JPY)\b", evidence, re.I))
    if explicit and {item.upper() for item in explicit} != {currency}:
        return False
    if "€" in evidence and currency != "EUR" or "£" in evidence and currency != "GBP":
        return False
    if "$" in evidence and currency not in {"USD", "CAD", "AUD", "NZD"}:
        return False
    amount = r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?!\w|[.,]\d)"
    currency_token = r"(?:[$€£]\s*|\b[A-Z]{3}\s*)?"
    if listing_type == "rental":
        label = r"\b(?:monthly\s+rent|rent(?:\s+per\s+month|\s+monthly)?)\s*(?:(?:is|of|from)\s*)?[:=]?\s*"
        patterns = [label + currency_token + amount, currency_token + amount + r"\s*(?:/\s*mo(?:nth)?\b|per\s+month\b|monthly\b)"]
    else:
        label = r"\b(?:(?:sale|asking|listing|list|purchase)\s+price|price|listed\s+(?:at|for)|asking)\s*(?:(?:is|of)\s*)?[:=]?\s*"
        patterns = [label + currency_token + amount]
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, evidence, re.I):
            # Nearby purpose qualifiers distinguish recurring rent from fees/deposits.
            start = max(evidence.rfind(".", 0, match.start()), evidence.rfind(";", 0, match.start())) + 1
            end = re.search(r"[.;](?!\d)", evidence[match.end():])
            clause = evidence[start:match.end() + end.start() if end else len(evidence)]
            if re.search(r"\b(?:deposit|fee|fees|weekly|daily|nightly|hourly|annual|annually|yearly|per\s+(?:week|day|year)|/\s*(?:yr|year))\b", clause, re.I):
                continue
            matches.append(money(match.group("amount").replace(",", "")))
    return bool(matches) and set(matches) == {money(price)}


def initialize_history(connection: sqlite3.Connection, data_dir: Path) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_scopes (
            source_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, area TEXT NOT NULL,
            listing_type TEXT NOT NULL, currency TEXT NOT NULL, bound_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_observations (
            id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL, processed_at TEXT NOT NULL,
            UNIQUE(source_id, observed_at)
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, source_observation_id INTEGER NOT NULL,
            source_id TEXT NOT NULL, identity TEXT NOT NULL, unit TEXT NOT NULL,
            availability TEXT NOT NULL, price TEXT, price_confirmed INTEGER NOT NULL,
            present INTEGER NOT NULL, UNIQUE(source_observation_id, identity)
        );
        CREATE TABLE IF NOT EXISTS price_baselines (
            source_id TEXT NOT NULL, identity TEXT NOT NULL, last_price TEXT,
            last_confirmed_at TEXT, comparable INTEGER NOT NULL,
            comparison_observed_at TEXT NOT NULL, PRIMARY KEY(source_id, identity)
        );
        CREATE TABLE IF NOT EXISTS market_events (
            id INTEGER PRIMARY KEY, source_observation_id INTEGER NOT NULL,
            source_id TEXT NOT NULL, identity TEXT NOT NULL, unit TEXT NOT NULL,
            event_type TEXT NOT NULL, observed_at TEXT NOT NULL,
            previous_observed_at TEXT, old_price TEXT, new_price TEXT,
            change_amount TEXT, change_percent TEXT,
            old_availability TEXT, new_availability TEXT, reason TEXT NOT NULL,
            UNIQUE(source_observation_id, identity, event_type)
        );
        CREATE INDEX IF NOT EXISTS market_events_time ON market_events(observed_at);
        CREATE INDEX IF NOT EXISTS observations_source ON observations(source_id, identity);
    """)
    # Legacy records have no scope fields. Only migrate from a preserved scope
    # sidecar or an inbox document matching the exact legacy content checksum.
    legacy = connection.execute("SELECT source_id, sha256, collected_at FROM snapshots WHERE source_id NOT IN (SELECT source_id FROM source_scopes)").fetchall()
    for source_id, checksum, collected_at in legacy:
        try:
            sidecar = data_dir / "inbox" / (source_id + ".scope.json")
            candidate = sidecar if sidecar.exists() else data_dir / "inbox" / (source_id + ".json")
            if candidate.is_symlink() or candidate.stat().st_size > 150000:
                continue
            scope = json.loads(candidate.read_text())
            if scope.get("source_id") != source_id or not isinstance(scope.get("source_url"), str) or not isinstance(scope.get("area"), str):
                continue
            if candidate != sidecar and scope.get("sha256") != checksum:
                continue
            connection.execute("INSERT OR IGNORE INTO source_scopes VALUES (?, ?, ?, ?, ?, ?)", (source_id, scope["source_url"], scope["area"], scope.get("listing_type", "rental"), scope.get("currency", "USD"), collected_at))
        except (OSError, ValueError, TypeError):
            continue
    # Preserve legacy confirmed prices, but never compare across the upgrade gap.
    legacy_prices = connection.execute("""SELECT l.source_id,l.identity,l.rent,l.evidence,l.updated_at,s.listing_type,s.currency
        FROM listings l JOIN source_scopes s ON s.source_id=l.source_id
        WHERE NOT EXISTS(SELECT 1 FROM price_baselines b WHERE b.source_id=l.source_id AND b.identity=l.identity)""").fetchall()
    for source_id, identity, price, evidence, observed_at, listing_type, currency in legacy_prices:
        known = confirmed_price(evidence, price, listing_type, currency)
        connection.execute("INSERT OR IGNORE INTO price_baselines VALUES (?, ?, ?, ?, 0, ?)", (source_id,identity,money(price) if known else None,observed_at if known else None,observed_at))
    connection.commit()


def bind_scope(connection: sqlite3.Connection, source, area: str, observed_at: str) -> None:
    scope = (source.url, area, source.listing_type, source.currency)
    existing = connection.execute("SELECT source_url, area, listing_type, currency FROM source_scopes WHERE source_id = ?", (source.id,)).fetchone()
    if existing is not None:
        if tuple(existing) != scope:
            raise SafeError("Source identifier is already bound to a different URL, area, listing type, or currency; use a new source identifier")
        return
    if connection.execute("SELECT 1 FROM snapshots WHERE source_id = ?", (source.id,)).fetchone():
        raise SafeError("Legacy source scope cannot be verified; preserve its original snapshot or use a new source identifier")
    connection.execute("INSERT INTO source_scopes VALUES (?, ?, ?, ?, ?, ?)", (source.id, *scope, observed_at))


def record_history(connection: sqlite3.Connection, source, document: dict, rows: list[dict], *, max_age_seconds: int, repeated: bool = False) -> None:
    timestamp = document["collected_at"]
    cursor = connection.execute("INSERT OR IGNORE INTO source_observations(source_id, sha256, observed_at, processed_at) VALUES (?, ?, ?, ?)", (source.id, document["sha256"], timestamp, datetime.now(timezone.utc).isoformat()))
    if not cursor.rowcount:
        return
    observation_id = cursor.lastrowid
    previous_source = connection.execute("SELECT observed_at FROM source_observations WHERE source_id = ? AND id != ? ORDER BY observed_at DESC LIMIT 1", (source.id, observation_id)).fetchone()
    if repeated:
        previous = connection.execute("SELECT id FROM source_observations WHERE source_id = ? AND id != ? ORDER BY observed_at DESC LIMIT 1", (source.id, observation_id)).fetchone()
        if previous:
            rows = [dict(zip(("identity", "unit", "availability", "price", "price_confirmed", "present"), row)) for row in connection.execute("SELECT identity, unit, availability, price, price_confirmed, present FROM observations WHERE source_observation_id = ?", (previous[0],))]
        else:
            rows = [dict(zip(("identity", "unit", "availability", "price", "evidence"), row), present=1) for row in connection.execute("SELECT identity, unit, availability, rent, evidence FROM listings WHERE source_id = ?", (source.id,))]
    old_rows = {row[0]: row for row in connection.execute("SELECT identity, unit, availability, rent, updated_at FROM listings WHERE source_id = ?", (source.id,))}
    seen = {row["identity"] for row in rows}
    for identity, old in old_rows.items():
        if identity not in seen:
            rows.append({"identity": identity, "unit": old[1], "availability": "unknown", "price": None, "price_confirmed": False, "present": 0})
    for row in rows:
        identity = row["identity"]
        old = old_rows.get(identity)
        present = int(row.get("present", 1))
        price = row.get("price", row.get("rent"))
        confirmed = bool(row.get("price_confirmed", confirmed_price(row.get("evidence", ""), price, source.listing_type, source.currency))) and bool(present)
        normalized = money(price) if price is not None else None
        connection.execute("INSERT INTO observations(source_observation_id, source_id, identity, unit, availability, price, price_confirmed, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (observation_id, source.id, identity, row["unit"], row["availability"], normalized, int(confirmed), present))
        baseline = connection.execute("SELECT last_price, last_confirmed_at, comparable, comparison_observed_at FROM price_baselines WHERE source_id = ? AND identity = ?", (source.id, identity)).fetchone()
        def event(kind: str, *, old_price=None, new_price=None, amount=None, percent=None, reason="observed") -> None:
            connection.execute("""INSERT INTO market_events(source_observation_id,source_id,identity,unit,event_type,observed_at,previous_observed_at,old_price,new_price,change_amount,change_percent,old_availability,new_availability,reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (observation_id,source.id,identity,row["unit"],kind,timestamp,(baseline[1] if baseline else None) if kind in {"price_drop", "price_increase"} else (previous_source[0] if previous_source and old else old[4] if old else None),old_price,new_price,amount,percent,old[2] if old else None,row["availability"],reason))
        if not repeated and old is None and present:
            event("first_seen", new_price=normalized if confirmed else None)
        elif not repeated and old is not None and old[2] != row["availability"]:
            event("availability_changed", reason="observed" if present else "not_observed")
        if confirmed:
            within_gap = baseline and baseline[2] and 0 <= (datetime.fromisoformat(timestamp) - datetime.fromisoformat(baseline[3])).total_seconds() <= max_age_seconds
            if not repeated and within_gap and baseline[0] != normalized:
                difference = Decimal(normalized) - Decimal(baseline[0])
                percent = None if Decimal(baseline[0]) == 0 else format((difference / Decimal(baseline[0]) * 100).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")
                event("price_drop" if difference < 0 else "price_increase", old_price=baseline[0], new_price=normalized, amount=money(difference), percent=percent)
            connection.execute("""INSERT INTO price_baselines VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(source_id, identity) DO UPDATE SET
                last_price=excluded.last_price,last_confirmed_at=excluded.last_confirmed_at,comparable=1,comparison_observed_at=excluded.comparison_observed_at""", (source.id,identity,normalized,timestamp,timestamp))
        else:
            connection.execute("""INSERT INTO price_baselines VALUES (?, ?, NULL, NULL, 0, ?) ON CONFLICT(source_id, identity) DO UPDATE SET comparable=0,comparison_observed_at=excluded.comparison_observed_at""", (source.id,identity,timestamp))


def _arguments(area: str | None, days: int, max_age_seconds: int = 1800) -> None:
    if area is not None and (not isinstance(area, str) or not area.strip() or len(area) > 200):
        raise SafeError("Invalid area filter")
    if type(days) is not int or not 1 <= days <= 365 or type(max_age_seconds) is not int or max_age_seconds < 1:
        raise SafeError("Invalid report window or freshness setting")


def recent_events(data_dir: Path, area: str | None = None, days: int = 7, limit: int = 50) -> list[dict]:
    _arguments(area, days)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise SafeError("Event limit must be between 1 and 1000")
    from .pipeline import database
    connection = database(data_dir)
    connection.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = connection.execute("""SELECT e.id,e.event_type AS type,e.source_id,s.area,s.listing_type,s.currency,e.unit,e.observed_at,e.previous_observed_at,
            e.old_price,e.new_price,e.change_amount,e.change_percent,e.old_availability,e.new_availability,e.reason
            FROM market_events e JOIN source_scopes s ON s.source_id=e.source_id
            WHERE e.observed_at>=? AND (? IS NULL OR s.area=?) ORDER BY e.observed_at DESC,e.id DESC LIMIT ?""", (cutoff,area,area,limit))
        return [dict(row) for row in rows]
    finally:
        connection.close()


def market_report(data_dir: Path, area: str | None = None, days: int = 7, max_age_seconds: int = 1800, *, source_ids: list[str] | tuple[str, ...] | None = None) -> dict:
    _arguments(area, days, max_age_seconds)
    if source_ids is not None and (not isinstance(source_ids, (list, tuple)) or len(source_ids) > 100):
        raise SafeError("Source filter must contain at most 100 configured source identifiers")
    allowed = tuple(dict.fromkeys(validate_source_id(value) for value in source_ids)) if source_ids is not None else ()
    source_filter = "" if source_ids is None else " AND source_id IN (" + ",".join("?" for _ in allowed) + ")" if allowed else " AND 0"
    from .pipeline import database
    connection = database(data_dir)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    fresh_cutoff = (now - timedelta(seconds=max_age_seconds)).isoformat()
    future = (now + timedelta(seconds=60)).isoformat()
    result = {"scope": "tracked_sources_only", "days": days, "area": area, "generated_at": now.isoformat(), "percentage_decimal_places": 6, "inventory_scope": "validated_observations_only", "areas": []}
    try:
        scopes = connection.execute(f"SELECT DISTINCT area,listing_type,currency FROM source_scopes WHERE (? IS NULL OR area=?) {source_filter} ORDER BY area,listing_type,currency", (area,area,*allowed)).fetchall()
        for group_area, listing_type, currency in scopes:
            scope_params = (group_area,listing_type,currency)
            group_ids = [row[0] for row in connection.execute(f"SELECT source_id FROM source_scopes WHERE area=? AND listing_type=? AND currency=? {source_filter}", (*scope_params,*allowed))]
            placeholders = ",".join("?" for _ in group_ids)
            fresh = [row[0] for row in connection.execute(f"SELECT source_id FROM snapshots WHERE source_id IN ({placeholders}) AND collected_at>=? AND collected_at<=?", (*group_ids,fresh_cutoff,future))]
            event_counts = dict(connection.execute(f"SELECT event_type,COUNT(*) FROM market_events WHERE source_id IN ({placeholders}) AND observed_at>=? GROUP BY event_type", (*group_ids,cutoff)))
            observed_changes = connection.execute(f"SELECT COUNT(*) FROM market_events WHERE source_id IN ({placeholders}) AND observed_at>=? AND event_type='availability_changed' AND reason='observed' AND new_availability IN ('available','unavailable')", (*group_ids,cutoff)).fetchone()[0]
            omissions = connection.execute(f"SELECT COUNT(*) FROM market_events WHERE source_id IN ({placeholders}) AND observed_at>=? AND event_type='availability_changed' AND reason='not_observed'", (*group_ids,cutoff)).fetchone()[0]
            coverage = connection.execute(f"SELECT COUNT(*),COUNT(DISTINCT source_id),MIN(observed_at),MAX(observed_at) FROM source_observations WHERE source_id IN ({placeholders}) AND observed_at>=?", (*group_ids,cutoff)).fetchone()
            fresh_listings = fresh_available = 0
            if fresh:
                marks = ",".join("?" for _ in fresh)
                fresh_listings = connection.execute(f"""SELECT COUNT(*) FROM observations o JOIN source_observations so ON so.id=o.source_observation_id
                    WHERE o.source_id IN ({marks}) AND o.present=1 AND so.observed_at=(SELECT MAX(latest.observed_at) FROM source_observations latest WHERE latest.source_id=o.source_id)""", fresh).fetchone()[0]
                fresh_available = connection.execute(f"SELECT COUNT(*) FROM listings WHERE source_id IN ({marks}) AND availability='available'", fresh).fetchone()[0]
            result["areas"].append({"area":group_area,"listing_type":listing_type,"currency":currency,"sources_tracked":len(group_ids),"fresh_sources":len(fresh),"stale_sources":len(group_ids)-len(fresh),"fresh_listings":fresh_listings,"fresh_available":fresh_available,"first_seen":event_counts.get("first_seen",0),"availability_changes":observed_changes,"no_longer_observed":omissions,"price_drops":event_counts.get("price_drop",0),"price_increases":event_counts.get("price_increase",0),"observations":coverage[0],"observed_sources":coverage[1],"coverage_start":coverage[2],"coverage_end":coverage[3]})
        result["unscoped_legacy_sources"] = connection.execute(f"SELECT COUNT(*) FROM snapshots WHERE source_id NOT IN (SELECT source_id FROM source_scopes) {source_filter}", allowed).fetchone()[0]
        return result
    finally:
        connection.close()

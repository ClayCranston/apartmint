"""Durable approval gate for provider-specific tour booking adapters.

This module has no HTTP client, model client, environment access, or real provider.
An adapter must implement durable idempotency and read a persisted reservation in
``lookup``. A successful submission response alone never confirms a booking.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import SafeError


@dataclass(frozen=True)
class BookingOffer:
    provider_id: str
    property_id: str
    booking_url: str
    slot_id: str
    timezone: str
    start: str
    end: str
    terms: str


@dataclass(frozen=True)
class BookingReceipt:
    confirmation_id: str
    provider_id: str
    property_id: str
    slot_id: str
    timezone: str
    start: str
    end: str
    idempotency_key: str
    status: str = "confirmed"


class BookingProvider(Protocol):
    """Trusted adapter; lookup is read-only and submit honors durable idempotency.

    Revalidation must obtain the current offer, including its terms, from the
    provider. Returning the input without actually checking is not sufficient.
    Contact details are given only to submit and must never be logged or sent to
    a language model. Exceptions may contain provider data and are suppressed.
    """

    provider_id: str

    def revalidate(self, offer: BookingOffer) -> BookingOffer | None: ...

    def submit(self, offer: BookingOffer, contact: dict, *, idempotency_key: str) -> BookingReceipt | None: ...

    def lookup(self, offer: BookingOffer, *, idempotency_key: str) -> BookingReceipt | None: ...


def _text(value: object, maximum: int = 200) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)):
        raise SafeError("Invalid booking field")
    return value


def _instant(value: str | datetime) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise SafeError("Booking times must include an explicit UTC offset") from None


def _now(value: datetime | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else _instant(value)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _offer(value: BookingOffer) -> BookingOffer:
    if not isinstance(value, BookingOffer):
        raise SafeError("A complete booking offer is required")
    for field in (value.provider_id, value.property_id, value.slot_id):
        _text(field)
    _text(value.terms, 8000)
    _text(value.timezone, 100)
    try:
        ZoneInfo(value.timezone)
        _text(value.booking_url, 2048)
        url = urlsplit(value.booking_url)
        loopback = False
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            pass
        if (not url.hostname or url.username is not None or url.password is not None or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and loopback))
                or url.port is not None and not 1 <= url.port <= 65535):
            raise ValueError
    except (ValueError, ZoneInfoNotFoundError):
        raise SafeError("Booking offer requires a valid timezone and provider URL") from None
    start, end = _instant(value.start), _instant(value.end)
    if end <= start:
        raise SafeError("Booking slot must end after it starts")
    return BookingOffer(value.provider_id, value.property_id, value.booking_url, value.slot_id,
                        value.timezone, start.isoformat(), end.isoformat(), value.terms)


def _contact(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"name", "email", "phone"}:
        raise SafeError("Contact profile requires name, email, and phone")
    result = {key: _text(value[key], 200 if key != "phone" else 50) for key in ("name", "email", "phone")}
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result["email"]) or not re.fullmatch(r"[+()0-9 .-]{3,50}", result["phone"]):
        raise SafeError("Invalid booking contact profile")
    return result


class BookingStore:
    """Private SQLite state. Returned dictionaries never contain contact values.

    State transitions are proposed -> approved -> submitting -> confirmed/unknown.
    Rejection, revocation, expiration and invalidation prevent submission. Once
    submitting has been committed, no method calls submit again: uncertainty is
    resolved by lookup, including after process death. Revocation at that point
    cannot promise cancellation of a potentially existing provider reservation.
    """

    def __init__(self, data_dir: Path):
        directory = Path(data_dir) / "bookings"
        if Path(data_dir).is_symlink() or directory.is_symlink():
            raise SafeError("Booking storage must not be a symbolic link")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "bookings.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise SafeError("Booking storage must be a regular file")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise SafeError("Could not open private booking storage") from None
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS booking_proposals (
                    proposal_id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, contact_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, expires_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('proposed','approved','rejected','revoked','expired','invalidated','submitting','unknown','confirmed')),
                    approval_digest TEXT, approved_at TEXT, receipt_json TEXT,
                    error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS booking_claims (
                    idempotency_key TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS immutable_booking_proposal
                BEFORE UPDATE OF proposal_id,digest,payload,contact_json,idempotency_key,expires_at,created_at ON booking_proposals
                BEGIN SELECT RAISE(ABORT, 'Booking proposal is immutable'); END;
            """)

    @contextmanager
    def _connection(self, *, transaction: bool = False):
        if self.path.is_symlink():
            raise SafeError("Booking storage must not be a symbolic link")
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            if transaction:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load(self, connection, proposal_id: str):
        _text(proposal_id)
        row = connection.execute("SELECT * FROM booking_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            raise SafeError("Booking proposal was not found")
        try:
            payload, contact = json.loads(row["payload"]), json.loads(row["contact_json"])
            if (_digest(payload) != row["digest"] or _digest(contact) != payload["contact_digest"]
                    or payload["expires_at"] != row["expires_at"]
                    or _digest({"offer": payload["offer"], "contact_digest": payload["contact_digest"]}) != row["idempotency_key"]):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise SafeError("Booking proposal integrity check failed") from None
        return row

    def _expire(self, connection, row, now: datetime):
        if row["state"] in {"proposed", "approved"} and _instant(row["expires_at"]) <= now:
            connection.execute("UPDATE booking_proposals SET state='expired',updated_at=? WHERE proposal_id=?", (now.isoformat(), row["proposal_id"]))
            return self._load(connection, row["proposal_id"])
        return row

    @staticmethod
    def _public(row) -> dict:
        payload = json.loads(row["payload"])
        return {"proposal_id": row["proposal_id"], "digest": row["digest"], "state": row["state"],
                "offer": payload["offer"], "contact_digest": payload["contact_digest"],
                "expires_at": row["expires_at"], "approved_at": row["approved_at"],
                "idempotency_key": row["idempotency_key"], "error_code": row["error_code"],
                "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None}

    def propose(self, offer: BookingOffer, contact: dict, *, expires_at: str | datetime,
                now: datetime | None = None) -> dict:
        offer, contact, current = _offer(offer), _contact(contact), _now(now)
        expires = _instant(expires_at)
        if not current < expires <= _instant(offer.start):
            raise SafeError("Proposal expiry must be in the future and no later than the slot")
        intent = {"offer": asdict(offer), "contact_digest": _digest(contact)}
        payload = {"version": 1, **intent, "expires_at": expires.isoformat()}
        digest = _digest(payload)
        with self._connection(transaction=True) as connection:
            connection.execute("""INSERT OR IGNORE INTO booking_proposals
                (proposal_id,digest,payload,contact_json,idempotency_key,expires_at,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'proposed',?,?)""", (uuid.uuid4().hex, digest, _json(payload), _json(contact),
                    _digest(intent), expires.isoformat(), current.isoformat(), current.isoformat()))
            proposal_id = connection.execute("SELECT proposal_id FROM booking_proposals WHERE digest=?", (digest,)).fetchone()[0]
            return self._public(self._load(connection, proposal_id))

    def status(self, proposal_id: str, *, now: datetime | None = None) -> dict:
        with self._connection(transaction=True) as connection:
            row = self._expire(connection, self._load(connection, proposal_id), _now(now))
            return self._public(row)

    def _decision(self, proposal_id: str, digest: str, action: str, now: datetime | None) -> dict:
        current = _now(now)
        failure = None
        with self._connection(transaction=True) as connection:
            row = self._expire(connection, self._load(connection, proposal_id), current)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not hmac.compare_digest(row["digest"], digest):
                failure = "Approval must name the exact immutable proposal digest"
            elif row["state"] == action:
                pass
            elif row["state"] not in ({"proposed", "approved"} if action in {"approved", "rejected"} else {"approved"}):
                failure = "Booking proposal cannot be changed in its current state"
            else:
                connection.execute("""UPDATE booking_proposals SET state=?,updated_at=?,
                    approval_digest=CASE WHEN ?='approved' THEN digest ELSE approval_digest END,
                    approved_at=CASE WHEN ?='approved' THEN ? ELSE approved_at END WHERE proposal_id=?""",
                    (action, current.isoformat(), action, action, current.isoformat(), proposal_id))
            result = self._public(self._load(connection, proposal_id))
        if failure:
            raise SafeError(failure)
        return result

    def approve(self, proposal_id: str, digest: str, *, now: datetime | None = None) -> dict:
        return self._decision(proposal_id, digest, "approved", now)

    def reject(self, proposal_id: str, digest: str, *, now: datetime | None = None) -> dict:
        return self._decision(proposal_id, digest, "rejected", now)

    def revoke(self, proposal_id: str, digest: str, *, now: datetime | None = None) -> dict:
        return self._decision(proposal_id, digest, "revoked", now)

    @staticmethod
    def _provider(provider: BookingProvider, offer: BookingOffer) -> None:
        if getattr(provider, "provider_id", None) != offer.provider_id:
            raise SafeError("Booking provider does not match the approved proposal")

    @staticmethod
    def _receipt(receipt: BookingReceipt, offer: BookingOffer, key: str) -> dict:
        if not isinstance(receipt, BookingReceipt):
            raise SafeError("Provider did not return a verifiable stored receipt")
        if not isinstance(receipt.confirmation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", receipt.confirmation_id):
            raise SafeError("Provider confirmation identifier is invalid")
        result = asdict(receipt)
        result["start"], result["end"] = _instant(receipt.start).isoformat(), _instant(receipt.end).isoformat()
        expected = {field: getattr(offer, field) for field in ("provider_id", "property_id", "slot_id", "timezone", "start", "end")}
        expected.update(idempotency_key=key, status="confirmed")
        if any(result.get(field) != value for field, value in expected.items()):
            raise SafeError("Stored receipt does not match the approved property and appointment")
        return result

    def _finish(self, proposal_id: str, receipt: dict | None, error_code: str | None, now: datetime | None) -> dict:
        with self._connection(transaction=True) as connection:
            row = self._load(connection, proposal_id)
            if row["state"] != "confirmed":
                if row["state"] not in {"submitting", "unknown"}:
                    raise SafeError("Booking submission state is inconsistent")
                connection.execute("UPDATE booking_proposals SET state=?,receipt_json=?,error_code=?,updated_at=? WHERE proposal_id=?",
                    ("confirmed" if receipt else "unknown", _json(receipt) if receipt else None, error_code, _now(now).isoformat(), proposal_id))
            return self._public(self._load(connection, proposal_id))

    def submit(self, proposal_id: str, provider: BookingProvider, *, now: datetime | None = None) -> dict:
        current = self.status(proposal_id, now=now)
        offer = _offer(BookingOffer(**current["offer"]))
        self._provider(provider, offer)
        if current["state"] in {"submitting", "unknown", "confirmed"}:
            return current
        if current["state"] != "approved":
            raise SafeError("Exact proposal approval is required before submission")
        try:
            refreshed = provider.revalidate(offer)
            matches = refreshed is not None and _offer(refreshed) == offer
        except Exception:
            raise SafeError("Provider could not revalidate the approved offer; no submission was attempted") from None
        failure = None
        with self._connection(transaction=True) as connection:
            row = self._expire(connection, self._load(connection, proposal_id), _now(now))
            if row["state"] in {"submitting", "unknown", "confirmed"}:
                return self._public(row)
            if row["state"] != "approved" or row["approval_digest"] != row["digest"]:
                failure = "Approval expired or changed before submission"
            elif not matches:
                connection.execute("UPDATE booking_proposals SET state='invalidated',error_code='offer_changed',updated_at=? WHERE proposal_id=?", (_now(now).isoformat(), proposal_id))
                failure = "Provider offer changed; a new proposal and approval are required"
            elif connection.execute("SELECT 1 FROM booking_claims WHERE idempotency_key=?", (row["idempotency_key"],)).fetchone():
                failure = "An equivalent booking request was already claimed; reconcile the original proposal"
            else:
                connection.execute("INSERT INTO booking_claims VALUES (?,?)", (row["idempotency_key"], proposal_id))
                connection.execute("UPDATE booking_proposals SET state='submitting',updated_at=? WHERE proposal_id=?", (_now(now).isoformat(), proposal_id))
                contact, key = json.loads(row["contact_json"]), row["idempotency_key"]
        if failure:
            raise SafeError(failure)
        # The committed claim precedes any provider side effect. Even if this
        # process dies now, later calls only reconcile the same durable key.
        try:
            submitted = self._receipt(provider.submit(offer, contact, idempotency_key=key), offer, key)
        except Exception:
            return self._finish(proposal_id, None, "submission_uncertain", now)
        try:
            stored = self._receipt(provider.lookup(offer, idempotency_key=key), offer, key)
            if stored != submitted:
                raise SafeError("Provider response and stored reservation disagree")
        except Exception:
            return self._finish(proposal_id, None, "receipt_unverified", now)
        return self._finish(proposal_id, stored, None, now)

    def reconcile(self, proposal_id: str, provider: BookingProvider, *, now: datetime | None = None) -> dict:
        current = self.status(proposal_id, now=now)
        offer = _offer(BookingOffer(**current["offer"]))
        self._provider(provider, offer)
        if current["state"] == "confirmed":
            return current
        if current["state"] not in {"submitting", "unknown"}:
            raise SafeError("Only a claimed submission can be reconciled")
        try:
            receipt = self._receipt(provider.lookup(offer, idempotency_key=current["idempotency_key"]), offer, current["idempotency_key"])
        except Exception:
            return self._finish(proposal_id, None, "receipt_unverified", now)
        return self._finish(proposal_id, receipt, None, now)

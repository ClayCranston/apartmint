"""Loopback-only synthetic booking fixture. Never contacts a real property."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MAX_BODY_BYTES = 8192
PROPERTY_IDS = ("synthetic-garden", "synthetic-courtyard")
PROPERTY_NAMES = ("Synthetic Garden Apartments", "Synthetic Courtyard Apartments")
SYNTHETIC_TERMS = "Synthetic fixture only; no real appointment or fee"
RECEIPT_PATTERN = re.compile(r"syn-[0-9a-f]{32}\Z")


class FixtureError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize(city: str, state_dir: Path) -> Path:
    if state_dir.is_symlink():
        raise ValueError("Fixture state directory must not be a symbolic link")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    path = state_dir / "synthetic-bookings.sqlite3"
    if path.is_symlink():
        raise ValueError("Fixture database must not be a symbolic link")
    connection = _connect(path)
    path.chmod(0o600)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS fixture_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS slots (
                id TEXT PRIMARY KEY, property_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                UNIQUE(property_id, start)
            );
            CREATE TABLE IF NOT EXISTS bookings (
                confirmation_id TEXT PRIMARY KEY, property_id TEXT NOT NULL,
                slot_id TEXT NOT NULL UNIQUE REFERENCES slots(id),
                idempotency_hash TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        connection.execute("BEGIN IMMEDIATE")
        configured = connection.execute("SELECT value FROM fixture_metadata WHERE key='city'").fetchone()
        if configured is not None and configured[0] != city:
            raise ValueError("Fixture state is already bound to another city")
        connection.execute("INSERT OR IGNORE INTO fixture_metadata VALUES ('city', ?)", (city,))
        now = datetime.now(timezone.utc)
        for property_id in PROPERTY_IDS:
            # Reuse the persisted schedule on restart. Add a fresh schedule only
            # after every persisted slot for this property is in the past.
            if connection.execute("SELECT 1 FROM slots WHERE property_id=? AND start>? LIMIT 1", (property_id, now.isoformat())).fetchone():
                continue
            for offset in (1, 2):
                start = (now + timedelta(days=offset)).replace(hour=16, minute=0, second=0, microsecond=0)
                end = start + timedelta(minutes=30)
                slot_id = property_id + "-" + start.strftime("%Y%m%dT%H%MZ")
                connection.execute("INSERT OR IGNORE INTO slots VALUES (?, ?, ?, ?)", (slot_id, property_id, start.isoformat(), end.isoformat()))
        connection.commit()
        return path
    finally:
        connection.close()


def _text(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum and value == value.strip() and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def _validate_booking(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"property_id", "slot_id", "contact", "idempotency_key"}:
        raise FixtureError(400, "Invalid synthetic booking payload")
    contact = value["contact"]
    if value["property_id"] not in PROPERTY_IDS or not _text(value["slot_id"], 1, 120):
        raise FixtureError(400, "Invalid synthetic property or slot")
    if not _text(value["idempotency_key"], 1, 200):
        raise FixtureError(400, "Invalid idempotency key")
    if not isinstance(contact, dict) or set(contact) != {"name", "email", "phone"}:
        raise FixtureError(400, "Synthetic contact fields are required")
    if not _text(contact["name"], 1, 100) or not _text(contact["phone"], 3, 40):
        raise FixtureError(400, "Invalid synthetic contact")
    if not _text(contact["email"], 3, 100) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@example\.com", contact["email"], re.I):
        raise FixtureError(400, "Synthetic contacts must use an @example.com email")
    return value


def _receipt(connection: sqlite3.Connection, confirmation_id: str) -> dict:
    row = connection.execute("""SELECT b.confirmation_id,b.property_id,b.slot_id,s.start,s.end
        FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.confirmation_id=?""", (confirmation_id,)).fetchone()
    if row is None:
        raise FixtureError(404, "Synthetic booking was not found")
    return {"synthetic": True, "status": "confirmed", **dict(row)}


def _book(path: Path, value: dict) -> dict:
    value = _validate_booking(value)
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    idempotency_hash = hashlib.sha256(value["idempotency_key"].encode()).hexdigest()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute("SELECT confirmation_id,payload_hash FROM bookings WHERE idempotency_hash=?", (idempotency_hash,)).fetchone()
        if previous:
            if previous["payload_hash"] != payload_hash:
                raise FixtureError(409, "Idempotency key already belongs to a different synthetic request")
            receipt = _receipt(connection, previous["confirmation_id"])
            connection.commit()
            return receipt
        slot = connection.execute("SELECT property_id,start FROM slots WHERE id=?", (value["slot_id"],)).fetchone()
        if slot is None or slot["property_id"] != value["property_id"]:
            raise FixtureError(404, "Synthetic slot was not found for this property")
        if datetime.fromisoformat(slot["start"]) <= datetime.now(timezone.utc):
            raise FixtureError(409, "Synthetic slot is no longer in the future")
        if connection.execute("SELECT 1 FROM bookings WHERE slot_id=?", (value["slot_id"],)).fetchone():
            raise FixtureError(409, "Synthetic slot is already booked")
        confirmation = "syn-" + uuid.uuid4().hex
        connection.execute("INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?)", (confirmation,value["property_id"],value["slot_id"],idempotency_hash,payload_hash,datetime.now(timezone.utc).isoformat()))
        receipt = _receipt(connection, confirmation)
        connection.commit()
        return receipt
    except sqlite3.IntegrityError:
        raise FixtureError(409, "Synthetic booking conflicts with an existing reservation") from None
    finally:
        connection.close()


def _handler(city: str, path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ApartmintSyntheticFixture"
        sys_version = ""

        def log_message(self, format, *args):
            pass

        def _send(self, status: int, value: dict) -> None:
            body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            self._send(code, {"synthetic": True, "error": "Unsupported fixture request"})

        def _request_path(self):
            # Reject browser-originated cross-site requests and unexpected Host
            # values. The fixture is for explicit local acceptance clients.
            expected_host = f"127.0.0.1:{self.server.server_address[1]}"
            if self.headers.get("Host") != expected_host or self.headers.get("Origin"):
                raise FixtureError(403, "Only direct loopback fixture requests are accepted")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or len(self.path) > 2048:
                raise FixtureError(400, "Invalid fixture path")
            return parsed

        def do_GET(self):
            try:
                parsed = self._request_path()
                if parsed.path == "/" and not parsed.query:
                    self._send(200, {"synthetic": True, "name": "LOCAL SYNTHETIC TEST SITE", "message": "Fictional properties and local test bookings only. No real appointments are made."})
                    return
                if parsed.path == "/properties":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"city"} or len(query["city"]) != 1:
                        raise FixtureError(400, "Provide one city filter")
                    properties = []
                    if query["city"][0] == city:
                        for index, property_id in enumerate(PROPERTY_IDS):
                            properties.append({"id": property_id, "name": PROPERTY_NAMES[index], "city": city, "listing_text": f"Fictional synthetic property. Unit A{index + 1}01 available. 1 bedroom. Monthly rent USD {1800 + index * 150}."})
                    self._send(200, {"synthetic": True, "properties": properties})
                    return
                if parsed.path == "/booking-lookup":
                    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                    if set(query) != {"key"} or len(query["key"]) != 1 or not _text(query["key"][0], 1, 200):
                        raise FixtureError(400, "Provide one synthetic idempotency key")
                    key_hash = hashlib.sha256(query["key"][0].encode()).hexdigest()
                    connection = _connect(path)
                    try:
                        row = connection.execute("SELECT confirmation_id FROM bookings WHERE idempotency_hash=?", (key_hash,)).fetchone()
                        self._send(200, {"synthetic": True, "receipt": _receipt(connection, row[0]) if row else None})
                    finally:
                        connection.close()
                    return
                if parsed.query:
                    raise FixtureError(400, "Query parameters are not accepted for this endpoint")
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "properties" and parts[2] == "slots":
                    if parts[1] not in PROPERTY_IDS:
                        raise FixtureError(404, "Synthetic property was not found")
                    connection = _connect(path)
                    try:
                        rows = connection.execute("""SELECT id,property_id,start,end FROM slots s WHERE property_id=? AND start>?
                            AND NOT EXISTS(SELECT 1 FROM bookings b WHERE b.slot_id=s.id) ORDER BY start""", (parts[1],datetime.now(timezone.utc).isoformat()))
                        self._send(200, {"synthetic": True, "property_id": parts[1],
                            "booking_url": "https://example.com/synthetic-tour/" + parts[1],
                            "terms": SYNTHETIC_TERMS, "timezone": "UTC", "slots": [dict(row) for row in rows]})
                    finally:
                        connection.close()
                    return
                if len(parts) == 2 and parts[0] == "bookings" and RECEIPT_PATTERN.fullmatch(parts[1]):
                    connection = _connect(path)
                    try:
                        self._send(200, _receipt(connection, parts[1]))
                    finally:
                        connection.close()
                    return
                raise FixtureError(404, "Synthetic endpoint was not found")
            except FixtureError as error:
                self._send(error.status, {"synthetic": True, "error": error.message})
            except (OSError, ValueError, sqlite3.Error):
                self._send(500, {"synthetic": True, "error": "Synthetic fixture request failed"})

        def do_POST(self):
            try:
                parsed = self._request_path()
                if parsed.path != "/bookings" or parsed.query:
                    raise FixtureError(404, "Synthetic endpoint was not found")
                if self.headers.get_content_type() != "application/json":
                    raise FixtureError(415, "Use application/json for synthetic bookings")
                if self.headers.get("Transfer-Encoding"):
                    raise FixtureError(400, "Chunked fixture requests are not accepted")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise FixtureError(411, "Provide one valid Content-Length")
                length = int(lengths[0])
                if not 1 <= length <= MAX_BODY_BYTES:
                    raise FixtureError(413, "Synthetic booking payload is too large or empty")
                self.connection.settimeout(5)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise FixtureError(400, "Incomplete synthetic booking payload")
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise FixtureError(400, "Invalid JSON booking payload") from None
                self._send(200, _book(path, value))
            except FixtureError as error:
                self._send(error.status, {"synthetic": True, "error": error.message})
            except (OSError, ValueError, sqlite3.Error):
                self._send(500, {"synthetic": True, "error": "Synthetic fixture request failed"})
    return Handler


@contextmanager
def fixture_server(city: str, state_dir: Path, port: int = 0):
    """Yield a 127.0.0.1 base URL; stop serving when the context exits."""
    if not _text(city, 1, 200):
        raise ValueError("A nonempty synthetic city is required")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid loopback fixture port")
    path = _initialize(city, Path(state_dir))
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(city, path))
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), name="apartmint-synthetic-booking", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

import json
import multiprocessing
import sqlite3
import stat
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from apartmint import booking
from apartmint.booking import BookingOffer, BookingReceipt, BookingStore
from apartmint.config import SafeError


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
CONTACT = {"name": "Synthetic Local Person", "email": "local-test@example.com", "phone": "+1-202-555-0108"}
OFFER = BookingOffer("synthetic", "property-a", "http://127.0.0.1:8765/tours", "slot-a", "America/New_York",
                     (NOW + timedelta(days=1)).isoformat(), (NOW + timedelta(days=1, minutes=30)).isoformat(),
                     "Synthetic appointment only; no charge.")


class Provider:
    """SQLite-backed synthetic adapter; response loss never removes its receipt."""

    provider_id = "synthetic"

    def __init__(self, path):
        self.path = path
        self.offer = OFFER
        self.lose_response = False
        self.before_revalidate = None
        self.after_submit = None
        self.lookup_override = None
        self.last_contact = None
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (key TEXT NOT NULL);
            """)

    def revalidate(self, offer):
        if self.before_revalidate:
            self.before_revalidate()
        return self.offer

    def submit(self, offer, contact, *, idempotency_key):
        self.last_contact = contact.copy()
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO calls VALUES (?)", (idempotency_key,))
            row = connection.execute("SELECT receipt FROM receipts WHERE key=?", (idempotency_key,)).fetchone()
            if row:
                receipt = BookingReceipt(**json.loads(row[0]))
            else:
                receipt = BookingReceipt("synthetic-confirmation", offer.provider_id, offer.property_id, offer.slot_id,
                                         offer.timezone, offer.start, offer.end, idempotency_key)
                connection.execute("INSERT INTO receipts VALUES (?,?)", (idempotency_key, json.dumps(asdict(receipt))))
        if self.after_submit:
            self.after_submit()
        if self.lose_response:
            raise TimeoutError("Synthetic contact must not leak: " + contact["email"])
        return receipt

    def lookup(self, offer, *, idempotency_key):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT receipt FROM receipts WHERE key=?", (idempotency_key,)).fetchone()
        receipt = BookingReceipt(**json.loads(row[0])) if row else None
        return self.lookup_override(receipt) if self.lookup_override else receipt

    def calls(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0]


@pytest.fixture
def setup(tmp_path):
    store = BookingStore(tmp_path)
    proposal = store.propose(OFFER, CONTACT, expires_at=NOW + timedelta(hours=1), now=NOW)
    return store, proposal, Provider(tmp_path / "provider.sqlite3")


def approve(store, proposal):
    return store.approve(proposal["proposal_id"], proposal["digest"], now=NOW)


def test_no_provider_operation_before_exact_approval(setup):
    store, proposal, provider = setup
    provider.before_revalidate = lambda: pytest.fail("Unapproved proposal reached provider")
    with pytest.raises(SafeError, match="approval"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    with pytest.raises(SafeError, match="digest"):
        store.approve(proposal["proposal_id"], "0" * 64, now=NOW)
    assert provider.calls() == 0
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "proposed"


@pytest.mark.parametrize("digest", [None, 123, "é" * 64, "a" * 65, "a\n"])
def test_malformed_approval_digest_fails_safely(setup, digest):
    store, proposal, _ = setup
    with pytest.raises(SafeError, match="digest"):
        store.approve(proposal["proposal_id"], digest, now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "proposed"


def test_wrong_provider_never_revalidates_or_submits(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.provider_id = "different-provider"
    provider.before_revalidate = lambda: pytest.fail("Wrong provider received proposal")
    with pytest.raises(SafeError, match="provider"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert provider.calls() == 0


def test_revalidation_error_can_retry_without_a_submission_claim(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    def unavailable():
        raise TimeoutError("Private upstream diagnostic")
    provider.before_revalidate = unavailable
    with pytest.raises(SafeError, match="no submission was attempted"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "approved"
    assert provider.calls() == 0
    provider.before_revalidate = None
    assert store.submit(proposal["proposal_id"], provider, now=NOW)["state"] == "confirmed"


def test_duplicate_approval_submission_and_restart_keep_one_receipt(setup, tmp_path):
    store, proposal, provider = setup
    assert approve(store, proposal)["state"] == "approved"
    assert approve(store, proposal)["state"] == "approved"
    result = store.submit(proposal["proposal_id"], provider, now=NOW)
    assert result["state"] == "confirmed"
    restarted = BookingStore(tmp_path)
    repeated = restarted.submit(proposal["proposal_id"], Provider(provider.path), now=NOW)
    assert repeated["receipt"] == result["receipt"]
    assert provider.calls() == 1


def test_contact_is_bound_immutable_and_not_public(setup, tmp_path):
    store, proposal, provider = setup
    contact = dict(CONTACT)
    duplicate = store.propose(OFFER, contact, expires_at=NOW + timedelta(hours=1), now=NOW)
    assert duplicate == proposal
    contact["email"] = "changed@example.com"
    changed = store.propose(OFFER, contact, expires_at=NOW + timedelta(hours=1), now=NOW)
    assert changed["digest"] != proposal["digest"]
    assert changed["idempotency_key"] != proposal["idempotency_key"]
    approve(store, proposal)
    result = store.submit(proposal["proposal_id"], provider, now=NOW)
    assert provider.last_contact == CONTACT
    for value in CONTACT.values():
        assert value not in json.dumps(result)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE booking_proposals SET contact_json='{}'")


@pytest.mark.parametrize("change", [
    {"slot_id": "other-slot"}, {"property_id": "other-property"},
    {"start": (NOW + timedelta(days=1, minutes=5)).isoformat()},
    {"end": (NOW + timedelta(days=1, minutes=60)).isoformat()},
    {"timezone": "America/Chicago"}, {"terms": "A nonrefundable charge is now required."},
    {"booking_url": "https://example.com/other-booking"},
])
def test_changed_offer_requires_a_new_proposal_and_approval(setup, change):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.offer = replace(OFFER, **change)
    with pytest.raises(SafeError, match="changed"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "invalidated"
    assert provider.calls() == 0


def test_withdrawn_slot_never_submits(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.offer = None
    with pytest.raises(SafeError, match="changed"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert provider.calls() == 0


@pytest.mark.parametrize("action", ["reject", "revoke"])
def test_rejected_or_revoked_proposal_cannot_be_reapproved_or_submitted(setup, action):
    store, proposal, provider = setup
    approve(store, proposal)
    getattr(store, action)(proposal["proposal_id"], proposal["digest"], now=NOW)
    with pytest.raises(SafeError):
        approve(store, proposal)
    with pytest.raises(SafeError):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert provider.calls() == 0


def test_expired_approval_is_durably_expired(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    with pytest.raises(SafeError):
        store.submit(proposal["proposal_id"], provider, now=NOW + timedelta(hours=1))
    assert store.status(proposal["proposal_id"], now=NOW + timedelta(hours=1))["state"] == "expired"
    assert provider.calls() == 0


def test_expiry_during_revalidation_prevents_submission(setup, monkeypatch):
    store, proposal, provider = setup
    approve(store, proposal)
    clock = [NOW]
    monkeypatch.setattr(booking, "_now", lambda ignored: clock[0])
    provider.before_revalidate = lambda: clock.__setitem__(0, NOW + timedelta(hours=2))
    with pytest.raises(SafeError, match="expired"):
        store.submit(proposal["proposal_id"], provider)
    assert store.status(proposal["proposal_id"])["state"] == "expired"
    assert provider.calls() == 0


def test_revocation_during_revalidation_wins_the_race(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.before_revalidate = lambda: store.revoke(proposal["proposal_id"], proposal["digest"], now=NOW)
    with pytest.raises(SafeError, match="changed"):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "revoked"
    assert provider.calls() == 0


def test_response_loss_uses_lookup_after_restart_never_resubmits(setup, tmp_path):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.lose_response = True
    result = store.submit(proposal["proposal_id"], provider, now=NOW)
    assert result["state"] == "unknown" and result["receipt"] is None
    assert CONTACT["email"] not in json.dumps(result)
    restarted = BookingStore(tmp_path)
    assert restarted.submit(proposal["proposal_id"], Provider(provider.path), now=NOW)["state"] == "unknown"
    # Expiry does not stop a read-only check for an already submitted booking.
    result = restarted.reconcile(proposal["proposal_id"], Provider(provider.path), now=NOW + timedelta(days=2))
    assert result["state"] == "confirmed"
    assert result["receipt"]["idempotency_key"] == proposal["idempotency_key"]
    assert provider.calls() == 1


def test_process_interruption_after_side_effect_leaves_reconcilable_claim(setup, tmp_path):
    store, proposal, provider = setup
    approve(store, proposal)
    def terminate():
        raise SystemExit("Synthetic process interruption")
    provider.after_submit = terminate
    with pytest.raises(SystemExit):
        store.submit(proposal["proposal_id"], provider, now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "submitting"
    assert BookingStore(tmp_path).reconcile(proposal["proposal_id"], Provider(provider.path), now=NOW)["state"] == "confirmed"
    assert provider.calls() == 1


@pytest.mark.parametrize("change", [
    {"property_id": "wrong-property"}, {"slot_id": "wrong-slot"}, {"provider_id": "wrong-provider"},
    {"timezone": "UTC"}, {"start": (NOW + timedelta(days=2)).isoformat()},
    {"end": (NOW + timedelta(days=2, minutes=30)).isoformat()},
    {"idempotency_key": "different-key"}, {"status": "pending"}, {"confirmation_id": "different-confirmation"},
])
def test_submission_response_does_not_override_mismatched_stored_receipt(setup, change):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.lookup_override = lambda receipt: replace(receipt, **change)
    result = store.submit(proposal["proposal_id"], provider, now=NOW)
    assert result["state"] == "unknown" and result["receipt"] is None
    assert provider.calls() == 1


def test_http_success_without_a_stored_receipt_is_unknown(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.lookup_override = lambda receipt: {"http_status": 200, "success": True}
    result = store.submit(proposal["proposal_id"], provider, now=NOW)
    assert result["state"] == "unknown"
    assert store.reconcile(proposal["proposal_id"], provider, now=NOW)["state"] == "unknown"
    assert provider.calls() == 1


def test_cannot_revoke_a_possibly_completed_submission(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.lose_response = True
    store.submit(proposal["proposal_id"], provider, now=NOW)
    with pytest.raises(SafeError, match="current state"):
        store.revoke(proposal["proposal_id"], proposal["digest"], now=NOW)
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "unknown"


def test_equivalent_offer_cannot_bypass_claim_with_a_new_expiry(setup):
    store, proposal, provider = setup
    second = store.propose(OFFER, CONTACT, expires_at=NOW + timedelta(hours=2), now=NOW)
    assert second["digest"] != proposal["digest"]
    assert second["idempotency_key"] == proposal["idempotency_key"]
    approve(store, proposal)
    approve(store, second)
    store.submit(proposal["proposal_id"], provider, now=NOW)
    with pytest.raises(SafeError, match="equivalent"):
        store.submit(second["proposal_id"], provider, now=NOW)
    assert provider.calls() == 1


def _process_submit(data_dir, provider_path, proposal_id, gate, results):
    try:
        store, provider = BookingStore(data_dir), Provider(provider_path)
        gate.wait(timeout=10)
        results.put(store.submit(proposal_id, provider, now=NOW)["state"])
    except Exception as error:
        results.put(type(error).__name__)


def test_processes_claim_submission_at_most_once(setup, tmp_path):
    store, proposal, provider = setup
    approve(store, proposal)
    context = multiprocessing.get_context("spawn")
    gate, results = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_process_submit, args=(tmp_path, provider.path, proposal["proposal_id"], gate, results)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        assert {results.get(timeout=2) for _ in processes} <= {"submitting", "confirmed"}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
    assert provider.calls() == 1
    assert store.status(proposal["proposal_id"], now=NOW)["state"] == "confirmed"


def test_offset_equivalent_provider_timestamps_validate(setup):
    store, proposal, provider = setup
    approve(store, proposal)
    provider.offer = replace(OFFER, start="2030-01-02T07:00:00-05:00", end="2030-01-02T07:30:00-05:00")
    provider.lookup_override = lambda receipt: replace(receipt, start="2030-01-02T07:00:00-05:00", end="2030-01-02T07:30:00-05:00")
    assert store.submit(proposal["proposal_id"], provider, now=NOW)["state"] == "confirmed"


@pytest.mark.parametrize("change", [
    {"start": "2030-01-02T12:00:00"}, {"timezone": "Not/A_Zone"},
    {"booking_url": "https://username:password@example.com/tours"},
    {"booking_url": "http://example.com/tours"}, {"end": NOW.isoformat()},
])
def test_invalid_offer_never_creates_proposal(tmp_path, change):
    with pytest.raises(SafeError):
        BookingStore(tmp_path).propose(replace(OFFER, **change), CONTACT, expires_at=NOW + timedelta(hours=1), now=NOW)

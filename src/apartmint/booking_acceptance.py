"""Exercise durable approval against a loopback fixture, never a real property."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
import uuid

from .booking import BookingOffer, BookingReceipt, BookingStore
from .booking_demo import FixtureClient, SYNTHETIC_CONTACT
from .booking_fixture import fixture_server
from .config import SafeError
from .evaluation import write_report


class SyntheticBookingProvider:
    provider_id = "synthetic_fixture"

    def __init__(self, base_url: str):
        self.client = FixtureClient(base_url)
        self.submissions = 0

    def revalidate(self, offer: BookingOffer) -> BookingOffer | None:
        if offer.provider_id != self.provider_id:
            return None
        response = self.client.request("GET", "/properties/" + quote(offer.property_id, safe="") + "/slots")
        for slot in response["slots"]:
            if slot["id"] == offer.slot_id and slot["property_id"] == offer.property_id:
                return replace(offer, start=slot["start"], end=slot["end"], timezone=response["timezone"],
                               terms=response["terms"], booking_url=response["booking_url"])
        return None

    def _receipt(self, result: dict, key: str) -> BookingReceipt:
        # The fixture persists all times as UTC; never copy expected property/slot
        # fields over the actual receipt returned by the provider.
        return BookingReceipt(confirmation_id=result["confirmation_id"], provider_id=self.provider_id,
                              property_id=result["property_id"], slot_id=result["slot_id"], timezone="UTC",
                              start=result["start"], end=result["end"], idempotency_key=key, status=result["status"])

    def submit(self, offer: BookingOffer, contact: dict, *, idempotency_key: str) -> BookingReceipt:
        self.submissions += 1
        result = self.client.request("POST", "/bookings", {"property_id": offer.property_id,
            "slot_id": offer.slot_id, "contact": contact, "idempotency_key": idempotency_key})
        return self._receipt(result, idempotency_key)

    def lookup(self, offer: BookingOffer, *, idempotency_key: str) -> BookingReceipt | None:
        result = self.client.request("GET", "/booking-lookup?" + urlencode({"key": idempotency_key}))
        receipt = result["receipt"]
        return None if receipt is None else self._receipt(receipt, idempotency_key)


def run_approval_demo(data_dir: Path) -> dict:
    run_dir = data_dir / "approval-tests" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, mode=0o700)
    city = "Synthetic Approval City"
    store = BookingStore(run_dir / "application")
    with fixture_server(city, run_dir / "provider") as base:
        provider = SyntheticBookingProvider(base)
        properties = provider.client.request("GET", "/properties?" + urlencode({"city": city}))["properties"]
        property_id = properties[0]["id"]
        schedule = provider.client.request("GET", "/properties/" + property_id + "/slots")
        slots = schedule["slots"]
        slot = slots[0]
        offer = BookingOffer(provider_id=provider.provider_id, property_id=property_id,
                             booking_url=schedule["booking_url"], slot_id=slot["id"], timezone=schedule["timezone"],
                             start=slot["start"], end=slot["end"], terms=schedule["terms"])
        proposal = store.propose(offer, contact=SYNTHETIC_CONTACT,
                                 expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
        try:
            store.submit(proposal["proposal_id"], provider)
        except SafeError:
            pass
        else:
            raise SafeError("Approval test failed: unapproved submission was accepted")
        if provider.submissions != 0:
            raise SafeError("Approval test failed: provider was contacted before approval")
        # This explicit simulation step stands in for a human only in this fixture.
        store.approve(proposal["proposal_id"], proposal["digest"])
        confirmed = store.submit(proposal["proposal_id"], provider)
        repeated = store.submit(proposal["proposal_id"], provider)
        if confirmed["state"] != "confirmed" or repeated["receipt"] != confirmed["receipt"] or provider.submissions != 1:
            raise SafeError("Approval test failed: receipt or duplicate suppression mismatch")
    # Reopen both ledgers and query the provider using the same durable key.
    with fixture_server(city, run_dir / "provider") as restarted:
        restored = BookingStore(run_dir / "application").status(proposal["proposal_id"])
        persisted = SyntheticBookingProvider(restarted).lookup(offer, idempotency_key=restored["idempotency_key"])
        if (restored["state"] != "confirmed" or restored["receipt"] != confirmed["receipt"]
                or persisted is None or asdict(persisted) != confirmed["receipt"]):
            raise SafeError("Approval test failed: persisted receipt did not survive restart")
    report = {"mode": "synthetic_approval_acceptance", "real_appointment_booked": False,
              "human_approval": "simulated_only_for_this_fixture", "unapproved_submissions": 0,
              "provider_submissions": provider.submissions, "duplicate_submission": "suppressed",
              "restart_check": "passed", "proposal_id": proposal["proposal_id"], "receipt": confirmed["receipt"]}
    write_report(run_dir / "report.json", report)
    return dict(report, report_path=str((run_dir / "report.json").resolve()))

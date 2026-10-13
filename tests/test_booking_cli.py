import json
from datetime import datetime, timedelta, timezone

from apartmint.cli import main
from apartmint.booking_cli import _env_contact
from apartmint.config import SafeError
import pytest


def proposal_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("APARTMINT_DATA_DIR", str(tmp_path / "data"))
    start = datetime.now(timezone.utc) + timedelta(days=1)
    offer = {"provider_id": "unconfigured_real_provider", "property_id": "p123",
             "booking_url": "https://example.com/schedule", "slot_id": "slot123", "timezone": "UTC",
             "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(), "terms": "A free guided tour"}
    offer_file = tmp_path / "offer.json"
    offer_file.write_text(json.dumps(offer))
    contact_file = tmp_path / "contact.json"
    contact_file.write_text(json.dumps({"name": "Test Applicant", "email": "private-canary@example.com", "phone": "+1-202-555-0100"}))
    contact_file.chmod(0o600)
    args = ["tour", "prepare", "--offer", str(offer_file), "--contact", str(contact_file),
            "--expires-at", (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()]
    return args, contact_file


def test_prepare_approve_and_handoff_never_claim_real_booking(tmp_path, monkeypatch, capsys):
    args, _ = proposal_inputs(tmp_path, monkeypatch)
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "private-canary" not in output and "Test Applicant" not in output
    proposal = json.loads(output)
    identifier, digest = proposal["proposal_id"], proposal["digest"]
    assert main(["tour", "handoff", identifier]) == 1
    assert main(["tour", "approve", identifier, "--digest", digest]) == 0
    capsys.readouterr()
    assert main(["tour", "handoff", identifier]) == 0
    handoff = json.loads(capsys.readouterr().out)
    assert handoff["real_appointment_booked"] is False and handoff["automatic_submission"] is False
    assert handoff["offer"]["booking_url"] == "https://example.com/schedule"
    assert main(["tour", "submit", identifier]) == 1
    assert "No real provider adapter" in capsys.readouterr().err


def test_world_readable_contact_is_rejected_without_echoing_values(tmp_path, monkeypatch, capsys):
    args, contact = proposal_inputs(tmp_path, monkeypatch)
    contact.chmod(0o644)
    assert main(args) == 1
    error = capsys.readouterr().err
    assert "chmod 600" in error and "private-canary" not in error


def test_environment_profile_reads_only_named_literals_without_execution(tmp_path):
    profile = tmp_path / ".env"
    canary_path = tmp_path / "must-not-exist"
    profile.write_text('UNRELATED_SECRET=never-return-this\nN="Test Person"\nE=test@example.com\nP=+12025550100\n')
    profile.chmod(0o600)
    mapping = {"name": "N", "email": "E", "phone": "P"}
    assert _env_contact(profile, mapping) == {"name": "Test Person", "email": "test@example.com", "phone": "+12025550100"}
    profile.write_text(f'N="$(touch {canary_path})"\nE=test@example.com\nP=+12025550100\n')
    with pytest.raises(SafeError, match="literal"):
        _env_contact(profile, mapping)
    assert not canary_path.exists()


def test_approval_demo_uses_real_local_fixture_and_persists_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APARTMINT_DATA_DIR", str(tmp_path))
    assert main(["tour", "approval-demo"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["real_appointment_booked"] is False
    assert report["provider_submissions"] == 1
    assert report["unapproved_submissions"] == 0
    assert report["restart_check"] == "passed"


def test_requirements_do_not_claim_a_real_provider_is_supported(capsys):
    assert main(["tour", "requirements"]) == 0
    assert json.loads(capsys.readouterr().out)["real_provider_submission_available"] is False


@pytest.mark.parametrize("changes", [{"terms": "Changed terms"}, {"booking_url": "https://example.com/changed"}])
def test_fixture_adapter_revalidates_provider_terms_and_url(tmp_path, changes):
    from apartmint.booking import BookingOffer, BookingStore
    from apartmint.booking_acceptance import SyntheticBookingProvider
    from apartmint.booking_fixture import fixture_server, PROPERTY_IDS
    with fixture_server("Synthetic City", tmp_path / "provider") as base:
        provider = SyntheticBookingProvider(base)
        property_id = PROPERTY_IDS[0]
        schedule = provider.client.request("GET", "/properties/" + property_id + "/slots")
        slot = schedule["slots"][0]
        fields = dict(provider_id=provider.provider_id, property_id=property_id, slot_id=slot["id"],
                      start=slot["start"], end=slot["end"], timezone=schedule["timezone"],
                      booking_url=schedule["booking_url"], terms=schedule["terms"])
        fields.update(changes)
        store = BookingStore(tmp_path / "application")
        proposal = store.propose(BookingOffer(**fields), {"name": "Test", "email": "test@example.com", "phone": "+12025550100"},
                                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
        store.approve(proposal["proposal_id"], proposal["digest"])
        with pytest.raises(SafeError, match="offer changed"):
            store.submit(proposal["proposal_id"], provider)
        assert provider.submissions == 0
        assert store.status(proposal["proposal_id"])["state"] == "invalidated"

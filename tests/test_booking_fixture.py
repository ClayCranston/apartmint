import copy
import http.client
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import pytest

from apartmint.booking_fixture import MAX_BODY_BYTES, fixture_server


def request(base, path, payload=None, *, method=None, headers=None):
    url = urlsplit(base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    body = json.dumps(payload).encode() if payload is not None else None
    actual_headers = {"Content-Type": "application/json"} if body else {}
    actual_headers.update(headers or {})
    connection.request(method or ("POST" if payload is not None else "GET"), path, body=body, headers=actual_headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def booking_payload(base, key="synthetic-test-key"):
    _, properties = request(base, "/properties?" + urlencode({"city": "Fictional City"}))
    property_id = properties["properties"][0]["id"]
    _, slots = request(base, f"/properties/{property_id}/slots")
    return {"property_id": property_id, "slot_id": slots["slots"][0]["id"], "contact": {"name": "Synthetic Test Person", "email": "synthetic-test@example.com", "phone": "+1-202-555-0101"}, "idempotency_key": key}


def test_city_discovery_is_exact_and_synthetic(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        assert base.startswith("http://127.0.0.1:")
        status, home = request(base, "/")
        assert status == 200 and home["synthetic"] is True
        assert "LOCAL SYNTHETIC TEST SITE" in home["name"]
        status, properties = request(base, "/properties?city=Fictional+City")
        assert status == 200 and len(properties["properties"]) == 2
        assert all(prop["city"] == "Fictional City" and "synthetic" in prop["listing_text"] for prop in properties["properties"])
        _, wrong_city = request(base, "/properties?city=fictional+city")
        assert wrong_city == {"synthetic": True, "properties": []}
        assert request(base, "/properties")[0] == 400


def test_booking_idempotency_conflicts_and_contact_free_receipt(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        payload = booking_payload(base)
        status, first = request(base, "/bookings", payload)
        assert status == 200 and first["synthetic"] and first["status"] == "confirmed"
        assert request(base, "/bookings", payload) == (200, first)
        assert request(base, "/bookings/" + first["confirmation_id"]) == (200, first)
        assert "contact" not in json.dumps(first) and payload["contact"]["email"] not in json.dumps(first)
        changed = copy.deepcopy(payload)
        changed["contact"]["name"] = "Another Synthetic Person"
        assert request(base, "/bookings", changed)[0] == 409
        occupied = dict(payload, idempotency_key="different-synthetic-key")
        assert request(base, "/bookings", occupied)[0] == 409
        _, remaining = request(base, f"/properties/{payload['property_id']}/slots")
        assert payload["slot_id"] not in {slot["id"] for slot in remaining["slots"]}
    connection = sqlite3.connect(tmp_path / "synthetic-bookings.sqlite3")
    stored = str(connection.execute("SELECT * FROM bookings").fetchall())
    connection.close()
    assert payload["contact"]["email"] not in stored and payload["contact"]["phone"] not in stored
    assert payload["idempotency_key"] not in stored


def test_restart_preserves_slots_receipts_and_idempotency(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        payload = booking_payload(base)
        _, original_slots = request(base, f"/properties/{payload['property_id']}/slots")
        for slot in original_slots["slots"]:
            assert datetime.fromisoformat(slot["start"]) > datetime.now(timezone.utc)
            assert datetime.fromisoformat(slot["end"]) > datetime.fromisoformat(slot["start"])
        _, receipt = request(base, "/bookings", payload)
    with fixture_server("Fictional City", tmp_path) as base:
        assert request(base, "/bookings", payload) == (200, receipt)
        assert request(base, "/bookings/" + receipt["confirmation_id"]) == (200, receipt)
        _, remaining = request(base, f"/properties/{payload['property_id']}/slots")
        assert remaining["slots"] == original_slots["slots"][1:]
    with pytest.raises(ValueError, match="another city"):
        with fixture_server("Different City", tmp_path):
            pytest.fail("Must not reuse fixture state for a different city")


def test_simultaneous_slot_claims_are_atomic(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        payload = booking_payload(base)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda key: request(base, "/bookings", dict(payload, idempotency_key=key))[0], ["one", "two"]))
        assert sorted(responses) == [200, 409]


@pytest.mark.parametrize("email", ["person@real.example", "person@example.com.evil.test", "person@sub.example.com", "person@example.com\r\nX-Secret: token"])
def test_non_synthetic_contacts_rejected(tmp_path, email):
    with fixture_server("Fictional City", tmp_path) as base:
        payload = booking_payload(base)
        payload["contact"]["email"] = email
        status, result = request(base, "/bookings", payload)
        assert status == 400 and result["synthetic"]
        assert email not in json.dumps(result)


def test_loopback_host_and_cross_origin_guards(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        assert request(base, "/", headers={"Host": "external.example"})[0] == 403
        assert request(base, "/", headers={"Origin": "https://external.example"})[0] == 403


def test_body_limit_unknown_routes_and_no_request_logs(tmp_path, capsys):
    with fixture_server("Fictional City", tmp_path) as base:
        assert request(base, "/bookings", {"data": "x" * MAX_BODY_BYTES})[0] == 413
        assert request(base, "/not-a-route")[0] == 404
        assert request(base, "/", method="DELETE")[0] == 501
        assert request(base, "/bookings", booking_payload(base), headers={"Content-Type": "text/plain"})[0] == 415
    captured = capsys.readouterr()
    assert captured.err == captured.out == ""


def test_listener_binds_only_ipv4_loopback(tmp_path, monkeypatch):
    from apartmint import booking_fixture
    real_server = booking_fixture.ThreadingHTTPServer
    bound = []
    def capture_server(address, handler):
        server = real_server(address, handler)
        bound.append(server.socket.getsockname())
        return server
    monkeypatch.setattr(booking_fixture, "ThreadingHTTPServer", capture_server)
    with fixture_server("Fictional City", tmp_path) as base:
        assert bound[0][0] == "127.0.0.1"
        assert request(base, "/")[0] == 200


def test_slot_cannot_be_booked_for_different_property(tmp_path):
    with fixture_server("Fictional City", tmp_path) as base:
        payload = booking_payload(base)
        payload["property_id"] = "synthetic-courtyard"
        assert request(base, "/bookings", payload)[0] == 404


def test_deeply_nested_json_gets_clean_error_without_traceback(tmp_path, capsys):
    with fixture_server("Fictional City", tmp_path) as base:
        url = urlsplit(base)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        connection.request("POST", "/bookings", body="[" * 2000 + "]" * 2000, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["synthetic"] is True
        connection.close()
    assert capsys.readouterr().err == ""

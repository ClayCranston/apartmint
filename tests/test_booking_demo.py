import json

import pytest

from apartmint import booking_demo
from apartmint.config import SafeError, Settings


def selecting_model(captured):
    def post(url, payload):
        captured.append(payload)
        tool = payload['tools'][0]['function']
        args = {key: field['enum'][0] for key, field in tool['parameters']['properties'].items()}
        return {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{
            'id': 'call-' + str(len(captured)), 'type': 'function',
            'function': {'name': tool['name'], 'arguments': json.dumps(args)},
        }]}}]}
    return post


def test_booking_requires_persisted_confirmation_and_keeps_contacts_out_of_model(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv('PRIVATE_TEST_CREDENTIAL', 'NEVER_SEND_THIS_SECRET')
    monkeypatch.setattr(booking_demo, 'post_local_json', selecting_model(sent))
    report = booking_demo.run_booking_demo(Settings.from_env(), 'Any configured city', tmp_path)
    assert report['real_appointment_booked'] is False
    assert report['receipt']['synthetic'] is True
    assert report['receipt']['status'] == 'confirmed'
    assert report['duplicate_submission'] == 'same_confirmation'
    assert report['restart_check'] == 'passed'
    assert [step['tool'] for step in report['tool_trace']] == ['find_properties', 'get_tour_slots', 'book_tour']
    payload = json.dumps(sent)
    assert 'NEVER_SEND_THIS_SECRET' not in payload
    assert booking_demo.SYNTHETIC_CONTACT['email'] not in payload
    assert booking_demo.SYNTHETIC_CONTACT['phone'] not in payload


@pytest.mark.parametrize('url', ['https://example.com', 'http://localhost:8080', 'http://127.0.0.1:8080/other', 'http://user:pass@127.0.0.1:8080', 'http://192.168.1.1:8080'])
def test_booking_demo_cannot_target_real_provider(url):
    with pytest.raises(SafeError):
        booking_demo.FixtureClient(url)


def test_model_text_cannot_claim_booking_success(tmp_path, monkeypatch):
    monkeypatch.setattr(booking_demo, 'post_local_json', lambda *a: {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Booked successfully!'}}]})
    with pytest.raises(SafeError):
        booking_demo.run_booking_demo(Settings.from_env(), 'Synthetic City', tmp_path)
    assert not list(tmp_path.rglob('report.json'))


def test_model_cannot_reserve_unoffered_slot(tmp_path, monkeypatch):
    normal = selecting_model([])
    def bad_slot(url, payload):
        response = normal(url, payload)
        if payload['tools'][0]['function']['name'] == 'book_tour':
            call = response['choices'][0]['message']['tool_calls'][0]
            args = json.loads(call['function']['arguments'])
            args['slot_id'] = 'invented-slot'
            call['function']['arguments'] = json.dumps(args)
        return response
    monkeypatch.setattr(booking_demo, 'post_local_json', bad_slot)
    requests = []
    original = booking_demo.FixtureClient.request
    def request(self, method, path, body=None):
        requests.append(method)
        return original(self, method, path, body)
    monkeypatch.setattr(booking_demo.FixtureClient, 'request', request)
    with pytest.raises(SafeError):
        booking_demo.run_booking_demo(Settings.from_env(), 'Synthetic City', tmp_path)
    assert 'POST' not in requests

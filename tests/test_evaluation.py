import json
from pathlib import Path

import pytest

from apartmint.evaluation import evaluate, load_cases, score_case, summarize
from apartmint.config import Settings
from apartmint import evaluation


def row(unit='a101', availability='available', price='1850.00', bedrooms=1):
    return {'unit': unit, 'availability': availability, 'confirmed_price': price, 'bedrooms': bedrooms}


def test_abstention_cannot_masquerade_as_perfect_accuracy():
    scores = score_case([row()], [], failed=True)
    result = summarize([{'scores': scores}])
    assert result['listing_recall'] == 0
    assert result['confirmed_price_recall'] == 0
    assert result['failed_extractions'] == 1
    assert result['passed'] is False


def test_unsafe_claims_are_counted_separately_from_misses():
    scores = score_case([row(availability='unknown', price=None)], [row(), row('invented')])
    assert scores['false_available'] == 2
    assert scores['false_confirmed_prices'] == 2
    assert scores['hallucinated_listings'] == 1
    assert not scores['exact_match']


def test_correct_numbers_attached_to_wrong_unit_fail():
    scores = score_case([row('a101', price='1800.00'), row('a102', price='1900.00')],
                        [row('a101', price='1900.00'), row('a102', price='1800.00')])
    assert scores['matched_listings'] == 2
    assert scores['false_confirmed_prices'] == 2
    assert not scores['exact_match']


def test_duplicate_identities_do_not_disappear_during_scoring():
    with pytest.raises(ValueError):
        score_case([row()], [row(), row()])


def test_empty_corpus_or_failed_empty_response_cannot_pass():
    assert summarize([])['passed'] is False
    assert not score_case([], [], failed=True)['exact_match']
    assert score_case([], [])['exact_match']


def test_evaluation_preserves_failed_case_and_returns_nonpassing_result(tmp_path, monkeypatch):
    corpus = tmp_path/'corpus.json'
    corpus.write_text(json.dumps({'version': 1, 'cases': [{'id': 'fixture', 'listing_type': 'rental', 'currency': 'USD', 'text': 'Unit A101 available. Monthly rent USD1850. 1 bedroom.', 'expected': [row()], 'tags': ['test']}]}))
    class Fake:
        def __init__(self, *a): pass
        def extract(self, text): return {'listings': []}
    monkeypatch.setattr(evaluation, 'LocalClient', Fake)
    output = tmp_path/'report.json'
    result = evaluate(Settings.from_env(), corpus, output)
    assert result['complete'] is True
    assert result['production_accuracy_established'] is False
    assert result['summary']['passed'] is False
    assert json.loads(output.read_text())['cases'][0]['predicted'] == []


def test_interrupted_run_cannot_reuse_old_pass_or_mark_exact_prefix_complete(tmp_path, monkeypatch):
    corpus = tmp_path/'corpus.json'
    cases = [{'id': name, 'listing_type': 'rental', 'currency': 'USD', 'text': 'No listings published.', 'expected': [], 'tags': ['test']} for name in ('first', 'second')]
    corpus.write_text(json.dumps({'version': 1, 'cases': cases}))
    output = tmp_path/'report.json'
    output.write_text(json.dumps({'complete': True, 'summary': {'passed': True}, 'old_report': True}))
    calls = []
    class InterruptedClient:
        def __init__(self, *args): pass
        def extract(self, text):
            pending = json.loads(output.read_text())
            assert pending['complete'] is False
            assert pending['summary']['passed'] is False
            assert 'old_report' not in pending
            if calls:
                raise KeyboardInterrupt
            calls.append(True)
            return {'listings': []}
    monkeypatch.setattr(evaluation, 'LocalClient', InterruptedClient)
    with pytest.raises(KeyboardInterrupt):
        evaluate(Settings.from_env(), corpus, output)
    saved = json.loads(output.read_text())
    assert len(saved['cases']) == 1 and saved['summary']['exact_cases'] == 1
    assert saved['complete'] is False and saved['summary']['passed'] is False


def test_doctor_command_exits_nonzero_for_unready_prerequisites(monkeypatch, capsys):
    from apartmint import diagnostics
    from apartmint.cli import main
    monkeypatch.setattr(diagnostics, 'doctor', lambda settings: {'ready': False, 'monitoring_verified': False})
    assert main(['doctor']) == 1
    assert json.loads(capsys.readouterr().out)['monitoring_verified'] is False

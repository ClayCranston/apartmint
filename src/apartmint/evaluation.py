"""Score local-model extraction against independently labelled synthetic facts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid
from decimal import InvalidOperation

from .config import SafeError, Settings, Source
from .market import canonical_identity, confirmed_price, money
from .pipeline import LocalClient, MAX_TEXT_BYTES, validate_extraction


def write_report(path: Path, report: dict) -> None:
    """Publish one complete JSON checkpoint instead of leaving partially written proof."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as handle:
            temporary.chmod(0o600)
            json.dump(report, handle, indent=2)
            handle.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def score_case(expected: list[dict], predicted: list[dict], *, failed: bool = False) -> dict:
    """Missing output lowers recall; rejecting everything can never pass this suite."""
    gold = {row['unit']: row for row in expected}
    actual = {row['unit']: row for row in predicted}
    if len(gold) != len(expected) or len(actual) != len(predicted):
        raise ValueError('Evaluation rows must have unique canonical unit identities')
    matched = gold.keys() & actual.keys()
    price_gold = {unit for unit, row in gold.items() if row['confirmed_price'] is not None}
    price_actual = {unit for unit, row in actual.items() if row['confirmed_price'] is not None}
    correct_prices = {unit for unit in matched & price_actual if actual[unit]['confirmed_price'] == gold[unit]['confirmed_price']}
    return {
        'exact_match': not failed and gold == actual,
        'expected_listings': len(gold), 'predicted_listings': len(actual),
        'matched_listings': len(matched), 'missed_listings': len(gold.keys() - actual.keys()),
        'hallucinated_listings': len(actual.keys() - gold.keys()),
        'false_available': sum(row['availability'] == 'available' and (unit not in gold or gold[unit]['availability'] != 'available') for unit, row in actual.items()),
        'correct_availability': sum(actual[unit]['availability'] == gold[unit]['availability'] for unit in matched),
        'correct_bedrooms': sum(actual[unit]['bedrooms'] == gold[unit]['bedrooms'] for unit in matched),
        'expected_confirmed_prices': len(price_gold), 'predicted_confirmed_prices': len(price_actual),
        'correct_confirmed_prices': len(correct_prices),
        'false_confirmed_prices': len(price_actual - correct_prices),
        'failed_extractions': int(failed),
    }


def summarize(results: list[dict]) -> dict:
    def ratio(numerator, denominator):
        return round(numerator / denominator, 6) if denominator else None
    keys = ('expected_listings', 'predicted_listings', 'matched_listings', 'missed_listings',
            'hallucinated_listings', 'false_available', 'correct_availability', 'correct_bedrooms',
            'expected_confirmed_prices', 'predicted_confirmed_prices', 'correct_confirmed_prices',
            'false_confirmed_prices', 'failed_extractions')
    totals = {key: sum(result['scores'][key] for result in results) for key in keys}
    exact = sum(result['scores']['exact_match'] for result in results)
    return {**totals, 'cases': len(results), 'exact_cases': exact,
            'exact_case_rate': ratio(exact, len(results)),
            'listing_precision': ratio(totals['matched_listings'], totals['predicted_listings']),
            'listing_recall': ratio(totals['matched_listings'], totals['expected_listings']),
            'availability_accuracy': ratio(totals['correct_availability'], totals['expected_listings']),
            'confirmed_price_precision': ratio(totals['correct_confirmed_prices'], totals['predicted_confirmed_prices']),
            'confirmed_price_recall': ratio(totals['correct_confirmed_prices'], totals['expected_confirmed_prices']),
            'passed': bool(results) and exact == len(results)}


def load_cases(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise SafeError('Evaluation corpus is too large')
    try:
        document = json.loads(raw)
        cases = document['cases']
        if document['version'] != 1 or not isinstance(cases, list) or not cases:
            raise ValueError
        ids = set()
        for case in cases:
            if not isinstance(case['id'], str) or not case['id'] or case['id'] in ids:
                raise ValueError
            ids.add(case['id'])
            if case['listing_type'] not in ('rental', 'sale') or not isinstance(case['currency'], str):
                raise ValueError
            if not isinstance(case['text'], str) or len(case['text'].encode('utf-8')) > MAX_TEXT_BYTES:
                raise ValueError
            if not isinstance(case['tags'], list) or not all(isinstance(tag, str) for tag in case['tags']):
                raise ValueError
            if not isinstance(case['expected'], list):
                raise ValueError
            for row in case['expected']:
                if set(row) != {'unit', 'availability', 'confirmed_price', 'bedrooms'}:
                    raise ValueError
                if not isinstance(row['unit'], str) or canonical_identity(row['unit']) != row['unit'] or not row['unit']:
                    raise ValueError
                if row['availability'] not in ('available', 'unavailable', 'unknown'):
                    raise ValueError
                if row['confirmed_price'] is not None and (not isinstance(row['confirmed_price'], str) or money(row['confirmed_price']) != row['confirmed_price']):
                    raise ValueError
                if row['bedrooms'] is not None and (type(row['bedrooms']) is not int or not 0 <= row['bedrooms'] <= 20):
                    raise ValueError
            score_case(case['expected'], case['expected'])
        return cases, hashlib.sha256(raw).hexdigest()
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise SafeError('Invalid evaluation corpus') from None


def evaluate(settings: Settings, corpus: Path, output: Path, *, repeats: int = 1) -> dict:
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise SafeError('Evaluation repeats must be between 1 and 10')
    cases, corpus_hash = load_cases(corpus)
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    report = {'scope': 'synthetic_local_model_regression', 'production_accuracy_established': False,
              'run_id': uuid.uuid4().hex, 'complete': False, 'status': 'running',
              'generated_at': datetime.now(timezone.utc).isoformat(), 'model': settings.model,
              'backend': settings.model_backend, 'corpus_sha256': corpus_hash, 'repeats': repeats,
              'unique_cases': len(cases), 'planned_evaluations': len(cases) * repeats,
              'summary': summarize([]), 'cases': results}
    write_report(output, report)
    for repeat in range(1, repeats + 1):
        for case in cases:
            begin = time.monotonic()
            source = Source('evaluation', 'https://example.com/evaluation', listing_type=case['listing_type'], currency=case['currency'])
            failure = None
            predicted = []
            try:
                client = LocalClient(settings, source)
                extracted = validate_extraction(client.extract(case['text']), case['text'], case['listing_type'])
                predicted = [{'unit': row['identity'], 'availability': row['availability'],
                              'confirmed_price': money(row['price']) if confirmed_price(row['evidence'], row['price'], case['listing_type'], case['currency']) else None,
                              'bedrooms': row['bedrooms']} for row in extracted]
            except SafeError as error:
                failure = str(error)
            result = {'id': case['id'], 'repeat': repeat, 'tags': case['tags'],
                      'expected': case['expected'], 'predicted': predicted,
                      'error': failure, 'seconds': round(time.monotonic()-begin, 3),
                      'scores': score_case(case['expected'], predicted, failed=failure is not None)}
            results.append(result)
            report['summary'] = summarize(results)
            report['complete'] = len(results) == len(cases) * repeats
            report['summary']['passed'] = report['complete'] and report['summary']['passed']
            report['status'] = ('passed' if report['summary']['passed'] else 'failed') if report['complete'] else 'running'
            report['seconds'] = round(time.monotonic()-started, 3)
            # Preserve completed-case evidence if inference is interrupted.
            write_report(output, report)
            print(json.dumps({'case': case['id'], 'repeat': repeat, 'exact_match': result['scores']['exact_match'], 'seconds': result['seconds']}), flush=True)
    return report

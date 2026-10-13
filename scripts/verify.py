#!/usr/bin/env python3
"""Run offline checks and explicitly requested local-model/package acceptance.

A passing run proves only the selected checks. Real bookings are never submitted.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from apartmint.evaluation import write_report


def run_check(name, command, output, root, *, timeout=600, environment=None):
    log = output/(name+'.log')
    start = time.monotonic()
    print('Running '+name+'...', flush=True)
    with log.open('w') as handle:
        try:
            result = subprocess.run(command, cwd=root, env=environment, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
            status, code = ('passed' if result.returncode == 0 else 'failed'), result.returncode
        except subprocess.TimeoutExpired:
            status, code = 'timed_out', None
        except OSError:
            status, code = 'could_not_start', None
    check = {'name': name, 'status': status, 'exit_code': code,
             'seconds': round(time.monotonic()-start, 2), 'log': str(log)}
    print(json.dumps(check), flush=True)
    return check


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model', action='store_true', help='Run the hand-labelled corpus and synthetic model flows')
    parser.add_argument('--repeats', type=int, default=1, help='Corpus repetitions, 1..10')
    parser.add_argument('--archive', type=Path, help='Also install and verify this tarball; requires --live-model')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.archive and not args.live_model:
        parser.error('--archive requires --live-model; package acceptance makes real local inference calls')
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats must be between 1 and 10')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = (args.output or root/'.runtime/verification'/timestamp).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['APARTMINT_DATA_DIR'] = str(output/'synthetic-data')
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'scope': 'internal_and_synthetic_acceptance',
              'real_property_booking_verified': False, 'fresh_machine_runtime_provisioning_verified': False,
              'complete': False, 'selected_checks_passed': False, 'release_ready': False,
              'checks': [{'name': 'offline-tests', 'status': 'pending'}], 'not_verified': ['real provider slot availability and booking', 'human approval through a real provider adapter',
                  'clean-machine native runtime installation', 'reboot and crash recovery', 'NVIDIA GPU execution',
                  'real-page extraction accuracy and city coverage']}
    if args.live_model:
        report['checks'].extend({'name': name, 'status': 'pending'} for name in ('model-quality', 'market-model', 'synthetic-booking'))
    else:
        report['checks'].append({'name': 'live-model-acceptance', 'status': 'not_run'})
    report['checks'].append({'name': 'package-install', 'status': 'pending' if args.archive else 'not_run'})
    write_report(output/'summary.json', report)
    def check(name, command, timeout=600):
        index = next(i for i, entry in enumerate(report['checks']) if entry['name'] == name)
        report['checks'][index]['status'] = 'running'
        write_report(output/'summary.json', report)
        item = run_check(name, command, output, root, timeout=timeout, environment=env)
        report['checks'][index] = item
        write_report(output/'summary.json', report)
    check('offline-tests', [sys.executable, '-m', 'pytest', '-q', '--junitxml='+str(output/'offline-tests.xml')], 180)
    if args.live_model:
        check('model-quality', [sys.executable, str(root/'scripts/eval_local_model.py'), '--output', str(output/'model-quality.json'), '--repeats', str(args.repeats)], 900*args.repeats)
        check('market-model', [sys.executable, str(root/'scripts/market_smoke.py')], 240)
        check('synthetic-booking', [sys.executable, '-m', 'apartmint', 'booking-demo', 'Synthetic Verification City'], 240)
    if args.archive:
        check('package-install', [sys.executable, str(root/'scripts/test_tar_deployment.py'), str(args.archive.resolve()), '--output', str(output/'installed-release')], 900)
    evaluated = [entry for entry in report['checks'] if entry['status'] != 'not_run']
    report['selected_checks_passed'] = all(entry['status'] == 'passed' for entry in evaluated)
    report['complete'] = True
    destination = output/'summary.json'
    write_report(destination, report)
    print(json.dumps({'selected_checks_passed': report['selected_checks_passed'], 'release_ready': False, 'report': str(destination)}))
    return 0 if report['selected_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

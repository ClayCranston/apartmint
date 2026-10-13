#!/usr/bin/env python3
"""Install a release tarball independently and run a synthetic booking with local Qwen.

Prerequisites: uv and a running local model server. No real appointments are made.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--city', default='Synthetic Test City')
    parser.add_argument('--model-base-url', default=os.environ.get('APARTMINT_MODEL_BASE_URL', 'http://127.0.0.1:11437/v1'))
    parser.add_argument('--model', default=os.environ.get('APARTMINT_MODEL', 'apartmint-qwen3'))
    parser.add_argument('--model-backend', choices=['vllm', 'ollama'], default=os.environ.get('APARTMINT_MODEL_BACKEND', 'vllm'))
    parser.add_argument('--output', type=Path, help='New output directory; defaults under .runtime/deployment-tests')
    args = parser.parse_args()
    archive = args.archive.resolve()
    uv = shutil.which('uv')
    if not uv:
        raise SystemExit('Install uv before running this acceptance test.')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = archive.with_suffix(archive.suffix + '.sha256').read_text().split()[0]
    if digest != expected:
        raise SystemExit('Archive checksum mismatch.')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = (args.output or Path('.runtime/deployment-tests') / timestamp).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {'archive': str(archive), 'sha256': digest, 'mode': 'tar_synthetic_booking_acceptance',
              'real_appointment_booked': False,
              'model_runtime': 'preprovisioned_local_endpoint', 'model_base_url': args.model_base_url,
              'model': args.model, 'model_backend': args.model_backend}
    report_path = output / 'acceptance-report.json'
    try:
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            roots = {Path(member.name).parts[0] for member in members}
            if len(roots) != 1:
                raise ValueError('Archive must contain one release directory')
            if sum(member.size for member in members) > 20_000_000:
                raise ValueError('Source archive is unexpectedly large')
            for member in members:
                parts = Path(member.name).parts
                if (not member.isfile() or member.issym() or member.islnk()
                    or Path(member.name).is_absolute() or '..' in parts
                    or set(parts) & {'.env', '.runtime', '.venv', 'data', 'models'}
                    or member.name.endswith(('/config/sources.json', '/config/tour-contact.json', '/config/tour-offer.json'))
                    or any(part.startswith('.apartmint-') for part in parts)):
                    raise ValueError('Unsafe or private archive entry')
            bundle.extractall(output, filter='data')
        installed = output / roots.pop()
        env = {'PATH': str(Path(uv).parent) + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
               'HOME': str(Path.home()), 'PYTHONNOUSERSITE': '1',
               'APARTMINT_MODEL_BASE_URL': args.model_base_url,
               'APARTMINT_MODEL': args.model, 'APARTMINT_MODEL_BACKEND': args.model_backend,
               'APARTMINT_DATA_DIR': str(installed / 'data')}
        with (output / 'setup.log').open('w') as log:
            subprocess.run(['bash', 'scripts/setup.sh'], cwd=installed, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=240)
        python = installed / '.venv/bin/python'
        provenance = json.loads(subprocess.check_output([str(python), '-c', 'import json,sys,apartmint;print(json.dumps({"prefix":sys.prefix,"package":apartmint.__file__}))'], cwd=installed, env=env, text=True))
        if Path(provenance['prefix']).resolve() != (installed / '.venv').resolve() or not Path(provenance['package']).resolve().is_relative_to(installed):
            raise ValueError('Installed package did not come from extracted release')
        report['install'] = str(installed)
        report['provenance'] = provenance
        # Run the extracted release's tests, including tiny-model/process uninstall
        # and approval failure cases. No real model weights are removed.
        with (output / 'offline-acceptance.log').open('w') as log:
            checked = subprocess.run([str(python), '-m', 'pytest', '-q'], cwd=installed, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, timeout=180)
        if checked.returncode:
            raise RuntimeError('Extracted offline acceptance tests failed')
        report['offline_tests'] = 'passed'
        report['uninstall_scope'] = 'disposable_tiny_models_and_test_processes_only'
        demo = subprocess.check_output([str(installed / '.venv/bin/apartmint'), 'demo'], cwd=installed, env=env, text=True, timeout=30)
        report['offline_demo'] = json.loads(demo)
        approved = subprocess.check_output([str(installed / '.venv/bin/apartmint'), 'tour', 'approval-demo'], cwd=installed, env=env, text=True, timeout=30)
        report['approval_demo'] = json.loads(approved)
        if (report['approval_demo'].get('real_appointment_booked') is not False
                or report['approval_demo'].get('provider_submissions') != 1
                or report['approval_demo'].get('unapproved_submissions') != 0
                or report['approval_demo'].get('restart_check') != 'passed'):
            raise ValueError('Extracted approval acceptance was not verified')
        command = [str(installed / '.venv/bin/apartmint'), 'booking-demo', args.city]
        if os.uname().sysname == 'Darwin':
            command = ['/usr/bin/sandbox-exec', '-f', str(installed / 'config/local-only.sb'), *command]
        started = time.monotonic()
        result = subprocess.run(command, cwd=installed, env=env, capture_output=True, text=True, timeout=600)
        if result.returncode:
            report['booking_error'] = result.stderr[:2000]
            raise RuntimeError('Extracted booking acceptance failed')
        report['booking'] = json.loads(result.stdout)
        if report['booking']['real_appointment_booked'] is not False or report['booking']['restart_check'] != 'passed':
            raise ValueError('Missing verified synthetic receipt')
        report['seconds'] = round(time.monotonic()-started, 2)
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error_type'] = type(error).__name__
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'report': str(report_path), 'real_appointment_booked': False, 'seconds': report['seconds']}))


if __name__ == '__main__':
    main()

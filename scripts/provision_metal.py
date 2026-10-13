#!/usr/bin/env python3
"""Provision a private, owned Apple Silicon model runtime (preview; unaccepted on GPU)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from apartmint import lifecycle

WHEELS = [
    {'name': 'vllm-0.30.0+cpu-cp312-cp312-macosx_11_0_arm64.whl', 'version': '0.30.0+cpu',
     'url': 'https://github.com/vllm-project/vllm/releases/download/v0.30.0/vllm-0.30.0%2Bcpu-cp312-cp312-macosx_11_0_arm64.whl',
     'sha256': 'fd9adfd566a8afa4ecbdf36b04bab10ebf43cd9ca18eca4b0a0bf2013b8de135'},
    {'name': 'vllm_metal-0.30.0-cp312-cp312-macosx_15_0_arm64.whl', 'version': '0.30.0',
     'url': 'https://github.com/vllm-project/vllm-metal/releases/download/v0.30.0/vllm_metal-0.30.0-cp312-cp312-macosx_15_0_arm64.whl',
     'sha256': '3955de8b2c75b633ef889b70fa469a4db661a0c5bb2f78e36272d68b16da9353'},
]
OWNED_PATHS = ['.runtime/metal', '.runtime/python', '.runtime/provision-cache']
PLAN_NAME = '.apartmint-metal-provision-plan.json'


def _plan(root):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise lifecycle.LifecycleError('Provisioning requires native Apple Silicon macOS')
    try:
        major = int(platform.mac_ver()[0].split('.')[0])
    except ValueError:
        major = 0
    if major < 15:
        raise lifecycle.LifecycleError('Provisioning requires macOS 15 or later')
    value = lifecycle._load(root)
    if (root / '.runtime').is_symlink():
        raise lifecycle.LifecycleError('Runtime parent must not be a symbolic link')
    for relative in OWNED_PATHS:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise lifecycle.LifecycleError('Private runtime, Python, and provisioning-cache paths must be absent; existing or borrowed runtimes are never replaced')
    uv = shutil.which('uv')
    if not uv:
        raise lifecycle.LifecycleError('Install uv before provisioning the private runtime')
    return {'installation_id': value['installation_id'], 'uv': str(Path(uv).resolve()),
            'python': '3.12', 'owned_paths': OWNED_PATHS, 'wheels': WHEELS,
            'gpu_execution_verified': False, 'model_quality_verified': False,
            'dependencies_fully_locked': False,
            'limitations': ['Only the two core wheels are pinned and digest-verified; transitive dependencies resolve during installation.',
                            'This runtime pin has not passed GPU serving or model-quality acceptance.',
                            'Existing development runtimes and external shared assets are never replaced.']}


def _terminate_group(process):
    """Stop the session created by this invocation, including build descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and lifecycle._group_members(process.pid):
        process.poll()  # Reap the direct child while waiting for its descendants.
        time.sleep(0.03)
    if lifecycle._group_members(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and lifecycle._group_members(process.pid):
        time.sleep(0.03)
    if lifecycle._group_members(process.pid):
        raise lifecycle.LifecycleError('Provisioning descendants did not stop; owned partial files must be retained')


def _run_owned(command, *, timeout, check=True, **kwargs):
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        if lifecycle._group_members(process.pid):
            raise lifecycle.LifecycleError('Provisioning command left background processes; refusing to mark it complete')
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        _terminate_group(process)
        raise


def _download(wheel, destination):
    # macOS system curl uses the host's trusted TLS store. Keep verification on,
    # ignore curlrc, and pass no inherited credentials, proxy, or CA overrides.
    command = ['/usr/bin/curl', '-q', '--fail', '--silent', '--show-error', '--location',
               '--noproxy', '*', '--proto', '=https', '--proto-redir', '=https',
               '--connect-timeout', '20', '--max-time', '300', '--max-filesize', str(2 * 1024**3),
               '--user-agent', 'Apartmint-runtime-provisioner', '--url', wheel['url']]
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(destination.parent), 'LC_ALL': 'C'}
    created = False
    try:
        # Pass the exclusively created file descriptor as stdout; curl never
        # reopens a path and cannot overwrite a preexisting file or symlink.
        with destination.open('xb') as output:
            created = True
            _run_owned(command, stdout=output, stderr=subprocess.PIPE,
                           env=env, check=True, timeout=330)
        if destination.stat().st_size > 2 * 1024**3:
            raise lifecycle.LifecycleError('Runtime wheel exceeds the download size limit')
        digest = hashlib.sha256()
        with destination.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != wheel['sha256']:
            raise lifecycle.LifecycleError('Runtime wheel SHA-256 verification failed')
    except FileExistsError:
        raise lifecycle.LifecycleError('Runtime download destination already exists; refusing to replace it') from None
    except (OSError, subprocess.SubprocessError, lifecycle.LifecycleError) as error:
        if created:
            destination.unlink(missing_ok=True)
        if isinstance(error, lifecycle.LifecycleError):
            raise
        raise lifecycle.LifecycleError('Runtime wheel download failed; TLS verification remains enabled') from None


def provision(root, *, dry_run=False, install=False):
    root = Path(root).resolve()
    if dry_run == install:
        raise lifecycle.LifecycleError('Choose --dry-run or --install')
    with lifecycle._locked(root):
        plan = _plan(root)
        plan_path = root / PLAN_NAME
        if dry_run:
            lifecycle._json_write(plan_path, {'hash': lifecycle._plan_hash(plan), 'created_at': time.time()})
            return {'dry_run': True, **plan}
        if plan_path.is_symlink():
            raise lifecycle.LifecycleError('Refusing a symbolic-link provisioning plan')
        try:
            reviewed = json.loads(plan_path.read_text())
            valid = reviewed['hash'] == lifecycle._plan_hash(plan) and 0 <= time.time() - reviewed['created_at'] <= 900
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise lifecycle.LifecycleError('Run a matching --dry-run within 15 minutes before --install')
        manifest = lifecycle._load(root)
        for relative in OWNED_PATHS:
            path = root / relative
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
            lifecycle._own(manifest, root, relative)
            # Persist each asset before any network request or Python download.
            lifecycle._json_write(root / lifecycle.MANIFEST, manifest)
        cache = root / '.runtime/provision-cache'
        temporary = cache / 'tmp'
        temporary.mkdir(mode=0o700)
        env = {'PATH': str(Path(plan['uv']).parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
               'HOME': str(cache), 'TMPDIR': str(temporary), 'PYTHONNOUSERSITE': '1',
               'UV_NO_CONFIG': '1', 'UV_PYTHON_INSTALL_DIR': str(root / '.runtime/python'),
               'UV_CACHE_DIR': str(cache / 'uv')}
        receipt_path = cache / 'apartmint-runtime.json'
        receipt = {**plan, 'status': 'provisioning', 'created_at': datetime.now(timezone.utc).isoformat()}
        lifecycle._json_write(receipt_path, receipt)
        try:
            wheel_paths = []
            for wheel in WHEELS:
                target = cache / wheel['name']
                _download(wheel, target)
                # Check independently at the install boundary as well as in the transport.
                digest = hashlib.sha256()
                with target.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != wheel['sha256']:
                    raise lifecycle.LifecycleError('Runtime wheel SHA-256 verification failed')
                wheel_paths.append(str(target))
            _run_owned([plan['uv'], 'venv', '--no-config', '--allow-existing', '--managed-python', '--python', '3.12', str(root / '.runtime/metal')], cwd=root, env=env, check=True, timeout=600)
            _run_owned([plan['uv'], 'pip', 'install', '--no-config', '--python', str(root / '.runtime/metal/bin/python'), *wheel_paths], cwd=root, env=env, check=True, timeout=900)
            receipt['status'] = 'installed_unverified'
            plan_path.unlink(missing_ok=True)
        except Exception:
            receipt['status'] = 'failed_owned_partial_install'
            raise
        finally:
            lifecycle._json_write(receipt_path, receipt)
            lifecycle._json_write(root / '.runtime/metal/apartmint-runtime.json', receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = provision(ROOT, dry_run=args.dry_run, install=args.install)
        print(json.dumps(result, indent=2))
        return 0
    except (lifecycle.LifecycleError, OSError, subprocess.SubprocessError, ValueError):
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, lifecycle.LifecycleError) else 'Private runtime provisioning failed; owned partial assets remain available for managed cleanup'
        print(json.dumps({'error': message}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
import subprocess

import pytest

from apartmint import lifecycle


@pytest.fixture
def provisioner(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts/provision_metal.py'
    spec = importlib.util.spec_from_file_location('apartmint_test_provision_metal', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(module.platform, 'mac_ver', lambda: ('15.7', ('', '', ''), 'arm64'))
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/controlled/bin/uv')
    return module


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / 'managed'
    root.mkdir()
    lifecycle.initialize(root)
    return root


def test_dry_run_never_downloads_or_creates_runtime(provisioner, managed, monkeypatch):
    monkeypatch.setattr(provisioner, '_download', lambda *args: pytest.fail('No download during dry run'))
    monkeypatch.setattr(provisioner, '_run_owned', lambda *args, **kwargs: pytest.fail('No uv during dry run'))
    report = provisioner.provision(managed, dry_run=True)
    assert report['dry_run']
    assert report['gpu_execution_verified'] is False
    assert report['dependencies_fully_locked'] is False
    assert not (managed / '.runtime').exists()
    assert all(len(wheel['sha256']) == 64 for wheel in report['wheels'])


@pytest.mark.parametrize('kind', ['directory', 'shared_symlink'])
def test_existing_or_shared_runtime_never_replaced(provisioner, managed, tmp_path, kind):
    runtime = managed / '.runtime/metal'
    runtime.parent.mkdir()
    shared = tmp_path / 'shared-brain-runtime'
    shared.mkdir()
    (shared / 'KEEP').write_text('shared test data')
    if kind == 'directory':
        runtime.mkdir()
        (runtime / 'KEEP').write_text('existing test runtime')
    else:
        runtime.symlink_to(shared, target_is_directory=True)
    with pytest.raises(lifecycle.LifecycleError, match='never replaced'):
        provisioner.provision(managed, dry_run=True)
    assert (shared / 'KEEP').exists()
    assert (runtime / 'KEEP').exists()


@pytest.mark.parametrize('system,machine,version', [('Linux', 'aarch64', '15.0'), ('Darwin', 'x86_64', '15.0'), ('Darwin', 'arm64', '14.9')])
def test_platform_requirements(provisioner, managed, monkeypatch, system, machine, version):
    monkeypatch.setattr(provisioner.platform, 'system', lambda: system)
    monkeypatch.setattr(provisioner.platform, 'machine', lambda: machine)
    monkeypatch.setattr(provisioner.platform, 'mac_ver', lambda: (version, ('', '', ''), machine))
    with pytest.raises(lifecycle.LifecycleError):
        provisioner.provision(managed, dry_run=True)


def test_install_requires_reviewed_plan(provisioner, managed):
    with pytest.raises(lifecycle.LifecycleError, match='matching --dry-run'):
        provisioner.provision(managed, install=True)
    assert not (managed / '.runtime').exists()


def test_tiny_verified_wheels_use_owned_python_and_clean_uv_environment(provisioner, managed, monkeypatch):
    payload = b'tiny synthetic wheel bytes'
    wheels = [dict(wheel, sha256=hashlib.sha256(payload).hexdigest()) for wheel in provisioner.WHEELS]
    monkeypatch.setattr(provisioner, 'WHEELS', wheels)
    monkeypatch.setenv('PRIVATE_PROVIDER_TOKEN', 'PRIVATE_CANARY')
    monkeypatch.setenv('UV_PYTHON_INSTALL_DIR', '/external/shared-python')
    monkeypatch.setenv('UV_CACHE_DIR', '/external/shared-cache')
    monkeypatch.setenv('HTTPS_PROXY', 'http://credential:secret@external.example')
    calls = []
    def download(wheel, target):
        manifest = lifecycle._load(managed)
        assert set(provisioner.OWNED_PATHS) <= set(manifest['assets'])
        assert all((managed / relative).is_dir() for relative in provisioner.OWNED_PATHS)
        target.write_bytes(payload)
    def uv(command, **kwargs):
        calls.append(command)
        assert command[0] == '/controlled/bin/uv'
        assert '--no-config' in command
        env = kwargs['env']
        assert env['UV_PYTHON_INSTALL_DIR'] == str(managed / '.runtime/python')
        assert env['UV_CACHE_DIR'] == str(managed / '.runtime/provision-cache/uv')
        assert set(env) == {'PATH', 'HOME', 'TMPDIR', 'PYTHONNOUSERSITE', 'UV_NO_CONFIG', 'UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR'}
        assert 'PRIVATE_CANARY' not in json.dumps(env)
        assert 'secret' not in json.dumps(env)
        if command[1] == 'venv':
            assert '--managed-python' in command
            assert '--allow-existing' in command
            assert command[command.index('--python') + 1] == '3.12'
            assert list((managed / '.runtime/metal').iterdir()) == []
            (managed / '.runtime/metal/bin').mkdir()
            (managed / '.runtime/metal/bin/python').write_text('synthetic interpreter')
        else:
            assert command[1:3] == ['pip', 'install']
            assert command[command.index('--python') + 1] == str(managed / '.runtime/metal/bin/python')
            assert [Path(path).name for path in command[-2:]] == [wheel['name'] for wheel in wheels]
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(provisioner, '_download', download)
    monkeypatch.setattr(provisioner, '_run_owned', uv)
    provisioner.provision(managed, dry_run=True)
    receipt = provisioner.provision(managed, install=True)
    assert len(calls) == 2
    assert receipt['status'] == 'installed_unverified'
    assert receipt['gpu_execution_verified'] is False
    assert receipt['model_quality_verified'] is False
    assert (managed / '.runtime/metal/apartmint-runtime.json').is_file()
    assert not (managed / provisioner.PLAN_NAME).exists()


def test_digest_mismatch_never_invokes_uv_and_partial_assets_remain_owned(provisioner, managed, monkeypatch):
    monkeypatch.setattr(provisioner, '_download', lambda wheel, target: target.write_bytes(b'incorrect fixture'))
    monkeypatch.setattr(provisioner, '_run_owned', lambda *args, **kwargs: pytest.fail('Must verify every wheel before uv'))
    provisioner.provision(managed, dry_run=True)
    with pytest.raises(lifecycle.LifecycleError, match='SHA-256'):
        provisioner.provision(managed, install=True)
    assert set(provisioner.OWNED_PATHS) <= set(lifecycle._load(managed)['assets'])
    receipt = json.loads((managed / '.runtime/metal/apartmint-runtime.json').read_text())
    assert receipt['status'] == 'failed_owned_partial_install'
    lifecycle.action(managed, 'uninstall', dry_run=True)
    lifecycle.action(managed, 'uninstall', yes=True)
    assert all(not (managed / relative).exists() for relative in provisioner.OWNED_PATHS)


def test_system_curl_download_uses_verified_tls_clean_environment_and_exclusive_output(provisioner, tmp_path, monkeypatch):
    payload = b'tiny digest-verified wheel fixture'
    wheel = dict(provisioner.WHEELS[0], sha256=hashlib.sha256(payload).hexdigest())
    destination = tmp_path / wheel['name']
    monkeypatch.setenv('HTTPS_PROXY', 'http://private:password@proxy.example')
    monkeypatch.setenv('CURL_CA_BUNDLE', '/private/custom-ca')
    monkeypatch.setenv('PRIVATE_TOKEN', 'PRIVATE_CANARY')
    calls = []
    def curl(command, **kwargs):
        calls.append(command)
        assert command[:2] == ['/usr/bin/curl', '-q']
        for flag, value in [('--noproxy', '*'), ('--proto', '=https'), ('--proto-redir', '=https'), ('--max-time', '300'), ('--connect-timeout', '20'), ('--max-filesize', str(2 * 1024**3))]:
            assert command[command.index(flag) + 1] == value
        assert '--insecure' not in command and '-k' not in command
        assert '--output' not in command and '-o' not in command
        assert kwargs['timeout'] == 330 and kwargs['check'] is True
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(tmp_path), 'LC_ALL': 'C'}
        assert kwargs['stdout'].name == str(destination)
        kwargs['stdout'].write(payload)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(provisioner, '_run_owned', curl)
    provisioner._download(wheel, destination)
    assert destination.read_bytes() == payload
    assert len(calls) == 1


def test_curl_digest_mismatch_removes_only_new_download(provisioner, tmp_path, monkeypatch):
    destination = tmp_path / 'wheel.whl'
    def curl(command, **kwargs):
        kwargs['stdout'].write(b'incorrect bytes')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(provisioner, '_run_owned', curl)
    with pytest.raises(lifecycle.LifecycleError, match='SHA-256'):
        provisioner._download(provisioner.WHEELS[0], destination)
    assert not destination.exists()
    destination.write_bytes(b'existing private fixture')
    monkeypatch.setattr(provisioner, '_run_owned', lambda *args, **kwargs: pytest.fail('No transfer may overwrite an existing destination'))
    with pytest.raises(lifecycle.LifecycleError, match='already exists'):
        provisioner._download(provisioner.WHEELS[0], destination)
    assert destination.read_bytes() == b'existing private fixture'


def test_curl_transfer_failure_keeps_tls_enabled_and_removes_partial_file(provisioner, tmp_path, monkeypatch):
    destination = tmp_path / 'wheel.whl'
    def curl(command, **kwargs):
        kwargs['stdout'].write(b'partial bytes')
        raise subprocess.CalledProcessError(60, command, stderr=b'PRIVATE_FAILURE_CANARY')
    monkeypatch.setattr(provisioner, '_run_owned', curl)
    with pytest.raises(lifecycle.LifecycleError, match='TLS verification remains enabled') as error:
        provisioner._download(provisioner.WHEELS[0], destination)
    assert 'PRIVATE_FAILURE_CANARY' not in str(error.value)
    assert not destination.exists()


def test_provisioning_timeout_stops_parent_and_build_descendant(provisioner, tmp_path):
    identities = tmp_path / 'processes.json'
    child_code = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    parent_code = ('import json,os,pathlib,signal,subprocess,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                   f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}]);'
                   f'pathlib.Path({str(identities)!r}).write_text(json.dumps([os.getpid(),child.pid]));time.sleep(60)')
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            provisioner._run_owned([sys.executable, '-c', parent_code], timeout=0.3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert time.monotonic() - started < 6
        pids = json.loads(identities.read_text())
        assert all(lifecycle._identity(pid) is None for pid in pids)
        assert not lifecycle._group_members(pids[0])
    finally:
        if identities.exists():
            pids = json.loads(identities.read_text())
            if lifecycle._group_members(pids[0]):
                os.killpg(pids[0], signal.SIGKILL)

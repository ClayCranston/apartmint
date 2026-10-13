import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from apartmint import lifecycle


@pytest.fixture
def install(tmp_path):
    root = tmp_path / 'installation'
    (root / 'src/apartmint').mkdir(parents=True)
    (root / 'scripts').mkdir()
    source = Path(__file__).resolve().parents[1]
    shutil.copyfile(source / 'src/apartmint/lifecycle.py', root / 'src/apartmint/lifecycle.py')
    (root / 'src/apartmint/__init__.py').write_text('')
    shutil.copyfile(source / 'scripts/manage.py', root / 'scripts/manage.py')
    lifecycle.initialize(root)
    return root


def test_bootstrap_works_without_virtual_environment(install):
    result = subprocess.run([sys.executable, str(install / 'scripts/manage.py'), 'status'], capture_output=True, text=True, check=True, timeout=5)
    report = json.loads(result.stdout)
    assert report['action'] == 'status'
    assert not (install / '.venv').exists()


def test_initialization_does_not_adopt_existing_models_or_runtime(tmp_path):
    (tmp_path / 'models/existing').mkdir(parents=True)
    (tmp_path / '.runtime/metal').mkdir(parents=True)
    (tmp_path / '.venv').mkdir()
    lifecycle.initialize(tmp_path)
    manifest = json.loads((tmp_path / lifecycle.MANIFEST).read_text())
    assert not manifest['assets']
    retained = {item['path'] for item in lifecycle.status(tmp_path)['retained']}
    assert {'models/existing', '.runtime/metal', '.venv'} <= retained


def test_new_owned_model_runtime_and_cache_are_removed_private_data_retained(install):
    for path in ('models/tiny-model', '.runtime/metal', '.venv'):
        directory = install / path
        directory.mkdir(parents=True)
        (directory / 'synthetic-asset').write_text('tiny test data')
        lifecycle.register_owned(install, path)
    lifecycle.prepare_caches(install)
    (install / 'data').mkdir()
    (install / 'data/listings.json').write_text('private synthetic history')
    (install / '.env').write_text('SYNTHETIC_SECRET=private')
    (install / 'config').mkdir(exist_ok=True)
    (install / 'config/sources.json').write_text('synthetic configuration')
    plan = lifecycle.action(install, 'uninstall', dry_run=True)
    assert all((install / row['path']).exists() for row in plan['remove'])
    assert 'SYNTHETIC_SECRET' not in json.dumps(plan)
    result = lifecycle.action(install, 'uninstall', yes=True)
    assert result['completed']
    for path in ('models/tiny-model', '.runtime/metal', '.venv', 'src', 'scripts', '.runtime/cache'):
        assert not (install / path).exists()
    assert (install / 'data/listings.json').is_file()
    assert (install / '.env').is_file()
    assert (install / 'config/sources.json').is_file()


def test_purge_private_data_requires_matching_explicit_plan(install):
    (install / 'data').mkdir()
    (install / 'data/private').write_text('fixture')
    (install / '.env').write_text('SYNTHETIC_ONLY=1')
    lifecycle.action(install, 'uninstall', dry_run=True)
    with pytest.raises(lifecycle.LifecycleError, match='matching dry-run'):
        lifecycle.action(install, 'uninstall', yes=True, purge_data=True)
    assert (install / '.env').exists()
    lifecycle.action(install, 'uninstall', dry_run=True, purge_data=True)
    lifecycle.action(install, 'uninstall', yes=True, purge_data=True)
    assert not (install / '.env').exists()
    assert not (install / 'data').exists()


def test_symlinked_shared_runtime_and_nested_model_target_are_never_deleted(install, tmp_path):
    external = tmp_path / 'shared-brain-runtime'
    external.mkdir()
    (external / 'KEEP').write_text('shared fixture')
    (install / '.runtime').mkdir()
    (install / '.runtime/metal').symlink_to(external, target_is_directory=True)
    lifecycle.register_owned(install, '.runtime/metal')
    model = install / 'models/tiny'
    model.mkdir(parents=True)
    (model / 'external-link').symlink_to(external, target_is_directory=True)
    lifecycle.register_owned(install, 'models/tiny')
    report = lifecycle.action(install, 'uninstall', dry_run=True)
    assert any(item.get('target') == str(external) for item in report['retained'])
    lifecycle.action(install, 'uninstall', yes=True)
    assert (external / 'KEEP').read_text() == 'shared fixture'
    assert not (install / '.runtime/metal').is_symlink()


@pytest.mark.parametrize('path', ['../shared', '.', 'data', '.env', 'config/sources.json'])
def test_cannot_register_unallowlisted_owned_paths(install, path):
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.register_owned(install, path)


def test_cannot_claim_through_symlink_parent(install, tmp_path):
    external = tmp_path / 'shared-models'
    (external / 'one').mkdir(parents=True)
    (install / 'models').symlink_to(external, target_is_directory=True)
    with pytest.raises(lifecycle.LifecycleError, match='symbolic-link'):
        lifecycle.register_owned(install, 'models/one')


def test_replaced_asset_blocks_deletion(install):
    lifecycle.action(install, 'uninstall', dry_run=True)
    (install / 'src').rename(install / 'original-src')
    (install / 'src').mkdir()
    (install / 'src/do-not-delete').write_text('different asset')
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.action(install, 'uninstall', yes=True)
    assert (install / 'src/do-not-delete').is_file()
    plan = lifecycle.action(install, 'uninstall', dry_run=True)
    assert plan['conflicts']
    with pytest.raises(lifecycle.LifecycleError, match='conflicts'):
        lifecycle.action(install, 'uninstall', yes=True)


def test_execution_requires_fresh_dry_run_and_rejects_expired_plan(install):
    with pytest.raises(lifecycle.LifecycleError, match='dry-run'):
        lifecycle.action(install, 'stop', yes=True)
    lifecycle.action(install, 'stop', dry_run=True)
    plan_path = install / '.apartmint-stop-plan.json'
    plan = json.loads(plan_path.read_text())
    plan['created_at'] -= 901
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(lifecycle.LifecycleError, match='dry-run'):
        lifecycle.action(install, 'stop', yes=True)


def test_stale_pid_identity_never_signals_unrelated_process(install, monkeypatch):
    manifest_path = install / lifecycle.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    current = lifecycle._identity(os.getpid())
    manifest['services']['model'] = dict(current, command_sha256='not-the-same-process')
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(lifecycle.os, 'killpg', lambda *args: pytest.fail('Must not signal an unrelated process'))
    plan = lifecycle.action(install, 'stop', dry_run=True)
    assert plan['services'][0]['state'] == 'identity_mismatch'
    with pytest.raises(lifecycle.LifecycleError, match='conflicts'):
        lifecycle.action(install, 'stop', yes=True)


def test_managed_stop_terminates_owned_parent_and_child_group(install):
    child_file = install / 'child.pid'
    child_code = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    parent_code = ('import pathlib,signal,subprocess,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                   f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}]);'
                   f'pathlib.Path({str(child_file)!r}).write_text(str(child.pid));time.sleep(60)')
    runner = subprocess.Popen([sys.executable, str(install / 'scripts/manage.py'), 'run', 'model', '--', sys.executable, '-c', parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    record = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            manifest = json.loads((install / lifecycle.MANIFEST).read_text())
            record = manifest['services'].get('model')
            if record and child_file.exists():
                break
            if runner.poll() is not None:
                pytest.fail('Managed fixture failed: ' + runner.stderr.read())
            time.sleep(0.03)
        assert record and child_file.exists()
        child_pid = int(child_file.read_text())
        plan = lifecycle.action(install, 'stop', dry_run=True)
        assert plan['services'][0]['state'] == 'running'
        lifecycle.action(install, 'stop', yes=True, grace_seconds=0.2)
        runner.wait(timeout=5)
        assert lifecycle._identity(record['pid']) is None
        assert lifecycle._identity(child_pid) is None
        assert not lifecycle._group_members(record['pgid'])
    finally:
        if record and lifecycle._identity(record['pid']) == record:
            os.killpg(record['pgid'], signal.SIGKILL)
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        runner.stderr.close()


@pytest.mark.parametrize('purge', [False, True])
def test_docker_resources_are_scoped_and_shared_images_are_retained(install, monkeypatch, purge):
    path = install / lifecycle.MANIFEST
    value = json.loads(path.read_text())
    project = 'apartmint-' + value['installation_id'][:12]
    value['docker'] = {'project': project}
    path.write_text(json.dumps(value))
    mutations = []
    def docker(command, **kwargs):
        assert command[0] == 'docker'
        if '--filter' in command:
            assert command[-1] == 'label=com.docker.compose.project=' + project
            if command[1:3] == ['ps', '-aq']: output = 'owned-container\n'
            elif command[1:3] == ['network', 'ls']: output = 'owned-network\n'
            else: output = 'owned-volume\n'
        elif 'inspect' in command:
            labels = {'com.docker.compose.project': project, 'com.docker.compose.project.working_dir': str(install)}
            output = json.dumps([{'Config': {'Labels': labels}} if command[1] == 'inspect' else {'Labels': labels}])
        else:
            mutations.append(command)
            output = ''
        return subprocess.CompletedProcess(command, 0, output, '')
    monkeypatch.setattr(lifecycle.subprocess, 'run', docker)
    report = lifecycle.action(install, 'uninstall', dry_run=True, purge_data=purge)
    assert not mutations
    assert any(row['path'] == 'Docker images' for row in report['retained'])
    lifecycle.action(install, 'uninstall', yes=True, grace_seconds=0, purge_data=purge)
    expected = [['docker', 'stop', '--time', '1', 'owned-container'], ['docker', 'rm', 'owned-container'], ['docker', 'network', 'rm', 'owned-network']]
    if purge:
        expected.append(['docker', 'volume', 'rm', 'owned-volume'])
    else:
        assert any(row['path'] == 'owned-volume' for row in report['retained'])
    assert mutations == expected


def test_wrong_docker_installation_labels_prevent_deletion(install, monkeypatch):
    path = install / lifecycle.MANIFEST
    value = json.loads(path.read_text())
    value['docker'] = {'project': 'apartmint-fixture'}
    path.write_text(json.dumps(value))
    def docker(command, **kwargs):
        if '--filter' in command: output = 'foreign-container'
        else: output = json.dumps([{'Config': {'Labels': {'com.docker.compose.project': 'apartmint-fixture', 'com.docker.compose.project.working_dir': '/another/install'}}}])
        return subprocess.CompletedProcess(command, 0, output, '')
    monkeypatch.setattr(lifecycle.subprocess, 'run', docker)
    with pytest.raises(lifecycle.LifecycleError, match='different installation'):
        lifecycle.action(install, 'uninstall', dry_run=True)


def test_private_native_runtime_requires_explicit_ownership(install):
    runtime = install / '.runtime/metal'
    runtime.mkdir(parents=True)
    with pytest.raises(lifecycle.LifecycleError, match='not registered'):
        lifecycle.register_external(install, runtime)
    lifecycle.register_owned(install, '.runtime/metal')
    lifecycle.register_external(install, runtime)
    assert 'model_runtime' not in json.loads((install / lifecycle.MANIFEST).read_text())['external']


def test_orphaned_group_blocks_uninstall_and_preserves_assets(install):
    child_file = install / 'child.pid'
    code = 'import os,pathlib,time;pathlib.Path(' + repr(str(child_file)) + ').write_text(str(os.getpid()));time.sleep(60)'
    runner = subprocess.Popen([sys.executable, str(install / 'scripts/manage.py'), 'run', 'model', '--', sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    record = None
    child_identity = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = json.loads((install / lifecycle.MANIFEST).read_text())['services'].get('model')
            if record and child_file.exists():
                break
            time.sleep(0.03)
        assert record and child_file.exists()
        child_pid = int(child_file.read_text())
        child_identity = lifecycle._identity(child_pid)
        os.kill(record['pid'], signal.SIGKILL)
        runner.wait(timeout=5)
        plan = lifecycle.action(install, 'uninstall', dry_run=True)
        assert plan['services'][0]['state'] == 'orphaned_group'
        with pytest.raises(lifecycle.LifecycleError, match='conflicts'):
            lifecycle.action(install, 'uninstall', yes=True)
        assert (install / 'src/apartmint/lifecycle.py').is_file()
        assert lifecycle._identity(child_pid) == child_identity
    finally:
        if child_identity and lifecycle._identity(child_identity['pid']) == child_identity:
            os.kill(child_identity['pid'], signal.SIGKILL)
        if record and lifecycle._identity(record['pid']) == record:
            os.killpg(record['pgid'], signal.SIGKILL)
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        runner.stderr.close()


def test_cli_uninstall_stops_model_and_removes_owned_runtime_without_venv(install):
    for relative in ('models/tiny-owned', '.runtime/metal'):
        target = install / relative
        target.mkdir(parents=True)
        (target / 'tiny-fixture').write_text('owned test asset')
        lifecycle.register_owned(install, relative)
    started_file = install / 'service-ready'
    code = 'import pathlib,time;pathlib.Path(' + repr(str(started_file)) + ').write_text("ready");time.sleep(60)'
    manager = str(install / 'scripts/manage.py')
    runner = subprocess.Popen([sys.executable, manager, 'run', 'model', '--', sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    record = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = json.loads((install / lifecycle.MANIFEST).read_text())['services'].get('model')
            if record and started_file.exists():
                break
            time.sleep(0.03)
        assert record and started_file.exists()
        dry = subprocess.run([sys.executable, manager, 'uninstall', '--dry-run'], capture_output=True, text=True, check=True, timeout=5)
        assert json.loads(dry.stdout)['services'][0]['state'] == 'running'
        assert (install / 'models/tiny-owned/tiny-fixture').exists()
        done = subprocess.run([sys.executable, manager, 'uninstall', '--yes'], capture_output=True, text=True, check=True, timeout=10)
        assert json.loads(done.stdout)['completed'] is True
        runner.wait(timeout=5)
        assert not (install / 'models/tiny-owned').exists()
        assert not (install / '.runtime/metal').exists()
        assert not (install / 'scripts').exists()
        assert lifecycle._identity(record['pid']) is None
    finally:
        if record and lifecycle._identity(record['pid']) == record:
            os.killpg(record['pgid'], signal.SIGKILL)
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        runner.stderr.close()


def test_restart_cannot_overwrite_dead_leader_with_live_group(install, monkeypatch):
    manifest_path = install / lifecycle.MANIFEST
    value = json.loads(manifest_path.read_text())
    old_record = {'pid': 1234567, 'pgid': 1234567, 'started': 'fixture start', 'command_sha256': 'fixture identity'}
    value['services']['model'] = old_record
    manifest_path.write_text(json.dumps(value))
    original_bytes = manifest_path.read_bytes()
    monkeypatch.setattr(lifecycle, '_identity', lambda pid: None)
    group_checks = []
    def members(pgid):
        group_checks.append(pgid)
        return [1234568]
    monkeypatch.setattr(lifecycle, '_group_members', members)
    monkeypatch.setattr(lifecycle.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Must not spawn over an orphaned service record'))
    monkeypatch.setattr(lifecycle, '_json_write', lambda *args, **kwargs: pytest.fail('Must not replace the existing manifest or create a launch file'))
    with pytest.raises(lifecycle.LifecycleError, match='Orphaned service group'):
        lifecycle.run_service(install, 'model', [sys.executable, '-c', 'pass'])
    assert group_checks == [old_record['pgid']]
    assert manifest_path.read_bytes() == original_bytes
    assert not (install / '.runtime').exists()


def test_startup_handshake_waits_for_wrapper_exec_and_untruncated_identity(install, monkeypatch):
    import threading
    original_python = sys.executable
    long_directory = install / ('long-interpreter-path-' + 'x' * 80) / ('nested-' + 'y' * 80)
    long_directory.mkdir(parents=True)
    wrapper = long_directory / 'python-wrapper'
    gate = install / 'wrapper-gate'
    marker = install / 'wrapper-entered'
    child_ready = install / 'model-started'
    os.mkfifo(gate)
    import shlex
    wrapper.write_text('#!/bin/sh\nprintf ready > ' + shlex.quote(str(marker)) + '\nread ready < ' + shlex.quote(str(gate)) + '\nexec ' + shlex.quote(original_python) + ' "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setattr(lifecycle.sys, 'executable', str(wrapper))
    monkeypatch.setenv('COLUMNS', '40')
    errors = []
    code = 'import pathlib,time;pathlib.Path(' + repr(str(child_ready)) + ').write_text("ready");time.sleep(60)'
    def run():
        try:
            lifecycle.run_service(install, 'model', [original_python, '-c', code])
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    record = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        # The wrapper has not exec'd its final interpreter: no identity may be
        # committed yet and the actual service must not have started.
        assert json.loads((install / lifecycle.MANIFEST).read_text())['services'] == {}
        assert not child_ready.exists()
        with gate.open('w') as handle:
            handle.write('continue\n')
        deadline = time.monotonic() + 5
        while not child_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_ready.exists()
        record = json.loads((install / lifecycle.MANIFEST).read_text())['services']['model']
        monkeypatch.setenv('COLUMNS', '300')
        assert lifecycle._identity(record['pid']) == record
        assert lifecycle.status(install)['services'][0]['state'] == 'running'
        lifecycle.action(install, 'stop', dry_run=True)
        lifecycle.action(install, 'stop', yes=True, grace_seconds=0.5)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
    finally:
        if record and lifecycle._identity(record['pid']) == record:
            os.killpg(record['pgid'], signal.SIGKILL)
        thread.join(timeout=1)

"""Installation-scoped lifecycle management; deliberately uses the standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

MANIFEST = '.apartmint-install.json'
APP_ASSETS = ['README.md', 'pyproject.toml', 'uv.lock', 'requirements.lock', 'Dockerfile', '.dockerignore', '.gitignore', '.env.example', 'compose.yaml', 'compose.local.yaml', 'src', 'scripts', 'tests', 'docs', 'config/local-only.sb', 'config/sources.example.json', 'config/tour-offer.example.json', 'config/tour-contact.example.json']
PRIVATE_ASSETS = ['data', '.env', 'config/sources.json']
CACHE_ASSETS = ['.runtime/cache', '.runtime/config', '.runtime/tmp', '.runtime/ipc', '.runtime/huggingface']


class LifecycleError(Exception):
    pass


def _json_write(path, value):
    if path.is_symlink():
        raise LifecycleError('Refusing a symbolic link for lifecycle state')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(path):
    info = path.lstat()
    return {'device': info.st_dev, 'inode': info.st_ino, 'kind': stat.S_IFMT(info.st_mode)}


def _relative(root, value):
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise LifecycleError('Owned paths must be inside this installation') from None
    if not path.parts or '..' in path.parts or str(path) == '.':
        raise LifecycleError('Invalid owned installation path')
    # Never traverse a symbolic-link parent to claim or delete an external file.
    if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts))):
        raise LifecycleError('Owned paths cannot traverse a symbolic-link directory')
    return str(path)


def _load(root):
    path = root / MANIFEST
    if path.is_symlink() or not path.is_file():
        raise LifecycleError('No managed installation manifest; run manage.py init explicitly')
    try:
        value = json.loads(path.read_text())
        if (value['schema'] != 1 or value['root'] != str(root)
                or value['root_identity'] != _fingerprint(root)
                or not re.fullmatch('[a-f0-9]{32}', value['installation_id'])):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError):
        raise LifecycleError('Installation identity is invalid or the installation was moved; refusing lifecycle actions') from None


@contextmanager
def _locked(root):
    path = root / '.apartmint-lifecycle.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _own(value, root, relative):
    relative = _relative(root, relative)
    allowed = relative in APP_ASSETS + CACHE_ASSETS + ['.venv', '.runtime/metal', '.runtime/python', '.runtime/provision-cache'] or relative.startswith('models/')
    if not allowed:
        raise LifecycleError('Path is outside the allowlisted application, model, runtime, and cache locations')
    path = root / relative
    if not path.exists() and not path.is_symlink():
        raise LifecycleError('Cannot register an asset that does not exist')
    value['assets'][relative] = _fingerprint(path)
    if path.is_symlink():
        value['external'][relative] = str(path.resolve())


def initialize(root, *, own_venv=False):
    root = Path(root).resolve()
    with _locked(root):
        if (root / MANIFEST).exists() or (root / MANIFEST).is_symlink():
            value = _load(root)
        else:
            value = {'schema': 1, 'installation_id': uuid.uuid4().hex, 'root': str(root),
                     'root_identity': _fingerprint(root), 'assets': {}, 'external': {}, 'services': {}, 'docker': None}
            for relative in APP_ASSETS:
                if (root / relative).exists() or (root / relative).is_symlink():
                    _own(value, root, relative)
        if own_venv:
            _own(value, root, '.venv')
        _json_write(root / MANIFEST, value)
    return {'managed': True, 'installation_id': value['installation_id'], 'owned_assets': sorted(value['assets']), 'note': 'Existing models and native runtimes are not adopted automatically.'}


def register_owned(root, relative):
    root = Path(root).resolve()
    with _locked(root):
        value = _load(root)
        _own(value, root, relative)
        _json_write(root / MANIFEST, value)
    return {'owned_path': relative, 'external_target_retained': value['external'].get(relative)}


def register_external(root, path):
    root = Path(root).resolve()
    path = Path(path)
    with _locked(root):
        value = _load(root)
        resolved = path.resolve()
        if resolved.is_relative_to(root) and not path.is_symlink() and str(resolved.relative_to(root)) not in value['assets']:
            raise LifecycleError('Private native runtime is not registered; run manage.py own .runtime/metal before serving')
        if not any(resolved == root / relative for relative in value['assets'] if not (root / relative).is_symlink()):
            value['external']['model_runtime'] = str(resolved)
        else:
            value['external'].pop('model_runtime', None)
        _json_write(root / MANIFEST, value)


def prepare_caches(root):
    root = Path(root).resolve()
    with _locked(root):
        value = _load(root)
        for relative in CACHE_ASSETS:
            path = root / _relative(root, relative)
            if not path.exists() and not path.is_symlink():
                path.mkdir(parents=True, mode=0o700)
                _own(value, root, relative)
        _json_write(root / MANIFEST, value)
    return {'prepared_caches': CACHE_ASSETS}


def _identity(pid):
    result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'lstart=', '-o', 'pgid=', '-o', 'stat=', '-o', 'args='], capture_output=True, text=True, timeout=3)
    parts = result.stdout.strip().split(None, 7)
    if result.returncode or len(parts) != 8 or parts[6].startswith('Z'):
        return None
    return {'pid': pid, 'pgid': int(parts[5]), 'started': ' '.join(parts[:5]),
            'command_sha256': hashlib.sha256(parts[7].encode()).hexdigest()}


def _group_members(pgid):
    result = subprocess.run(['ps', '-ax', '-o', 'pid=', '-o', 'pgid=', '-o', 'stat='], capture_output=True, text=True, check=True, timeout=3, start_new_session=True)
    members = []
    for line in result.stdout.splitlines():
        row = line.split()
        if len(row) == 3 and row[1] == str(pgid) and not row[2].startswith('Z'):
            members.append(int(row[0]))
    return members


def supervise(command_file):
    """Publish identity after interpreter startup; start children only once recorded."""
    command_file = Path(command_file)
    command = json.loads(command_file.read_text())
    command_file.unlink()
    ready_file = command_file.with_suffix('.ready.json')
    go_file = command_file.with_suffix('.go.json')
    identity = _identity(os.getpid())
    if identity is None or identity['pgid'] != os.getpid():
        raise LifecycleError('Supervisor could not establish its stable process identity')
    _json_write(ready_file, {'identity': identity, 'token': command_file.name})
    deadline = time.monotonic() + 10
    while not go_file.exists():
        if time.monotonic() >= deadline:
            raise LifecycleError('Supervisor ownership was not recorded before startup deadline')
        time.sleep(0.01)
    if go_file.is_symlink() or json.loads(go_file.read_text()) != {'token': command_file.name}:
        raise LifecycleError('Supervisor startup acknowledgement did not match')
    go_file.unlink()
    # Group TERM also reaches the real service. Keep the leader available for
    # identity verification and bounded group KILL if a child ignores TERM.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    child = subprocess.Popen(command)
    code = child.wait()
    while any(pid != os.getpid() for pid in _group_members(os.getpgrp())):
        time.sleep(0.05)
    return code


def run_service(root, name, command):
    root = Path(root).resolve()
    if not re.fullmatch('[a-z][a-z0-9_-]{0,40}', name) or not command:
        raise LifecycleError('Provide a service name and command after --')
    with _locked(root):
        value = _load(root)
        previous = value['services'].get(name)
        if previous:
            if _identity(previous['pid']) is not None:
                raise LifecycleError('Service already has a live or conflicting process record; resolve it before restarting')
            if _group_members(previous['pgid']):
                raise LifecycleError('Orphaned service group remains; preserve the existing record and resolve it before restarting')
        runtime = root / '.runtime'
        if runtime.is_symlink():
            raise LifecycleError('Runtime directory must not be a symbolic link')
        runtime.mkdir(mode=0o700, exist_ok=True)
        command_file = runtime / ('launch-' + uuid.uuid4().hex + '.json')
        _json_write(command_file, command)
        process = subprocess.Popen([sys.executable, str(root / 'scripts/manage.py'), '_supervise', str(command_file)], cwd=root, start_new_session=True)
        ready_file = command_file.with_suffix('.ready.json')
        go_file = command_file.with_suffix('.go.json')
        try:
            deadline = time.monotonic() + 10
            while not ready_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise LifecycleError('Supervisor did not complete its startup handshake')
                time.sleep(0.01)
            if ready_file.is_symlink():
                raise LifecycleError('Refusing a symbolic-link supervisor handshake')
            ready = json.loads(ready_file.read_text())
            identity = _identity(process.pid)
            if (identity is None or identity['pgid'] != process.pid
                    or ready != {'identity': identity, 'token': command_file.name}):
                raise LifecycleError('Supervisor process identity changed during startup')
            value['services'][name] = identity
            _json_write(root / MANIFEST, value)
            # No model/worker child can start until the stable identity is durable.
            _json_write(go_file, {'token': command_file.name})
            ready_file.unlink()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            for pending in (command_file, ready_file, go_file):
                pending.unlink(missing_ok=True)
            raise

    try:
        return process.wait()
    except KeyboardInterrupt:
        if _identity(process.pid) == identity:
            os.killpg(process.pid, signal.SIGTERM)
        return process.wait()


def _docker(root, value):
    record = value.get('docker')
    if not record:
        return {'containers': [], 'networks': [], 'volumes': []}
    project = record['project']
    result = {}
    for kind, command in [('containers', ['ps', '-aq']), ('networks', ['network', 'ls', '-q']), ('volumes', ['volume', 'ls', '-q'])]:
        found = subprocess.run(['docker', *command, '--filter', 'label=com.docker.compose.project=' + project], capture_output=True, text=True, check=True, timeout=10)
        result[kind] = []
        for identifier in found.stdout.split():
            inspect = ['docker', 'inspect', identifier] if kind == 'containers' else ['docker', kind[:-1], 'inspect', identifier]
            details = json.loads(subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=10).stdout)[0]
            labels = details.get('Config', {}).get('Labels', {}) if kind == 'containers' else details.get('Labels', {})
            if labels.get('com.docker.compose.project') != project:
                raise LifecycleError('Docker resource project identity did not match')
            if kind == 'containers' and labels.get('com.docker.compose.project.working_dir') != str(root):
                raise LifecycleError('Docker container belongs to a different installation directory')
            result[kind].append(identifier)
    return result


def compose(root, file, arguments):
    root = Path(root).resolve()
    if file not in ('compose.yaml', 'compose.local.yaml'):
        raise LifecycleError('Use an installation Compose file')
    with _locked(root):
        value = _load(root)
        project = 'apartmint-' + value['installation_id'][:12]
        value['docker'] = {'project': project}
        _json_write(root / MANIFEST, value)
    return subprocess.call(['docker', 'compose', '--project-name', project, '--project-directory', str(root), '-f', str(root / file), *arguments], cwd=root)


def _snapshot(root, value, action, purge_data=False):
    services = []
    conflicts = []
    for name, recorded in sorted(value['services'].items()):
        current = _identity(recorded['pid'])
        state = ('orphaned_group' if _group_members(recorded['pgid']) else 'stopped') if current is None else 'running' if current == recorded and recorded['pgid'] == recorded['pid'] else 'identity_mismatch'
        services.append({'service': name, 'state': state, **recorded})
        if state == 'identity_mismatch':
            conflicts.append('Process identity changed for ' + name)
        elif state == 'orphaned_group':
            conflicts.append('Orphaned process group remains for ' + name + '; ownership cannot be re-established safely, so assets will be retained')
    assets = []
    if action == 'uninstall':
        for relative, recorded in sorted(value['assets'].items()):
            path = root / _relative(root, relative)
            if not path.exists() and not path.is_symlink():
                continue
            if _fingerprint(path) != recorded:
                conflicts.append('Asset was replaced: ' + relative)
                continue
            assets.append({'path': relative, 'identity': recorded})
        if purge_data:
            for relative in PRIVATE_ASSETS:
                path = root / _relative(root, relative)
                if path.exists() or path.is_symlink():
                    assets.append({'path': relative, 'identity': _fingerprint(path)})
    retained = [{'path': key, 'reason': 'shared or external resource', 'target': target} for key, target in value['external'].items()]
    for relative in ['.runtime/metal', '.venv', 'models', *PRIVATE_ASSETS]:
        path = root / relative
        if (path.exists() or path.is_symlink()) and relative not in value['assets'] and not (purge_data and relative in PRIVATE_ASSETS):
            if relative == 'models':
                if not path.is_symlink():
                    for model in path.iterdir():
                        if str(model.relative_to(root)) not in value['assets']:
                            retained.append({'path': str(model.relative_to(root)), 'reason': 'not registered as owned'})
                else:
                    retained.append({'path': relative, 'reason': 'unowned external model directory'})
            else:
                retained.append({'path': relative, 'reason': 'private data retained' if relative in PRIVATE_ASSETS else 'not registered as owned', **({'target': str(path.resolve())} if path.is_symlink() else {})})
    if value.get('docker'):
        retained.append({'path': 'Docker images', 'reason': 'potentially shared images are never removed or pruned'})
    docker_resources = _docker(root, value)
    if not purge_data:
        retained.extend({'path': identifier, 'reason': 'Docker volume retained; use --purge-data to remove private state'} for identifier in docker_resources['volumes'])
    return {'installation_id': value['installation_id'], 'action': action, 'purge_data': purge_data,
            'services': services, 'remove': assets, 'docker': docker_resources, 'conflicts': conflicts, 'retained': retained}


def status(root):
    root = Path(root).resolve()
    with _locked(root):
        return _snapshot(root, _load(root), 'status')


def _plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def _stop_owned(plan, grace_seconds):
    active = [record for record in plan['services'] if record['state'] == 'running']
    for record in active:
        identity = {key: record[key] for key in ('pid', 'pgid', 'started', 'command_sha256')}
        if _identity(record['pid']) != identity:
            raise LifecycleError('Process identity changed before stop; run a new dry run')
        os.killpg(record['pgid'], signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and any(_identity(record['pid']) is not None for record in active):
        time.sleep(0.05)
    for record in active:
        identity = {key: record[key] for key in ('pid', 'pgid', 'started', 'command_sha256')}
        current = _identity(record['pid'])
        if current is not None:
            if current != identity:
                raise LifecycleError('Process identity changed during shutdown; refusing further signals')
            os.killpg(record['pgid'], signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_identity(record['pid']) is not None for record in active):
        time.sleep(0.05)
    if any(_identity(record['pid']) is not None or _group_members(record['pgid']) for record in active):
        raise LifecycleError('Owned process group did not stop; retaining installation assets')
    for identifier in plan['docker']['containers']:
        subprocess.run(['docker', 'stop', '--time', str(max(1, int(grace_seconds))), identifier], capture_output=True, check=True, timeout=grace_seconds + 5)


def action(root, name, *, dry_run=False, yes=False, purge_data=False, grace_seconds=5):
    root = Path(root).resolve()
    if name not in ('stop', 'uninstall') or dry_run == yes:
        raise LifecycleError('Choose --dry-run to review or --yes to execute the matching reviewed plan')
    if purge_data and name != 'uninstall':
        raise LifecycleError('--purge-data is only supported for uninstall')
    with _locked(root):
        value = _load(root)
        plan = _snapshot(root, value, name, purge_data)
        plan_path = root / ('.apartmint-' + name + '-plan.json')
        if dry_run:
            _json_write(plan_path, {'hash': _plan_hash(plan), 'created_at': time.time()})
            return {'dry_run': True, **plan}
        if plan_path.is_symlink():
            raise LifecycleError('Refusing a symbolic-link plan')
        try:
            approved = json.loads(plan_path.read_text())
            valid = approved['hash'] == _plan_hash(plan) and 0 <= time.time() - approved['created_at'] <= 900
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise LifecycleError('A matching dry-run plan from the last 15 minutes is required')
        if plan['conflicts']:
            raise LifecycleError('Ownership conflicts must be resolved before executing this plan')
        _stop_owned(plan, grace_seconds)
        if name == 'uninstall':
            for kind, command in [('containers', ['rm']), ('networks', ['network', 'rm']), ('volumes', ['volume', 'rm'])]:
                if kind == 'volumes' and not purge_data:
                    continue
                for identifier in plan['docker'][kind]:
                    subprocess.run(['docker', *command, identifier], capture_output=True, check=True, timeout=15)
            # Deepest paths first; unlink symlinks themselves, never their targets.
            for entry in sorted(plan['remove'], key=lambda row: len(Path(row['path']).parts), reverse=True):
                path = root / _relative(root, entry['path'])
                if _fingerprint(path) != entry['identity']:
                    raise LifecycleError('Asset changed during uninstall; remaining deletion aborted')
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            value['assets'] = {}
            value['uninstalled'] = True
        value['services'] = {}
        _json_write(root / MANIFEST, value)
        plan_path.unlink(missing_ok=True)
        return {'completed': True, **plan}


def main(argv=None, *, root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    parser = argparse.ArgumentParser(description='Manage only explicitly owned Apartmint installation resources.')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--own-venv', action='store_true')
    commands.add_parser('status')
    commands.add_parser('prepare-caches')
    own = commands.add_parser('own')
    own.add_argument('path')
    external = commands.add_parser('external-runtime')
    external.add_argument('path')
    run = commands.add_parser('run')
    run.add_argument('service')
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    supervisor = commands.add_parser('_supervise', help=argparse.SUPPRESS)
    supervisor.add_argument('command_file')
    docker = commands.add_parser('compose')
    docker.add_argument('--file', choices=['compose.yaml', 'compose.local.yaml'], default='compose.yaml')
    docker.add_argument('arguments', nargs=argparse.REMAINDER)
    for name in ('stop', 'uninstall'):
        item = commands.add_parser(name)
        group = item.add_mutually_exclusive_group(required=True)
        group.add_argument('--dry-run', action='store_true')
        group.add_argument('--yes', action='store_true')
        if name == 'uninstall':
            item.add_argument('--purge-data', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init': result = initialize(root, own_venv=args.own_venv)
        elif args.command == 'status': result = status(root)
        elif args.command == 'prepare-caches': result = prepare_caches(root)
        elif args.command == 'own': result = register_owned(root, args.path)
        elif args.command == 'external-runtime':
            register_external(root, args.path)
            result = {'external_runtime_retained': True}
        elif args.command == 'run': return run_service(root, args.service, args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments)
        elif args.command == '_supervise': return supervise(args.command_file)
        elif args.command == 'compose': return compose(root, args.file, args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments)
        else: result = action(root, args.command, dry_run=args.dry_run, yes=args.yes, purge_data=getattr(args, 'purge_data', False))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (LifecycleError, OSError, subprocess.SubprocessError, ValueError):
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, LifecycleError) else 'Lifecycle operation failed; no private command or environment was logged'
        print(json.dumps({'error': message}), file=sys.stderr)
        return 1

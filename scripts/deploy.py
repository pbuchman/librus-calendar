#!/usr/bin/env python3
"""Prepare packaged, SHA-addressed releases before activating Linux user services.

No calendar or database write, backup, credential read, or Tailscale change occurs here.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Operator-side configuration validation only; runtime uses -I/wheel.
UNITS = ('librus-web.service', 'librus-sync.service', 'librus-sync.timer',
         'librus-monitor.service', 'librus-monitor.timer')
BACKUP_UNITS = ('librus-backup.service', 'librus-backup.timer')
MARKER = '# Managed by librus-calendar deployment v1\n'
RESOURCES = ('templates/inbox.html', 'static/style.css', 'schemas/analysis.json',
             'schemas/tool.json', 'schemas/analysis.md', 'schemas/executor.md',
             'schemas/source_analysis.json', 'schemas/source_match.json',
             'schemas/source_facts.md', 'schemas/source_match.md',
             'schemas/source_verify.json', 'schemas/source_verify.md',
             'schemas/source_recovery.json', 'schemas/source_recovery.md',
             'templates/sources.html', 'templates/source_preview.html')


class DeploymentError(RuntimeError):
    """Diagnostics contain only technical states, never command output/secrets."""


def command(args, *, cwd=None, env=None, capture=False):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            capture_output=True, text=True, umask=0o077)
    if result.returncode:
        raise DeploymentError('Deployment command failed: ' + Path(str(args[0])).name)
    if capture:
        return result.stdout.strip()


def source_sha(root, sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise DeploymentError('An exact full lowercase source SHA is required.')
    head = command(['git', '-C', root, 'rev-parse', 'HEAD'], capture=True)
    if head != sha:
        raise DeploymentError('Requested SHA must equal the checkout HEAD.')
    if command(['git', '-C', root, 'status', '--porcelain', '--untracked-files=all'], capture=True):
        raise DeploymentError('Source checkout must be clean, including untracked files.')
    command(['git', '-C', root, 'merge-base', '--is-ancestor', sha, 'refs/remotes/origin/main'])
    return sha


def safe_path(path, *, directory=True):
    """Check all existing parents before writes; symlinks including dangling ones fail."""
    path = Path(path).absolute()
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise DeploymentError('Unsafe symlink in installation path.')
        if item.exists() and item.is_dir():
            parent_info = item.stat()
            if parent_info.st_uid not in {0, os.getuid()} or (parent_info.st_mode & 0o022 and not parent_info.st_mode & stat.S_ISVTX):
                raise DeploymentError('Unsafe installation parent ownership or permissions.')
        if item.exists() and not item.is_dir() and (item != path or directory):
            raise DeploymentError('Unsafe installation directory.')
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or (directory and info.st_mode & 0o022):
            raise DeploymentError('Installation target ownership or permissions are unsafe.')
        if not directory and not stat.S_ISREG(info.st_mode):
            raise DeploymentError('Installation target must be a regular file.')


def release_base(home):
    return Path(home) / '.local/lib/librus-calendar'


def private_mkdir(path):
    """Explicit mode on every new owned parent, independent of the caller's umask."""
    path = Path(path)
    safe_path(path)
    for directory in reversed((path, *path.parents)):
        if not directory.exists():
            directory.mkdir(mode=0o700)


def release_path(home, sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise DeploymentError('An exact full lowercase release SHA is required.')
    return release_base(home) / 'releases' / sha


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def offline_env(home):
    return {**{k: v for k, v in os.environ.items() if not k.startswith('LIBRUS_')}, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '',
            'LIBRUS_CONFIG_FILE': '', 'HOME': str(home), 'LIBRUS_BACKUP_ENABLED': '0'}


def package_probe(release, *, resources=RESOURCES, app_directory=None):
    python = release / '.venv/bin/python'
    app_check = f'assert p == Path({str(app_directory)!r}); ' if app_directory else ''
    script = ('from pathlib import Path; import app; '
              'p=Path(app.__file__).resolve().parent; '
              f'assert p.is_relative_to(Path({str(release)!r})); '
              + app_check +
              f'assert all((p/n).is_file() for n in {resources!r}); '
              'import app.web, app.codex_runtime, app.monitoring; print("package-ok")')
    with tempfile.TemporaryDirectory(prefix='librus-package-probe-') as folder:
        command([python, '-I', '-c', script], cwd=folder)


def relative_artifact(name):
    """Manifest and wheel names must be unambiguous relative POSIX file paths."""
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError()
    path = Path(name)
    if not path.parts or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        raise ValueError()
    return path


def package_contract(release, manifest):
    """Bind this release's installed app/resources to its hash-verified wheel.

    Legacy manifests already record the wheel and every installed application file,
    so their resource inventory can be recovered without consulting the new source.
    """
    artifacts = manifest['artifacts']
    wheels = [name for name in artifacts
              if re.fullmatch(r'artifacts/librus_calendar-[^/]+\.whl', name)]
    roots = [str(Path(name).parent) for name in artifacts
             if re.fullmatch(r'\.venv/lib/python\d+\.\d+/site-packages/app/__init__\.py', name)]
    if len(wheels) != 1 or len(roots) != 1:
        raise ValueError()
    app_prefix = roots[0] + '/'
    installed = {name.removeprefix(app_prefix): expected for name, expected in artifacts.items()
                 if name.startswith(app_prefix)}
    packaged = {}
    seen = set()
    with zipfile.ZipFile(release / wheels[0]) as wheel:
        for member in wheel.infolist():
            name = member.filename.removesuffix('/') if member.is_dir() else member.filename
            relative_artifact(name)
            if name in seen or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                raise ValueError()
            seen.add(name)
            if not member.is_dir() and name.startswith('app/'):
                packaged[name.removeprefix('app/')] = hashlib.sha256(wheel.read(member)).hexdigest()
    if '__init__.py' not in packaged or packaged != installed:
        raise ValueError()
    resources = sorted(name for name in packaged
                       if name.split('/')[0] in {'templates', 'static', 'schemas'})
    if not resources:
        raise ValueError()
    if 'resources' in manifest:
        declared = manifest['resources']
        if not isinstance(declared, list):
            raise ValueError()
        for name in declared:
            relative_artifact(name)
        if sorted(declared) != resources:
            raise ValueError()
    return release / roots[0], tuple(resources)


def validate_release(home, sha):
    release = release_path(home, sha)
    safe_path(release)
    safe_path(release / 'release.json', directory=False)
    try:
        manifest = json.loads((release / 'release.json').read_text())
        if (not isinstance(manifest, dict) or manifest['sha'] != sha
                or manifest['checks'] != ['offline_tests', 'source_privacy', 'package_probe']
                or not isinstance(manifest['artifacts'], dict) or not manifest['artifacts']):
            raise ValueError()
        for name, expected in manifest['artifacts'].items():
            path = release / relative_artifact(name)
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError()
            safe_path(path, directory=False)
            if digest(path) != expected:
                raise ValueError()
        app_directory, resources = package_contract(release, manifest)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, NotImplementedError):
        raise DeploymentError('Release manifest or artifact verification failed.') from None
    package_probe(release, resources=resources, app_directory=app_directory)
    return release


def prepare(root, home, sha, python=sys.executable):
    source_sha(root, sha)
    destination = release_path(home, sha)
    safe_path(destination)
    if destination.exists():
        validate_release(home, sha)
        package_probe(destination)  # Reuse still enforces this source's required resources.
        return destination
    # Privacy history is checked from the source repository, not from an unversioned archive.
    command([python, root / 'scripts/check_public_tree.py', '--working-tree', '--all-history'], cwd=root)
    archive = subprocess.run(['git', '-C', str(root), 'archive', sha], capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix='librus-build-') as folder:
        workspace = Path(folder)
        source = workspace / 'source'
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter='data')
        private_home = workspace / 'test-home'
        private_home.mkdir(mode=0o700)
        command([python, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=source,
                env=offline_env(private_home))
        wheels = workspace / 'wheels'
        command([python, '-m', 'pip', 'wheel', '--disable-pip-version-check', '--no-deps',
                 '--wheel-dir', wheels, source], cwd=workspace,
                env={**os.environ, 'SOURCE_DATE_EPOCH': command(
                    ['git', '-C', root, 'show', '-s', '--format=%ct', sha], capture=True)})
        wheel = next(wheels.glob('librus_calendar-*.whl'))
        private_mkdir(destination)
        try:
            command([python, '-m', 'venv', destination / '.venv'])
            command([destination / '.venv/bin/python', '-m', 'pip', 'install',
                     '--disable-pip-version-check', '--constraint', source / 'requirements.lock', wheel], cwd=workspace)
            shutil.copytree(source / 'deploy', destination / 'deploy')
            (destination / 'artifacts').mkdir(mode=0o700)
            shutil.copy2(wheel, destination / 'artifacts' / wheel.name)
            shutil.copy2(source / 'requirements.lock', destination / 'artifacts/requirements.lock')
            package_probe(destination)
            artifacts = {p.relative_to(destination).as_posix(): digest(p)
                         for folder in ('deploy', 'artifacts') for p in (destination / folder).rglob('*') if p.is_file()}
            # Bind the installed application bytes to the release manifest as well as the wheel.
            for site in (destination / '.venv/lib').glob('python*/site-packages'):
                for package in (site / 'app', *site.glob('librus_calendar-*.dist-info')):
                    for path in package.rglob('*'):
                        if path.is_file() and '__pycache__' not in path.parts:
                            artifacts[path.relative_to(destination).as_posix()] = digest(path)
            manifest = {'sha': sha, 'checks': ['offline_tests', 'source_privacy', 'package_probe'],
                        'artifacts': artifacts, 'backup_policy': 'disabled'}
            _, resources = package_contract(destination, manifest)
            manifest['resources'] = list(resources)
            (destination / 'release.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
        except BaseException:
            shutil.rmtree(destination)
            raise
    source_sha(root, sha)
    return validate_release(home, sha)


def current_release(home):
    current = release_base(home) / 'current'
    if not current.is_symlink():
        if current.exists():
            raise DeploymentError('Current release must be an owned deployment symlink.')
        return None
    target = current.resolve(strict=True)
    if target.parent != release_base(home) / 'releases':
        raise DeploymentError('Current release points outside the release directory.')
    return validate_release(home, target.name)


def legacy_contents(release, unit):
    old = (release / 'deploy/legacy' / unit).read_text()
    variants = {old, old.replace('%h/librus-calendar', '%h/librus-poc')}
    if unit == 'librus-monitor.service':
        # The known original installed monitor predates EnvironmentFile support.
        for text in list(variants):
            text = text.replace('EnvironmentFile=%h/.config/librus-calendar/monitor.env\n', '')
            variants.add(text)
            variants.add(text.replace(' -%h/.codex', ' -%h/.config/librus-poc -%h/.codex'))
    return variants


def preflight_units(home, release, migrate=False):
    unit_dir = Path(home) / '.config/systemd/user'
    config_dir = Path(home) / '.config/librus-calendar'
    safe_path(unit_dir)
    safe_path(config_dir)
    previous = current_release(home)
    for name in UNITS + BACKUP_UNITS:
        dropins = unit_dir / (name + '.d')
        safe_path(dropins)
        if dropins.exists() and any(dropins.iterdir()):
            raise DeploymentError('Refusing existing service drop-ins: ' + name)
        target = unit_dir / name
        safe_path(target, directory=False)
        if not target.exists():
            continue
        text = target.read_text()
        accepted = set()
        if name in UNITS:
            accepted.add((release / 'deploy' / name).read_text())
            if previous:
                accepted.add((previous / 'deploy' / name).read_text())
        if migrate:
            accepted.update(legacy_contents(release, name))
        if text not in accepted:
            raise DeploymentError('Refusing to overwrite a different existing unit: ' + name)
    for name in ('web.env', 'monitor.env', 'config.json', 'credentials.json',
                 'config.json.example', 'monitor.env.example'):
        target = config_dir / name
        safe_path(target, directory=False)
        if target.exists() and target.stat().st_mode & 0o077:
            raise DeploymentError('Private configuration target permissions must be 0600.')
    return unit_dir, config_dir


def install_units(home, release, *, migrate=False, activation=False):
    unit_dir, config_dir = preflight_units(home, release, migrate)
    if migrate and not activation:
        raise DeploymentError('Legacy migration is allowed only during explicit activation.')
    private_mkdir(unit_dir)
    private_mkdir(config_dir)
    config_dir.chmod(0o700)
    for unit in UNITS:
        target = unit_dir / unit
        shutil.copyfile(release / 'deploy' / unit, target)
        target.chmod(0o600)
    for name in ('web.env', 'monitor.env', 'config.json.example', 'monitor.env.example'):
        target = config_dir / name
        if not target.exists():
            source_name = name + '.example' if name in ('web.env', 'monitor.env') else name
            shutil.copyfile(release / 'deploy' / source_name, target)
            target.chmod(0o600)
    # Existing private configuration remains byte-for-byte untouched.
    if migrate:
        for name in BACKUP_UNITS:
            (unit_dir / name).unlink(missing_ok=True)
    return unit_dir


def service_state(unit):
    output = command(['systemctl', '--user', 'show', unit,
                      '--property=LoadState,ActiveState,SubState,FragmentPath,DropInPaths'], capture=True)
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def preflight_effective_units(home):
    for unit in UNITS + BACKUP_UNITS:
        state = service_state(unit)
        if state.get('LoadState') == 'not-found':
            continue
        if (state.get('LoadState') != 'loaded' or state.get('DropInPaths')
                or state.get('FragmentPath') != str(Path(home) / '.config/systemd/user' / unit)):
            raise DeploymentError('Effective systemd unit differs from the owned installation: ' + unit)


def env_file(path):
    values = {}
    try:
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            if not re.fullmatch(r'LIBRUS_[A-Z_]+', key) or key in values or '$' in value:
                raise ValueError()
            tokens = shlex.split(value, comments=False)
            values[key] = tokens[0] if tokens else ''
            if len(tokens) > 1:
                raise ValueError()
    except (OSError, ValueError):
        raise DeploymentError('Service environment file is missing or malformed.') from None
    return values


def preflight_runtime_config(home):
    # Import only configuration validation, never a store or production credentials.
    from app.config import ConfigurationError, load_config
    folder = Path(home) / '.config/librus-calendar'
    try:
        web = load_config(folder / 'config.json', environ=env_file(folder / 'web.env'))
        web.require_web()
        web.require_calendar()
        monitor_values = env_file(folder / 'monitor.env')
        if monitor_values.get('LIBRUS_CONFIG_FILE') != '' or monitor_values.get('LIBRUS_BACKUP_ENABLED') != '0':
            raise DeploymentError('Monitor must explicitly disable private config discovery and backup policy.')
        monitor = load_config(environ=monitor_values)
        monitor.require_web()
        if (monitor.state_path.resolve() != web.state_path.resolve()
                or monitor.allowed_hosts != web.allowed_hosts
                or monitor.tailscale_owner != web.tailscale_owner
                or monitor.monitor_output != web.monitor_output or web.backup_enabled):
            raise DeploymentError('Web and monitor must use matching state and proxy configuration with backups disabled.')
    except ConfigurationError:
        raise DeploymentError('Private runtime configuration is incomplete or invalid.') from None
    return web


def stop_if_loaded(unit):
    if service_state(unit).get('LoadState') == 'loaded':
        command(['systemctl', '--user', 'stop', unit])


def activate(home, sha, *, migrate=False, no_sync=False):
    release = validate_release(home, sha)
    package_probe(release)  # The target must also satisfy this source checkout's contract.
    preflight_units(home, release, migrate)
    preflight_runtime_config(home)
    preflight_effective_units(home)
    base = release_base(home)
    temporary = base / '.current-next'
    if temporary.exists() or temporary.is_symlink():
        raise DeploymentError('Stale current-symlink staging target; inspect before activation.')
    # Build, import and all owned-target checks happen before the first stop.
    # Prevent new runs, then refuse activation while an existing writer is alive.
    # Never kill a worker that may be committing an external calendar write.
    stop_if_loaded('librus-sync.timer')
    worker = service_state('librus-sync.service')
    if worker.get('ActiveState') in {'active', 'activating', 'deactivating', 'reloading'}:
        raise DeploymentError('Sync timer stopped; worker still running. Retry activation after it finishes.')
    if migrate:
        existing_backups = [unit for unit in BACKUP_UNITS
                            if (Path(home) / '.config/systemd/user' / unit).exists()]
        if existing_backups:
            command(['systemctl', '--user', 'disable', '--now', *existing_backups])
    for unit in ('librus-monitor.timer', 'librus-monitor.service', 'librus-web.service'):
        stop_if_loaded(unit)
    install_units(home, release, migrate=migrate, activation=True)
    temporary.symlink_to(Path('releases') / sha)
    os.replace(temporary, base / 'current')
    command(['systemctl', '--user', 'daemon-reload'])
    start_units = ['librus-web.service', 'librus-monitor.timer']
    if no_sync:
        command(['systemctl', '--user', 'disable', 'librus-sync.timer'])
    else:
        start_units.append('librus-sync.timer')
    command(['systemctl', '--user', 'enable', '--now', *start_units])
    return release


def verify(home, *, no_sync=False):
    release = current_release(home)
    if release is None:
        raise DeploymentError('No current release installed.')
    preflight_units(home, release)
    preflight_runtime_config(home)
    preflight_effective_units(home)
    units = ['librus-web.service', 'librus-monitor.timer']
    if not no_sync:
        units.append('librus-sync.timer')
    for unit in units:
        command(['systemctl', '--user', 'is-active', '--quiet', unit])
        command(['systemctl', '--user', 'is-enabled', '--quiet', unit])
    command([release / '.venv/bin/python', '-I', '-c',
             'from app.monitoring import collect_snapshot; '
             's=collect_snapshot(); assert s["db_ok"] and s["web_ok"] ' +
             ('' if no_sync else 'and s["sync_timer_ok"] ') +
             'and s["sync_unit_ok"] and not s["backup_enabled"]'], cwd=release)
    command([release / '.venv/bin/python', '-I', '-c',
             'import json; from datetime import datetime,timezone; '
             'from app.config import load_config; from app.monitoring import sanitize_state; '
             'c=load_config(); p=c.monitor_output; assert not p.is_symlink(); '
             's=json.loads(p.read_text()); now=datetime.now(timezone.utc); '
             'assert type(s["schema_version"]) is int and s["schema_version"] == 1; '
             'assert s["db_ok"] and s["web_ok"] and not s["backup_enabled"]; '
             'assert 0 <= (now-datetime.fromisoformat(s["generated_at"])).total_seconds() <= 180; '
             # Exported state omits the raw DB schema field; the envelope owns it.
             # Revalidate every aggregate and require the exact closed output shape.
             'assert isinstance(s["state"],dict); '
             'assert sanitize_state(dict(s["state"],schema_version=s["schema_version"]),now) == s["state"]'], cwd=release)
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('prepare', 'activate', 'install-units'):
        child = sub.add_parser(name)
        child.add_argument('--sha', required=True)
        if name == 'activate':
            child.add_argument('--migrate-legacy', action='store_true')
            child.add_argument('--no-sync', action='store_true')
    sub.add_parser('verify').add_argument('--no-sync', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    home = Path.home()
    try:
        if args.action == 'prepare':
            release = prepare(args.source.resolve(), home, args.sha)
        elif args.action == 'activate':
            source_sha(args.source.resolve(), args.sha)
            release = activate(home, args.sha, migrate=args.migrate_legacy, no_sync=args.no_sync)
        elif args.action == 'install-units':
            release = validate_release(home, args.sha)
            install_units(home, release)
            command(['systemctl', '--user', 'daemon-reload'])
        else:
            release = verify(home, no_sync=args.no_sync)
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        # Never print raw stderr, command arguments, paths or config values.
        print('Deployment failed; inspect owned paths and local command availability.', file=sys.stderr)
        return 1
    print(args.action + ' complete: ' + release.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

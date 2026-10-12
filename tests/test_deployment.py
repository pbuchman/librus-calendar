"""Deployment uses synthetic homes/repos, packaged artifacts and fake systemd only."""
from pathlib import Path
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import deploy

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.release = deploy.release_path(self.home, SHA)
        self.release.mkdir(parents=True)
        shutil.copytree(ROOT / 'deploy', self.release / 'deploy')
        self.units = self.home / '.config/systemd/user'
        self.config = self.home / '.config/librus-calendar'

    def test_repeat_unit_install_preserves_secrets_and_never_starts_or_backs_up(self):
        self.config.mkdir(parents=True, mode=0o700)
        private = self.config / 'web.env'
        private.write_text('SYNTHETIC=retained-value\n')
        private.chmod(0o600)
        with patch.object(deploy, 'command') as command:
            deploy.install_units(self.home, self.release)
            deploy.install_units(self.home, self.release)
        command.assert_not_called()
        self.assertEqual(private.read_text(), 'SYNTHETIC=retained-value\n')
        self.assertEqual(set(p.name for p in self.units.iterdir()), set(deploy.UNITS))
        self.assertFalse(list(self.home.rglob('*.sqlite3')))
        for name in deploy.UNITS:
            self.assertEqual((self.units / name).stat().st_mode & 0o777, 0o600)

    def test_private_installation_parents_are_secure_under_group_writable_umask(self):
        previous = os.umask(0o002)
        try:
            deploy.install_units(self.home, self.release)
            path = self.home / 'fresh/.local/lib/librus-calendar/releases'
            deploy.private_mkdir(path)
        finally:
            os.umask(previous)
        for parent in (self.home / '.config', self.home / '.config/systemd', self.units,
                       self.config, self.home / 'fresh/.local', path.parent, path):
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)

    def test_all_service_interpreters_ignore_inherited_pythonpath(self):
        injected = self.home / 'injected-checkout'
        injected.mkdir()
        for name in ('librus-web.service', 'librus-sync.service', 'librus-monitor.service'):
            text = (ROOT / 'deploy' / name).read_text()
            start = next(line.removeprefix('ExecStart=') for line in text.splitlines()
                         if line.startswith('ExecStart='))
            args = shlex.split(start)
            module_index = args.index('-m')
            # Exercise the exact interpreter options deployed by each service.
            probe = subprocess.run([sys.executable, *args[1:module_index], '-c',
                'import sys; assert sys.flags.isolated == 1; '
                f'assert {str(injected)!r} not in sys.path'], cwd=injected,
                env={**os.environ, 'PYTHONPATH': str(injected)}, text=True, capture_output=True)
            self.assertEqual(probe.returncode, 0, name + ': ' + probe.stderr)

    def test_preflight_all_units_and_config_before_any_install_write(self):
        for kind in ('unit', 'config', 'directory'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                units = home / '.config/systemd/user'
                units.mkdir(parents=True)
                if kind == 'unit':
                    (units / 'librus-monitor.timer').write_text('Unrelated admin unit')
                elif kind == 'config':
                    config = home / '.config/librus-calendar'
                    config.mkdir()
                    (config / 'credentials.json').symlink_to(home / 'missing')
                else:
                    shutil.rmtree(units)
                    units.symlink_to(home / 'missing')
                with self.assertRaises(deploy.DeploymentError):
                    deploy.install_units(home, self.release)
                self.assertFalse((units / 'librus-web.service').exists())

    def test_legacy_replacement_requires_explicit_activation_and_exact_known_units(self):
        self.units.mkdir(parents=True)
        for name in deploy.UNITS + deploy.BACKUP_UNITS:
            text = (self.release / 'deploy/legacy' / name).read_text().replace('%h/librus-calendar', '%h/librus-poc')
            (self.units / name).write_text(text)
        with self.assertRaises(deploy.DeploymentError):
            deploy.install_units(self.home, self.release)
        with self.assertRaises(deploy.DeploymentError):
            deploy.install_units(self.home, self.release, migrate=True)
        deploy.install_units(self.home, self.release, migrate=True, activation=True)
        for name in deploy.BACKUP_UNITS:
            self.assertFalse((self.units / name).exists())
        target = self.units / 'librus-sync.service'
        target.write_text(target.read_text() + 'ExecStartPost=/bin/false\n')
        with self.assertRaises(deploy.DeploymentError):
            deploy.preflight_units(self.home, self.release, migrate=True)

    def test_activation_prepares_all_checks_before_stop_and_sets_packaged_current(self):
        calls = []
        with patch.object(deploy, 'validate_release', return_value=self.release) as validation, \
             patch.object(deploy, 'preflight_effective_units'), \
             patch.object(deploy, 'preflight_runtime_config'), \
             patch.object(deploy, 'service_state', return_value={'LoadState': 'loaded', 'ActiveState': 'inactive'}), \
             patch.object(deploy, 'command', side_effect=lambda args, **kw: calls.append(args)):
            deploy.activate(self.home, SHA)
        self.assertTrue(validation.called)
        self.assertEqual(calls[0][2], 'stop')
        self.assertEqual((deploy.release_base(self.home) / 'current').resolve(), self.release)
        self.assertEqual(calls[-1][2:4], ['enable', '--now'])
        self.assertNotIn('librus-backup.timer', str(calls))

    def test_running_sync_worker_prevents_switch_and_is_never_killed(self):
        calls = []
        def state(unit):
            return {'LoadState': 'loaded', 'ActiveState': 'activating' if unit.endswith('.service') else 'active'}
        with patch.object(deploy, 'validate_release', return_value=self.release), \
             patch.object(deploy, 'preflight_effective_units'), \
             patch.object(deploy, 'preflight_runtime_config'), \
             patch.object(deploy, 'service_state', side_effect=state), \
             patch.object(deploy, 'command', side_effect=lambda args, **kw: calls.append(args)):
            with self.assertRaisesRegex(deploy.DeploymentError, 'worker still running'):
                deploy.activate(self.home, SHA)
        self.assertEqual(calls, [['systemctl', '--user', 'stop', 'librus-sync.timer']])
        self.assertFalse((deploy.release_base(self.home) / 'current').exists())
        self.assertFalse(self.units.exists())

    def test_fresh_activation_handles_missing_units_without_stop_errors(self):
        calls = []
        with patch.object(deploy, 'validate_release', return_value=self.release), \
             patch.object(deploy, 'preflight_runtime_config'), \
             patch.object(deploy, 'service_state', return_value={'LoadState': 'not-found'}), \
             patch.object(deploy, 'command', side_effect=lambda args, **kw: calls.append(args)):
            deploy.activate(self.home, SHA, no_sync=True)
        self.assertFalse(any('stop' in call for call in calls))
        self.assertEqual(calls[-1], ['systemctl', '--user', 'enable', '--now',
                                    'librus-web.service', 'librus-monitor.timer'])

    def test_failed_preflight_never_stops_live_services(self):
        self.units.mkdir(parents=True)
        (self.units / 'librus-web.service').write_text('unrelated')
        with patch.object(deploy, 'validate_release', return_value=self.release), \
             patch.object(deploy, 'command') as command:
            with self.assertRaises(deploy.DeploymentError):
                deploy.activate(self.home, SHA)
        command.assert_not_called()

    def test_dropin_override_refused_before_service_commands(self):
        dropins = self.units / 'librus-web.service.d'
        dropins.mkdir(parents=True)
        (dropins / 'override.conf').write_text('[Service]\nExecStart=/bin/false\n')
        with patch.object(deploy, 'validate_release', return_value=self.release), \
             patch.object(deploy, 'command') as command:
            with self.assertRaisesRegex(deploy.DeploymentError, 'drop-ins'):
                deploy.activate(self.home, SHA)
        command.assert_not_called()

    def test_systemd_effective_override_or_foreign_unit_refused(self):
        base = {'LoadState': 'loaded', 'FragmentPath': str(self.units / 'librus-web.service')}
        for changes in ({'DropInPaths': '/etc/systemd/user/override.conf'},
                        {'FragmentPath': '/etc/systemd/user/librus-web.service'}):
            with patch.object(deploy, 'service_state', return_value={**base, **changes}), \
                 self.assertRaises(deploy.DeploymentError):
                deploy.preflight_effective_units(self.home)

    def test_monitor_must_follow_configured_private_state_and_policy(self):
        self.config.mkdir(parents=True, mode=0o700)
        config = json.loads((ROOT / 'deploy/config.json.example').read_text())
        config['state_path'] = str(self.home / 'new-state.sqlite3')
        path = self.config / 'config.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        shutil.copyfile(ROOT / 'deploy/web.env.example', self.config / 'web.env')
        monitor = self.config / 'monitor.env'
        monitor.write_text((ROOT / 'deploy/monitor.env.example').read_text())
        with self.assertRaisesRegex(deploy.DeploymentError, 'matching state'):
            deploy.preflight_runtime_config(self.home)
        monitor.write_text(monitor.read_text() + 'LIBRUS_STATE_PATH=' + config['state_path'] + '\n')
        self.assertEqual(deploy.preflight_runtime_config(self.home).state_path, Path(config['state_path']))
        monitor.write_text(monitor.read_text().replace('LIBRUS_BACKUP_ENABLED=0', 'LIBRUS_BACKUP_ENABLED=1'))
        with self.assertRaisesRegex(deploy.DeploymentError, 'backup policy'):
            deploy.preflight_runtime_config(self.home)

    def test_source_requires_clean_exact_sha_reachable_origin_main(self):
        repo = self.home / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True,
                text=True, capture_output=True).stdout.strip()
        git('init', '-b', 'main')
        git('config', 'user.name', 'Synthetic Test')
        git('config', 'user.email', 'synthetic@example.test')
        (repo / 'tracked.txt').write_text('first')
        git('add', '.')
        git('commit', '-m', 'Synthetic source')
        sha = git('rev-parse', 'HEAD')
        with self.assertRaises(deploy.DeploymentError):
            deploy.source_sha(repo, sha)
        git('update-ref', 'refs/remotes/origin/main', sha)
        self.assertEqual(deploy.source_sha(repo, sha), sha)
        for invalid in ('main', sha[:12], 'b' * 40):
            with self.assertRaises(deploy.DeploymentError):
                deploy.source_sha(repo, invalid)
        (repo / 'untracked.txt').write_text('changes')
        with self.assertRaises(deploy.DeploymentError):
            deploy.source_sha(repo, sha)
        (repo / 'untracked.txt').unlink()
        (repo / 'tracked.txt').write_text('dirty')
        with self.assertRaises(deploy.DeploymentError):
            deploy.source_sha(repo, sha)

    def test_prepare_checks_source_before_writes_or_service_calls(self):
        with patch.object(deploy, 'source_sha', side_effect=deploy.DeploymentError('dirty')), \
             patch.object(deploy, 'command') as command:
            with self.assertRaises(deploy.DeploymentError):
                deploy.prepare(ROOT, self.home, SHA)
        command.assert_not_called()

    def test_package_wheel_import_and_resources_outside_source_checkout(self):
        # Build/install without a network or production HOME. Wheel metadata itself declares pins.
        source = self.home / 'source'
        source.mkdir()
        shutil.copytree(ROOT / 'app', source / 'app', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'pyproject.toml', source / 'pyproject.toml')
        wheels = self.home / 'wheels'
        built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-build-isolation',
            '--no-deps', '--wheel-dir', str(wheels), str(source)], cwd=self.home,
            text=True, capture_output=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        target = self.home / 'installed'
        installed = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps',
            '--target', str(target), str(next(wheels.glob('*.whl')))], cwd=self.home,
            text=True, capture_output=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        shutil.rmtree(source)
        code = ('import sys; from pathlib import Path; '
                f'sys.path.insert(0,{str(target)!r}); import app; '
                'p=Path(app.__file__).parent; '
                f'assert p.is_relative_to(Path({str(target)!r})); '
                f'assert all((p/n).is_file() for n in {deploy.RESOURCES!r})')
        probe = subprocess.run([sys.executable, '-I', '-c', code], cwd=self.home,
                               text=True, capture_output=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)


if __name__ == '__main__':
    unittest.main()

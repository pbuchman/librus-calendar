"""Run installer preflights in synthetic filesystems with all service/copy tools fake."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def local_setup(self, folder):
        # Rewrite only fixed paths in a disposable copy; never change the user's HOME.
        sandbox = Path(folder).resolve()
        clone = sandbox / 'librus-calendar'
        shutil.copytree(ROOT / 'deploy', clone / 'deploy')
        (clone / 'scripts').mkdir()
        script = clone / 'scripts/install_linux.sh'
        log = sandbox / 'calls.txt'
        text = (ROOT / 'scripts/install_linux.sh').read_text().replace('$HOME', str(sandbox))
        text = text.replace('umask 077\n', 'umask 077\n' +
            f'install() {{ echo install >> "{log}"; }}\n'
            f'systemctl() {{ echo systemctl >> "{log}"; }}\n')
        script.write_text(text)
        python = clone / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\necho python >> "{log}"\n')
        python.chmod(0o755)
        return sandbox, clone, script, python, log

    def run_local(self, script, python):
        return subprocess.run(['bash', str(script)], text=True, capture_output=True,
            env={**os.environ, 'LIBRUS_PYTHON': str(python)}, timeout=10)

    def test_user_installer_rejects_symlinked_directory_before_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            sandbox, clone, script, python, log = self.local_setup(folder)
            outside = sandbox / 'untouched'
            outside.mkdir()
            (sandbox / '.config').symlink_to(outside, target_is_directory=True)
            result = self.run_local(script, python)
            self.assertEqual(result.returncode, 1)
            self.assertIn('unsafe installation directory', result.stderr)
            self.assertFalse(log.exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_user_installer_rejects_dangling_config_target_before_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            sandbox, clone, script, python, log = self.local_setup(folder)
            config = sandbox / '.config/librus-calendar'
            config.mkdir(parents=True)
            (config / 'web.env').symlink_to(sandbox / 'missing-outside')
            result = self.run_local(script, python)
            self.assertEqual(result.returncode, 1)
            self.assertIn('unsafe configuration target', result.stderr)
            self.assertFalse(log.exists())

    def test_user_installer_rejects_unit_symlink_or_different_unit_before_tools(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as folder:
                sandbox, clone, script, python, log = self.local_setup(folder)
                units = sandbox / '.config/systemd/user'
                units.mkdir(parents=True)
                target = units / 'librus-sync.service'
                if symlink:
                    target.symlink_to(clone / 'deploy/librus-sync.service')
                else:
                    target.write_text('Synthetic unrelated existing unit')
                result = self.run_local(script, python)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(log.exists())

    def monitor_setup(self, folder):
        sandbox = Path(folder).resolve()
        deploy = sandbox / 'deploy'
        shutil.copytree(ROOT / 'deploy', deploy)
        log = sandbox / 'calls.txt'
        script = deploy / 'install-librus-monitor.sh'
        text = script.read_text()
        # Every absolute destination is confined to this synthetic Linux root.
        for prefix in ('/var/lib', '/etc/netdata', '/usr/libexec'):
            text = text.replace(prefix, str(sandbox) + prefix)
        text = text.replace('set -eu\n', 'set -eu\n' +
            'id() { [ "$1" != -u ] || echo 0; }\ngetent() { return 0; }\n' +
            f'install() {{ echo install >> "{log}"; }}\n')
        script.write_text(text)
        for directory in ('var/lib', 'etc/netdata/python.d', 'etc/netdata/health.d', 'usr/libexec/netdata/python.d'):
            (sandbox / directory).mkdir(parents=True, exist_ok=True)
        return sandbox, deploy, script, log

    def test_monitor_installer_preflights_all_targets_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            sandbox, deploy, script, log = self.monitor_setup(folder)
            conflicting = sandbox / 'etc/netdata/health.d/librus_local.conf'
            conflicting.write_text('Synthetic existing administrator settings')
            result = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Refusing to overwrite different', result.stderr)
            self.assertFalse(log.exists())
            self.assertEqual(conflicting.read_text(), 'Synthetic existing administrator settings')

    def test_monitor_installer_rejects_same_content_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            sandbox, deploy, script, log = self.monitor_setup(folder)
            target = sandbox / 'usr/libexec/netdata/python.d/librus.chart.py'
            target.symlink_to(deploy / 'netdata/librus.chart.py')
            result = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Unsafe Netdata installation target', result.stderr)
            self.assertFalse(log.exists())

    def test_monitor_installer_accepts_identical_regular_files(self):
        with tempfile.TemporaryDirectory() as folder:
            sandbox, deploy, script, log = self.monitor_setup(folder)
            for source, target in (
                ('librus.chart.py', 'usr/libexec/netdata/python.d/librus.chart.py'),
                ('librus.conf', 'etc/netdata/python.d/librus.conf'),
                ('librus_local-health.conf', 'etc/netdata/health.d/librus_local.conf')):
                shutil.copyfile(deploy / 'netdata' / source, sandbox / target)
            result = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().count('install'), 4)


if __name__ == '__main__':
    unittest.main()

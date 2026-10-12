"""Run installer preflights in synthetic filesystems with all service/copy tools fake."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
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
            self.assertEqual(log.read_text().count('install'), 5)

    def test_monitor_installer_explicit_known_legacy_migration(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            sandbox, deploy, script, log = self.monitor_setup(folder)
            old = b'Synthetic known prior collector\n'
            target = sandbox / 'usr/libexec/netdata/python.d/librus.chart.py'
            target.write_bytes(old)
            registry = deploy / 'netdata/legacy.sha256'
            registry.write_text(hashlib.sha256(old).hexdigest() + '  librus.chart.py\n')
            rejected = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(rejected.returncode, 1)
            self.assertFalse(log.exists())
            accepted = subprocess.run(['sh', str(script), 'synthetic-user', '--migrate-legacy'], text=True, capture_output=True, timeout=10)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(log.read_text().count('install'), 5)

    def test_monitor_installer_allows_verified_managed_upgrade_but_not_modified_files(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            sandbox, deploy, script, log = self.monitor_setup(folder)
            # Root ownership is represented by the current synthetic process's owner.
            script.write_text(script.read_text().replace('stat -c %u "$registry")" = 0',
                'stat -c %u "$registry")" = ' + str(os.getuid())))
            records = []
            for filename, target in (
                ('librus.chart.py', 'usr/libexec/netdata/python.d/librus.chart.py'),
                ('librus.conf', 'etc/netdata/python.d/librus.conf'),
                ('librus_local-health.conf', 'etc/netdata/health.d/librus_local.conf')):
                content = ('Synthetic previous managed ' + filename).encode()
                (sandbox / target).write_bytes(content)
                records.append(hashlib.sha256(content).hexdigest() + '  ' + filename)
            registry = sandbox / 'etc/netdata/python.d/librus.managed.sha256'
            registry.write_text('\n'.join(records) + '\n')
            registry.chmod(0o644)
            accepted = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(log.read_text().count('install'), 5)
            log.unlink()
            (sandbox / 'etc/netdata/health.d/librus_local.conf').write_text('Synthetic administrator edit')
            refused = subprocess.run(['sh', str(script), 'synthetic-user'], text=True, capture_output=True, timeout=10)
            self.assertEqual(refused.returncode, 1)
            self.assertFalse(log.exists())


if __name__ == '__main__':
    unittest.main()

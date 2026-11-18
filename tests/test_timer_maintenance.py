"""Exercise deployment and monitoring with a simulated systemd user manager."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import monitoring
from scripts import deploy
from test_monitoring import COLLECTOR, NOW, state
from test_support import TEST_CONFIG

SYNC_TIMER = 'librus-sync.timer'
MAINTENANCE = 'librus-sync-timer-maintenance.service'
SHA = 'a' * 40


class TimerMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.release = deploy.release_path(self.home, SHA)
        self.release.mkdir(parents=True)
        self.calls = []
        self.elapsed = 0
        self.timer_active = True
        self.timer_enabled = True
        self.worker_running = False
        self.maintenance_started = None
        self.web_ok = True
        self.metrics = COLLECTOR.HealthMetrics()

    def service_state(self, unit):
        if unit == SYNC_TIMER:
            return {'LoadState': 'loaded', 'ActiveState': 'active' if self.timer_active else 'inactive',
                    'UnitFileState': 'enabled' if self.timer_enabled else 'disabled'}
        if unit == 'librus-sync.service':
            return {'LoadState': 'loaded', 'ActiveState': 'activating' if self.worker_running else 'inactive'}
        if unit == MAINTENANCE:
            started = self.maintenance_started
            if started is None or self.elapsed - started >= 900:
                return {'LoadState': 'not-found'}
            return {'LoadState': 'loaded', 'Transient': 'yes', 'ActiveState': 'active', 'SubState': 'running',
                    'ActiveEnterTimestamp': '@' + str(int(NOW.timestamp()) + started),
                    'ActiveEnterTimestampMonotonic': str((10_000 + started) * 1_000_000)}
        return {'LoadState': 'loaded', 'ActiveState': 'active'}

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'systemd-run':
            self.assertEqual(args, ['systemd-run', '--user', '--collect',
                '--unit=' + MAINTENANCE, '--service-type=exec',
                '--property=Conflicts=' + SYNC_TIMER, '--property=After=' + SYNC_TIMER,
                '/usr/bin/sleep', '900'])
            self.maintenance_started = self.elapsed
            self.timer_active = False
        elif args[2] == 'stop' and SYNC_TIMER in args[3:]:
            self.timer_active = False
        elif args[2] == 'disable' and SYNC_TIMER in args[3:]:
            self.timer_enabled = False
        elif args[2:4] == ['enable', '--now'] and SYNC_TIMER in args[4:]:
            self.timer_enabled = True
            self.timer_active = True
            self.maintenance_started = None  # Starting a conflicting timer stops the service.

    def activate(self, *, no_sync=False):
        with patch.object(deploy, 'validate_release', return_value=self.release), \
             patch.object(deploy, 'package_probe'), patch.object(deploy, 'preflight_units'), \
             patch.object(deploy, 'preflight_runtime_config'), \
             patch.object(deploy, 'preflight_effective_units'), patch.object(deploy, 'install_units'), \
             patch.object(deploy, 'service_state', side_effect=self.service_state), \
             patch.object(deploy, 'command', side_effect=self.command):
            return deploy.activate(self.home, SHA, no_sync=no_sync)

    def sample(self):
        def state_if_loaded(unit):
            unit_state = self.service_state(unit)
            return unit_state if unit_state['LoadState'] == 'loaded' else None

        with patch.object(monitoring, 'unit_state', side_effect=state_if_loaded), \
             patch.object(monitoring, 'web_available', return_value=self.web_ok):
            return monitoring.collect_snapshot(now=NOW + timedelta(seconds=self.elapsed),
                reader=lambda *args, **kwargs: state(), config=TEST_CONFIG)

    def calculate(self, report):
        with patch.object(COLLECTOR.time, 'monotonic_ns', return_value=(10_000 + self.elapsed) * 10**9):
            return self.metrics.calculate(report, NOW.timestamp() + self.elapsed)

    def test_staged_activation_expires_without_a_fresh_export_and_counts_the_outage(self):
        self.assertEqual(self.activate(no_sync=True), self.release)
        self.assertFalse(self.timer_enabled)
        self.web_ok = False
        report = self.sample()
        self.assertFalse(report['sync_timer_ok'])
        self.assertIsNotNone(report.get('sync_timer_maintenance'))
        self.calculate(report)
        self.elapsed = 899
        current = self.calculate(report)
        self.assertEqual(current['sync_timer_down'], 0)
        self.assertGreater(current['web_down'], 5)
        self.assertEqual(current['invalid'], 1)
        self.elapsed = 900
        self.assertEqual(self.calculate(report)['sync_timer_down'], 15)
        self.assertIsNone(self.sample().get('sync_timer_maintenance'))

    def test_worker_abort_and_staged_retry_keep_original_deadline(self):
        self.worker_running = True
        with self.assertRaisesRegex(deploy.DeploymentError, 'worker still running'):
            self.activate()
        self.assertTrue(self.worker_running)
        self.assertFalse((deploy.release_base(self.home) / 'current').exists())
        self.assertIsNotNone(self.sample().get('sync_timer_maintenance'))
        self.assertEqual(len(self.calls), 1)
        self.elapsed = 420
        self.worker_running = False
        self.activate(no_sync=True)
        self.assertEqual(self.maintenance_started, 0)
        self.assertEqual(sum(args[0] == 'systemd-run' for args in self.calls), 1)
        current = self.sample()
        self.assertEqual(self.calculate(current)['sync_timer_down'], 0)
        self.elapsed = 900
        self.assertEqual(self.calculate(self.sample())['sync_timer_down'], 15)

    def test_resuming_timer_cancels_window_before_a_later_outage(self):
        self.activate(no_sync=True)
        self.assertIsNotNone(self.sample().get('sync_timer_maintenance'))
        self.elapsed = 120
        self.command(['systemctl', '--user', 'enable', '--now', SYNC_TIMER])
        active = self.sample()
        self.assertTrue(active['sync_timer_ok'])
        self.assertIsNone(active.get('sync_timer_maintenance'))
        # Even a stop between collector polls gets no stale maintenance window.
        self.command(['systemctl', '--user', 'stop', SYNC_TIMER])
        self.assertIsNone(self.sample().get('sync_timer_maintenance'))
        self.calculate(self.sample())
        self.elapsed = 420
        self.assertEqual(self.calculate(self.sample())['sync_timer_down'], 5)

    def test_unavailable_timer_and_invalid_maintenance_metadata_do_not_mask_outage(self):
        self.timer_active = False
        self.activate(no_sync=True)
        self.assertFalse(any(args[0] == 'systemd-run' for args in self.calls))
        report = self.sample()
        report['sync_timer_maintenance'] = {'started_at': 'later', 'started_monotonic_us': 1}
        self.calculate(report)
        self.elapsed = 300
        self.assertEqual(self.calculate(report)['sync_timer_down'], 5)


if __name__ == '__main__':
    unittest.main()

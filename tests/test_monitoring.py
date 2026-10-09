from test_support import TEST_CONFIG
"""Offline fault tests: no credentials, live Librus or calendar access."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

from app import monitoring

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def stamp(minutes=0):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def state():
    return dict(schema_version=1, initialized_at=stamp(60), last_attempt_at=stamp(10),
        last_successful_sync=stamp(10), last_result='ok', last_stage='complete', last_error_code=None,
        last_duration_seconds=30.5, running_since=None, consecutive_failures=0, writes_paused=False,
        writes_paused_since=None, messages_pending=0, operations_pending=0, operations_unknown=0,
        oldest_pending_at=None, review_count=0, last_backup_success_at=stamp(60),
        last_backup_attempt_at=stamp(60), last_backup_result='ok')


def snapshot():
    return dict(schema_version=1, generated_at=stamp(), db_ok=True, state=state(), sync_timer_ok=True,
        backup_timer_ok=True, sync_unit_ok=True, sync_running=False, sync_failed=False, sync_timeout=False,
        backup_failed=False, web_ok=True)


def load_collector():
    # The host's installed python.d framework is validated at deployment. Stub only its base here.
    module_name = 'bases.FrameworkServices.SimpleService'
    module = types.ModuleType(module_name)
    class Base:
        def __init__(self, configuration=None, name=None):
            self.configuration = configuration or {}
    module.SimpleService = Base
    specification = importlib.util.spec_from_file_location('librus_test_collector', ROOT/'deploy/netdata/librus.chart.py')
    collector = importlib.util.module_from_spec(specification)
    with patch.dict(sys.modules, {module_name: module}):
        specification.loader.exec_module(collector)
    return collector


COLLECTOR = load_collector()


class ExportTests(unittest.TestCase):
    def test_snapshot_closed_whitelist_drops_private_fields(self):
        value = state()
        value.update(message_body='private content', password='dummy-never-exported')
        with patch.object(monitoring, 'unit_state', return_value={
                'ActiveState': 'active', 'UnitFileState': 'enabled', 'Result': 'success'}), \
                patch.object(monitoring, 'web_available', return_value=True):
            actual = monitoring.collect_snapshot(now=NOW, reader=lambda *a, **kw: value, config=TEST_CONFIG)
        self.assertTrue(actual['db_ok'])
        self.assertNotIn('private', json.dumps(actual))
        self.assertNotIn('password', json.dumps(actual))
        COLLECTOR.validate_snapshot(actual, NOW.timestamp())

    def test_database_error_is_sanitized_and_observable(self):
        def failing(*a, **kw):
            raise RuntimeError('sensitive error content')
        with patch.object(monitoring, 'unit_state', return_value=None), patch.object(monitoring, 'web_available', return_value=False):
            actual = monitoring.collect_snapshot(now=NOW, reader=failing, config=TEST_CONFIG)
        self.assertFalse(actual['db_ok'])
        self.assertIsNone(actual['state'])
        self.assertNotIn('sensitive', json.dumps(actual))
        COLLECTOR.validate_snapshot(actual, NOW.timestamp())

    def test_future_aggregate_is_database_source_failure(self):
        value = state()
        value['last_successful_sync'] = stamp(-1)
        with self.assertRaises(ValueError):
            monitoring.sanitize_state(value, NOW)

    def test_bool_counts_negative_counts_unknown_results_are_rejected(self):
        for key, value in [('messages_pending', True), ('operations_pending', -1), ('last_result', 'healthy'),
                           ('last_stage', 'private secret'), ('last_error_code', 'https://secret'),
                           ('last_duration_seconds', float('nan')), ('last_duration_seconds', float('inf'))]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                sample = state()
                sample[key] = value
                monitoring.sanitize_state(sample, NOW)

    def test_atomic_write_mode_and_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            monitoring.atomic_write(snapshot(), path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            value = snapshot()
            value['web_ok'] = False
            monitoring.atomic_write(value, path)
            self.assertFalse(json.loads(path.read_text())['web_ok'])
            self.assertEqual(list(Path(directory).glob('.status-*')), [])

    def test_export_rejects_world_accessible_or_symlink_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            path.chmod(0o755)
            with self.assertRaises(PermissionError):
                monitoring.atomic_write(snapshot(), path/'status.json')
            path.chmod(0o700)
            link = path/'link'
            link.symlink_to(path, target_is_directory=True)
            with self.assertRaises(OSError):
                monitoring.atomic_write(snapshot(), link/'status.json')

    def test_timer_requires_active_and_enabled(self):
        self.assertTrue(monitoring.timer_available({'ActiveState': 'active', 'UnitFileState': 'enabled'}))
        for value in (None, {}, {'ActiveState': 'inactive', 'UnitFileState': 'enabled'},
                      {'ActiveState': 'active', 'UnitFileState': 'disabled'}):
            self.assertFalse(monitoring.timer_available(value))

    def test_inactive_dead_oneshot_is_normal_timeout_observed(self):
        timer = {'ActiveState': 'active', 'UnitFileState': 'enabled'}
        inactive = {'ActiveState': 'inactive', 'SubState': 'dead', 'Result': 'success', 'ExecMainStatus': '0'}
        values = {'librus-sync.timer': timer, 'librus-backup.timer': timer,
                  'librus-sync.service': inactive, 'librus-backup.service': inactive}
        with patch.object(monitoring, 'unit_state', side_effect=lambda unit: values[unit]), \
                patch.object(monitoring, 'web_available', return_value=True):
            good = monitoring.collect_snapshot(now=NOW, reader=lambda *a, **kw: state(), config=TEST_CONFIG)
            values['librus-sync.service'] = {**inactive, 'Result': 'timeout', 'ExecMainStatus': '15'}
            bad = monitoring.collect_snapshot(now=NOW, reader=lambda *a, **kw: state(), config=TEST_CONFIG)
        self.assertTrue(good['sync_unit_ok'])
        self.assertFalse(good['sync_failed'])
        self.assertFalse(good['backup_failed'])
        self.assertTrue(bad['sync_failed'])
        self.assertTrue(bad['sync_timeout'])

    def test_systemd_missing_timeout_and_failure_are_not_healthy(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('systemctl', 5),
                      subprocess.CalledProcessError(1, 'systemctl')):
            with patch.object(monitoring.subprocess, 'run', side_effect=error):
                self.assertIsNone(monitoring.unit_state('librus-sync.timer'))
        with patch.object(monitoring.subprocess, 'run', return_value=types.SimpleNamespace(stdout='LoadState=not-found\n')):
            self.assertIsNone(monitoring.unit_state('librus-sync.timer'))

    def test_probe_only_loopback_authenticated_get_no_proxy_or_redirect(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"messages":0}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(monitoring, 'build_opener', return_value=opener) as factory:
            self.assertTrue(monitoring.web_available(config=TEST_CONFIG))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8795/api/status')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Host'), 'school.example.test:8445')
        self.assertEqual(request.get_header('Tailscale-user-login'), 'owner@example.test')
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsNone(factory.call_args.args[1].redirect_request(None, None, 302, '', {}, 'https://external.invalid'))

    def test_probe_http_error_oversized_and_malformed_are_not_healthy(self):
        for status, body in ((403,b'{}'), (200,b'not json'), (200,b'[]'), (200,b'x'*65537)):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status, response.read.return_value = status, body
            opener = MagicMock()
            opener.open.return_value = response
            with patch.object(monitoring, 'build_opener', return_value=opener):
                self.assertFalse(monitoring.web_available(config=TEST_CONFIG))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.metrics = COLLECTOR.HealthMetrics()
        self.now = NOW.timestamp()

    def test_success_age_is_live_when_export_stalls(self):
        value = snapshot()
        first = self.metrics.calculate(value, self.now)
        later = self.metrics.calculate(value, self.now + 151*60)
        self.assertEqual(first['success_age'], 10)
        self.assertEqual(later['success_age'], 161)
        self.assertEqual(later['snapshot_age'], 151)
        self.assertEqual(later['invalid'], 1)

    def test_missing_last_success_and_backup_cannot_look_healthy(self):
        value = snapshot()
        value['state']['last_successful_sync'] = None
        value['state']['last_backup_success_at'] = None
        actual = self.metrics.calculate(value, self.now)
        self.assertGreater(actual['success_age'], 150)
        self.assertGreater(actual['backup_age'], 48)
        self.assertEqual(actual['success_missing'], 1)
        self.assertEqual(actual['backup_missing'], 1)

    def test_pause_suppresses_stale_success_queue_and_warns_after_90_minutes(self):
        value = snapshot()
        value['state'].update(writes_paused=True, writes_paused_since=stamp(91),
            last_successful_sync=stamp(200), operations_pending=3, oldest_pending_at=stamp(200))
        actual = self.metrics.calculate(value, self.now)
        self.assertEqual(actual['success_age'], 200)
        self.assertEqual(actual['pending_age'], 200)
        self.assertEqual(actual['success_alert_age'], 0)
        self.assertEqual(actual['pending_alert_age'], 0)
        self.assertEqual(actual['pause_minutes'], 91)
        self.assertEqual(actual['operations'], 3)

    def test_review_queue_is_informational(self):
        value = snapshot()
        value['state']['review_count'] = 100
        actual = self.metrics.calculate(value, self.now)
        self.assertEqual(actual['review'], 100)
        self.assertEqual(actual['pending_age'], 0)
        self.assertEqual(actual['failures'], 0)

    def test_failures_and_unknown_after_run_not_while_running(self):
        value = snapshot()
        value['sync_running'] = True
        value['state'].update(last_result='running', running_since=stamp(46), operations_unknown=2)
        actual = self.metrics.calculate(value, self.now)
        self.assertEqual(actual['unknown'], 0)
        self.assertEqual(actual['running_minutes'], 46)
        value['sync_running'] = False
        after_kill = self.metrics.calculate(value, self.now)
        self.assertEqual(after_kill['unknown'], 2)
        value['state'].update(last_result='interrupted', running_since=None, consecutive_failures=2)
        actual = self.metrics.calculate(value, self.now)
        self.assertEqual(actual['unknown'], 2)
        self.assertEqual(actual['failures'], 2)
        self.assertEqual(actual['interrupted'], 1)

    def test_unit_kill_before_history_finalized_is_critical(self):
        value = snapshot()
        value.update(sync_failed=True, sync_timeout=True)
        actual = self.metrics.calculate(value, self.now)
        self.assertEqual(actual['failures'], 1)
        self.assertEqual(actual['interrupted'], 1)

    def test_service_outages_and_recovery_at_two_and_five_minutes(self):
        value = snapshot()
        value.update(sync_timer_ok=False, backup_timer_ok=False, web_ok=False, sync_unit_ok=False)
        self.metrics.calculate(value, self.now)
        for minutes in (2,5):
            actual = self.metrics.calculate(value, self.now + minutes*60)
            for dimension in ('sync_timer_down','backup_timer_down','web_down','sync_unit_down'):
                self.assertEqual(actual[dimension], minutes)
        value.update(sync_timer_ok=True, backup_timer_ok=True, web_ok=True, sync_unit_ok=True)
        actual = self.metrics.calculate(value, self.now+6*60)
        self.assertEqual(actual['web_down'], 0)
        self.assertEqual(actual['sync_timer_down'], 0)

    def test_missing_bad_file_never_disables_collector_and_ages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            service = COLLECTOR.Service({'path': str(path)})
            self.assertTrue(service.check())
            with patch.object(COLLECTOR.time, 'time', return_value=self.now):
                first = service.get_data()
            self.assertEqual(first['invalid'], COLLECTOR.WIRE_SCALE)
            for minutes, content in ((3,'not json'), (5,'{}')):
                path.write_text(content)
                with patch.object(COLLECTOR.time, 'time', return_value=self.now+minutes*60):
                    actual = service.get_data()
                self.assertEqual(actual['invalid_minutes'], minutes * COLLECTOR.WIRE_SCALE)
                self.assertEqual(actual['snapshot_age'], minutes * COLLECTOR.WIRE_SCALE)
                self.assertEqual(set(actual), {line[0] for chart in COLLECTOR.CHARTS.values() for line in chart['lines']})

    def test_corruption_retains_last_known_fault_and_ages_business_metrics(self):
        value = snapshot()
        value['state']['consecutive_failures'] = 2
        self.metrics.calculate(value, self.now)
        first = self.metrics.invalid_data(self.now+60)
        later = self.metrics.invalid_data(self.now+6*60)
        self.assertEqual(first['failures'], 2)
        self.assertEqual(later['failures'], 2)
        self.assertEqual(later['success_age'], 16)
        self.assertEqual(later['invalid_minutes'], 5)

    def test_future_missing_negative_and_inconsistent_fields_rejected(self):
        samples=[]
        for key,value in [('generated_at',stamp(-1)), ('schema_version', True), ('db_ok',1)]:
            sample = snapshot(); sample[key] = value; samples.append(sample)
        sample=snapshot(); del sample['web_ok']; samples.append(sample)
        sample=snapshot(); del sample['state']['last_error_code']; samples.append(sample)
        for key,value in [('last_successful_sync',stamp(-1)), ('operations_pending',-1), ('messages_pending',True),
                          ('writes_paused','false'), ('last_result','healthy'), ('initialized_at',None),
                          ('last_duration_seconds',float('nan')), ('last_duration_seconds',float('inf')),
                          ('last_stage','secret words')]:
            sample=snapshot(); sample['state'][key]=value; samples.append(sample)
        for changes in [dict(operations_pending=1, oldest_pending_at=None),
                        dict(last_result='running', running_since=None), dict(writes_paused=True,writes_paused_since=None)]:
            sample=snapshot(); sample['state'].update(changes); samples.append(sample)
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises((ValueError, KeyError)):
                COLLECTOR.validate_snapshot(sample, self.now)

    def test_database_failed_export_ages_without_false_success(self):
        value = snapshot(); value.update(db_ok=False,state=None)
        self.metrics.calculate(value,self.now)
        actual = self.metrics.calculate(value,self.now+5*60)
        self.assertEqual(actual['db_down'],5)
        self.assertEqual(actual['invalid'],1)

    def test_symlink_or_oversized_export_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'; target=Path(directory)/'target'
            target.write_text(json.dumps(snapshot()))
            path.symlink_to(target)
            service=COLLECTOR.Service({'path':str(path)})
            self.assertEqual(service.get_data()['invalid'],COLLECTOR.WIRE_SCALE)
            path.unlink(); path.write_text('x'*65537)
            self.assertEqual(service.get_data()['invalid'],COLLECTOR.WIRE_SCALE)

    def test_recovery_clears_source_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'; service=COLLECTOR.Service({'path':str(path)})
            with patch.object(COLLECTOR.time,'time',return_value=self.now): service.get_data()
            path.write_text(json.dumps(snapshot()))
            with patch.object(COLLECTOR.time,'time',return_value=self.now+300): actual=service.get_data()
            self.assertEqual(actual['invalid_minutes'],0)
            path.write_text('bad')
            with patch.object(COLLECTOR.time,'time',return_value=self.now+360): actual=service.get_data()
            self.assertEqual(actual['invalid_minutes'],0)

    def test_native_wire_preserves_fractional_hours_and_minutes_at_thresholds(self):
        value = snapshot()
        value['state']['last_backup_success_at'] = stamp(36.1 * 60)
        value['state']['last_successful_sync'] = stamp(90.1)
        raw = self.metrics.calculate(value, self.now)
        self.assertAlmostEqual(raw['backup_age'], 36.1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            path.write_text(json.dumps(value))
            service = COLLECTOR.Service({'path': str(path)})
            with patch.object(COLLECTOR.time, 'time', return_value=self.now):
                wire = service.get_data()
        # Match native Dimension.get_value(): int(value), then chart multiplier/divisor.
        dimensions = {line[0]: line for chart in COLLECTOR.CHARTS.values() for line in chart['lines']}
        for name, threshold in (('backup_age', 36), ('success_alert_age', 90)):
            line = dimensions[name]
            self.assertEqual(line[2:], ['absolute', 1, 1000])
            displayed = int(wire[name]) * line[3] / line[4]
            self.assertGreater(displayed, threshold)
            self.assertAlmostEqual(displayed, raw[name], places=3)
        for name, value in wire.items():
            self.assertIs(type(value), int)
            self.assertEqual(dimensions[name][2:], ['absolute', 1, 1000])

    def test_native_wire_rejects_nonfinite_or_unbounded_metrics(self):
        for value in (float('nan'), float('inf'), -1, True, 10**12 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                COLLECTOR.encode_metrics({'bad': value})

    def test_thresholds_and_collector_missing_rule_are_explicit(self):
        content=(ROOT/'deploy/netdata/librus_local-health.conf').read_text()
        # Verify evaluated configuration values rather than comments.
        blocks={}
        for block in content.split('template: ')[1:]:
            lines=block.splitlines(); blocks[lines[0]]=dict(line.strip().split(': ',1) for line in lines[1:] if ': ' in line)
        expected={'librus_full_sync_stale':('$this > 90','$this > 150'),
            'librus_sync_failures':('$this >= 1','$this >= 2'),
            'librus_sync_runtime':('$this > 30','$this >= 45'),
            'librus_automatic_queue_stale':('$this > 90','$this > 150'),
            'librus_sync_timer_down':('$this >= 2','$this >= 5'),
            'librus_backup_timer_down':('$this >= 2','$this >= 5'),
            'librus_web_down':('$this >= 2','$this >= 5'),
            'librus_monitor_snapshot_stale':('$this >= 3','$this >= 5'),
            'librus_monitor_source_invalid':('$this >= 3','$this >= 5'),
            'librus_backup_stale':('$this > 36','$this > 48')}
        for name,(warn,crit) in expected.items():
            self.assertEqual(blocks[name]['warn'],warn); self.assertEqual(blocks[name]['crit'],crit)
        self.assertIn('$last_collected_t',blocks['librus_collector_missing']['calc'])
        self.assertEqual(blocks['librus_unknown_calendar_write']['crit'],'$this > 0')
        self.assertEqual(blocks['librus_writes_left_paused']['warn'],'$this > 90')
        for block in blocks.values():
            self.assertEqual(block['every'],'60s'); self.assertEqual(block['repeat'],'off')
            self.assertNotIn('no-clear-notification',block.get('options',''))


if __name__ == '__main__':
    unittest.main()

"""Verify the real exported monitor format using only temporary synthetic state."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import monitoring
from app.core import AppStore
from scripts import deploy
from test_support import TEST_CONFIG


class SnapshotVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.home.chmod(0o700)
        self.release = self.home / 'release'
        self.release.mkdir()
        self.config = replace(TEST_CONFIG, state_path=self.home / 'state.sqlite3',
                              monitor_output=self.home / 'status.json')
        store = AppStore(self.config.state_path, config=self.config)
        store.close()
        self.units = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                      'UnitFileState': 'disabled', 'Result': 'success', 'ExecMainStatus': '0'}
        with patch.object(monitoring, 'unit_state', return_value=self.units), \
             patch.object(monitoring, 'web_available', return_value=True):
            self.snapshot = monitoring.collect_snapshot(config=self.config)
        self.assertTrue(self.snapshot['db_ok'])
        self.assertNotIn('schema_version', self.snapshot['state'])
        monitoring.atomic_write(self.snapshot, self.config.monitor_output)

    def verify(self):
        def run(args, **kwargs):
            if args[0] == 'systemctl':
                return None  # No host systemd access; only execute the runtime validators.
            self.assertEqual(args[1:3], ['-I', '-c'])
            exec(compile(args[3], '<release-verifier>', 'exec'), {})

        with patch.object(deploy, 'current_release', return_value=self.release), \
             patch.object(deploy, 'preflight_units'), \
             patch.object(deploy, 'preflight_runtime_config'), \
             patch.object(deploy, 'preflight_effective_units'), \
             patch.object(deploy, 'command', side_effect=run), \
             patch('app.config.load_config', return_value=self.config), \
             patch.object(monitoring, 'load_config', return_value=self.config), \
             patch.object(monitoring, 'unit_state', return_value=self.units), \
             patch.object(monitoring, 'web_available', return_value=True):
            return deploy.verify(self.home, no_sync=True)

    def test_actual_collector_and_atomic_writer_output_verifies_without_inner_version(self):
        self.assertEqual(self.verify(), self.release)

    def test_stale_or_future_export_is_rejected(self):
        for seconds in (-181, 60):
            with self.subTest(seconds=seconds):
                value = dict(self.snapshot, generated_at=(datetime.now(timezone.utc)
                             + timedelta(seconds=seconds)).isoformat())
                monitoring.atomic_write(value, self.config.monitor_output)
                with self.assertRaises((AssertionError, ValueError)):
                    self.verify()

    def test_exported_state_and_envelope_must_match_closed_versioned_format(self):
        for mutation in ('version', 'count', 'missing', 'unexpected', 'raw_version'):
            with self.subTest(mutation=mutation):
                value = json.loads(json.dumps(self.snapshot))
                if mutation == 'version':
                    value['schema_version'] = 2
                elif mutation == 'count':
                    value['state']['operations_pending'] = '0'
                elif mutation == 'missing':
                    del value['state']['initialized_at']
                elif mutation == 'unexpected':
                    value['state']['unexpected_field'] = 'synthetic'
                else:
                    value['state']['schema_version'] = 1
                monitoring.atomic_write(value, self.config.monitor_output)
                with self.assertRaises((AssertionError, ValueError, KeyError)):
                    self.verify()


if __name__ == '__main__':
    unittest.main()

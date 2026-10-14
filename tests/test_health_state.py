from test_support import TEST_CONFIG
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.core import AppStore
from app.health_state import read_monitoring_state


class HealthStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = AppStore(self.path, config=TEST_CONFIG)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_reader_does_not_migrate_chmod_or_export_private_data(self):
        self.store.save_message({'id': 'PRIVATE-ID', 'text': 'PRIVATE-BODY', 'subject': 'PRIVATE-SUBJECT'})
        self.store.set_setting('read_marking_observation', 'PRIVATE-OBSERVATION')
        self.path.chmod(0o640)
        before = self.path.stat()
        schema = list(self.store.db.execute('SELECT sql FROM sqlite_master'))
        with patch('app.core.AppStore', side_effect=AssertionError('must not initialize')):
            state = read_monitoring_state(self.path)
        after = self.path.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(after.st_mode & 0o777, 0o640)
        self.assertEqual(schema, list(self.store.db.execute('SELECT sql FROM sqlite_master')))
        self.assertNotIn('PRIVATE', json.dumps(state))
        self.assertEqual(state['messages_pending'], 1)
        self.assertIsNotNone(state['oldest_pending_at'])
        self.assertIsNone(state['last_successful_sync'])

    def test_missing_or_invalid_database_is_not_created_or_healthy(self):
        missing = self.path.parent / 'missing' / 'state.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            read_monitoring_state(missing)
        self.assertFalse(missing.parent.exists())
        corrupt = self.path.parent / 'corrupt.sqlite3'
        corrupt.write_bytes(b'not SQLite')
        with self.assertRaises(sqlite3.DatabaseError):
            read_monitoring_state(corrupt)
        empty = self.path.parent / 'empty.sqlite3'
        sqlite3.connect(empty).close()
        with self.assertRaises(ValueError):
            read_monitoring_state(empty)

    def test_legacy_defaults_preserve_attempt_without_inventing_success(self):
        stamp = '2026-10-08T12:00:00+02:00'
        with self.store.db:
            self.store.db.execute('DROP TABLE sync_runs')
            self.store.db.execute("DELETE FROM settings WHERE key LIKE 'health_%' OR key='writes_paused_since'")
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_sync',?)", (stamp,))
        before = self.path.read_bytes()
        state = read_monitoring_state(self.path)
        self.assertEqual(state['last_attempt_at'], '2026-10-08T10:00:00+00:00')
        self.assertIsNone(state['last_successful_sync'])
        self.assertIsNone(state['initialized_at'])
        self.assertEqual(state['last_result'], 'idle')
        self.assertEqual(before, self.path.read_bytes())

    def test_read_snapshot_includes_running_and_finished_attempt_metadata(self):
        run_id = self.store.start_sync_run()
        self.store.sync_stage(run_id, 'login')
        running = read_monitoring_state(self.path)
        self.assertEqual(running['last_result'], 'running')
        self.assertEqual(running['last_stage'], 'login')
        self.assertEqual(running['last_attempt_at'], running['running_since'])
        self.store.finish_sync_run(run_id, 'failed', 'login_failed', 2.5)
        failed = read_monitoring_state(self.path)
        self.assertEqual(failed['last_duration_seconds'], 2.5)
        self.assertEqual(failed['last_error_code'], 'login_failed')
        self.assertEqual(failed['consecutive_failures'], 1)
        self.assertIsNone(failed['running_since'])

    def test_abandoned_runs_are_recovered_and_history_retained_90_days(self):
        abandoned = self.store.start_sync_run()
        old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=89)).isoformat()
        with self.store.db:
            self.store.db.execute("INSERT INTO sync_runs(id,started_at,finished_at,stage,result) VALUES ('old',?,?,'complete','ok')", (old, old))
            self.store.db.execute("INSERT INTO sync_runs(id,started_at,finished_at,stage,result) VALUES ('recent',?,?,'complete','ok')", (recent, recent))
        new = self.store.start_sync_run()
        previous = self.store.db.execute('SELECT result,error_code,finished_at FROM sync_runs WHERE id=?', (abandoned,)).fetchone()
        self.assertEqual(tuple(previous[:2]), ('interrupted', 'worker_interrupted'))
        self.assertTrue(previous[2])
        self.assertIsNone(self.store.db.execute("SELECT id FROM sync_runs WHERE id='old'").fetchone())
        self.assertIsNotNone(self.store.db.execute("SELECT id FROM sync_runs WHERE id='recent'").fetchone())
        self.assertEqual(read_monitoring_state(self.path)['consecutive_failures'], 1)
        self.store.finish_sync_run(new, 'ok')
        self.assertEqual(read_monitoring_state(self.path)['consecutive_failures'], 0)

    def test_pause_timestamp_changes_only_on_transitions(self):
        first = self.store.status()['writes_paused_since']
        self.store.set_writes_paused(True)
        self.assertEqual(first, self.store.status()['writes_paused_since'])
        self.store.set_writes_paused(False)
        self.assertIsNone(self.store.status()['writes_paused_since'])
        self.store.set_writes_paused(True)
        self.assertIsNotNone(self.store.status()['writes_paused_since'])

    def test_unrecognized_metadata_cannot_leak_as_an_error_code(self):
        run_id = self.store.start_sync_run()
        with self.assertRaises(ValueError):
            self.store.finish_sync_run(run_id, 'failed', 'PRIVATE-TOKEN')
        with self.store.db:
            self.store.db.execute("UPDATE sync_runs SET error_code='PRIVATE-TOKEN' WHERE id=?", (run_id,))
        with self.assertRaises(ValueError):
            read_monitoring_state(self.path)

    def test_message_queue_age_is_preserved_on_retry_but_restarts_after_new_edit(self):
        self.store.note_discovered({'messageId': '1'})
        old = '2026-10-01T10:00:00+00:00'
        with self.store.db:
            self.store.db.execute('UPDATE messages SET discovered_at=?,pending_since=? WHERE id=?', (old, old, '1'))
        self.store.save_message({'id': '1', 'text': 'Original message'})
        self.store.fail_message('1', 'analysis', 'private error')
        self.store.save_message({'id': '1', 'text': 'Original message'})
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)
        self.store.save_proposals('1', [])
        self.assertIsNone(read_monitoring_state(self.path)['oldest_pending_at'])
        self.store.save_message({'id': '1', 'text': 'Edited message'})
        self.assertGreater(read_monitoring_state(self.path)['oldest_pending_at'], old)

    def test_migration_preserves_legacy_queue_age_through_first_retry(self):
        old = '2026-10-01T10:00:00+00:00'
        self.store.save_message({'id': '1', 'text': 'Original message'})
        with self.store.db:
            self.store.db.execute('UPDATE messages SET discovered_at=NULL,pending_since=NULL,fetched_at=? WHERE id=?', (old, '1'))
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.store.save_message({'id': '1', 'text': 'Original message'})
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)

    def probe_attempt(self, run_id, number=1, *, result='ok', stamp=None):
        stamp = stamp or datetime.now(timezone.utc).isoformat()
        attempt = dict(attempt_number=number, model='gpt-6.1-sol', effort='high', cli_version='0.162.1',
            started_at=stamp, completed_at=None, result='running', safe_code=None, process_code=None,
            started_count=None, completed_count=None, parse_error_count=None, unknown_event_count=None,
            incomplete_count=None, expected_tool_name=None, expected_tool_arguments=None)
        self.store.record_probe_attempt(run_id, attempt)
        if result != 'running':
            attempt.update(result=result, completed_at=stamp, safe_code='unexpected_tool_count' if result == 'failed' else None,
                process_code=0, started_count=0, completed_count=1 if result == 'ok' else 0,
                parse_error_count=0, unknown_event_count=0, incomplete_count=0,
                expected_tool_name=True, expected_tool_arguments=True)
            self.store.record_probe_attempt(run_id, attempt)
        return attempt

    def test_probe_health_unknown_until_evidence_and_legacy_reader_does_not_migrate(self):
        fields = ('calendar_probe_result', 'calendar_probe_attempts', 'calendar_probe_recovered',
                  'calendar_probe_retries_24h', 'calendar_probe_recoveries_24h')
        self.assertTrue(all(self.store.status()[key] is None for key in fields))
        run = self.store.start_sync_run()
        self.assertTrue(all(self.store.status()[key] is None for key in fields))
        self.probe_attempt(run, result='running')
        state = read_monitoring_state(self.path)
        self.assertEqual((state['calendar_probe_result'], state['calendar_probe_attempts']), ('running', 1))
        self.assertIsNone(state['calendar_probe_recovered'])
        self.assertEqual(state['calendar_probe_retries_24h'], 0)
        with self.store.db:
            self.store.db.execute('DROP TABLE calendar_probe_attempts')
        before = self.path.read_bytes()
        self.assertTrue(all(read_monitoring_state(self.path)[key] is None for key in fields))
        self.assertEqual(before, self.path.read_bytes())

    def test_probe_recovery_counters_are_bounded_to_24h_and_latest_run(self):
        run = self.store.start_sync_run()
        first = self.probe_attempt(run, result='failed')
        second = self.probe_attempt(run, 2)
        self.store.finish_sync_run(run, 'ok')
        state = read_monitoring_state(self.path)
        self.assertEqual((state['calendar_probe_result'], state['calendar_probe_attempts'], state['calendar_probe_recovered']), ('ok', 2, True))
        self.assertEqual((state['calendar_probe_retries_24h'], state['calendar_probe_recoveries_24h']), (1, 1))
        later = datetime.fromisoformat(second['completed_at']) + timedelta(hours=25)
        state = read_monitoring_state(self.path, now=later)
        self.assertEqual((state['calendar_probe_retries_24h'], state['calendar_probe_recoveries_24h']), (0, 0))
        next_run = self.store.start_sync_run()
        state = read_monitoring_state(self.path)
        self.assertIsNone(state['calendar_probe_result'])
        self.assertIsNone(state['calendar_probe_recovered'])
        self.assertEqual(state['calendar_probe_recoveries_24h'], 1)
        self.probe_attempt(next_run)
        self.assertFalse(read_monitoring_state(self.path)['calendar_probe_recovered'])

    def test_probe_attempt_whitelist_and_durable_retry_evidence(self):
        run = self.store.start_sync_run()
        first = self.probe_attempt(run, result='running')
        for changes in ({'stdout': 'PRIVATE'}, {'model': 'PRIVATE'}, {'safe_code': 'PRIVATE'},
                        {'cli_version': 'PRIVATE'}, {'expected_tool_name': 'PRIVATE'},
                        {'attempt_number': 3}, {'completed_count': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.record_probe_attempt(run, dict(first, **changes))
        with self.assertRaises(ValueError):
            self.probe_attempt(run, 2, result='running')
        with self.store.db:
            self.store.db.execute("UPDATE calendar_probe_attempts SET cli_version='PRIVATE' WHERE sync_run_id=?", (run,))
        with self.assertRaises(ValueError):
            read_monitoring_state(self.path)

    def test_abandoned_probe_attempt_is_interrupted_and_history_pruned_after_90_days(self):
        run = self.store.start_sync_run()
        self.probe_attempt(run, result='running')
        next_run = self.store.start_sync_run()
        row = self.store.db.execute('SELECT result,safe_code,completed_at FROM calendar_probe_attempts WHERE sync_run_id=?', (run,)).fetchone()
        self.assertEqual((row['result'], row['safe_code']), ('interrupted', 'worker_interrupted'))
        self.assertTrue(row['completed_at'])
        self.store.finish_sync_run(next_run, 'ok')
        old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        with self.store.db:
            self.store.db.execute('UPDATE sync_runs SET started_at=?,finished_at=? WHERE id=?', (old, old, run))
            self.store.db.execute('UPDATE calendar_probe_attempts SET started_at=?,completed_at=? WHERE sync_run_id=?', (old, old, run))
        self.store.start_sync_run()
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM calendar_probe_attempts WHERE sync_run_id=?', (run,)).fetchone())
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM sync_runs WHERE id=?', (run,)).fetchone())

    def test_probe_attempt_health_uses_existing_snapshot_and_store_status_starts_one(self):
        trace = []
        self.store.db.set_trace_callback(trace.append)
        self.store.status()
        self.assertIn('BEGIN', trace)
        self.assertIn('ROLLBACK', trace)
        self.assertFalse(self.store.db.in_transaction)


if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

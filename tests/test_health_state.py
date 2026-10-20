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
from app.monitoring import sanitize_state
from test_core import NOW, message, proposal


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
        for name in ('last_completed_result', 'last_completed_stage', 'last_completed_error_code'):
            self.assertIsNone(state[name])
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

    def test_running_preview_and_pause_keep_last_completed_failure_until_success(self):
        failed = self.store.start_sync_run()
        self.store.sync_stage(failed, 'analysis')
        self.store.finish_sync_run(failed, 'partial', 'agenda_context_overflow')
        for result in ('dry_run', 'paused'):
            attempt = self.store.start_sync_run()
            self.store.finish_sync_run(attempt, result)
        running = self.store.start_sync_run()
        self.store.sync_stage(running, 'reconcile')
        before = self.path.read_bytes()
        state = read_monitoring_state(self.path)
        self.assertEqual(state['last_result'], 'running')
        self.assertIsNone(state['last_error_code'])
        self.assertEqual(state['last_completed_result'], 'partial')
        self.assertEqual(state['last_completed_stage'], 'analysis')
        self.assertEqual(state['last_completed_error_code'], 'agenda_context_overflow')
        self.assertEqual(state['consecutive_failures'], 1)
        self.assertEqual(self.path.read_bytes(), before)
        exported = sanitize_state(state, datetime.now(timezone.utc))
        self.assertEqual(exported['last_completed_error_code'], 'agenda_context_overflow')
        self.store.sync_stage(running, 'complete')
        self.store.finish_sync_run(running, 'ok')
        state = read_monitoring_state(self.path)
        self.assertEqual(state['last_completed_result'], 'ok')
        self.assertEqual(state['last_completed_stage'], 'complete')
        self.assertIsNone(state['last_completed_error_code'])
        self.assertEqual(state['consecutive_failures'], 0)

    def test_completed_sync_selection_uses_finish_instant_not_start_or_offset_text(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO sync_runs(id,started_at,finished_at,stage,result,error_code) "
                                  "VALUES ('latest-finish','2026-10-16T07:00:00Z','2026-10-16T08:30:00Z',"
                                  "'analysis','failed','codex_failed')")
            self.store.db.execute("INSERT INTO sync_runs(id,started_at,finished_at,stage,result) "
                                  "VALUES ('later-start','2026-10-16T09:45:00+02:00','2026-10-16T10:00:00+02:00',"
                                  "'complete','ok')")
        self.store.start_sync_run()
        state = read_monitoring_state(self.path)
        self.assertEqual(state['last_completed_result'], 'failed')
        self.assertEqual(state['last_completed_stage'], 'analysis')
        self.assertEqual(state['last_completed_error_code'], 'codex_failed')

    def test_completed_metadata_is_optional_and_closed_before_export(self):
        state = read_monitoring_state(self.path)
        now = datetime.now(timezone.utc)
        fields = ('last_completed_result', 'last_completed_stage', 'last_completed_error_code')
        legacy = {key: value for key, value in state.items() if key not in fields}
        clean = sanitize_state(legacy, now)
        self.assertTrue(all(clean[name] is None for name in fields))
        for name, value in (('last_completed_result', 'running'), ('last_completed_result', 'dry_run'),
                            ('last_completed_stage', 'PRIVATE-TEXT'),
                            ('last_completed_error_code', 'private_school_identifier')):
            with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                sanitize_state({**state, name: value}, now)
        failed = self.store.start_sync_run()
        self.store.finish_sync_run(failed, 'failed', 'codex_failed')
        self.store.start_sync_run()
        with self.store.db:
            self.store.db.execute("UPDATE sync_runs SET error_code='PRIVATE-TOKEN' WHERE id=?", (failed,))
        with self.assertRaisesRegex(ValueError, 'completed sync health metadata'):
            read_monitoring_state(self.path)

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

    def test_old_review_approval_starts_age_at_eligibility_and_reapproval_keeps_it(self):
        old = '2026-10-01T10:00:00+00:00'
        approved = '2026-10-16T06:52:38+00:00'
        later = '2026-10-16T07:30:00+00:00'
        with patch('app.core.utcnow', return_value=old):
            self.store.save_message(message())
            item = self.store.save_proposals('1', [proposal(needs_review=True)], NOW)[0]
        self.assertEqual(item['status'], 'review')
        self.assertIsNone(read_monitoring_state(self.path)['oldest_pending_at'])
        with patch('app.core.utcnow', return_value=approved):
            self.store.resolve_proposal(item['id'], 'approve')
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], approved)
        with patch('app.core.utcnow', return_value=later):
            self.store.resolve_proposal(item['id'], 'approve')
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], approved)
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], approved)
        self.assertEqual(self.store.db.execute('SELECT created_at FROM proposals').fetchone()[0], old)

    def test_automatic_queue_age_survives_preparation_and_failed_operation_retries(self):
        old = '2026-10-01T10:00:00+00:00'
        later = '2026-10-16T07:30:00+00:00'
        with patch('app.core.utcnow', return_value=old):
            self.store.save_message(message())
            item = self.store.save_proposals('1', [proposal()], NOW)[0]
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)
        with patch('app.core.utcnow', return_value=later):
            operation = self.store.queue_operations(NOW)[0]
            self.store.queue_operations(NOW)
            self.store.set_writes_paused(False)
            self.assertTrue(self.store.claim_operation(operation['operation_id']))
            self.store.record_result(operation['operation_id'], {'status': 'failed', 'error': 'operation_failed'})
            self.store.queue_operations(NOW)
        self.assertEqual(read_monitoring_state(self.path)['operations_pending'], 1)
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)
        self.assertEqual(self.store.db.execute('SELECT pending_since FROM proposals WHERE id=?', (item['id'],)).fetchone()[0], old)

    def test_review_removes_queue_age_and_fresh_approval_starts_a_new_age(self):
        first = '2026-10-01T10:00:00+00:00'
        second = '2026-10-16T06:52:38+00:00'
        with patch('app.core.utcnow', return_value=first):
            self.store.save_message(message())
            item = self.store.save_proposals('1', [proposal()], NOW)[0]
            operation = self.store.queue_operations(NOW)[0]
        self.store.record_result(operation['operation_id'], {'status': 'review', 'error': 'Synthetic review'})
        self.assertIsNone(read_monitoring_state(self.path)['oldest_pending_at'])
        self.assertIsNone(self.store.db.execute('SELECT pending_since FROM proposals').fetchone()[0])
        with patch('app.core.utcnow', return_value=second):
            self.store.resolve_proposal(item['id'], 'approve')
        self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], second)
        self.store.resolve_proposal(item['id'], 'ignore')
        self.assertIsNone(read_monitoring_state(self.path)['oldest_pending_at'])
        self.assertIsNone(self.store.db.execute('SELECT pending_since FROM proposals').fetchone()[0])

    def test_legacy_proposal_reader_is_read_only_and_migration_keeps_conservative_age(self):
        old = '2026-10-01T10:00:00+00:00'
        with patch('app.core.utcnow', return_value=old):
            self.store.save_message(message())
            self.store.save_proposals('1', [proposal()], NOW)
        with self.store.db:
            self.store.db.execute('ALTER TABLE proposals DROP COLUMN pending_since')
        before = self.path.read_bytes()
        with patch('app.core.AppStore', side_effect=AssertionError('must not initialize')):
            self.assertEqual(read_monitoring_state(self.path)['oldest_pending_at'], old)
        self.assertEqual(self.path.read_bytes(), before)
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.assertEqual(self.store.db.execute('SELECT pending_since FROM proposals').fetchone()[0], old)
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

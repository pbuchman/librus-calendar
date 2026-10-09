from test_support import TEST_CONFIG
import fcntl
import io
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.core import AppStore
from app.sync import SyncController
from test_core import NOW, message, proposal


METADATA = {'model': 'gpt-6.1-sol', 'effort': 'high', 'prompt_version': 'school-actions-v2',
            'decision_reason': 'A material must be brought on the stated school day.'}


class AnalysisOnlyRuntime:
    def __init__(self, proposals=None):
        self.proposals = proposals if proposals is not None else [proposal(temporal_kind='deadline',
            start='2030-11-10', end=None, all_day=True, due_at='2030-11-10T16:00:00+01:00')]
        self.analyzed = []

    def analyze(self, source, events):
        self.analyzed.append(source['id'])
        self.last_analysis_metadata = dict(METADATA)
        return self.proposals

    def probe(self):
        raise AssertionError('Cached reanalysis must not probe Calendar.')

    def execute(self, operation):
        raise AssertionError('Cached reanalysis must not call Calendar execution.')


class ReanalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name)/'data/state.sqlite3', config=TEST_CONFIG)
        self.store.save_message(message())
        self.old = self.store.save_proposals('1', [proposal(confidence='low')], NOW)[0]
        self.runtime = AnalysisOnlyRuntime()
        self.controller = SyncController(self.store, runtime=self.runtime,
            client_factory=lambda stage: self.fail('Cached reanalysis cannot log into Librus.'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def snapshot(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)]
                for table in ('messages', 'proposals', 'operations', 'events', 'sync_runs', 'settings', 'analysis_audit')}

    def test_preview_is_read_only_and_classifies_deadline(self):
        before = self.snapshot()
        result = self.controller.reanalyze(['1'], current=NOW)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['calendar_writes'], 0)
        item = result['messages'][0]['proposals'][0]
        self.assertEqual(item['temporal_kind'], 'deadline')
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['due_at'], '2030-11-10T16:00:00+01:00')
        self.assertEqual(before, self.snapshot())

    def test_apply_supersedes_without_erasing_evidence_or_advancing_health(self):
        before_health = self.store.status()
        original_payload = self.store.db.execute('SELECT payload FROM proposals WHERE id=?', (self.old['id'],)).fetchone()[0]
        result = self.controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['calendar_writes'], 0)
        rows = {item['id']: item for item in self.store.list_proposals()}
        self.assertEqual(rows[self.old['id']]['status'], 'superseded')
        self.assertEqual(self.store.db.execute('SELECT payload FROM proposals WHERE id=?', (self.old['id'],)).fetchone()[0], original_payload)
        self.assertEqual(self.store.status()['proposals_pending'], 1)
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.list_events(), [])
        for field in ('last_sync', 'last_attempt_at', 'last_successful_sync', 'last_result', 'consecutive_failures'):
            self.assertEqual(self.store.status()[field], before_health[field])
        audit = self.store.get_analysis_history('1')[0]
        self.assertEqual(audit['mode'], 'cached_reanalysis')
        self.assertEqual(audit['superseded_ids'], [self.old['id']])
        with self.assertRaises(ValueError):
            self.store.resolve_proposal(self.old['id'], 'approve')

    def test_rerun_is_idempotent_and_does_not_make_self_duplicate(self):
        first = self.controller.reanalyze(['1'], apply=True, current=NOW)
        second = self.controller.reanalyze(['1', '1'], apply=True, current=NOW)
        self.assertEqual(first['messages'][0]['proposals'], second['messages'][0]['proposals'])
        self.assertEqual(second['messages'][0]['superseded_ids'], [])
        self.assertEqual(len(self.store.list_proposals()), 2)
        self.assertEqual(self.runtime.analyzed, ['1', '1'])

    def test_old_prompt_pending_id_is_retired_with_unattempted_operation(self):
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending',payload=? WHERE id=?", (json.dumps(dict(self.old, confidence='high', needs_review=False)), self.old['id']))
        queued = self.store.queue_operations(NOW)[0]
        result = self.controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.db.execute('SELECT status FROM operations WHERE id=?', (queued['operation_id'],)).fetchone()[0], 'superseded')
        self.assertEqual(self.store.status()['operations_pending'], 1)  # One new proposal, no retired operation.
        self.store.set_writes_paused(False)
        self.assertFalse(self.store.claim_operation(queued['operation_id']))
        # A later ordinary sync may queue exactly the replacement, never the retired row.
        self.assertEqual(len(self.store.queue_operations(NOW)), 1)
        self.assertEqual(len(self.store.pending_operations()), 1)

    def test_applied_ignored_approved_and_edited_proposals_are_immutable(self):
        for state, flags in [('applied', {}), ('ignored', {}), ('pending', {'user_approved': True}),
                             ('review', {'user_edited': True})]:
            with self.subTest(state=state, flags=flags):
                with self.store.db:
                    self.store.db.execute('UPDATE proposals SET status=?,payload=? WHERE id=?',
                        (state, json.dumps(dict(self.old, **flags)), self.old['id']))
                before = self.snapshot()
                result = self.controller.reanalyze(['1'], apply=True, current=NOW)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.runtime.analyzed, [])

    def test_unknown_inflight_review_and_attempted_operations_refuse_without_recovery(self):
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending',payload=? WHERE id=?", (json.dumps(dict(self.old, confidence='high', needs_review=False)), self.old['id']))
        operation = self.store.queue_operations(NOW)[0]
        for state, attempts in [('unknown', 1), ('inflight', 1), ('review', 0), ('pending', 1)]:
            with self.subTest(state=state), self.store.db:
                self.store.db.execute('UPDATE operations SET status=?,attempts=? WHERE id=?', (state, attempts, operation['operation_id']))
                before = self.snapshot()
                result = self.controller.reanalyze(['1'], apply=True, current=NOW)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(before, self.snapshot())
        self.assertEqual(self.runtime.analyzed, [])

    def test_historical_event_or_tombstone_prevents_reanalysis(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)', ('event', 'calendar@example.test', '1', self.old['id'], '{}', 'fp', 'marker', NOW.isoformat(), 'cancelled'))
        before = self.snapshot()
        self.assertEqual(self.controller.reanalyze(['1'], apply=True, current=NOW)['status'], 'refused')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.runtime.analyzed, [])

    def test_every_id_is_prevalidated_before_runtime_factory(self):
        calls = []
        controller = SyncController(self.store, runtime_factory=lambda: calls.append('runtime'))
        result = controller.reanalyze(['1', 'missing'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(calls, [])
        self.assertEqual(self.store.list_proposals()[0]['status'], 'review')

    def test_lock_precedes_runtime_initialization_and_cli_database_open(self):
        calls = []
        with (self.store.path.parent/'sync.lock').open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            controller = SyncController(self.store, runtime_factory=lambda: calls.append('runtime'))
            self.assertEqual(controller.reanalyze(['1'], apply=True)['status'], 'already_running')
            from app.cli import main
            with patch('app.sync.AppStore', side_effect=AssertionError('Must not open DB before lock')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--state', str(self.store.path), 'reanalyze', '--message-id', '1'], config=TEST_CONFIG), 1)
        self.assertEqual(calls, [])

    def test_empty_analysis_keeps_reason_and_supersedes_only_target(self):
        self.store.save_message(message('2'))
        other = self.store.save_proposals('2', [proposal(start='2030-11-11T09:00:00+01:00')], NOW)[0]
        runtime = AnalysisOnlyRuntime([])
        controller = SyncController(self.store, runtime=runtime)
        result = controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['messages'][0]['proposal_count'], 0)
        self.assertEqual(self.store.get_message('1')['analysis_metadata'], METADATA)
        self.assertEqual(self.store.get_analysis_history('1')[0]['proposal_ids'], [])
        self.assertEqual(next(item for item in self.store.list_proposals() if item['id'] == other['id'])['status'], 'pending')

    def test_missing_current_metadata_cannot_reuse_previous_call(self):
        self.runtime.last_analysis_metadata = dict(METADATA)
        self.runtime.analyze = lambda source, events: []
        self.controller.reanalyze(['1'], apply=True, current=NOW)
        metadata = self.store.get_message('1')['analysis_metadata']
        self.assertIsNone(metadata['model'])
        self.assertIsNone(metadata['prompt_version'])
        self.assertIn('No proposals', metadata['decision_reason'])

    def test_failed_analysis_preserves_cached_evidence_and_health(self):
        def fail(source, events):
            raise RuntimeError('PRIVATE API RESPONSE')
        self.runtime.analyze = fail
        before = self.snapshot()
        result = self.controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(before, self.snapshot())

    def test_source_change_during_analysis_prevents_apply(self):
        def changed(source, events):
            self.store.save_message(message(text='Changed cached source'))
            return []
        self.runtime.analyze = changed
        result = self.controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.store.get_message('1')['status'], 'analysis_pending')
        self.assertEqual(len(self.store.get_analysis_history('1')), 1)

    def test_atomic_failure_rolls_back_superseding_and_metadata(self):
        before = self.snapshot()
        with patch.object(self.store, '_save_analysis', side_effect=RuntimeError('write failed')):
            result = self.controller.reanalyze(['1'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(before, self.snapshot())

    def test_web_approval_during_analysis_wins_before_replacement(self):
        other = AppStore(self.store.path, config=TEST_CONFIG)
        try:
            def approved(source, events):
                other.resolve_proposal(self.old['id'], 'approve')
                return self.runtime.proposals
            self.runtime.analyze = approved
            result = self.controller.reanalyze(['1'], apply=True, current=NOW)
            self.assertEqual(result['status'], 'partial')
            saved = next(item for item in self.store.list_proposals() if item['id'] == self.old['id'])
            self.assertTrue(saved['user_approved'])
            self.assertEqual(saved['status'], 'pending')
            self.assertEqual(len(self.store.list_proposals()), 1)
        finally:
            other.close()

    def test_replacement_reserves_sqlite_write_lock_before_eligibility_reads(self):
        other = AppStore(self.store.path, config=TEST_CONFIG)
        other.db.execute('PRAGMA busy_timeout=25')
        original = self.store.cached_reanalysis_scope
        attempted = []
        def concurrent_approval():
            try:
                other.resolve_proposal(self.old['id'], 'approve')
                attempted.append('approved')
            except sqlite3.OperationalError:
                attempted.append('locked')
        def check_scope(message_id):
            if self.store.db.in_transaction:
                worker = threading.Thread(target=concurrent_approval)
                worker.start()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(attempted, ['locked'])
            return original(message_id)
        try:
            with patch.object(self.store, 'cached_reanalysis_scope', side_effect=check_scope):
                result = self.controller.reanalyze(['1'], apply=True, current=NOW)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(attempted, ['locked'])
            with self.assertRaises(ValueError):
                other.resolve_proposal(self.old['id'], 'approve')
        finally:
            other.close()

    def test_waiting_web_approval_cannot_resurrect_superseded_payload(self):
        other = AppStore(self.store.path, config=TEST_CONFIG)
        original = self.store.cached_reanalysis_scope
        started = threading.Event()
        outcomes = []
        workers = []
        def approve():
            started.set()
            try:
                other.resolve_proposal(self.old['id'], 'approve')
                outcomes.append('approved')
            except ValueError:
                outcomes.append('retired')
        def check_scope(message_id):
            if self.store.db.in_transaction:
                worker = threading.Thread(target=approve)
                workers.append(worker)
                worker.start()
                self.assertTrue(started.wait(timeout=1))
            return original(message_id)
        try:
            with patch.object(self.store, 'cached_reanalysis_scope', side_effect=check_scope):
                result = self.controller.reanalyze(['1'], apply=True, current=NOW)
            for worker in workers:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(outcomes, ['retired'])
            self.assertEqual(next(item for item in self.store.list_proposals() if item['id'] == self.old['id'])['status'], 'superseded')
            self.assertEqual(self.store.status()['proposals_pending'], 1)
        finally:
            other.close()

    def test_cli_preview_and_apply_skip_librus_and_calendar(self):
        from app.cli import main
        with patch('app.librus_client.load_credentials', side_effect=AssertionError('No login')), \
                patch('app.codex_runtime.CodexRuntime', return_value=self.runtime):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--state', str(self.store.path), 'reanalyze', '--message-id', '1'], config=TEST_CONFIG), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'preview')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--state', str(self.store.path), 'reanalyze', '--message-id', '1', '--apply'], config=TEST_CONFIG), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'applied')


if __name__ == '__main__':
    unittest.main()

"""Human choices protect an exact Calendar event across later AI actions."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.core import AppStore, digest, normalize_proposal
from app.data_admin import migrate_decisions
from app.sync import SyncController
from test_core import NOW, message, proposal
from test_support import TEST_CONFIG, event_fingerprint
from test_sync import FakeClient, FakeRuntime


class HumanEventDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name) / 'state.sqlite3', config=TEST_CONFIG)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def create_event(self, mid='1', eid='event-1', approved=True, edited=False, start=None):
        self.store.save_message(message(mid))
        raw = proposal(**({'start': start} if start else {}))
        item = self.store.save_proposals(mid, [raw], NOW)[0]
        if approved:
            self.store.resolve_proposal(item['id'], 'approve', {'title': 'Human title'} if edited else None)
        operation = next(op for op in self.store.queue_operations(NOW) if op['proposal_id'] == item['id'])
        snapshot = {**operation['event'], 'id': eid}
        self.store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': eid,
            'snapshot': snapshot, 'fingerprint': event_fingerprint(snapshot)})
        return next(p for p in self.store.list_proposals() if p['id'] == item['id'])

    def new_update(self, mid='2', eid='event-1', **patch):
        self.store.save_message(message(mid))
        return self.store.save_proposals(mid, [proposal(kind='update', event_id=eid,
            title='Automatic replacement', **patch)], NOW)[0]

    def set_legacy_pending(self, item):
        raw = dict(item, status='pending', needs_review=False, review_reason='', user_approved=False)
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending',payload=? WHERE id=?",
                                  (json.dumps(raw), item['id']))
        return raw

    def insert_legacy_operation(self, item, **patch):
        operation = {'operation_id': digest(['legacy', item['id']])[:32], 'proposal_id': item['id'],
            'message_id': item['message_id'], 'action': item['kind'], 'calendar_id': TEST_CONFIG.calendar_id,
            'event_id': item['event_id'], 'user_approved': True, 'deletion_approved': True}
        operation.update(patch)
        with self.store.db:
            self.store.db.execute('INSERT INTO operations(id,proposal_id,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                (operation['operation_id'], item['id'], json.dumps(operation), 'pending', NOW.isoformat(), NOW.isoformat()))
        return operation

    def test_approved_create_protects_updates_across_source_and_preserves_edits(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                original = self.create_event(mid=str(10 + edited), eid='event-' + str(edited), edited=edited,
                                             start='2030-11-%02dT09:00:00+01:00' % (10 + edited))
                for mid in (original['message_id'], str(20 + edited)):
                    update = self.new_update(mid=mid, eid='event-' + str(edited), user_approved=True, user_edited=True)
                    self.assertEqual(update['status'], 'review')
                    self.assertFalse(update['user_approved'])
                    self.assertIn('human', update['review_reason'].lower())
                self.assertEqual(self.store.queue_operations(NOW), [])
                saved = next(p for p in self.store.list_proposals() if p['id'] == original['id'])
                self.assertEqual(saved, original)

    def test_migrated_create_protects_new_update_without_original_operation_history(self):
        original = self.create_event(edited=True)
        self.store.set_setting('librus_account_hash', digest('synthetic-account'))
        target = AppStore(Path(self.tmp.name) / 'target.sqlite3', config=TEST_CONFIG)
        try:
            migrate_decisions(target, self.store.path)
            target.set_setting('rebuild_status', 'rebuilt')
            target.save_message(message('2'))
            update = target.save_proposals('2', [proposal(kind='update', event_id='event-1')], NOW)[0]
            self.assertEqual(update['status'], 'review')
            self.assertEqual(target.queue_operations(NOW), [])
            self.assertEqual(next(p for p in target.list_proposals() if p['id'] == original['id'])['status'], 'applied')
        finally:
            target.close()

    def test_migration_preserves_original_event_identity_despite_moved_date_and_sibling_collision(self):
        self.store.save_message(message('1'))
        items = self.store.save_proposals('1', [proposal(), proposal(start='2030-11-12T09:00:00+01:00')], NOW)
        self.store.resolve_proposal(items[0]['id'], 'approve')
        for op in self.store.queue_operations(NOW):
            eid = 'human' if op['proposal_id'] == items[0]['id'] else 'automatic'
            snapshot = {**op['event'], 'id': eid}
            self.store.record_result(op['operation_id'], {'status': 'applied', 'event_id': eid,
                'snapshot': snapshot, 'fingerprint': event_fingerprint(snapshot)})
        # Older releases may have moved both dates and replaced the latest
        # association. Only the settled create result still identifies the choice.
        with self.store.db:
            for event in self.store.list_events():
                snapshot = dict(event['snapshot'], start={'dateTime': '2030-11-%sT09:00:00+01:00' % ('11' if event['id'] == 'human' else '10')})
                self.store.db.execute('UPDATE events SET proposal_id=?,snapshot=? WHERE id=?',
                    ('legacy-auto-' + event['id'], json.dumps(snapshot), event['id']))
            record = json.loads(self.store.db.execute('SELECT payload FROM decision_intents WHERE id=?', (items[0]['id'],)).fetchone()[0])
            record.pop('event_ids', None)
            self.store.db.execute('UPDATE decision_intents SET payload=? WHERE id=?', (json.dumps(record), items[0]['id']))
        self.store.set_setting('librus_account_hash', digest('synthetic-account'))
        target = AppStore(Path(self.tmp.name) / 'moved-target.sqlite3', config=TEST_CONFIG)
        try:
            migrate_decisions(target, self.store.path)
            target.set_setting('rebuild_status', 'rebuilt')
            target.save_message(message('2'))
            human = target.save_proposals('2', [proposal(kind='update', event_id='human')], NOW)[0]
            sibling = target.save_proposals('2', [proposal(kind='update', event_id='automatic')], NOW)[0]
            self.assertEqual(human['status'], 'review')
            self.assertEqual(sibling['status'], 'pending')
            record = json.loads(target.db.execute('SELECT payload FROM decision_intents WHERE id=?', (items[0]['id'],)).fetchone()[0])
            self.assertEqual(record['event_ids'], ['human'])
        finally:
            target.close()

    def test_transfer_augments_missing_event_links_without_touching_cached_analysis_or_journals(self):
        original = self.create_event(edited=True)
        self.store.set_setting('librus_account_hash', digest('synthetic-account'))
        target = AppStore(Path(self.tmp.name) / 'augmented.sqlite3', config=TEST_CONFIG)
        try:
            migrate_decisions(target, self.store.path)
            target.set_setting('rebuild_status', 'rebuilt')
            for mid in map(str, range(100, 126)):
                target.save_message(message(mid))
                target.save_proposals(mid, [], NOW)
            with target.db:
                record = json.loads(target.db.execute('SELECT payload FROM decision_intents WHERE id=?', (original['id'],)).fetchone()[0])
                record.pop('event_ids')
                target.db.execute('UPDATE decision_intents SET payload=? WHERE id=?', (json.dumps(record), original['id']))
                target.db.execute('INSERT INTO operations(id,proposal_id,payload,status,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                    ('uncertain-target-only', 'unresolved-proposal', json.dumps({'immutable': 'synthetic unknown intent'}),
                     'unknown', 1, NOW.isoformat(), NOW.isoformat()))
            tables = ('messages', 'analysis_audit', 'proposals', 'operations', 'events', 'metadata_refresh')
            before = {table: [tuple(row) for row in target.db.execute('SELECT * FROM ' + table)] for table in tables}
            migrate_decisions(target, self.store.path)
            after = {table: [tuple(row) for row in target.db.execute('SELECT * FROM ' + table)] for table in tables}
            self.assertEqual(after, before)
            self.assertEqual(target.status()['messages'], 26)
            record = json.loads(target.db.execute('SELECT payload FROM decision_intents WHERE id=?', (original['id'],)).fetchone()[0])
            self.assertEqual(record['event_ids'], ['event-1'])
            self.assertEqual(target._setting('rebuild_status'), 'rebuilt')
        finally:
            target.close()

    def test_automatic_event_still_updates_and_human_sibling_does_not_protect_it(self):
        self.store.save_message(message('1'))
        items = self.store.save_proposals('1', [proposal(), proposal(start='2030-11-11T09:00:00+01:00')], NOW)
        self.store.resolve_proposal(items[0]['id'], 'approve')
        for op in self.store.queue_operations(NOW):
            eid = 'human' if op['proposal_id'] == items[0]['id'] else 'automatic'
            snapshot = {**op['event'], 'id': eid}
            self.store.record_result(op['operation_id'], {'status': 'applied', 'event_id': eid,
                'snapshot': snapshot, 'fingerprint': event_fingerprint(snapshot)})
        update = self.new_update(eid='automatic')
        self.assertEqual(update['status'], 'pending')
        self.assertEqual(len(self.store.queue_operations(NOW)), 1)

    def test_historical_create_and_cross_source_update_intents_survive_reassociation(self):
        original = self.create_event()
        # A later legacy automatic update changed the latest proposal association.
        with self.store.db:
            self.store.db.execute("UPDATE events SET proposal_id='legacy-auto' WHERE id='event-1'")
        update = self.new_update()
        self.assertEqual(update['status'], 'review')
        self.store.resolve_proposal(update['id'], 'approve')
        op = self.store.queue_operations(NOW)[0]
        snapshot = dict(self.store.list_events()[0]['snapshot'], summary='Latest approved change')
        self.store.record_result(op['operation_id'], {'status': 'applied', 'event_id': 'event-1',
            'snapshot': snapshot, 'fingerprint': event_fingerprint(snapshot)})
        with self.store.db:
            self.store.db.execute("UPDATE events SET proposal_id='other-auto',message_id='unrelated' WHERE id='event-1'")
            self.store.db.execute('DELETE FROM proposals WHERE id=?', (update['id'],))
            self.store.db.execute('DELETE FROM operations WHERE proposal_id IN (?,?)', (original['id'], update['id']))
        later = self.new_update(mid='3')
        self.assertEqual(later['status'], 'review')

    def test_current_explicit_update_or_cancel_approval_allows_only_that_action(self):
        self.create_event(edited=True)
        update = self.new_update()
        self.assertEqual(update['status'], 'review')
        approved = self.store.resolve_proposal(update['id'], 'approve')
        self.assertEqual(approved['status'], 'pending')
        operation = self.store.queue_operations(NOW)[0]
        self.assertEqual(operation['expected_fingerprint'], self.store.list_events()[0]['fingerprint'])
        self.store.set_writes_paused(False)
        self.assertTrue(self.store.claim_operation(operation['operation_id']))
        self.store.record_result(operation['operation_id'], {'status': 'failed', 'error': 'No external write.'})
        self.store.resolve_proposal(update['id'], 'ignore')
        self.store.save_message(message('3'))
        cancel = self.store.save_proposals('3', [proposal(kind='cancel', event_id='event-1',
            user_approved=True, deletion_approved=True)], NOW)[0]
        self.assertEqual(cancel['status'], 'review')
        self.assertFalse(cancel['user_approved'])
        self.assertFalse(cancel['deletion_approved'])
        approved = self.store.resolve_proposal(cancel['id'], 'confirm_cancel')
        self.assertTrue(approved['user_approved'])
        operation = self.store.queue_operations(NOW)[0]
        self.assertTrue(self.store.claim_operation(operation['operation_id']))

    def test_paused_queue_reclassifies_existing_unapproved_pending_update(self):
        original = self.create_event(edited=True)
        update = self.set_legacy_pending(self.new_update())
        operation = self.insert_legacy_operation(update)
        self.assertTrue(self.store.status()['writes_paused'])
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(next(p for p in self.store.list_proposals() if p['id'] == update['id'])['status'], 'review')
        self.assertEqual(self.store.db.execute('SELECT status,attempts FROM operations WHERE id=?',
            (operation['operation_id'],)).fetchone()[:], ('review', 0))
        self.assertEqual(next(p for p in self.store.list_proposals() if p['id'] == original['id']), original)

    def test_claim_refuses_legacy_queued_update_using_trusted_proposal_not_operation_flags(self):
        self.create_event(edited=True)
        update = self.set_legacy_pending(self.new_update())
        operation = self.insert_legacy_operation(update)
        self.store.set_writes_paused(False)
        self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.assertEqual(self.store.db.execute('SELECT status,attempts FROM operations WHERE id=?',
            (operation['operation_id'],)).fetchone()[:], ('review', 0))
        self.assertEqual(next(p for p in self.store.list_proposals() if p['id'] == update['id'])['status'], 'review')

    def test_queue_does_not_overwrite_concurrent_explicit_approval_with_stale_review(self):
        self.create_event()
        update = self.set_legacy_pending(self.new_update())
        ui_store = AppStore(self.store.path, config=TEST_CONFIG)
        original_list = self.store.list_proposals

        def approve_after_pending_read(status=None, limit=100):
            result = original_list(status, limit)
            if status == 'pending':
                ui_store.resolve_proposal(update['id'], 'approve')
            return result

        try:
            with patch.object(self.store, 'list_proposals', side_effect=approve_after_pending_read):
                queued = self.store.queue_operations(NOW)
            self.assertEqual(len(queued), 1)
            saved = next(p for p in self.store.list_proposals() if p['id'] == update['id'])
            self.assertTrue(saved['user_approved'])
            self.assertEqual(saved['status'], 'pending')
        finally:
            ui_store.close()

    def test_queue_serializes_normalization_and_body_creation_against_ui_edits(self):
        self.create_event()
        update = self.new_update()
        self.store.resolve_proposal(update['id'], 'approve', {'title': 'First approved title'})
        ui_store = AppStore(self.store.path, config=TEST_CONFIG)
        launched, completed = threading.Event(), threading.Event()
        errors = []

        def approve():
            try:
                ui_store.resolve_proposal(update['id'], 'approve', {'title': 'Concurrent approved title'})
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        worker = threading.Thread(target=approve)

        def normalize_during_edit(item, current=None):
            result = normalize_proposal(item, current)
            if item.get('id') == update['id'] and not launched.is_set():
                launched.set()
                worker.start()
                completed.wait(0.5)
            return result

        try:
            with patch('app.core.normalize_proposal', side_effect=normalize_during_edit):
                self.store.queue_operations(NOW)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            saved = next(p for p in self.store.list_proposals() if p['id'] == update['id'])
            self.assertEqual(saved['title'], '[Librus] Concurrent approved title')
            self.assertTrue(saved['user_approved'])
            latest = self.store.queue_operations(NOW)[0]
            self.assertEqual(latest['event']['summary'], saved['title'])
        finally:
            if worker.is_alive():
                worker.join(15)
            ui_store.close()

    def test_claim_refuses_stale_operation_target_or_action_after_new_approval(self):
        self.create_event(eid='event-1')
        self.create_event(mid='3', eid='event-2', start='2030-11-11T09:00:00+01:00')
        update = self.new_update()
        self.store.resolve_proposal(update['id'], 'approve')
        self.store.set_writes_paused(False)
        for changed in ({'event_id': 'event-2'}, {'action': 'cancel'}):
            with self.subTest(changed=changed):
                self.store.resolve_proposal(update['id'], 'approve')
                operation = self.insert_legacy_operation(update, **changed)
                self.assertFalse(self.store.claim_operation(operation['operation_id']))
                self.assertEqual(self.store.db.execute('SELECT status,attempts FROM operations WHERE id=?',
                    (operation['operation_id'],)).fetchone()[:], ('review', 0))

    def test_sync_never_executes_cached_payload_after_ui_reapproves_new_content(self):
        self.create_event()
        update = self.new_update()
        self.store.resolve_proposal(update['id'], 'approve', {'title': 'First approved title'})
        self.store.queue_operations(NOW)
        self.store.set_writes_paused(False)
        ui_store = AppStore(self.store.path, config=TEST_CONFIG)
        original_pending = self.store.pending_operations

        def reapprove_after_operation_read(limit=100):
            cached = original_pending(limit)
            ui_store.resolve_proposal(update['id'], 'approve', {'title': 'Latest approved title'})
            ui_store.queue_operations(NOW)
            return cached

        runtime = FakeRuntime(propose=False, outcome='failed')
        try:
            with patch.object(self.store, 'pending_operations', side_effect=reapprove_after_operation_read):
                SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
            self.assertEqual(runtime.executed, [])
            latest = self.store.pending_operations()[0]
            self.assertEqual(latest['event']['summary'], '[Librus] Latest approved title')
            self.assertEqual(self.store.db.execute('SELECT attempts FROM operations WHERE id=?',
                (latest['operation_id'],)).fetchone()[0], 0)
            SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
            self.assertEqual(len(runtime.executed), 1)
            self.assertEqual(runtime.executed[0]['event']['summary'], '[Librus] Latest approved title')
        finally:
            ui_store.close()


if __name__ == '__main__':
    unittest.main()

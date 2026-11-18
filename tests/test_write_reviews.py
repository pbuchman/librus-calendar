"""Durable handling of a definite create whose verification finds an edit."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core import AppStore
from app.codex_runtime import RuntimeFailure
from test_core import NOW, message, proposal
from test_support import CodexRuntime, TEST_CONFIG


class Calendar(CodexRuntime):
    def __init__(self):
        super().__init__()
        self.event = None
        self.writes = []
        self.read_error = None

    def _tool(self, name, args):
        if name == 'search_events':
            return {'events': []}
        if name == 'read_event':
            if self.read_error:
                raise self.read_error
            assert args['event_id'] == self.event['id']
            return {'event': deepcopy(self.event)}
        if name not in ('create_event', 'update_event'):
            raise AssertionError(name)
        eid = 'fictional-' + str(len(self.writes) + 1) if name == 'create_event' else args['event_id']
        self.writes.append((name, eid))
        event = {'id': eid, 'summary': args['title'], 'description': args['description'],
                 'location': args['location'], 'reminders': args['reminders'],
                 'start': {'date': args['start_date']}, 'end': {'date': args['end_date']}}
        self.event = deepcopy(event)
        if name == 'create_event':
            self.event.update(location='Sala 12', summary='Tytuł rodzica')
        return {'event': event}


class WriteReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'state.db'
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.addCleanup(lambda: self.store.close())
        self.runtime = Calendar()
        self.store.save_message(message())
        self.proposal, = self.store.save_proposals('1', [proposal(start='2030-03-12', all_day=True)], NOW)
        self.original, = self.store.queue_operations(NOW)
        self.store.set_writes_paused(False)

    def run_write(self, operation):
        self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
        result = self.runtime.execute(operation)
        self.store.record_result(operation['operation_id'], result)
        return result

    def restart(self):
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)

    def operation_row(self):
        return self.store.db.execute('SELECT * FROM operations WHERE id=?', (self.original['operation_id'],)).fetchone()

    def approve_move(self):
        self.store.resolve_proposal(self.proposal['id'], 'approve', {'start': '2030-03-19', 'end': '2030-03-20'})
        self.store.queue_operations(NOW)
        pending, = self.store.pending_operations()
        return pending

    def test_approved_move_preserves_verified_identity_history_and_external_fields_after_restart(self):
        initial = self.run_write(self.original)
        self.assertEqual((initial['status'], initial['error']), ('review', 'write_verification_mismatch'))
        event_id = initial['event_id']
        self.restart()
        operation = self.approve_move()
        self.restart()
        self.assertEqual(self.store.pending_operations(), [operation])
        result = self.run_write(operation)
        self.assertEqual((result['status'], result['event_id']), ('applied', event_id))
        self.assertEqual(self.runtime.writes, [('create_event', event_id), ('update_event', event_id)])
        stored, = self.store.list_events()
        self.assertEqual(stored['id'], event_id)
        self.assertEqual(stored['snapshot']['start'], {'date': '2030-03-19'})
        self.assertEqual(stored['snapshot']['location'], 'Sala 12')
        self.assertEqual(stored['snapshot']['summary'], 'Tytuł rodzica')
        row = self.operation_row()
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(json.loads(row['payload'])['reviewed_create']['result'], initial)

    def test_change_after_approval_requires_fresh_review_without_another_create(self):
        initial = self.run_write(self.original)
        correction = self.approve_move()
        self.runtime.event['location'] = 'Sala 15'
        result = self.run_write(correction)
        self.assertEqual((result['status'], result['error']), ('review', 'manual_edit_conflict'))
        self.assertEqual(self.runtime.writes, [('create_event', initial['event_id'])])
        self.restart()
        self.store.resolve_proposal(self.proposal['id'], 'approve')
        correction, = self.store.pending_operations()
        self.assertEqual(self.run_write(correction)['status'], 'applied')
        self.assertEqual(self.runtime.event['location'], 'Sala 15')
        self.assertEqual(self.runtime.event['start'], {'date': '2030-03-19'})
        self.assertEqual(self.runtime.writes, [('create_event', initial['event_id']), ('update_event', initial['event_id'])])

    def test_create_id_remains_bound_when_the_verification_read_fails(self):
        self.runtime.read_error = RuntimeFailure('calendar_event_not_found')
        result = self.run_write(self.original)
        self.assertEqual((result['status'], result['event_id']), ('unknown', self.runtime.event['id']))
        self.restart()
        operation, = self.store.reconcile_operations()
        self.assertEqual(operation['event_id'], self.runtime.event['id'])
        reviewed = self.runtime.execute(operation)
        self.assertEqual(reviewed['status'], 'review')
        self.store.record_result(operation['operation_id'], reviewed)
        with self.assertRaises(ValueError):
            self.store.resolve_proposal(self.proposal['id'], 'approve', {'start': '2030-03-19', 'end': '2030-03-20'})
        self.assertEqual(self.store.list_proposals()[0]['status'], 'review')
        self.assertEqual(len(self.runtime.writes), 1)

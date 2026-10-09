from test_support import CALENDAR_ID, CodexRuntime, TEST_CONFIG, event_fingerprint
import copy
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.codex_runtime import RuntimeFailure
from app.core import AppStore, digest
from app.metadata_refresh import MetadataRefreshController
from app.sync import SyncController, SyncInterrupted
from test_core import NOW, message, proposal
from test_sync import FakeClient, FakeRuntime


class CalendarRuntime(CodexRuntime):
    def __init__(self, events):
        super().__init__()
        self.events = copy.deepcopy(events)
        self.operations, self.reads, self.writes, self.probes = [], [], [], 0
        self.failure = None

    def probe(self):
        self.probes += 1
        return {'calendar_id': CALENDAR_ID, 'access_role': 'owner'}

    def analyze(self, source, events):
        raise AssertionError('Metadata refresh must not analyze teacher messages.')

    def execute(self, operation):
        self.operations.append(copy.deepcopy(operation))
        return super().execute(operation)

    def _fetch(self, event_id, expected_event=None):
        self.reads.append(event_id)
        return copy.deepcopy(self.events[event_id])

    def _tool(self, name, args):
        if name != 'update_event':
            raise AssertionError('Metadata refresh must only update an existing event.')
        self.writes.append(copy.deepcopy(args))
        if self.failure in ('failed', 'unknown_no_write'):
            raise RuntimeFailure('calendar_tool_failed', possible_write=self.failure == 'unknown_no_write')
        updated = {**self.events[args['event_id']], 'summary': args['title'], 'description': args['description'],
                   'location': args['location'], 'reminders': {'useDefault': args['reminders']['use_default'],
                   'overrides': args['reminders']['overrides']}}
        for boundary in ('start', 'end'):
            updated[boundary] = {'date': args[boundary+'_date']} if boundary+'_date' in args else {'dateTime': args[boundary+'_time']}
        self.events[args['event_id']] = updated
        if self.failure == 'unknown_after_write':
            raise RuntimeFailure('calendar_tool_failed', possible_write=True)
        if self.failure == 'interrupted_after_write':
            raise SyncInterrupted('sync_interrupted')
        return copy.deepcopy(updated)


class MetadataRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name)/'data/state.sqlite3', config=TEST_CONFIG)
        self.store.set_writes_paused(False)
        self.originals = {}
        self.proposals = {}
        self.markers = {}
        for index in range(4):
            mid, eid = str(index), 'existing-'+str(index)
            self.store.save_message(message(mid))
            item = self.store.save_proposals(mid, [proposal(start='2030-11-%02dT09:00:00+01:00' % (10+index))], NOW)[0]
            marker = 'librus-calendar:'+digest(['old', eid])[:32]
            snapshot = {'id': eid, 'summary': '[Szkoła] Wycieczka '+mid, 'description': 'Original teacher source\n'+marker,
                        'start': {'dateTime': '2030-11-%02dT09:00:00+01:00' % (10+index)},
                        'end': {'dateTime': '2030-11-%02dT10:30:00+01:00' % (10+index)},
                        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 123}]},
                        'location': 'School room', 'visibility': 'private', 'attendees': [], 'guestsCanModify': False}
            if index > 0:
                snapshot['start'] = {'date': '2030-11-%02d' % (10+index)}
                snapshot['end'] = {'date': '2030-11-%02d' % (11+index)}
            if index == 2:
                snapshot['description'] = snapshot['description'].replace('librus-calendar:', 'librus-\ncalendar:')
            self.markers[eid] = marker
            self.originals[eid], self.proposals[eid] = copy.deepcopy(snapshot), item
            with self.store.db:
                approved = dict(item, user_approved=True, status='applied')
                self.store.db.execute('UPDATE proposals SET status=\'applied\',payload=? WHERE id=?', (json.dumps(approved), item['id']))
                self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                    (eid, CALENDAR_ID, mid, item['id'], json.dumps(snapshot), event_fingerprint(snapshot), marker, NOW.isoformat(), 'active'))
        self.runtime = CalendarRuntime(self.originals)
        self.controller = MetadataRefreshController(self.store, self.runtime)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def journal(self, event_id='existing-0'):
        return self.store.db.execute('SELECT * FROM metadata_refresh WHERE event_id=?', (event_id,)).fetchone()

    def test_preview_four_targets_never_initializes_runtime_or_writes(self):
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM events')]
        controller = MetadataRefreshController(self.store, runtime_factory=lambda: self.fail('Preview must not initialize Calendar.'))
        result = controller.run(list(self.originals), current=NOW)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(len(result['events']), 4)
        self.assertTrue(all(item['title_after'].startswith('[Librus] ') for item in result['events']))
        self.assertEqual(result['calendar_writes'], 0)
        self.assertEqual(before, [tuple(row) for row in self.store.db.execute('SELECT * FROM events')])
        self.assertEqual(list(self.store.db.execute('SELECT * FROM metadata_refresh')), [])

    def test_apply_updates_existing_ids_preserves_fields_associations_and_manual_approval(self):
        proposals_before = [tuple(row) for row in self.store.db.execute('SELECT * FROM proposals')]
        health_before = self.store.status()
        result = self.controller.run(list(self.originals), apply=True, current=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['calendar_writes'], 4)
        self.assertEqual(len(self.store.list_events()), 4)
        self.assertEqual(proposals_before, [tuple(row) for row in self.store.db.execute('SELECT * FROM proposals')])
        for event in self.store.list_events():
            remote = self.runtime.events[event['id']]
            original = self.originals[event['id']]
            self.assertEqual(event['proposal_id'], self.proposals[event['id']]['id'])
            self.assertEqual(event['marker'], self.markers[event['id']])
            self.assertTrue(remote['description'].endswith(event['marker']))
            self.assertEqual(remote['description'].count(event['marker']), 1)
            if 'date' in original['start']:
                self.assertEqual(remote['start'], original['start'])
                self.assertEqual(remote['end'], original['end'])
            self.assertEqual(remote['visibility'], 'private')
            self.assertEqual(remote['attendees'], [])
            self.assertEqual(remote['location'], original['location'])
            self.assertEqual(remote['reminders'], original['reminders'])
            self.assertEqual(remote['guestsCanModify'], False)
            expected = dict(original, summary=remote['summary'], description=remote['description'])
            self.assertEqual(event_fingerprint(expected), event_fingerprint(remote))
            self.assertEqual(event['fingerprint'], event_fingerprint(remote))
            self.assertIn('Synchronizacja rozpoczęta: 08.10.2026 12:00:00 (Europe/Warsaw)', remote['description'])
        for field in ('last_sync', 'last_attempt_at', 'last_successful_sync', 'last_result'):
            self.assertEqual(self.store.status()[field], health_before[field])

    def test_applied_rerun_reuses_timestamp_and_performs_no_calendar_calls(self):
        self.controller.run(['existing-2'], apply=True, current=NOW)
        before = tuple(self.journal('existing-2'))
        controller = MetadataRefreshController(self.store, runtime_factory=lambda: self.fail('Completed migration must not initialize Calendar.'))
        result = controller.run(['existing-2'], apply=True)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['calendar_writes'], 0)
        self.assertEqual(tuple(self.journal('existing-2')), before)

    def test_remote_manual_edit_conflict_is_review_and_no_update(self):
        self.runtime.events['existing-0']['summary'] = 'Manually changed title'
        before = self.store.list_events()
        result = self.controller.run(['existing-0'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['events'][0]['status'], 'review')
        self.assertEqual(self.runtime.writes, [])
        self.assertEqual(self.store.list_events(), before)
        self.assertEqual(self.journal()['status'], 'review')
        self.assertEqual(self.store.status()['review_count'], 1)

    def test_failed_unwritten_retry_reads_again_and_keeps_exact_frozen_intent(self):
        self.runtime.failure = 'failed'
        first = self.controller.run(['existing-0'], apply=True, current=NOW)
        self.assertEqual(first['events'][0]['status'], 'failed')
        self.assertEqual(self.journal()['status'], 'pending')
        payload = self.journal()['payload']
        self.runtime.failure = None
        self.controller.run(['existing-0'], apply=True)
        self.assertEqual(self.journal()['payload'], payload)
        self.assertEqual(self.runtime.operations[0], self.runtime.operations[1])
        self.assertEqual(self.runtime.reads, ['existing-0', 'existing-0', 'existing-0'])
        self.assertEqual(self.journal()['attempts'], 2)

    def test_unknown_after_write_reconciles_read_only_and_updates_local_snapshot(self):
        self.runtime.failure = 'unknown_after_write'
        first = self.controller.run(['existing-0'], apply=True, current=NOW)
        self.assertEqual(first['events'][0]['status'], 'unknown')
        self.assertEqual(self.store.status()['operations_unknown'], 1)
        old_payload = self.journal()['payload']
        self.runtime.failure = None
        second = self.controller.run(['existing-0'], apply=True)
        self.assertEqual(second['events'][0]['status'], 'applied')
        self.assertEqual(second['calendar_writes'], 0)
        self.assertTrue(self.runtime.operations[-1]['reconcile_only'])
        self.assertEqual(len(self.runtime.writes), 1)
        self.assertEqual(self.journal()['payload'], old_payload)
        self.assertEqual(self.store.status()['operations_unknown'], 0)

    def test_unknown_without_write_never_authorizes_blind_retry(self):
        self.runtime.failure = 'unknown_no_write'
        self.controller.run(['existing-0'], apply=True, current=NOW)
        self.runtime.failure = None
        second = self.controller.run(['existing-0'], apply=True)
        self.assertEqual(second['events'][0]['status'], 'unknown')
        self.assertEqual(len(self.runtime.writes), 1)
        self.assertTrue(self.runtime.operations[-1]['reconcile_only'])
        self.assertEqual(self.journal()['attempts'], 1)
        sync = SyncController(self.store, FakeClient(count=0), FakeRuntime(propose=False)).run(current=NOW)
        self.assertEqual(sync['status'], 'partial')
        self.assertEqual(sync['error_code'], 'operations_unknown')

    def test_interrupted_write_and_abandoned_inflight_only_reconcile(self):
        self.runtime.failure = 'interrupted_after_write'
        first = self.controller.run(['existing-0'], apply=True, current=NOW)
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(self.journal()['status'], 'unknown')
        with self.store.db:
            self.store.db.execute("UPDATE metadata_refresh SET status='inflight' WHERE event_id='existing-0'")
        self.runtime.failure = None
        self.controller.run(['existing-0'], apply=True)
        self.assertTrue(self.runtime.operations[-1]['reconcile_only'])
        self.assertEqual(len(self.runtime.writes), 1)
        self.assertEqual(self.journal()['status'], 'applied')

    def test_pause_keeps_durable_pending_intent_and_pending_health_age(self):
        self.store.set_writes_paused(True)
        result = self.controller.run(['existing-0'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(self.runtime.writes, [])
        self.assertEqual(self.store.status()['operations_pending'], 1)
        self.assertEqual(self.store.status()['oldest_pending_at'], self.journal()['created_at'])

    def test_active_metadata_inflight_counts_as_pending_not_unknown_and_lock_blocks_sync_recovery(self):
        original = self.runtime.execute
        def active(operation):
            self.assertEqual(self.journal()['status'], 'inflight')
            self.assertEqual(self.store.status()['operations_pending'], 1)
            self.assertEqual(self.store.status()['operations_unknown'], 0)
            self.assertEqual(self.store.status()['oldest_pending_at'], self.journal()['created_at'])
            attempted = SyncController(self.store, FakeClient(count=0), FakeRuntime(propose=False)).run(current=NOW)
            self.assertEqual(attempted['status'], 'already_running')
            self.assertEqual(self.journal()['status'], 'inflight')
            return original(operation)
        self.runtime.execute = active
        self.assertEqual(self.controller.run(['existing-0'], apply=True, current=NOW)['status'], 'applied')
        self.assertEqual(self.store.status()['operations_pending'], 0)

    def test_next_locked_sync_converts_abandoned_metadata_inflight_to_unknown_without_write(self):
        self.store.set_writes_paused(True)
        self.controller.run(['existing-0'], apply=True, current=NOW)
        with self.store.db:
            self.store.db.execute("UPDATE metadata_refresh SET status='inflight',attempts=1 WHERE event_id='existing-0'")
        self.assertEqual(self.store.status()['operations_unknown'], 0)
        result = SyncController(self.store, FakeClient(count=0), FakeRuntime(propose=False)).run(current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['error_code'], 'operations_unknown')
        self.assertEqual(self.journal()['status'], 'unknown')
        self.assertEqual(self.store.status()['operations_unknown'], 1)
        self.assertEqual(self.runtime.writes, [])

    def test_refuses_unowned_missing_cancelled_or_external_attendee_targets(self):
        result = self.controller.run(['existing-0', 'missing'], apply=True, current=NOW)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.runtime.probes, 0)
        self.assertIsNone(self.journal())
        for changes in ({'description': 'foreign'}, {'attendees': [{'email': 'guest@example.test'}]}):
            with self.subTest(changes=changes), self.store.db:
                snapshot = dict(self.originals['existing-0'], **changes)
                self.store.db.execute('UPDATE events SET snapshot=?,fingerprint=? WHERE id=\'existing-0\'',
                    (json.dumps(snapshot), event_fingerprint(snapshot)))
                self.assertEqual(self.controller.run(['existing-0'], apply=True)['status'], 'refused')
        with self.store.db:
            self.store.db.execute("UPDATE events SET state='cancelled' WHERE id='existing-0'")
        self.assertEqual(self.controller.run(['existing-0'], apply=True)['status'], 'refused')

    def test_lock_precedes_cli_database_open_and_runtime_initialization(self):
        from app.cli import main
        with (self.store.path.parent/'sync.lock').open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('app.metadata_refresh.AppStore', side_effect=AssertionError('DB opened before lock')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--state', str(self.store.path), 'metadata-refresh', '--event-id', 'existing-0', '--apply'], config=TEST_CONFIG), 1)
        self.assertIsNone(self.journal())


if __name__ == '__main__':
    unittest.main()

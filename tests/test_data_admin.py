"""Synthetic-only migration, retention and Calendar reconstruction evidence."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core import AppStore, digest
from app.codex_runtime import CodexRuntime, RuntimeFailure, event_fingerprint
from app.data_admin import migrate_decisions, prune_state, RebuildController
from app.health_state import read_monitoring_state
from test_support import TEST_CONFIG
from test_core import NOW, QUOTE, message, proposal
from test_sync import FakeClient, FakeRuntime


class ReadRuntime(FakeRuntime):
    read_only = True

    def __init__(self, events=(), propose=True):
        super().__init__(propose=propose)
        self.events = list(events)
        self.reads = []

    def discover_events(self):
        self.reads.append('discover')
        return self.events

    def read_event(self, event_id, expected_event=None):
        self.reads.append(event_id)
        found = next((event for event in self.events if event['id'] == event_id), None)
        if not found:
            raise RuntimeFailure('calendar_event_not_found')
        return found


class ProjectionRuntime(CodexRuntime):
    """Real read normalization around synthetic connector-shaped responses."""
    def __init__(self, hints, events):
        super().__init__(TEST_CONFIG, read_only=True)
        self.hints, self.events, self.calls = hints, events, []

    def probe(self):
        return {'calendar_id': TEST_CONFIG.calendar_id, 'access_role': 'owner'}

    def discover_events(self):
        return self.hints

    def analyze(self, source, events):
        return []

    def _tool(self, name, args):
        self.calls.append(name)
        if name != 'read_event':
            raise AssertionError('Reconstruction must only read existing events.')
        return {'event': self.events[args['event_id']]}


def owned_event(mid='0', eid='calendar-original', marker='operation123'):
    return {'id': eid, 'summary': 'A manually changed Calendar title',
            'description': 'Źródło: Librus, wiadomość ' + mid + '\nlibrus-calendar:' + marker,
            'start': {'date': '2030-11-10'}, 'end': {'date': '2030-11-11'}}


class DataAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name)/'new/state.sqlite3', config=TEST_CONFIG)
        self.old = AppStore(Path(self.tmp.name)/'old/state.sqlite3', config=TEST_CONFIG)
        self.old.set_setting('librus_account_hash', digest('synthetic-account'))

    def tearDown(self):
        self.old.close()
        self.store.close()
        self.tmp.cleanup()

    def decision(self, mid, kind='approve', patch=None, apply=False):
        self.old.save_message(message(mid))
        item = self.old.save_proposals(mid, [proposal(start='2030-11-%02dT09:00:00+01:00' % (10 + int(mid)))], NOW)[0]
        self.old.resolve_proposal(item['id'], kind, patch)
        if apply:
            op = next(op for op in self.old.queue_operations(NOW) if op['message_id'] == mid)
            snap = dict(op['event'], id='event-' + mid)
            self.old.record_result(op['operation_id'], {'status': 'applied', 'event_id': snap['id'],
                'snapshot': snap, 'fingerprint': event_fingerprint(snap, TEST_CONFIG.calendar_owner)})
        return item

    def test_direct_transfer_preserves_human_intents_not_automatic_ignore_or_bodies(self):
        self.decision('0', apply=True)
        self.decision('1', patch={'title': 'Human choice', 'start': '2030-11-11T13:00:00+01:00', 'end': None}, apply=True)
        self.decision('2', kind='ignore')
        self.old.save_message(message('3'))
        self.old.save_proposals('3', [proposal(kind='ignore')], NOW)
        before = self.old.path.read_bytes()
        result = migrate_decisions(self.store, self.old.path)
        self.assertEqual(result['decisions'], 3)
        self.assertEqual(result['approved'], 2)
        self.assertEqual(result['edited'], 1)
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['events'], 2)
        self.assertEqual(self.old.path.read_bytes(), before)
        self.assertEqual(self.store.status()['messages'], 0)
        self.assertNotIn(QUOTE, '\n'.join(row[0] for row in self.store.db.execute('SELECT payload FROM decision_intents')))
        self.assertNotIn(QUOTE, '\n'.join(row[0] for row in self.store.db.execute('SELECT snapshot FROM events')))
        self.assertEqual(migrate_decisions(self.store, self.old.path)['decisions'], 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0], 3)

    def test_identity_mismatch_refuses_before_transfer(self):
        self.decision('0')
        self.store.set_setting('librus_account_hash', digest('another-account'))
        with self.assertRaises(ValueError):
            migrate_decisions(self.store, self.old.path)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            migrate_decisions(self.store, self.store.path)

    def test_legacy_unbound_source_requires_explicit_owner_probe_and_matching_calendar(self):
        self.decision('0', apply=True)
        self.old.db.execute("DELETE FROM settings WHERE key IN ('calendar_binding','calendar_owner_binding')")
        self.old.db.commit()
        with self.assertRaises(ValueError):
            migrate_decisions(self.store, self.old.path)
        with self.assertRaises(ValueError):
            migrate_decisions(self.store, self.old.path, allow_legacy_binding=True,
                              verified_calendar={'calendar_id': TEST_CONFIG.calendar_id, 'access_role': 'writer'})
        result = migrate_decisions(self.store, self.old.path, allow_legacy_binding=True,
                                  verified_calendar={'calendar_id': TEST_CONFIG.calendar_id, 'access_role': 'owner'})
        self.assertTrue(result['legacy_binding_verified'])
        self.old.db.execute("UPDATE events SET calendar_id='different@example.test'")
        self.old.db.commit()
        with self.assertRaises(ValueError):
            migrate_decisions(self.store, self.old.path, allow_legacy_binding=True,
                              verified_calendar={'calendar_id': TEST_CONFIG.calendar_id, 'access_role': 'owner'})

    def test_transfer_preserves_confirmed_cancellation_and_inflight_metadata(self):
        self.decision('0', apply=True)
        self.old.save_message(message('4'))
        item = self.old.save_proposals('4', [proposal(kind='cancel', event_id='event-0')], NOW)[0]
        self.old.resolve_proposal(item['id'], 'confirm_cancel')
        self.old.db.execute('INSERT INTO metadata_refresh VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('refresh-id','event-0','synthetic-v1',json.dumps({'event_id':'event-0','marker':'librus-calendar:refresh123'}),
             'inflight',1,None,None,NOW.isoformat(),NOW.isoformat()))
        self.old.db.commit()
        result = migrate_decisions(self.store, self.old.path)
        self.assertEqual(result['unresolved_metadata_operations'], 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM metadata_refresh').fetchone()[0], 'unknown')
        record = json.loads(self.store.db.execute('SELECT payload FROM decision_intents WHERE id=?', (item['id'],)).fetchone()[0])
        self.assertTrue(record['proposal']['deletion_approved'])

    def test_decisions_survive_changed_ai_output_and_no_rejected_resurrection(self):
        original = self.decision('0', kind='ignore')
        migrate_decisions(self.store, self.old.path)
        self.store.save_message(message('0'))
        same = self.store.save_proposals('0', [proposal(title='Different model wording')], NOW)[0]
        self.assertTrue(same.get('user_rejected'))
        self.assertEqual(same['status'], 'ignored')
        changed = self.store.save_proposals('0', [proposal(start='2030-11-12T12:00:00+01:00')], NOW)[0]
        self.assertEqual(changed['status'], 'review')
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_unwritten_approval_retains_choice_for_an_exact_source_and_action_match(self):
        self.decision('0')
        migrate_decisions(self.store, self.old.path)
        self.store.save_message(message('0'))
        item = self.store.save_proposals('0', [proposal(title='New wording')], NOW)[0]
        self.assertTrue(item['user_approved'])
        self.assertEqual(item['title'], '[Librus] Wycieczka')
        self.assertEqual(item['status'], 'pending')
        # Transfer itself requires successful reconstruction before writes.
        self.store.set_setting('rebuild_status', 'rebuilt')
        self.assertEqual(len(self.store.queue_operations(NOW)), 1)

    def test_transfer_preserves_queue_age_and_attempted_operations_from_old_and_new_schemas(self):
        created = '2026-10-01T10:00:00+00:00'
        eligible = '2026-10-15T15:00:00+00:00'
        prepared = '2026-10-16T06:00:00+00:00'
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                source = AppStore(Path(self.tmp.name)/str(legacy)/'source.sqlite3', config=TEST_CONFIG)
                target = AppStore(Path(self.tmp.name)/str(legacy)/'target.sqlite3', config=TEST_CONFIG)
                try:
                    source.set_setting('librus_account_hash', digest('synthetic-account'))
                    with patch('app.core.utcnow', return_value=created):
                        for mid in ('0', '1'):
                            source.save_message(message(mid))
                            source.save_proposals(mid, [proposal(start='2030-11-%02dT09:00:00+01:00' % (10 + int(mid)))], NOW)
                    approved = source.list_proposals()[0]
                    with patch('app.core.utcnow', return_value=eligible):
                        source.resolve_proposal(approved['id'], 'approve')
                    with source.db:
                        source.db.execute('UPDATE proposals SET pending_since=?', (eligible,))
                    with patch('app.core.utcnow', return_value=prepared):
                        operations = source.queue_operations(NOW)
                        source.set_writes_paused(False)
                        for operation in operations:
                            self.assertTrue(source.claim_operation(operation['operation_id']))
                            source.record_result(operation['operation_id'], {'status': 'failed', 'error': 'operation_failed'})
                    if legacy:
                        with source.db:
                            source.db.execute('ALTER TABLE proposals DROP COLUMN pending_since')
                    before = source.path.read_bytes()
                    journals = [dict(row) for row in source.db.execute('SELECT * FROM operations ORDER BY id')]
                    result = migrate_decisions(target, source.path)
                    self.assertEqual(result['unresolved_operations'], 2)
                    self.assertEqual(source.path.read_bytes(), before)
                    self.assertEqual([dict(row) for row in target.db.execute('SELECT * FROM operations ORDER BY id')], journals)
                    self.assertEqual(target._setting('calendar_binding'), source._setting('calendar_binding'))
                    self.assertEqual(target._setting('calendar_owner_binding'), source._setting('calendar_owner_binding'))
                    self.assertEqual(target._setting('librus_account_hash'), source._setting('librus_account_hash'))
                    ages = [row[0] for row in target.db.execute('SELECT pending_since FROM proposals')]
                    self.assertEqual(ages, [created if legacy else eligible] * 2)
                    self.assertEqual(read_monitoring_state(target.path)['oldest_pending_at'], created if legacy else eligible)
                finally:
                    source.close()
                    target.close()

    def test_decision_only_transfer_uses_conservative_source_initialization_age(self):
        item = self.decision('0')
        baseline = self.old._setting('health_initialized_at')
        with self.old.db:
            self.old.db.execute('DELETE FROM proposals WHERE id=?', (item['id'],))
        before = self.old.path.read_bytes()
        migrate_decisions(self.store, self.old.path)
        self.assertEqual(self.old.path.read_bytes(), before)
        self.assertEqual(self.store.db.execute('SELECT pending_since FROM proposals').fetchone()[0], baseline)
        self.assertEqual(read_monitoring_state(self.store.path)['oldest_pending_at'], baseline)

    def test_rebuild_complete_over_100_preserves_calendar_edits_and_repeated_ids(self):
        self.store.set_writes_paused(False)
        runtime = ReadRuntime([owned_event()], propose=False)
        client = FakeClient(count=125)
        result = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(result['status'], 'rebuilt')
        self.assertEqual(result['fetched'], 125)
        self.assertEqual(self.store.status()['messages'], 125)
        self.assertEqual(runtime.executed, [])
        self.assertEqual(result['calendar_writes'], 0)
        event = self.store.list_events()[0]
        self.assertEqual(event['id'], 'calendar-original')
        self.assertEqual(event['snapshot']['summary'], owned_event()['summary'])
        ids = [event['id'] for event in self.store.list_events()]
        again = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(again['status'], 'rebuilt')
        self.assertEqual(ids, [event['id'] for event in self.store.list_events()])
        self.assertEqual(again['analyzed'], 0)

    def test_saved_all_day_shape_overrides_lossy_discovery_and_survives_interrupted_retry(self):
        from app.sync import SyncInterrupted
        original = owned_event()
        eid = original['id']
        self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
            (eid, TEST_CONFIG.calendar_id, '0', 'prior-proposal', json.dumps(original),
             event_fingerprint(original, TEST_CONFIG.calendar_owner), 'librus-calendar:operation123', NOW.isoformat(), 'active'))
        self.store.db.commit()
        raw = dict(original, start='2030-11-10T00:00:00', end='2030-11-11T00:00:00')
        hint = dict(raw, start={'dateTime':raw['start']}, end={'dateTime':raw['end']})
        runtime = ProjectionRuntime([hint], {eid:raw})
        runtime.analyze = lambda source, events: (_ for _ in ()).throw(SyncInterrupted('sync_interrupted'))
        first = RebuildController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(first['calendar_ambiguities'], 0)
        self.assertEqual(self.store.list_events()[0]['snapshot']['start'], original['start'])
        runtime.analyze = lambda source, events: []
        resumed = RebuildController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(resumed['status'], 'rebuilt')
        self.assertEqual(resumed['calendar_ambiguities'], 0)
        self.assertEqual(self.store.list_events()[0]['fingerprint'], event_fingerprint(original, TEST_CONFIG.calendar_owner))
        self.assertEqual(runtime.calls, ['read_event', 'read_event'])

    def test_discovered_all_day_hint_converts_actual_remote_dates_without_filling_hint_values(self):
        hint = owned_event()
        raw = dict(hint, start='2030-11-12T00:00:00', end='2030-11-13T00:00:00')
        runtime = ProjectionRuntime([hint], {hint['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'rebuilt')
        snapshot = self.store.list_events()[0]['snapshot']
        self.assertEqual(snapshot['start'], {'date':'2030-11-12'})
        self.assertEqual(snapshot['end'], {'date':'2030-11-13'})

    def test_discovered_midnight_without_temporal_kind_is_blocking_review(self):
        raw = dict(owned_event(), start='2030-11-10T00:00:00', end='2030-11-11T00:00:00')
        runtime = ProjectionRuntime([raw], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 1)
        self.assertEqual(self.store.list_events(), [])

    def test_explicit_remote_timed_kind_overrides_prior_all_day_hint(self):
        from app.data_admin import minimal_snapshot
        original = owned_event()
        self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
            (original['id'], TEST_CONFIG.calendar_id, '0', 'prior-proposal', json.dumps(minimal_snapshot(original, 'librus-calendar:operation123')),
             event_fingerprint(original, TEST_CONFIG.calendar_owner), 'librus-calendar:operation123', NOW.isoformat(), 'active'))
        self.store.db.commit()
        raw = dict(original, all_day=False, start='2030-11-10T00:00:00', end='2030-11-11T00:00:00')
        runtime = ProjectionRuntime([raw], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 0)
        self.assertEqual(self.store.list_events()[0]['snapshot']['start'], raw['start'])

    def test_explicit_discovery_timed_kind_overrides_saved_date_hint_when_read_omits_flag(self):
        original = owned_event()
        self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
            (original['id'], TEST_CONFIG.calendar_id, '0', 'prior-proposal', json.dumps(original),
             event_fingerprint(original, TEST_CONFIG.calendar_owner), 'librus-calendar:operation123', NOW.isoformat(), 'active'))
        self.store.db.commit()
        raw = dict(original, start='2030-11-10T00:00:00', end='2030-11-11T00:00:00')
        hint = dict(raw, all_day=False)
        runtime = ProjectionRuntime([hint], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 0)
        self.assertEqual(self.store.list_events()[0]['snapshot']['start'], raw['start'])

    def test_explicit_all_day_flag_with_unconverted_datetime_is_blocking_review(self):
        raw = dict(owned_event(), all_day=True, start='2030-11-10T00:00:00+01:00', end='2030-11-11T00:00:00+01:00')
        runtime = ProjectionRuntime([raw], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 1)
        self.assertEqual(self.store.list_events(), [])

    def test_missing_remote_boundary_is_blocking_and_preserves_saved_operational_dates(self):
        original = owned_event()
        self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
            (original['id'], TEST_CONFIG.calendar_id, '0', 'prior-proposal', json.dumps(original),
             event_fingerprint(original, TEST_CONFIG.calendar_owner), 'librus-calendar:operation123', NOW.isoformat(), 'active'))
        self.store.db.commit()
        raw = dict(original, start='', end='')
        runtime = ProjectionRuntime([raw], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 1)
        self.assertEqual(self.store.list_events()[0]['snapshot'], original)

    def test_new_explicit_all_day_read_is_authoritative_despite_weak_search_projection(self):
        raw = dict(owned_event(), all_day=True, start='2030-11-10T00:00:00', end='2030-11-11T00:00:00')
        hint = {key:value for key,value in raw.items() if key != 'all_day'}
        runtime = ProjectionRuntime([hint], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'rebuilt')
        self.assertEqual(self.store.list_events()[0]['snapshot']['start'], {'date':'2030-11-10'})

    def test_new_aware_midnight_event_keeps_timed_kind(self):
        raw = dict(owned_event(), start='2030-11-10T00:00:00+01:00', end='2030-11-11T00:00:00+01:00')
        runtime = ProjectionRuntime([raw], {raw['id']:raw})
        result = RebuildController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'rebuilt')
        self.assertEqual(self.store.list_events()[0]['snapshot']['start'], raw['start'])

    def test_partial_inbox_fetch_resumes_and_inconsistent_pagination_cannot_claim_success(self):
        client, runtime = FakeClient(count=125, fail_ids={'111'}), ReadRuntime(propose=False)
        result = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.store.get_message('111')['status'], 'fetch_failed')
        client.fail_ids.clear()
        resumed = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(resumed['status'], 'rebuilt')
        self.assertEqual(len(runtime.analyzed), 125)
        client.messages = lambda **kwargs: ([], 100)
        self.assertEqual(RebuildController(self.store, client, runtime).run(current=NOW)['status'], 'partial')
        prior_completion = self.store._setting('rebuild_completed_at')
        RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(self.store._setting('rebuild_completed_at'), prior_completion)

    def test_interrupted_rebuild_over_100_resumes_without_reanalyzing_completed_sources(self):
        from app.sync import SyncInterrupted
        runtime = ReadRuntime(propose=False)
        def interrupting(source, events):
            runtime.analyzed.append(source['id'])
            if len(runtime.analyzed) == 51:
                raise SyncInterrupted('sync_interrupted')
            return []
        runtime.analyze = interrupting
        client = FakeClient(count=125)
        first = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(first['analyzed'], 50)
        self.assertEqual(self.store._setting('rebuild_status'), 'interrupted')
        self.assertIsNone(self.store._setting('rebuild_completed_at'))
        completed = set(runtime.analyzed[:50])
        runtime.analyzed.clear()
        runtime.analyze = lambda source, events: runtime.analyzed.append(source['id']) or []
        second = RebuildController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(second['status'], 'rebuilt')
        self.assertEqual(second['analyzed'], 75)
        self.assertTrue(completed.isdisjoint(runtime.analyzed))
        self.assertIsNotNone(self.store._setting('rebuild_completed_at'))
        self.assertEqual(runtime.executed, [])

    def test_multiple_owned_events_same_source_do_not_collapse_and_ambiguity_stays_review(self):
        events = [owned_event(eid='one'), owned_event(eid='two', marker='operation456')]
        runtime = ReadRuntime(events)
        result = RebuildController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(result['events'], 2)
        self.assertEqual({event['id'] for event in self.store.list_events()}, {'one', 'two'})
        self.assertTrue(all(item['status'] == 'review' for item in self.store.list_proposals()))
        self.assertEqual(self.store.pending_operations(), [])
        duplicate = ReadRuntime([owned_event(eid='three'), owned_event(eid='four')], propose=False)
        result = RebuildController(self.store, FakeClient(), duplicate).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertGreater(result['calendar_ambiguities'], 0)
        self.store.save_message(message('extra'))
        self.store.save_proposals('extra', [proposal()], NOW)
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_manual_calendar_edit_remains_targeted_and_existing_review_rebase_recovers(self):
        self.decision('0', apply=True)
        remote = dict(self.old.list_events()[0]['snapshot'], summary='Preserved manual title')
        migrate_decisions(self.store, self.old.path)
        runtime = ReadRuntime([remote], propose=False)
        runtime.analyze = lambda source, events: [proposal(kind='update', event_id='event-0')] if source['id'] == '1' else []
        result = RebuildController(self.store, FakeClient(count=2), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['calendar_blocking_ambiguities'], 0)
        pending_change = next(item for item in self.store.list_proposals('review') if item['kind'] == 'update')
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.store.resolve_proposal(pending_change['id'], 'approve')
        operation = self.store.queue_operations(NOW)[0]
        executor = CodexRuntime(TEST_CONFIG)
        calls = []
        executor._tool = lambda name, args: calls.append(name) or {'event':remote}
        conflict = executor.execute(operation)
        self.assertEqual(conflict['error'], 'manual_edit_conflict')
        self.assertEqual(calls, ['read_event'])
        self.store.record_result(operation['operation_id'], conflict)
        self.store.resolve_proposal(operation['proposal_id'], 'approve')
        rebased = self.store.queue_operations(NOW)[0]
        self.assertEqual(rebased['expected_fingerprint'], event_fingerprint(remote, TEST_CONFIG.calendar_owner))

    def test_prune_preview_removes_old_bodies_but_keeps_future_and_unresolved_safety(self):
        old = (NOW - timedelta(days=31)).isoformat()
        self.store.save_message(message('0', sent_at=old))
        self.store.save_proposals('0', [proposal()], NOW)
        operation = self.store.queue_operations(NOW)[0]
        self.store.db.execute("UPDATE operations SET status='unknown',attempts=1 WHERE id=?", (operation['operation_id'],))
        self.store.db.commit()
        self.store.save_message(message('1', sent_at=old))
        self.store.save_proposals('1', [proposal()], NOW)
        self.store.save_message(message('2'))
        before = self.store.get_message('1')['text']
        preview = prune_state(self.store, current=NOW)
        self.assertEqual(preview['status'], 'preview')
        self.assertEqual(self.store.get_message('1')['text'], before)
        applied = prune_state(self.store, apply=True, current=NOW)
        self.assertEqual(applied['calendar_writes'], 0)
        self.assertEqual(self.store.get_message('0')['text'], '')
        self.assertEqual(self.store.get_message('1')['text'], '')
        self.assertEqual(self.store.get_message('2')['text'], QUOTE)
        self.assertEqual([item['id'] for item in self.store.list_messages()], ['2'])
        self.assertEqual(self.store.status()['operations_unknown'], 1)
        self.assertEqual(self.store.reconcile_operations()[0]['event'], operation['event'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM analysis_audit WHERE message_id IN (?,?)', ('0','1')).fetchone()[0], 0)
        self.assertEqual(prune_state(self.store, apply=True, current=NOW)['messages_pruned'], 0)

    def test_prune_settled_metadata_content_and_preserve_uncertain_journal(self):
        self.store.save_message(message('0', sent_at=(NOW - timedelta(days=31)).isoformat()))
        event = owned_event()
        self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
            (event['id'], TEST_CONFIG.calendar_id, '0', 'synthetic-proposal', json.dumps(event),
             event_fingerprint(event, TEST_CONFIG.calendar_owner), 'librus-calendar:operation123', NOW.isoformat(), 'active'))
        payload = {'event_id': event['id'], 'event': dict(event, description=QUOTE), 'prior_snapshot': dict(event, description=QUOTE)}
        for index, status in enumerate(('applied', 'unknown')):
            self.store.db.execute('INSERT INTO metadata_refresh VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('metadata-' + str(index),event['id'],'synthetic-' + str(index),json.dumps(payload),status,1,
                 json.dumps({'status':status,'snapshot':dict(event,description=QUOTE)}),None,NOW.isoformat(),NOW.isoformat()))
        self.store.db.commit()
        prune_state(self.store, apply=True, current=NOW)
        rows = {row['status']: row for row in self.store.db.execute('SELECT * FROM metadata_refresh')}
        self.assertNotIn(QUOTE, rows['applied']['payload'] + rows['applied']['result'])
        self.assertEqual(json.loads(rows['unknown']['payload'])['event']['description'], QUOTE)

    def test_compact_cancel_intent_is_hydrated_before_write_and_survives_unknown_deletion(self):
        self.decision('0', apply=True)
        original = self.old.list_events()[0]['snapshot']
        migrate_decisions(self.store, self.old.path)
        self.store.set_setting('rebuild_status', 'rebuilt')
        self.store.set_writes_paused(False)
        self.store.save_message(message('4'))
        item = self.store.save_proposals('4', [proposal(kind='cancel', event_id='event-0')], NOW)[0]
        self.store.resolve_proposal(item['id'], 'confirm_cancel')
        runtime = CodexRuntime(TEST_CONFIG)
        runtime.probe = lambda: {'calendar_id':TEST_CONFIG.calendar_id, 'access_role':'owner'}
        calls = []
        responses = iter((RuntimeFailure('codex_timeout'), {'event':original}, {'event':original}, RuntimeFailure('codex_timeout', possible_write=True)))
        def tool(name, args):
            calls.append(name)
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        runtime._tool = tool
        from app.sync import SyncController
        failed_read = SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(failed_read['state']['operations_pending'], 1)
        self.assertEqual(calls, ['read_event'])
        first = SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(first['state']['operations_unknown'], 1)
        operation = self.store.reconcile_operations()[0]
        self.assertEqual(operation['prior_snapshot'], original)
        runtime._tool = lambda name, args: (_ for _ in ()).throw(RuntimeFailure('calendar_event_not_found'))
        second = SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(second['state']['events_cancelled'], 1)
        self.assertEqual(second['state']['operations_unknown'], 0)
        self.assertEqual(calls.count('delete_event'), 1)

    def test_rebuild_write_gate_applies_at_claim_for_an_existing_pending_operation(self):
        self.store.save_message(message('0'))
        self.store.save_proposals('0', [proposal()], NOW)
        operation = self.store.queue_operations(NOW)[0]
        self.store.set_writes_paused(False)
        for status in ('required','running','partial','interrupted'):
            self.store.set_setting('rebuild_status', status)
            self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.store.set_setting('rebuild_status', 'review')
        self.store.db.execute('INSERT INTO calendar_discovery_review VALUES (?,?,?,?)', ('problem','Ambiguous ownership',NOW.isoformat(),1))
        self.store.db.commit()
        self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.store.db.execute('UPDATE calendar_discovery_review SET blocking=0')
        self.store.db.commit()
        self.assertTrue(self.store.claim_operation(operation['operation_id']))

    def test_readonly_runtime_denies_writes_before_starting_a_process(self):
        calls = []
        runtime = CodexRuntime(TEST_CONFIG, read_only=True, run_process=lambda *args: calls.append(args))
        for name in ('create_event', 'update_event', 'delete_event', 'unrecognized_tool'):
            with self.assertRaises(RuntimeFailure):
                runtime._tool(name, {})
        self.assertEqual(calls, [])
        args = runtime._args('/tmp/synthetic', 'tool.json', ('search_events',))
        self.assertIn('apps.connector_synthetic.destructive_enabled=false', args)

    def test_calendar_pagination_is_complete_and_repeating_tokens_are_refused(self):
        runtime = CodexRuntime(TEST_CONFIG, read_only=True)
        calls = []
        def pages(name, args):
            calls.append((name, dict(args)))
            if args.get('next_page_token'):
                return {'events': [owned_event('100', 'event-100', 'operation100')], 'next_page_token': None}
            return {'events': [owned_event(str(i), 'event-' + str(i), 'operation%03d' % i) for i in range(100)], 'next_page_token': 'second-page'}
        runtime._tool = pages
        self.assertEqual(len(runtime.discover_events()), 101)
        self.assertEqual(len(calls), 4)
        runtime._tool = lambda name, args: {'events': [], 'next_page_token': 'same'}
        with self.assertRaises(RuntimeFailure):
            runtime.discover_events()
        runtime._tool = lambda name, args: {'events': [owned_event(str(i), str(i)) for i in range(100)]}
        with self.assertRaises(RuntimeFailure):
            runtime.discover_events()

    def test_read_event_preserves_all_day_and_dst_offset_without_inventing_dates(self):
        runtime = CodexRuntime(TEST_CONFIG, read_only=True)
        runtime._tool = lambda name, args: {'event': {'id':'all-day', 'start':'2026-10-25T00:00:00', 'end':'2026-10-26T00:00:00'}}
        all_day = runtime.read_event('all-day', {'start_date':'2026-10-25','end_date':'2026-10-26'})
        self.assertEqual(all_day['start'], {'date':'2026-10-25'})
        runtime._tool = lambda name, args: {'event': {'id':'timed', 'start':'2026-10-25T02:30:00+01:00', 'end':'2026-10-25T03:30:00+01:00'}}
        timed = runtime.read_event('timed', {'all_day': False})
        self.assertEqual(timed['start'], '2026-10-25T02:30:00+01:00')


if __name__ == '__main__':
    unittest.main()

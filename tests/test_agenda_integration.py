"""Whole-agenda integration against SQLite and a fictional Calendar."""
import copy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from datetime import timedelta
from unittest.mock import patch

from test_agenda import AgendaTests, NOW, action, item, plan
from app.agenda import AgendaPlanner, assemble_agenda_snapshot, materialize_plan, agenda_prompt
from app.codex_runtime import event_fingerprint, RuntimeFailure
from app.sync import SyncController
from test_support import TEST_CONFIG


class Calendar:
    def __init__(self, select):
        self.select = select
        self.calls = 0
        self.events = {}
        self.writes = []
        self.timeout_after_write = False
        self.on_plan = None
        self.last_agenda_metadata = {'model': 'gpt-6.1-sol', 'effort': 'high', 'prompt_version': 'fictional'}

    def probe(self):
        return {'calendar_id': TEST_CONFIG.calendar_id, 'access_role': 'owner'}

    def read_event(self, eid, expected=None):
        if eid not in self.events:
            raise RuntimeFailure('calendar_event_not_found')
        return copy.deepcopy(self.events[eid])

    def plan_agenda(self, snapshot):
        self.calls += 1
        result = plan(snapshot, self.select(snapshot))
        if self.on_plan:
            self.on_plan()
        return result

    def execute(self, operation):
        eid = operation.get('event_id') or 'fictional-' + operation['operation_id']
        if operation.get('reconcile_only'):
            found = next((event for event in self.events.values() if operation['marker'] in event['description']), None)
            return {'status': 'applied', 'event_id': found['id'], 'snapshot': found,
                    'fingerprint': event_fingerprint(found), 'reconciled': True} if found else {'status': 'unknown'}
        event = ({**self.events[eid], 'status': 'cancelled'} if operation['action'] == 'cancel'
                 else {**operation['event'], 'id': eid})
        self.events[eid] = event
        self.writes.append(operation)
        if self.timeout_after_write:
            self.timeout_after_write = False
            return {'status': 'unknown', 'error': 'codex_timeout'}
        return {'status': 'applied', 'event_id': eid, 'snapshot': event, 'fingerprint': event_fingerprint(event)}


class AgendaIntegrationTests(AgendaTests):
    def ready(self):
        with self.store.db:
            self.store.db.execute("UPDATE settings SET value='ok' WHERE key='source_last_collection_result'")
            for kind in ('message', 'announcement', 'school_event', 'substitution'):
                self.store.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',
                    (kind, NOW.isoformat(), NOW.isoformat(), 'ok', None, 1))
        return self.store

    def apply(self, select):
        runtime = Calendar(select)
        self.ready()
        planner = AgendaPlanner(self.store, runtime)
        result = planner.run(NOW)
        return result, runtime, planner

    def execute_pending(self, runtime, planner):
        queued = self.store.queue_operations(NOW)
        for operation in self.store.pending_operations():
            self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
            self.store.record_result(operation['operation_id'], runtime.execute(operation),
                                     agenda_calendar=planner.calendar, current=NOW)
        return queued

    def approved_cancel(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.store.set_writes_paused(False)
        self.execute_pending(runtime, planner)
        target = self.store.list_events()[0]['id']
        aid = result['items'][0]['action_id']
        runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=aid,
                                               review_change='cancel', action=None)]
        snapshot = self.snapshot(runtime.events)
        cancellation = materialize_plan(self.store, snapshot, plan(snapshot, runtime.select(snapshot)),
                                        runtime.events, current=NOW)['items'][0]
        self.store.resolve_proposal(cancellation['proposal_id'], 'confirm_cancel')
        return aid, cancellation['proposal_id'], target, runtime, planner

    def test_approved_pending_cancel_survives_duplicate_model_review_and_queues_once(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        before = tuple(self.store.db.execute('SELECT * FROM decision_intents WHERE id=?', (pid,)).fetchone())
        runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=aid,
            review_change='cancel', action=dict(action(), title='Opis modelu zmieniony',
                                              description='Inne uzasadnienie odwołania.', optional=True))]
        repeated = planner.run(NOW)['items'][0]
        self.assertEqual((repeated['operation'], repeated['proposal_id']), ('link', pid))
        retained = next(p for p in self.store.list_proposals() if p['id'] == pid)
        self.assertEqual(retained['status'], 'pending')
        self.assertTrue(retained['user_approved'] and retained['deletion_approved'])
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM decision_intents WHERE id=?', (pid,)).fetchone()), before)
        self.assertEqual(self.store.get_action(aid)['proposal_id'], pid)
        queued = self.store.queue_operations(NOW)
        self.assertEqual([(op['action'], op['proposal_id'], op['event_id']) for op in queued], [('cancel', pid, target)])
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(planner.run(NOW)['status'], 'unchanged')
        self.assertEqual(len(self.store.pending_operations()), 1)
        operation = self.store.pending_operations()[0]
        self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
        self.store.record_result(operation['operation_id'], runtime.execute(operation), agenda_calendar=planner.calendar, current=NOW)
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(len([op for op in runtime.writes if op['action'] == 'cancel']), 1)
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual(self.store.list_events(include_cancelled=True)[0]['id'], target)
        calls = runtime.calls
        self.assertEqual(planner.run(NOW)['status'], 'unchanged')
        self.assertEqual(runtime.calls, calls)

    def test_changed_source_does_not_reuse_pending_cancel_approval(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='new-material-information' WHERE kind='announcement'")
        revised = planner.run(NOW)['items'][0]
        self.assertEqual(revised['operation'], 'review')
        self.assertNotEqual(revised['proposal_id'], pid)
        self.assertFalse(self.store.get_action(aid)['payload']['deletion_approved'])
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertIsNotNone(self.store.db.execute('SELECT 1 FROM decision_intents WHERE id=?', (pid,)).fetchone())

    def test_external_edit_does_not_reuse_pending_cancel_approval(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        runtime.events[target]['summary'] = 'Ręczna zmiana Calendar'
        revised = planner.run(NOW)['items'][0]
        self.assertEqual(revised['operation'], 'review')
        self.assertNotEqual(revised['proposal_id'], pid)
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_changed_owned_target_does_not_reuse_pending_cancel_approval(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        replacement = 'fictional-replacement-target'
        runtime.events[replacement] = dict(runtime.events.pop(target), id=replacement)
        with self.store.db:
            self.store.db.execute('UPDATE events SET id=?,snapshot=? WHERE id=?',
                                  (replacement, json.dumps(runtime.events[replacement]), target))
        revised = planner.run(NOW)['items'][0]
        self.assertEqual(revised['operation'], 'review')
        self.assertNotEqual(revised['proposal_id'], pid)
        self.assertEqual(self.store.get_action(aid)['payload']['event_id'], replacement)
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_different_change_kind_does_not_inherit_cancel_approval(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=aid,
            review_change='update', action=dict(action(), title='Nowa czynność'))]
        revised = planner.run(NOW)['items'][0]
        self.assertEqual(revised['operation'], 'review')
        self.assertNotEqual(revised['proposal_id'], pid)
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_unknown_cancel_write_cannot_reuse_or_bypass_pending_approval(self):
        aid, pid, target, runtime, planner = self.approved_cancel()
        operation = self.store.queue_operations(NOW)[0]
        self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
        self.store.record_result(operation['operation_id'], {'status': 'unknown'}, agenda_calendar=planner.calendar, current=NOW)
        revised = planner.run(NOW)['items'][0]
        self.assertEqual(revised['operation'], 'review')
        with self.assertRaisesRegex(ValueError, 'prior write'):
            self.store.resolve_proposal(revised['proposal_id'], 'confirm_cancel')
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_three_sources_one_action_review_is_local_and_replay_skips_inference(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot),
            item(snapshot, 'review', review_change='create', action=None, reason='Odbiorca lub data wymaga decyzji.')])
        self.assertEqual(result['model_calls'], 1)
        self.assertEqual(len(self.store.list_actions()), 2)
        clear = next(entry for entry in result['items'] if entry['operation'] == 'create')
        self.assertEqual(len(self.store.get_action(clear['action_id'])['source_links']), 3)
        self.store.set_writes_paused(False)
        self.assertEqual(len(self.execute_pending(runtime, planner)), 1)
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual(planner.run(NOW)['status'], 'unchanged')
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(self.store.agenda_status()['latest_plan']['status'], 'applied')

    def test_omitted_legacy_pending_requires_review_but_explicit_approval_keeps_ids(self):
        raw = dict(action(), kind='create', confidence='high', needs_review=False,
                   source_quote='Klasa 1x przynosi papier do 12 marca.')
        legacy, approved = self.store.save_proposals('fictional-message',
            [raw, dict(raw, title='Drugi obowiązek', start='2030-03-13')], NOW)
        self.store.resolve_proposal(approved['id'], 'approve')
        before_ids = {entry['id'] for entry in self.store.list_actions()}
        self.assertNotIn('canonical_action_id', legacy)
        self.apply(lambda snapshot: [item(snapshot, 'ignore', action=None)])
        queued = self.store.queue_operations(NOW)
        self.assertEqual([operation['proposal_id'] for operation in queued], [approved['id']])
        proposals = {entry['id']: entry for entry in self.store.list_proposals()}
        self.assertEqual(proposals[legacy['id']]['status'], 'review')
        self.assertEqual(proposals[approved['id']]['status'], 'pending')
        self.assertTrue(proposals[approved['id']]['user_approved'])
        self.assertEqual({entry['id'] for entry in self.store.list_actions()}, before_ids)
        self.assertEqual(set(proposals), {legacy['id'], approved['id']})

    def test_omitted_legacy_already_queued_operation_cannot_be_claimed(self):
        raw = dict(action(), kind='create', confidence='high', needs_review=False,
                   source_quote='Klasa 1x przynosi papier do 12 marca.')
        legacy = self.store.save_proposals('fictional-message', [raw], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        self.apply(lambda snapshot: [item(snapshot, 'ignore', action=None)])
        self.store.set_writes_paused(False)
        self.assertFalse(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
        self.assertEqual(self.store.list_proposals()[0]['id'], legacy['id'])
        self.assertEqual(self.store.list_proposals()[0]['status'], 'review')
        saved = self.store.db.execute('SELECT id,status,attempts FROM operations').fetchone()
        self.assertEqual(tuple(saved), (operation['operation_id'], 'review', 0))
        self.assertEqual(self.store.list_events(), [])

    def test_fresh_link_refreshes_unwritten_refs_without_replacing_human_intent(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        original = result['items'][0]
        self.store.resolve_proposal(original['proposal_id'], 'approve', {'title': 'Tytuł wybrany przez rodzica'})
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='fresh-announcement' WHERE kind='announcement'")
        self.assertEqual(self.store.queue_operations(NOW), [])
        before = self.store.list_proposals()[0]
        runtime.select = lambda snapshot: [item(snapshot, 'link', action_id=original['action_id'], action=None)]
        linked = planner.run(NOW)
        after = self.store.list_proposals()[0]
        self.assertEqual(linked['items'][0]['proposal_id'], original['proposal_id'])
        self.assertEqual(after['id'], before['id'])
        for key in ('title', 'user_approved', 'user_edited', 'status'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(len(after['agenda_sources']), 3)
        self.assertEqual(next(ref['source_hash'] for ref in after['agenda_sources']
                              if ref['source_id'].startswith('announcement:')), 'fresh-announcement')
        # A link alone does not clear a review created by the stale-source guard.
        self.assertEqual(after['status'], 'review')
        self.store.resolve_proposal(after['id'], 'approve')
        self.assertEqual([op['proposal_id'] for op in self.store.queue_operations(NOW)], [before['id']])
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertEqual(runtime.writes, [])

    def test_preview_isolated_and_failed_plan_has_safe_separate_status(self):
        self.ready()
        tables = ('proposals', 'canonical_actions', 'decision_intents', 'operations', 'proposal_groups', 'action_sources')
        before = {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)] for table in tables}
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        planner = AgendaPlanner(self.store, runtime)
        self.assertEqual(planner.run(NOW, preview=True)['status'], 'preview')
        self.assertEqual(before, {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)] for table in tables})
        self.assertIsNone(self.store.agenda_status()['latest_plan'])
        self.assertEqual(self.store.agenda_status()['latest_preview']['status'], 'preview')
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='new-input-for-failure' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'link', action_id='foreign', action=None)]
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_action_reference_invalid'):
            planner.run(NOW)
        self.assertEqual(self.store.pending_operations(), [])
        status = self.store.agenda_status()
        self.assertEqual(status['latest_plan']['error_code'], 'agenda_action_reference_invalid')
        self.assertNotIn('payload', status['latest_plan'])

    def test_exact_snapshot_preview_reused_by_preview_and_active_without_more_inference(self):
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        planner = AgendaPlanner(self.store, runtime)
        self.assertEqual(planner.run(NOW, preview=True)['model_calls'], 1)
        self.assertEqual(planner.run(NOW, preview=True)['model_calls'], 0)
        self.assertEqual(self.store.list_actions(), [])
        applied = planner.run(NOW)
        self.assertEqual(applied['model_calls'], 0)
        self.assertTrue(applied['reused'])
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertEqual(runtime.calls, 1)

    def test_concurrent_decision_invalidates_plan_without_any_materialization(self):
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        runtime.on_plan = lambda: self.store.set_setting('source_learner_context', json.dumps({'id': 'fictional-child', 'class_id': '1x', 'role': 'changed'}))
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_stale'):
            AgendaPlanner(self.store, runtime).run(NOW)
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.pending_operations(), [])

    def test_materialization_failure_rolls_back_plan_and_all_group_members(self):
        self.ready()
        snapshot = self.snapshot()
        orphan = item(snapshot, 'review', review_change='create', new_offer_key='incomplete-offer')
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_group_reference_invalid'):
            materialize_plan(self.store, snapshot, plan(snapshot, [item(snapshot), orphan]), {}, current=NOW)
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_plans').fetchone()[0], 0)

    def test_own_timeout_reconciles_without_second_create_and_skips_next_plan(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        unknown = self.store.reconcile_operations()[0]
        outcome = runtime.execute(unknown)
        self.assertTrue(outcome['reconciled'])
        self.store.record_result(unknown['operation_id'], outcome, agenda_calendar=planner.calendar, current=NOW)
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual(planner.run(NOW)['status'], 'unchanged')
        self.assertEqual(runtime.calls, 1)

    def test_external_event_edit_forces_review_but_link_preserves_human_hours(self):
        event = self.inventory()
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot, 'link', action_id='existing-action', action=None)])
        runtime.events[event['id']] = event
        planner = AgendaPlanner(self.store, runtime)
        linked = planner.run(NOW)
        self.assertEqual(linked['items'][0]['proposal_id'], 'existing-proposal')
        self.assertEqual(self.store.db.execute('SELECT snapshot FROM events').fetchone()[0], json.dumps(event))
        runtime.events[event['id']]['summary'] = 'Rodzic zmienił tytuł'
        runtime.select = lambda snapshot: [item(snapshot, 'update', action_id='existing-action')]
        revised = planner.run(NOW)
        self.assertEqual(revised['items'][0]['operation'], 'review')
        self.assertEqual(self.store.queue_operations(NOW), [])
        # Old event's proposal ID remains history; one canonical action survives.
        self.assertEqual(len(self.snapshot(runtime.events)['agenda']), 1)

    def test_new_offer_five_choices_selects_one_and_closed_choice_is_not_revived(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, 'review', review_change='create',
            action=dict(action(), activity_scope='extracurricular', temporal_kind='event', start='2030-03-%02d' % day),
            new_offer_key='fictional-one-of-five') for day in range(12, 17)])
        group = self.store.list_proposal_groups()[0]
        self.assertEqual(len(group['members']), 5)
        chosen = group['members'][0]['id']
        self.store.resolve_proposal_group(group['id'], 'select', chosen, expected_revision=group['revision'], current=NOW)
        self.store.set_writes_paused(False)
        self.assertEqual(len(self.execute_pending(runtime, planner)), 1)
        runtime.select = lambda snapshot: [item(snapshot, 'review', review_change='create', offer_group_id=group['id'])]
        held = planner.run(NOW)
        self.assertEqual(held['items'][0]['operation'], 'ignore')
        self.assertEqual(len(self.store.list_proposal_groups()), 1)
        self.assertEqual(len(self.store.queue_operations(NOW)), 0)
        self.assertEqual(len(runtime.writes), 1)

    def test_selected_offer_changes_require_approval_and_keep_closed_membership(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, 'review', review_change='create',
            action=dict(action(), activity_scope='extracurricular', temporal_kind='event', start='2030-03-%02d' % day),
            new_offer_key='selected-offer') for day in (12, 13)])
        group = self.store.list_proposal_groups()[0]
        chosen = group['members'][0]['id']
        aid = next(entry['action_id'] for entry in result['items'] if entry['proposal_id'] == chosen)
        self.store.resolve_proposal_group(group['id'], 'select', chosen, expected_revision=group['revision'], current=NOW)
        self.store.set_writes_paused(False)
        self.execute_pending(runtime, planner)
        eid = self.store.list_events()[0]['id']
        original_group = [tuple(row) for row in self.store.db.execute('SELECT * FROM proposal_groups')]
        original_members = [tuple(row) for row in self.store.db.execute('SELECT * FROM proposal_group_members')]
        for change in ('update', 'cancel'):
            with self.subTest(change=change):
                with self.store.db:
                    self.store.db.execute('UPDATE sources SET content_hash=? WHERE kind=?', ('changed-' + change, 'announcement'))
                runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=aid, offer_group_id=group['id'],
                    review_change=change, action=None if change == 'cancel' else dict(action(), title='Nowy opis wybranego terminu', activity_scope='extracurricular', temporal_kind='event'))]
                revised = planner.run(NOW)['items'][0]
                self.assertEqual(revised['operation'], 'review')
                self.assertEqual(self.store.queue_operations(NOW), [])
                self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM proposal_groups')], original_group)
                self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM proposal_group_members')], original_members)
                decision = 'confirm_cancel' if change == 'cancel' else 'approve'
                self.store.resolve_proposal(revised['proposal_id'], decision)
                self.store.resolve_proposal(revised['proposal_id'], decision)
                queued = self.execute_pending(runtime, planner)
                self.assertEqual([(op['action'], op['event_id']) for op in queued], [(change, eid)])
                self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(len(runtime.writes), 3)
        self.assertEqual([op['event_id'] for op in runtime.writes[1:]], [eid, eid])

    def test_rejected_offer_cannot_admit_selected_change_path(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, 'review', review_change='create',
            action=dict(action(), activity_scope='extracurricular', temporal_kind='event', start='2030-03-%02d' % day),
            new_offer_key='rejected-offer') for day in (12, 13)])
        group = self.store.list_proposal_groups()[0]
        self.store.resolve_proposal_group(group['id'], 'reject', expected_revision=group['revision'], current=NOW)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='changed-rejected-offer' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=result['items'][0]['action_id'],
            offer_group_id=group['id'], review_change='cancel', action=None)]
        self.assertEqual(planner.run(NOW)['items'][0]['operation'], 'ignore')
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(self.store.list_proposal_groups()[0]['status'], 'rejected')
        self.assertEqual(runtime.writes, [])

    def test_unknown_old_revision_blocks_new_revision_and_cannot_be_approved(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        aid = result['items'][0]['action_id']
        runtime.select = lambda snapshot: [item(snapshot, 'review', action_id=aid, review_change='update')]
        # A separate changed input triggers a plan while the old create is unknown.
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='changed-version' WHERE kind='announcement'")
        newer = planner.run(NOW)['items'][0]
        with self.assertRaisesRegex(ValueError, 'prior write'):
            self.store.resolve_proposal(newer['proposal_id'], 'approve')
        self.assertEqual(len(self.store.reconcile_operations()), 1)
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(len(self.snapshot()['agenda']), 1)

    def test_existing_group_member_cannot_escape_membership_via_new_proposal(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, 'review', review_change='create',
            action=dict(action(), activity_scope='extracurricular', temporal_kind='event', start='2030-03-%02d' % day),
            new_offer_key='offer') for day in (12, 13)])
        target = result['items'][0]['action_id']
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='new-version' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'update', action_id=target)]
        newer = planner.run(NOW)['items'][0]
        with self.assertRaisesRegex(ValueError, 'existing offer'):
            self.store.resolve_proposal(newer['proposal_id'], 'approve')
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?", (newer['proposal_id'],))
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_realistic_budget_growth_and_changed_input_remains_bounded(self):
        # Representative current legacy inventory, with full source bodies kept.
        with self.store.db:
            for index in range(32):
                raw = dict(action(), title='Fikcyjny istniejący obowiązek %d' % index,
                           description='Opis zachowany. ' * 35, user_approved=True, source_quote='Historyczny cytat. ' * 20)
                self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                    ('legacy-%d' % index, 'legacy-proposal-%d' % index, json.dumps(raw), 'applied', NOW.isoformat()))
            row = self.store.db.execute("SELECT id,payload FROM sources WHERE kind='announcement'").fetchone()
            payload = json.loads(row['payload'])
            payload['text'] = 'Pełny fikcyjny tekst. ' * 3000
            self.store.db.execute('UPDATE sources SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        self.ready()
        # Match the measured private input size using fictional padding, then
        # exercise the same two-create/seven-review growth without dropping text.
        current_size = len(agenda_prompt(self.snapshot()).encode())
        payload['text'] += 'x' * (148000 - current_size)
        with self.store.db:
            if len(payload['text']) > 90000:
                other = self.store.db.execute("SELECT id,payload FROM sources WHERE kind='school_event'").fetchone()
                other_payload = json.loads(other['payload'])
                other_payload['text'] += payload['text'][90000:]
                payload['text'] = payload['text'][:90000]
                self.store.db.execute('UPDATE sources SET payload=? WHERE id=?', (json.dumps(other_payload), other['id']))
            self.store.db.execute('UPDATE sources SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        before = len(agenda_prompt(self.snapshot()).encode())
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, action=dict(action(), title='Nowy %d' % index)) if index < 2 else
            item(snapshot, 'review', review_change='create', action=dict(action(), title='Do decyzji %d' % index)) for index in range(9)])
        after = len(agenda_prompt(self.snapshot()).encode())
        self.assertGreater(after, before)
        self.assertGreaterEqual(before, 148000)
        self.assertLess(after, 160000)
        self.store.set_writes_paused(False)
        self.assertEqual(len(self.execute_pending(runtime, planner)), 2)
        self.assertLess(len(agenda_prompt(self.snapshot(runtime.events)).encode()), 160000)
        self.assertEqual(planner.run(NOW)['model_calls'], 0)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='next-version' WHERE kind='announcement'")
        self.assertLess(len(agenda_prompt(self.snapshot(runtime.events)).encode()), 160000)
        self.assertEqual(len(self.store.list_actions()), 41)
        # One additional ordinary full source can exceed this deliberately tight
        # fixture. Fail visibly before another model call or local/Calendar write.
        sources = [source for source in self.store.list_sources() if source['kind'] != 'message']
        added = dict(sources[0], upstream_id='fictional-next-announcement', kind='announcement',
                     text='Nowa fikcyjna informacja. ' + 'x' * 2048, metadata={})
        self.store.ingest_sources({'learner': {'id': 'fictional-child', 'class_id': '1x'},
            'sources': sources + [added], 'complete_kinds': ['announcement','school_event','substitution'], 'status':'ok'}, NOW)
        calls = runtime.calls
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow'):
            planner.run(NOW)
        self.assertEqual(runtime.calls, calls)
        self.assertEqual(len(self.store.list_actions()), 41)
        self.assertEqual(len(runtime.writes), 2)
        self.assertEqual(self.store.agenda_status()['latest_plan']['status'], 'failed')
        self.assertEqual(self.store.agenda_status()['latest_plan']['error_code'], 'agenda_context_overflow')
        failed = self.store.latest_agenda_plan()
        self.assertEqual(failed['payload'], {})
        with patch('app.agenda.MAX_INPUT_BYTES', 1000000):
            from app.agenda_incremental import assemble_incremental_snapshot
            full_input_bytes = len(agenda_prompt(assemble_incremental_snapshot(
                self.store, runtime.events, current=NOW, enforce_budget=False)[0]).encode())
        self.assertEqual(failed['metadata'], {
            'input_bytes': full_input_bytes,
            'input_limit_bytes': 160000})

    def test_exact_duplicate_create_plan_fails_before_persistence_or_queue(self):
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot), item(snapshot)])
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_conflicting_operations'):
            AgendaPlanner(self.store, runtime).run(NOW)
        self.assertEqual(self.store.list_actions(), [])
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_complete_local_post_plan_state_has_no_prompt_byte_budget(self):
        from app.agenda_incremental import assemble_incremental_snapshot
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        original_bytes = len(agenda_prompt(assemble_incremental_snapshot(self.store, {}, current=NOW)[0]).encode())
        budget = original_bytes + 100
        # Complete CAS/baseline state is local and never rejected just because
        # the old all-body projection grows. Only actual requests are measured.
        with patch('app.agenda.MAX_INPUT_BYTES', budget):
            planner = AgendaPlanner(self.store, runtime)
            result = planner.run(NOW)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(planner.run(NOW)['model_calls'], 0)
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertEqual(len(self.store.list_proposals()), 1)

    def test_cli_preview_and_sync_summary_never_print_private_planned_items(self):
        from app.cli import main
        self.ready()
        sentinel = 'FICTIONAL_PRIVATE_ACTION_SENTINEL'
        runtime = Calendar(lambda snapshot: [item(snapshot, action=dict(action(), title=sentinel, description=sentinel))])
        runtime.last_agenda_metadata['decision_reason'] = sentinel
        output = io.StringIO()
        with patch('app.codex_runtime.CodexRuntime', return_value=runtime), redirect_stdout(output):
            code = main(['--state', str(self.path), 'agenda-preview'], config=replace(TEST_CONFIG, state_path=self.path))
        self.assertEqual(code, 0)
        self.assertNotIn(sentinel, output.getvalue())
        self.assertNotIn('sources', json.dumps(json.loads(output.getvalue())['agenda']))
        self.assertIn(sentinel, json.dumps(self.store.latest_agenda_plan(preview=True)['payload']))
        self.assertNotIn(sentinel, json.dumps(self.store.agenda_status()))

    def test_plan_prose_expires_without_losing_future_operational_bindings(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot, reason='FICTIONAL_PRIVATE_PLAN_PROSE')])
        proposal = self.store.list_proposals()[0]
        with self.store.db:
            self.store.db.execute("UPDATE agenda_plans SET created_at='2030-01-01T00:00:00+01:00'")
            counts = self.store.prune_source_content(NOW, apply=True)
        self.assertEqual(counts['agenda_plans_pruned'], 1)
        retained = self.store.latest_agenda_plan()
        self.assertNotIn('FICTIONAL_PRIVATE_PLAN_PROSE', json.dumps(retained))
        self.assertTrue(retained['payload']['content_expired'])
        self.assertEqual(retained['payload']['items'][0]['proposal_id'], proposal['id'])
        from app.data_admin import minimal_proposal
        minimal = minimal_proposal(proposal)
        self.assertEqual(minimal['agenda_sources'], proposal['agenda_sources'])
        self.assertEqual(minimal['agenda_plan_id'], result['plan_id'])

    def test_incremental_checkpoint_rollback_is_atomic_after_injected_failure(self):
        from app.agenda_incremental import content_baseline_hash
        tables = ('sources','messages','canonical_actions','proposals','action_sources','agenda_processed_sources','settings')
        def state():
            return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                    for table in tables}
        self.ready()
        before = state()
        def fail_after_authority(snapshot):
            if self.store._setting('agenda_authority_baseline') is not None:
                raise RuntimeError('fictional injected crash')
            return content_baseline_hash(snapshot)
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        with patch('app.agenda_incremental.content_baseline_hash', side_effect=fail_after_authority):
            with self.assertRaisesRegex(RuntimeError, 'injected crash'):
                AgendaPlanner(self.store, runtime).run(NOW)
        self.assertEqual(state(), before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM agenda_plans WHERE status='applied'").fetchone()[0], 0)
        self.assertEqual(runtime.writes, [])

    def test_incremental_checkpoint_scopes_message_id_hash_and_excludes_upstream_collision(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO messages(id,payload,payload_hash,status) VALUES ('unrelated-pending','{}','unrelated-hash','analysis_pending')")
            self.store.db.execute("INSERT INTO messages(id,status) VALUES ('unrelated-fetch','fetch_pending')")
            self.store.db.execute("UPDATE sources SET upstream_id='unrelated-pending' WHERE kind='announcement'")
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.assertEqual(self.store.get_message('fictional-message')['status'], 'analyzed')
        self.assertEqual(tuple(self.store.db.execute("SELECT status,analyzed_hash FROM messages WHERE id='unrelated-pending'").fetchone()), ('analysis_pending', None))
        self.assertEqual(self.store.get_message('unrelated-fetch')['status'], 'fetch_pending')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 3)
        metadata = self.store.latest_agenda_plan()['metadata']
        self.assertEqual((metadata['source_count'], metadata['catalog_count']), (3, 3))
        self.assertGreater(metadata['input_bytes'], 0)
        self.assertEqual(len(metadata['request_hash']), 64)

    def test_incremental_committed_plan_restart_before_queue_reuses_one_intent_next_day(self):
        from app.core import AppStore
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.assertEqual(self.store.pending_operations(), [])
        ids = {entry['id'] for entry in self.store.list_proposals()}
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)
        planner = AgendaPlanner(self.store, runtime)
        self.assertEqual(planner.run(NOW + timedelta(days=1))['model_calls'], 0)
        self.assertEqual({entry['id'] for entry in self.store.list_proposals()}, ids)
        self.store.set_writes_paused(False)
        self.assertEqual(len(self.execute_pending(runtime, planner)), 1)
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual(planner.run(NOW + timedelta(days=1))['model_calls'], 0)

    def test_incremental_own_create_update_cancel_advance_both_baselines(self):
        from app.agenda_incremental import assemble_incremental_snapshot
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        self.store.set_writes_paused(False)
        self.execute_pending(runtime, planner)
        aid = result['items'][0]['action_id']
        def baseline_matches():
            from app.agenda import read_calendar
            _, contract = assemble_incremental_snapshot(self.store, read_calendar(self.store, runtime), current=NOW)
            self.assertEqual(json.loads(self.store._setting('agenda_authority_baseline')), contract['authority_versions'])
            self.assertEqual(self.store._setting('agenda_baseline_hash'), contract['full_snapshot_hash'])
            self.assertEqual(planner.run(NOW + timedelta(days=1))['model_calls'], 0)
        baseline_matches()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='fictional-update-version' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'update', action_id=aid, action=dict(action(), start='2030-03-13'))]
        updated = planner.run(NOW)
        self.assertEqual(updated['items'][0]['operation'], 'update')
        self.execute_pending(runtime, planner)
        baseline_matches()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='fictional-cancel-version' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'review', review_change='cancel', action_id=aid, action=None)]
        cancellation = planner.run(NOW)['items'][0]
        self.store.resolve_proposal(cancellation['proposal_id'], 'confirm_cancel')
        runtime.select = lambda snapshot: [item(snapshot, 'link', action_id=aid, action=None)]
        planner.run(NOW)  # Observe the new explicit decision before executing it.
        self.execute_pending(runtime, planner)
        baseline_matches()
        self.assertEqual(len(runtime.writes), 3)

    def test_incremental_preview_stale_and_overflow_do_not_advance_checkpoint(self):
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        planner = AgendaPlanner(self.store, runtime)
        planner.run(NOW, preview=True)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 0)
        self.assertIsNone(self.store._setting('agenda_authority_baseline'))
        self.assertIsNone(self.store._setting('agenda_baseline_hash'))
        with self.store.db:
            self.store.db.execute('DELETE FROM agenda_plans')
        def race():
            with self.store.db:
                self.store.db.execute("UPDATE sources SET content_hash='fictional-CAS-race' WHERE kind='announcement'")
        runtime.on_plan = race
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_stale'):
            planner.run(NOW)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 0)
        self.assertEqual(self.store.list_proposals(), [])
        runtime.on_plan = None
        before_calls = runtime.calls
        with patch('app.agenda.MAX_INPUT_BYTES', 1):
            with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow'):
                planner.run(NOW)
        self.assertEqual(runtime.calls, before_calls)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 0)
        self.assertIsNone(self.store._setting('agenda_baseline_hash'))

    def test_incremental_unknown_action_is_protected_independent_same_source_duty_executes(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        aid = result['items'][0]['action_id']
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations WHERE status='unknown'").fetchone()[0], 1)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='changed-during-unknown' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, 'update', action_id=aid),
            item(snapshot, action=dict(action(), title='Independent new duty', start='2030-03-14'))]
        guarded = planner.run(NOW)
        self.assertTrue(next(proposal for proposal in self.store.list_proposals()
            if proposal['id'] == guarded['items'][0]['proposal_id'])['deferred_unknown_operation'])
        self.assertEqual(guarded['items'][1]['operation'], 'create')
        self.assertEqual(len(self.execute_pending(runtime, planner)), 1)
        self.assertEqual(len(runtime.writes), 2)
        operation = self.store.reconcile_operations()[0]
        self.store.record_result(operation['operation_id'], runtime.execute(operation), agenda_calendar=planner.calendar, current=NOW)
        self.assertEqual(len(runtime.writes), 2)  # Marker read finds original, never another create.
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations WHERE status='unknown'").fetchone()[0], 0)

    def test_incremental_missing_ledger_row_is_processed_despite_same_content_baseline(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        with self.store.db:
            self.store.db.execute("DELETE FROM agenda_processed_sources WHERE source_id IN (SELECT id FROM sources WHERE kind='announcement')")
        runtime.select = lambda snapshot: [item(snapshot, 'ignore', action=None)]
        result = planner.run(NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['model_calls'], 1)
        self.assertEqual(self.store.latest_agenda_plan()['metadata']['source_count'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 3)

    def test_incremental_cached_preview_retains_actual_original_request_evidence(self):
        runtime = Calendar(lambda snapshot: [item(snapshot)])
        self.ready()
        planner = AgendaPlanner(self.store, runtime)
        planner.run(NOW, preview=True)
        preview_metadata = self.store.latest_agenda_plan(preview=True)['metadata']
        result = planner.run(NOW + timedelta(hours=1))
        active_metadata = self.store.latest_agenda_plan()['metadata']
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(active_metadata['request_hash'], preview_metadata['request_hash'])
        self.assertEqual(active_metadata['input_bytes'], preview_metadata['input_bytes'])

    def test_incremental_exact_unknown_creation_links_without_duplicate_and_independent_duty_executes(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        original_aid = result['items'][0]['action_id']
        original_pid = result['items'][0]['proposal_id']
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='changed-after-exact-unknown' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot),
            item(snapshot, action=dict(action(), title='Independent duty', start='2030-03-15'))]
        result = planner.run(NOW)
        self.assertEqual([entry['operation'] for entry in result['items']], ['link', 'create'])
        self.assertEqual(result['items'][0]['action_id'], original_aid)
        self.assertEqual(result['items'][0]['proposal_id'], original_pid)
        self.assertEqual(len(self.store.list_actions()), 2)  # Original obligation plus independent duty.
        self.assertEqual(len(self.execute_pending(runtime, planner)), 1)
        self.assertEqual(len(runtime.writes), 2)
        operation = self.store.reconcile_operations()[0]
        self.store.record_result(operation['operation_id'], runtime.execute(operation), agenda_calendar=planner.calendar, current=NOW)
        self.assertEqual(len(runtime.writes), 2)

    def test_incremental_unknown_candidate_cannot_be_approved_before_reconciliation(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        aid = result['items'][0]['action_id']
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='candidate-related-change' WHERE kind='announcement'")
        runtime.select = lambda snapshot: [item(snapshot, candidate_action_ids=[aid], action=dict(action(), title='Changed candidate title')),
            item(snapshot, action=dict(action(), title='Independent duty', start='2030-03-16'))]
        result = planner.run(NOW)
        dependent = result['items'][0]['proposal_id']
        with self.assertRaisesRegex(ValueError, 'Reconcile the prior write'):
            self.store.resolve_proposal(dependent, 'approve')
        queued = self.execute_pending(runtime, planner)
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(runtime.writes), 2)
        self.assertNotEqual(queued[0]['proposal_id'], dependent)

    def test_incremental_exact_unknown_link_resolution_rechecks_target_conflicts(self):
        result, runtime, planner = self.apply(lambda snapshot: [item(snapshot)])
        aid = result['items'][0]['action_id']
        self.store.set_writes_paused(False)
        runtime.timeout_after_write = True
        self.execute_pending(runtime, planner)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='unknown-target-conflict' WHERE kind='announcement'")
        checkpoint = [tuple(row) for row in self.store.db.execute('SELECT * FROM agenda_processed_sources ORDER BY 1')]
        runtime.select = lambda snapshot: [item(snapshot), item(snapshot, 'update', action_id=aid)]
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_conflicting_operations'):
            planner.run(NOW)
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM agenda_processed_sources ORDER BY 1')], checkpoint)

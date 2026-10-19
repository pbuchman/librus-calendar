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
        event = {**operation['event'], 'id': eid}
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
        self.apply(lambda snapshot: [])
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
        self.apply(lambda snapshot: [])
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
        runtime.select = lambda snapshot: [item(snapshot, 'create', offer_group_id=group['id'])]
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

    def test_exact_duplicate_create_plan_fails_before_persistence_or_queue(self):
        self.ready()
        runtime = Calendar(lambda snapshot: [item(snapshot), item(snapshot)])
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_conflicting_operations'):
            AgendaPlanner(self.store, runtime).run(NOW)
        self.assertEqual(self.store.list_actions(), [])
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.queue_operations(NOW), [])

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

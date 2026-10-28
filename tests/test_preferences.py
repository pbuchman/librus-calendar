"""Fictional future-only human examples and automatic participation boundaries."""
from datetime import timedelta
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.agenda import materialize_plan, validate_agenda_plan, agenda_prompt
from app.codex_runtime import RuntimeFailure
from app.core import normalize_proposal
from app.preferences import (enable_preferences, preference_snapshot, preference_status,
                             authorizes_participation, CONTEXT_EXAMPLES)
from app.source_admin import write_private_json
import test_agenda as agenda_tests
import test_agenda_integration as integration_tests
from test_agenda import NOW, action, item, plan
from test_agenda_integration import Calendar
from test_offer_groups import option, EVIDENCE


class PreferenceTests(unittest.TestCase):
    setUp = agenda_tests.AgendaTests.setUp
    tearDown = agenda_tests.AgendaTests.tearDown
    snapshot = agenda_tests.AgendaTests.snapshot

    def source(self, name, text='A painting workshop for class 1x on 20 March at 16:00.'):
        mid = 'fictional-preference-' + name
        self.store.save_message({'id': mid, 'subject': 'Optional painting workshop',
            'text': text,
            'sent_at': '2030-03-01T09:00:00+01:00'})
        return mid

    def proposal(self, name, decision=None):
        mid = self.source(name)
        proposed = option(title='Painting workshop', description='Optional painting workshop for class 1x.',
                          needs_review=True)
        proposed.pop('alternative_group'); proposed.pop('alternative_group_evidence')
        self.store.save_proposals(mid, [proposed], NOW)
        pid = self.store.db.execute('SELECT id FROM proposals WHERE message_id=?', (mid,)).fetchone()[0]
        if decision:
            self.store.resolve_proposal(pid, decision)
        return pid

    def enable(self):
        receipt = Path(self.temp.name) / 'boundary.json'
        write_private_json(receipt, {'kind': 'administrative_bulk_cleanup',
            'created_at': (NOW-timedelta(seconds=1)).isoformat(),
            'excluded_from_preference_learning': True,
            'preexisting_proposal_ids_excluded_from_future_examples': [row[0] for row in self.store.db.execute('SELECT id FROM proposals')],
            'preexisting_group_ids_excluded_from_future_examples': [row[0] for row in self.store.db.execute('SELECT id FROM proposal_groups')]})
        return enable_preferences(self.store, receipt)

    def examples(self, outcome='approve'):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.enable()
            self.proposal('one', outcome)
            self.proposal('two', outcome)
        return preference_snapshot(self.store.db)

    def learned(self, context, *, operation='create', **changes):
        mid = self.source('new')
        snapshot = self.snapshot()
        proposed = {**action(), 'title': 'Painting workshop', 'description': 'Painting for class 1x.',
                    'activity_scope': 'extracurricular', 'temporal_kind': 'event', 'start': '2030-03-20'}
        basis = {'outcome': 'accept' if operation == 'create' else 'reject',
                 'example_ids': [example['id'] for example in context['examples']]}
        selected = item(snapshot, operation, action=proposed if operation != 'ignore' else None,
                        sources=[{'source_id': 'message:'+mid,
                                  'source_hash': self.store.get_source('message:'+mid)['content_hash']}],
                        preference_basis=basis, **changes)
        return snapshot, selected

    def apply(self, snapshot, selected):
        return materialize_plan(self.store, snapshot, plan(snapshot, [selected]), {}, current=NOW)

    def test_legacy_default_and_administrative_old_rows_never_become_examples(self):
        old = self.proposal('old')
        self.store.resolve_proposal(old, 'ignore')
        self.assertEqual(preference_snapshot(self.store.db), {'enabled': False, 'examples': []})
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.enable()
            self.store.resolve_proposal(old, 'ignore')
        self.assertEqual(preference_status(self.store.db)['examples'], 0)
        snap = self.snapshot()
        selected = item(snap, action={**action(), 'activity_scope': 'extracurricular'})
        with self.assertRaises(RuntimeFailure): validate_agenda_plan(plan(snap, [selected]), snap)

    def test_only_future_human_calls_capture_once_and_changed_choice_is_visible(self):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.enable()
            pid = self.proposal('future', 'approve')
            self.store.resolve_proposal(pid, 'approve')
            self.assertEqual(preference_status(self.store.db)['examples'], 1)
            self.store.resolve_proposal(pid, 'ignore')
        self.assertEqual({example['outcome'] for example in preference_snapshot(self.store.db)['examples']}, {'accept', 'reject'})

    def test_proposal_arriving_after_cutoff_before_enable_is_eligible(self):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            pid = self.proposal('between')
            receipt = Path(self.temp.name)/'between.json'
            write_private_json(receipt, {'kind':'administrative_bulk_cleanup',
                'created_at':(NOW-timedelta(seconds=1)).isoformat(), 'excluded_from_preference_learning':True,
                'preexisting_proposal_ids_excluded_from_future_examples':[],
                'preexisting_group_ids_excluded_from_future_examples':[]})
            enable_preferences(self.store, receipt)
            self.store.resolve_proposal(pid, 'approve')
        self.assertEqual(preference_status(self.store.db)['examples'], 1)

    def test_group_selection_is_one_accept_example_not_rejected_siblings(self):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.enable()
            mid = self.source('offer', EVIDENCE)
            self.store.save_proposals(mid, [option(), option('2030-03-12T16:00:00+01:00')], NOW)
            group = self.store.list_proposal_groups()[0]
            self.store.resolve_proposal_group(group['id'], 'select', group['members'][0]['id'], expected_revision=group['revision'])
            self.store.resolve_proposal_group(group['id'], 'select', group['members'][0]['id'], expected_revision=group['revision'])
        examples = preference_snapshot(self.store.db)['examples']
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]['outcome'], 'accept')
        self.assertEqual(examples[0]['alternative_count'], 2)

    def test_automatic_accept_reaches_existing_queue_and_is_not_human_example(self):
        context = self.examples()
        snapshot, selected = self.learned(context)
        result = self.apply(snapshot, selected)
        pid = result['items'][0]['proposal_id']
        proposed = next(value for value in self.store.list_proposals() if value['id'] == pid)
        self.assertEqual(proposed['status'], 'pending')
        self.assertFalse(proposed['user_approved'])
        self.assertTrue(authorizes_participation(self.store, proposed))
        runtime = Calendar(lambda snapshot: [])
        integration_tests.AgendaIntegrationTests.ready(self)
        self.store.set_writes_paused(False)
        queued = self.store.queue_operations(NOW)
        learned_operation = next(operation for operation in queued if operation['proposal_id'] == pid)
        self.assertIn('Automatyczna akceptacja', learned_operation['event']['description'])
        for operation in queued:
            self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
            self.store.record_result(operation['operation_id'], runtime.execute(operation))
        self.assertEqual(preference_status(self.store.db)['examples'], 2)
        self.assertEqual(self.store.db.execute('SELECT status FROM proposals WHERE id=?', (pid,)).fetchone()[0], 'applied')

    def test_automatic_reject_keeps_provenance_and_has_no_human_rejection_flag(self):
        context = self.examples('ignore')
        snapshot, selected = self.learned(context, operation='ignore')
        result = self.apply(snapshot, selected)
        self.assertEqual(result['items'][0]['operation'], 'ignore')
        self.assertIn('Automatic decline', result['items'][0]['reason'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM preference_examples').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM proposals WHERE message_id='fictional-preference-new'").fetchone()[0], 0)

    def test_insufficient_or_conflicting_citations_materialize_review(self):
        context = self.examples()
        snapshot, selected = self.learned(context)
        selected['preference_basis']['example_ids'] = selected['preference_basis']['example_ids'][:1]
        result = self.apply(snapshot, selected)
        self.assertEqual(result['items'][0]['status'], 'review')
        self.assertEqual(result['items'][0]['operation'], 'review')
        with patch('app.core.utcnow', return_value=NOW.isoformat()): self.proposal('opposed', 'ignore')
        snapshot = self.snapshot()
        context = snapshot['preferences']
        selected['preference_basis']['example_ids'] = [example['id'] for example in context['examples']]
        selected['operation'] = 'ignore'; selected['action'] = None
        result = self.apply(snapshot, selected)
        self.assertEqual(result['items'][0]['status'], 'review')

    def test_saved_flag_or_modified_content_does_not_self_authorize(self):
        context = self.examples()
        snapshot, selected = self.learned(context)
        selected['action'].update(temporal_kind='deadline', due_at='2030-03-20T16:00:00+01:00')
        result = self.apply(snapshot, selected)
        pid = result['items'][0]['proposal_id']
        row = self.store.db.execute('SELECT payload FROM proposals WHERE id=?', (pid,)).fetchone()
        proposed = json.loads(row[0])
        self.assertTrue(authorizes_participation(self.store, proposed))
        self.assertEqual(normalize_proposal(proposed, NOW)['status'], 'review')
        different_deadline = {**proposed, 'due_at': '2030-03-20T17:00:00+01:00'}
        self.assertFalse(authorizes_participation(self.store, different_deadline))
        different_duration = {**proposed, 'default_duration': not proposed['default_duration']}
        self.assertFalse(authorizes_participation(self.store, different_duration))
        proposed['title'] = 'Different activity'
        self.assertFalse(authorizes_participation(self.store, proposed))
        with self.store.db:
            self.store.db.execute('UPDATE proposals SET payload=? WHERE id=?', (json.dumps(proposed), pid))
        integration_tests.AgendaIntegrationTests.ready(self)
        self.store.queue_operations(NOW)
        self.assertEqual(self.store.db.execute('SELECT status FROM proposals WHERE id=?', (pid,)).fetchone()[0], 'review')
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM operations WHERE proposal_id=?', (pid,)).fetchone())

    def test_latest50_explicit_window_retains_ledger_and_marks_compact_description(self):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.enable()
            for index in range(CONTEXT_EXAMPLES+2): self.proposal(str(index), 'ignore')
        with self.store.db:
            row = self.store.db.execute('SELECT id,payload FROM preference_examples ORDER BY created_at DESC,id DESC LIMIT 1').fetchone()
            example = json.loads(row['payload']); example['action']['description'] = 'x'*2000
            self.store.db.execute('UPDATE preference_examples SET payload=? WHERE id=?', (json.dumps(example), row['id']))
        context = preference_snapshot(self.store.db)
        self.assertEqual(len(context['examples']), 50)
        self.assertEqual(context['omitted_older_examples'], 2)
        self.assertEqual(context['total_examples'], 52)
        self.assertTrue(context['examples'][0]['action']['description_truncated'])
        snap = self.snapshot()
        with patch('app.agenda.MAX_INPUT_BYTES', 100):
            with self.assertRaises(RuntimeFailure): agenda_prompt(snap)

    def test_preferences_never_choose_alternatives_or_override_unknown_scope(self):
        context = self.examples()
        snapshot, selected = self.learned(context)
        invalid = copy.deepcopy(selected); invalid['action']['activity_scope'] = 'unknown'
        with self.assertRaises(RuntimeFailure): validate_agenda_plan(plan(snapshot, [invalid]), snapshot)
        first = copy.deepcopy(selected); first['new_offer_key'] = 'painting-options'
        second = copy.deepcopy(first); second['action']['start'] = '2030-03-21'
        result = materialize_plan(self.store, snapshot, plan(snapshot, [first, second]), {}, current=NOW)
        self.assertTrue(all(proposed['status'] == 'review' for proposed in result['items']))
        self.assertEqual(self.store.list_proposal_groups()[0]['status'], 'open')

    def test_enable_idempotent_and_no_existing_state_rewritten(self):
        with patch('app.core.utcnow', return_value=NOW.isoformat()):
            self.proposal('old', 'ignore')
            before = {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM '+table)]
                      for table in ('decision_intents','proposals','events','operations')}
            self.enable()
            result = enable_preferences(self.store, Path(self.temp.name)/'boundary.json')
        self.assertTrue(result['enabled'])
        for table, value in before.items(): self.assertEqual(value, [tuple(row) for row in self.store.db.execute('SELECT * FROM '+table)])

    def test_historical_exact_rejection_wins_over_new_positive_preferences(self):
        old = self.proposal('old-rejected', 'ignore')
        context = self.examples()
        with self.store.db: self.store._initialize_sources()
        before = tuple(self.store.db.execute('SELECT * FROM decision_intents WHERE id=?', (old,)).fetchone())
        snapshot, selected = self.learned(context)
        old_entry = next(entry for entry in snapshot['agenda'] if entry['proposal_id'] == old)
        selected.update(operation='update', action_id=old_entry['id'])
        result = self.apply(snapshot, selected)
        self.assertEqual(result['items'][0]['operation'], 'ignore')
        self.assertEqual(result['items'][0]['reason'], 'Saved rejection retained.')
        self.assertEqual(before, tuple(self.store.db.execute('SELECT * FROM decision_intents WHERE id=?', (old,)).fetchone()))

    def test_new_example_between_queue_and_claim_blocks_learned_write(self):
        context = self.examples()
        snapshot, selected = self.learned(context)
        pid = self.apply(snapshot, selected)['items'][0]['proposal_id']
        integration_tests.AgendaIntegrationTests.ready(self)
        self.store.set_writes_paused(False)
        operation = next(operation for operation in self.store.queue_operations(NOW) if operation['proposal_id'] == pid)
        with patch('app.core.utcnow', return_value=NOW.isoformat()): self.proposal('new-negative', 'ignore')
        self.assertFalse(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
        row = self.store.db.execute('SELECT status,attempts FROM operations WHERE proposal_id=?', (pid,)).fetchone()
        self.assertEqual(tuple(row), ('review', 0))

    def test_unknown_prior_write_stays_review_despite_positive_examples(self):
        context = self.examples()
        with self.store.db: self.store._initialize_sources()
        snapshot, selected = self.learned(context)
        prior = snapshot['agenda'][0]
        with self.store.db:
            self.store.db.execute("INSERT INTO operations(id,proposal_id,payload,status,attempts,created_at,updated_at) VALUES ('unknown-write',?,'{}','unknown',1,'2030','2030')", (prior['proposal_id'],))
        snapshot = self.snapshot()
        selected['candidate_action_ids'] = [prior['id']]
        result = self.apply(snapshot, selected)
        self.assertEqual(result['items'][0]['status'], 'review')
        proposed = next(value for value in self.store.list_proposals() if value['id'] == result['items'][0]['proposal_id'])
        self.assertNotIn('preference_decision', proposed)
        self.assertEqual(proposed['deferred_unknown_operation'], 'unknown-write')

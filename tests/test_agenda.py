"""Fictional, offline contracts for the single agenda planning POC."""
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.agenda import assemble_agenda_snapshot, MAX_INPUT_BYTES, validate_agenda_plan
from app.codex_runtime import CodexRuntime, RuntimeFailure, normalize_event
from app.codex_runtime import SCHEMAS
from app.core import AppStore, WARSAW
from test_support import TEST_CONFIG

NOW = datetime(2030, 3, 1, 12, tzinfo=WARSAW)
LEARNER = {'id': 'fictional-child', 'class_id': '1x'}


def action():
    return {'title': 'Przynieść papier', 'description': '', 'start': '2030-03-12', 'end': None,
            'all_day': True, 'temporal_kind': 'deadline', 'due_at': None,
            'activity_scope': 'school', 'optional': False}


def item(snapshot, operation='create', **changes):
    result = {'operation': operation, 'action_id': None, 'candidate_action_ids': [],
              'sources': [{'source_id': source['id'], 'source_hash': source['content_hash']}
                          for source in snapshot['sources']],
              'reason': 'Jedno zadanie potwierdzone przez źródła.', 'action': action(),
              'review_change': None, 'offer_group_id': None, 'new_offer_key': None}
    result.update(changes)
    return result


def plan(snapshot, items):
    return {'snapshot_hash': snapshot['snapshot_hash'], 'decision_reason': 'Wspólny plan agendy.', 'items': items}


class AgendaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.store.ingest_sources({'learner': LEARNER, 'sources': [
            {'kind': kind, 'upstream_id': 'fictional-' + kind, 'learner_id': LEARNER['id'],
             'title': 'Materiały', 'sender': 'Fikcyjna szkoła',
             'published_at': '2030-03-01T09:00:00+01:00', 'updated_at': None,
             'text': 'Klasa 1x przynosi papier do 12 marca.', 'metadata': {}, 'deleted': False}
            for kind in ('announcement', 'school_event')], 'complete_kinds': []}, NOW)
        self.store.save_message({'id': 'fictional-message', 'subject': 'Materiały', 'sender': 'Fikcyjny nauczyciel',
                                    'sent_at': '2030-03-01T09:00:00+01:00',
                                    'text': 'Klasa 1x przynosi papier do 12 marca.'})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, calendar=None, current=NOW):
        return assemble_agenda_snapshot(self.store, calendar or {}, current=current,
                                        owner_email=TEST_CONFIG.calendar_owner)

    def inventory(self):
        event = {'id': 'fictional-calendar-event', 'summary': 'Uroczystość',
                 'start': {'dateTime': '2030-03-12T10:30:00+01:00'},
                 'end': {'dateTime': '2030-03-12T12:00:00+01:00'}}
        with self.store.db:
            self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                                  ('existing-action', 'existing-proposal', json.dumps(action()), 'applied', NOW.isoformat()))
            self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                                  (event['id'], TEST_CONFIG.calendar_id, 'fictional-message', 'existing-proposal',
                                   json.dumps(event), 'saved-fingerprint', 'fictional-marker', NOW.isoformat(), 'active'))
        return event

    def runtime(self, result, calls):
        def process(args, prompt, timeout):
            calls.append((args, prompt))
            record = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}}
            return 0, json.dumps(record), ''
        return CodexRuntime(TEST_CONFIG, run_process=process, read_only=True)

    def test_full_read_only_snapshot_three_sources_and_one_runtime_task(self):
        snapshot = self.snapshot()
        self.assertEqual({source['kind'] for source in snapshot['sources']}, {'message', 'announcement', 'school_event'})
        before = self.store.db.total_changes
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(snapshot, assemble_agenda_snapshot(db, {}, current=NOW))
        calls = []
        runtime = self.runtime(plan(snapshot, [item(snapshot)]), calls)
        result = runtime.plan_agenda(snapshot)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(result['items'][0]['sources']), 3)
        self.assertIsNone(result['items'][0]['action_id'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.db.total_changes, before)
        args, prompt = calls[0]
        self.assertIn('gpt-6.1-sol', args)
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn('apps', args)
        self.assertIn('agenda_plan.md', ' '.join(args))
        self.assertNotIn('Fikcyjna', json.dumps(runtime.last_agenda_metadata))

    def test_uncertain_duplicate_review_does_not_block_clear_action(self):
        event = self.inventory()
        snapshot = self.snapshot({event['id']: event})
        result = plan(snapshot, [item(snapshot, 'review', review_change='create', candidate_action_ids=['existing-action']),
                                 item(snapshot)])
        self.assertEqual([entry['operation'] for entry in validate_agenda_plan(result, snapshot)['items']], ['review', 'create'])

    def test_current_calendar_and_saved_decisions_preserved_and_change_hash(self):
        event = self.inventory()
        snapshot = self.snapshot({event['id']: event})
        self.assertEqual(snapshot['agenda'][0]['calendar'][0]['current']['start'], normalize_event(event)['start'])
        changed = dict(event, summary='Rodzic poprawił tytuł')
        self.assertNotEqual(snapshot['snapshot_hash'], self.snapshot({event['id']: changed})['snapshot_hash'])
        with self.store.db:
            self.store.db.execute('INSERT INTO canonical_decisions VALUES (?,?,?)',
                                  ('existing-action', json.dumps({'user_rejected': True}), NOW.isoformat()))
        decided = self.snapshot({event['id']: event})
        self.assertTrue(decided['agenda'][0]['decisions'][0]['user_rejected'])
        self.assertNotEqual(snapshot['snapshot_hash'], decided['snapshot_hash'])
        linked = item(decided, 'link', action_id='existing-action', action=None)
        self.assertEqual(validate_agenda_plan(plan(decided, [linked]), decided)['items'][0]['action_id'], 'existing-action')

    def test_missing_calendar_fetch_is_error_but_observed_not_found_is_context(self):
        event = self.inventory()
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_calendar_snapshot_incomplete'):
            self.snapshot()
        snapshot = self.snapshot({event['id']: None})
        self.assertIsNone(snapshot['agenda'][0]['calendar'][0]['current'])

    def test_fetch_clock_does_not_invalidate_but_source_content_does(self):
        snapshot = self.snapshot()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET fetched_at='2030-03-01T14:00:00+01:00',analyzed_at='later'")
        self.assertEqual(snapshot['snapshot_hash'], self.snapshot(current=NOW + timedelta(hours=1))['snapshot_hash'])
        self.assertIn('last_collection_at', snapshot['observation'])
        with self.store.db:
            self.store.db.execute("UPDATE sources SET content_hash='a-new-source-version' WHERE kind='announcement'")
        self.assertNotEqual(snapshot['snapshot_hash'], self.snapshot()['snapshot_hash'])

    def test_context_overflow_before_runtime_no_truncation(self):
        snapshot = self.snapshot()
        snapshot['sources'][0]['text'] = 'x' * MAX_INPUT_BYTES
        calls = []
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow'):
            self.runtime(plan(snapshot, []), calls).plan_agenda(snapshot)
        self.assertEqual(calls, [])

    def test_foreign_references_duplicate_targets_and_bad_dates_fail(self):
        event = self.inventory()
        snapshot = self.snapshot({event['id']: event})
        foreign = item(snapshot, 'link', action_id='foreign-action', action=None)
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_action_reference_invalid'):
            validate_agenda_plan(plan(snapshot, [foreign]), snapshot)
        linked = item(snapshot, 'link', action_id='existing-action', action=None)
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_conflicting_operations'):
            validate_agenda_plan(plan(snapshot, [linked, linked]), snapshot)
        bad_date = item(snapshot, action=dict(action(), start='2030-02-30'))
        with self.assertRaisesRegex(RuntimeFailure, 'invalid_analysis_temporal_fields'):
            validate_agenda_plan(plan(snapshot, [bad_date]), snapshot)
        stale = plan(snapshot, [])
        stale['snapshot_hash'] = 'f' * 64
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_stale'):
            validate_agenda_plan(stale, snapshot)

    def test_empty_plan_is_read_only_no_deletion_and_model_config_is_explicit(self):
        event = self.inventory()
        snapshot = self.snapshot({event['id']: event})
        calls = []
        self.runtime(plan(snapshot, []), calls).plan_agenda(snapshot)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        runtime = CodexRuntime(replace(TEST_CONFIG, analysis_effort='medium'), run_process=lambda *args: self.fail('must fail before call'))
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_model_configuration_required'):
            runtime.plan_agenda(snapshot)

    def test_runtime_rejects_tools_instead_of_accepting_model_claim(self):
        snapshot = self.snapshot()
        record = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call',
                  'tool': 'google_calendar.create_event'}}
        runtime = CodexRuntime(TEST_CONFIG, read_only=True,
                               run_process=lambda *args: (0, json.dumps(record), ''))
        with self.assertRaisesRegex(RuntimeFailure, 'analysis_tool_forbidden'):
            runtime.plan_agenda(snapshot)

    def test_duplicate_candidate_ids_rejected_without_provider_schema_keyword(self):
        event = self.inventory()
        snapshot = self.snapshot({event['id']: event})
        review = item(snapshot, 'review', review_change='create',
                      candidate_action_ids=['existing-action', 'existing-action'])
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_action_reference_invalid'):
            validate_agenda_plan(plan(snapshot, [review]), snapshot)

    def test_compact_projection_keeps_full_sources_legacy_authority_and_quotes_once(self):
        event = self.inventory()
        source = self.store.db.execute("SELECT * FROM sources WHERE kind='school_event'").fetchone()
        payload = json.loads(source['payload'])
        full_text = 'Pełny fikcyjny opis szkolnego obowiązku. ' * 500
        audience = {'version': 'fictional-audience', 'learner_id': LEARNER['id'], 'class_id': '1x'}
        payload.update(text=full_text, upstream_id='redundant-provider-id', url='https://example.test/do-not-fetch')
        payload['metadata'] = {'event_start': '2030-03-12', 'served_class_calendar': audience,
                               'attachment_count': 2, 'fields': {'opis': full_text}, 'content_hash': 'duplicate-hash'}
        old_action = dict(action(), source_quote=full_text, user_approved=False,
                          action_identity={'action_type': 'bring', 'object': 'papier', 'occasion': 'lekcja', 'evidence': full_text})
        latest = dict(old_action, start='2030-03-13', user_approved=True, user_edited=True, default_duration=False)
        authority = {'proposal': latest, 'signature': {'kind': 'create', 'quote_hash': 'saved-hash'}, 'event_ids': [event['id']]}
        with self.store.db:
            self.store.db.execute('UPDATE sources SET payload=? WHERE id=?', (json.dumps(payload), source['id']))
            self.store.db.execute('UPDATE canonical_actions SET payload=? WHERE id=?', (json.dumps(old_action), 'existing-action'))
            self.store.db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)',
                                  ('existing-proposal', 'fictional-message', 'old-version', json.dumps(latest), 'applied', NOW.isoformat(), NOW.isoformat(), None))
            self.store.db.execute('INSERT INTO action_sources VALUES (?,?,?,?,?,?)',
                                  ('existing-action', source['id'], 'historical-source-hash', full_text, 'origin', NOW.isoformat()))
            self.store.db.execute('INSERT INTO canonical_decisions VALUES (?,?,?)', ('existing-action', json.dumps(authority), NOW.isoformat()))
            self.store.db.execute('INSERT INTO decision_intents VALUES (?,?,?,?,?)',
                                  ('existing-proposal', 'fictional-message', 'old-version', json.dumps(authority), NOW.isoformat()))
            self.store.db.execute('INSERT INTO proposal_groups VALUES (?,?,?,?,?,?,?,?,?,?)',
                                  ('offer-1', 'fictional-message', 'old-version', 'one-offer', 'Wybierz jeden termin', 'selected',
                                   'existing-proposal', 3, NOW.isoformat(), NOW.isoformat()))
            self.store.db.execute('INSERT INTO proposal_group_members VALUES (?,?,?)', ('offer-1', 'existing-proposal', 0))
        snapshot = self.snapshot({event['id']: event})
        projected = next(item for item in snapshot['sources'] if item['id'] == source['id'])
        self.assertEqual(projected['text'], full_text)
        self.assertEqual(projected['metadata']['served_class_calendar'], audience)
        self.assertEqual(projected['metadata']['event_start'], '2030-03-12')
        self.assertEqual(projected['metadata']['attachment_count'], 2)
        self.assertNotIn('fields', projected['metadata'])
        self.assertNotIn('upstream_id', projected)
        self.assertNotIn('url', projected)
        self.assertEqual(len(snapshot['agenda']), 1)
        entry = snapshot['agenda'][0]
        self.assertEqual(entry['id'], 'existing-action')
        self.assertEqual(entry['origin'], {'message_id': 'fictional-message', 'source_hash': 'old-version'})
        self.assertTrue(entry['action']['user_approved'])
        self.assertTrue(entry['action']['user_edited'])
        self.assertEqual(entry['action']['start'], '2030-03-13')
        self.assertEqual(entry['links'][0]['source_hash'], 'historical-source-hash')
        self.assertEqual(entry['links'][0]['source_quote'], full_text)
        self.assertNotIn('legacy_quotes', entry)
        self.assertNotIn('source_quote', entry['action'])
        self.assertNotIn('evidence', entry['action']['action_identity'])
        self.assertEqual(len(entry['decisions']), 1)
        self.assertEqual(entry['decisions'][0]['proposal']['start'], '2030-03-13')
        self.assertEqual(entry['decisions'][0]['event_ids'], [event['id']])
        self.assertEqual(snapshot['offer_groups'][0]['selected_proposal_id'], 'existing-proposal')
        self.assertEqual(snapshot['offer_groups'][0]['revision'], 3)
        self.assertEqual(snapshot['offer_groups'][0]['members'][0]['proposal_id'], 'existing-proposal')
        self.assertNotIn('saved_snapshot', entry['calendar'][0])
        for duplicate_table in ('canonical_actions', 'proposals', 'canonical_decisions', 'decision_intents', 'action_sources', 'calendar'):
            self.assertNotIn(duplicate_table, snapshot)
        self.assertLess(len(json.dumps(snapshot, ensure_ascii=False).encode()), len(full_text.encode()) * 3)

    def test_prompt_contract_distinguishes_tasks_from_occasion_dates_and_unknown_audience(self):
        # This offline test checks supplied instructions and input, not the real
        # model's semantic accuracy. The bounded private POC establishes that.
        bodies = [
            'Klasa 1x przynosi karton do 12 marca. Finał szkolnej akcji odbędzie się 17 marca.',
            'Wieczór biblioteczny odbędzie się 18 marca. Biblioteka zaprasza.',
            'Wszystkie klasy szkoły uczestniczą w dniu czytania 19 marca.',
        ]
        with self.store.db:
            for row, body in zip(self.store.db.execute('SELECT id,payload FROM sources ORDER BY id').fetchall(), bodies):
                payload = json.loads(row['payload'])
                payload['text'] = body
                self.store.db.execute('UPDATE sources SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        snapshot = self.snapshot()
        unknown = item(snapshot, 'review', review_change='create', action=None,
                       reason='Nie ustalono, czy wydarzenie dotyczy wybranej klasy.')
        calls = []
        runtime = self.runtime(plan(snapshot, [item(snapshot), unknown]), calls)
        result = runtime.plan_agenda(snapshot)
        self.assertEqual([entry['operation'] for entry in result['items']], ['create', 'review'])
        self.assertEqual(len(calls), 1)
        for body in bodies:
            self.assertIn(body, calls[0][1])
        policy = (SCHEMAS / 'agenda_plan.md').read_text()
        self.assertIn('context, not a second parent/child deadline', policy)
        self.assertIn('independently states a distinct task or deliverable', policy)
        self.assertIn('Announcement visibility alone does not establish', policy)
        self.assertIn('explicit schoolwide or matching class/learner', policy)
        self.assertIn('no Calendar envelope is required', policy)
        self.assertIn('Do not transfer class/date authority', policy)

    def test_message_unread_attachment_count_is_context_without_attachment_content(self):
        message = self.store.get_message('fictional-message')
        self.store.save_message(dict(message, attachment_count=2))
        snapshot = self.snapshot()
        source = next(source for source in snapshot['sources'] if source['kind'] == 'message')
        self.assertEqual(source['metadata']['attachment_count'], 2)
        self.assertNotIn('attachments', source)


if __name__ == '__main__':
    unittest.main()

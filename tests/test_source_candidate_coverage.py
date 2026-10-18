"""Crowded fictional inventory and planner-version upgrades stay fail-closed."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from app.core import digest
from app.health_state import read_connection_state
from app.source_admin import activate_sources
from app.source_matching import _hard_match_decision, candidate_is_distinct
from app.source_planner import SourcePlanner
from app.source_preview import SOURCE_PREVIEW_PLANNER_VERSION, current_preview_sql, preview_is_current
import test_source_evidence as evidence_fixtures
from test_source_planner import Runtime, action, source, NOW, LEARNER, KINDS, TEXT
from test_source_budget import SourceClient, SourceRuntime
from app.sync import SyncController


class CandidateCoverageTests(unittest.TestCase):
    setUp = evidence_fixtures.SourceEvidenceTests.setUp
    tearDown = evidence_fixtures.SourceEvidenceTests.tearDown
    ingest = evidence_fixtures.SourceEvidenceTests.ingest
    seed = evidence_fixtures.SourceEvidenceTests.seed

    def add_candidate(self, index, **changes):
        item = action(**changes)
        with self.store.db:
            self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                ('fictional-candidate-' + str(index), 'fictional-proposal-' + str(index),
                 json.dumps(item), 'review', NOW.isoformat()))

    def test_proven_distinct_inventory_is_filtered_before_cap_and_target_is_retained(self):
        target, _ = self.seed(quote=TEXT, body=TEXT)
        for index in range(40):
            self.add_candidate(index, temporal_kind='event', action_identity={'action_type': 'attend'})
        fresh = self.ingest(source('fresh-deadline'))
        runtime = Runtime()
        result = SourcePlanner(self.store, runtime).analyze(fresh, NOW, preview=True)
        self.assertEqual(result['outcomes'][0]['action_id'], target)
        self.assertEqual(result['outcomes'][0]['outcome'], 'linked')
        self.assertIn(('match', 1), runtime.calls)
        self.assertTrue(self.store._source_candidates_complete)

    def test_unknown_and_owned_inventory_are_not_exclusion_proof(self):
        incoming = action()
        unknown = {'title': TEXT, 'source_quote': TEXT}
        self.assertEqual(_hard_match_decision(incoming, unknown), 'distinct')
        self.assertFalse(candidate_is_distinct(incoming, unknown))
        partial = action(action_identity={'object': 'fictional object'})
        self.assertFalse(candidate_is_distinct(incoming, partial))
        self.assertFalse(candidate_is_distinct(incoming, action(temporal_kind='unknown-future-kind')))
        inventory = action(temporal_kind='event', inventory_only=True, event_id='fictional-owned-event')
        self.assertEqual(_hard_match_decision(incoming, inventory), 'distinct')
        self.assertFalse(candidate_is_distinct(incoming, inventory))
        for index, value in enumerate((unknown, partial, inventory)):
            with self.store.db:
                self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                    ('unknown-' + str(index), 'unknown-proposal-' + str(index), json.dumps(value), 'applied', NOW.isoformat()))
        src = self.ingest(source('unknown-query'))
        candidates = self.store.candidate_actions(dict(incoming, source_id=src['id']))
        self.assertEqual(len(candidates), 3)
        self.assertTrue(self.store._source_candidates_complete)

    def test_positive_proof_predicate_keeps_inputs_and_existing_guard_semantics(self):
        original = action()
        values = [action(temporal_kind='event'),
                  action(action_identity={'action_type': 'attend'}),
                  action(recipient={'scope': 'class', 'class_id': 'different-fictional-class'}),
                  action(action_identity=None), action(recipient={'scope': 'class'})]
        before = deepcopy((original, values))
        self.assertEqual([candidate_is_distinct(original, item) for item in values], [True, True, True, False, False])
        self.assertEqual((original, values), before)

    def test_one_hundred_thirty_plausible_untyped_candidates_still_prevent_matching(self):
        self.seed(quote=TEXT, body=TEXT)
        for index in range(130):
            self.add_candidate(index, action_identity=None)
        src = self.ingest(source('crowded-unknown-query'))
        runtime = Runtime()
        runtime.verify_source_action = Mock(side_effect=AssertionError('No unique full candidate set'))
        runtime.recover_source_action_evidence = Mock(side_effect=AssertionError('No complete candidate set'))
        result = SourcePlanner(self.store, runtime).analyze(src, NOW, preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'], 'review')
        self.assertFalse(self.store._source_candidates_complete)
        self.assertEqual([call[0] for call in runtime.calls], ['analyze'])
        runtime.verify_source_action.assert_not_called()
        runtime.recover_source_action_evidence.assert_not_called()

    def test_malformed_legacy_types_are_not_exclusion_proof_or_mutated(self):
        incoming=action()
        values=[action(temporal_kind=[]),action(action_identity={'action_type':[]}),
                action(recipient=[])]
        for identifier in (['fictional-class'],{'id':'fictional-class'},42,'   ',None):
            values.append(action(recipient={'scope':'class','class_id':identifier}))
        before=deepcopy((incoming,values))
        self.assertTrue(all(not candidate_is_distinct(incoming,item)for item in values))
        self.assertEqual((incoming,values),before)


class PreviewVersionTests(unittest.TestCase):
    setUp = evidence_fixtures.SourceEvidenceTests.setUp
    tearDown = evidence_fixtures.SourceEvidenceTests.tearDown
    ingest = evidence_fixtures.SourceEvidenceTests.ingest
    seed = evidence_fixtures.SourceEvidenceTests.seed

    def protected(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                for table in ('proposals', 'operations', 'events', 'decision_intents', 'proposal_groups',
                              'proposal_group_members', 'canonical_actions', 'canonical_decisions', 'action_sources')}

    def preview(self):
        src = self.ingest(mode='preview')
        SourcePlanner(self.store, Runtime()).analyze(src, NOW, preview=True)
        return src

    def make_old(self, version=None):
        row = self.store.db.execute('SELECT * FROM source_previews ORDER BY id LIMIT 1').fetchone()
        payload = json.loads(row['payload'])
        if version is None:
            payload.pop('planner_version')
        else:
            payload['planner_version'] = version
        with self.store.db:
            self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        return row['id']

    def test_unknown_version_ready_is_pending_stale_and_not_health_or_api_readiness(self):
        self.seed(quote=TEXT, body=TEXT)
        src = self.preview()
        self.make_old()
        before = self.protected()
        original_hash = src['content_hash']
        self.assertEqual([item['id'] for item in self.store.pending_sources(preview=True)], [src['id']])
        self.assertEqual(self.store.get_source(src['id'])['content_hash'], original_hash)
        self.assertEqual(self.store.source_status()['preview_count'], 0)
        self.assertEqual(self.store.source_status()['preview_pending'], 1)
        self.assertEqual(self.store.list_source_previews(status='ready'), [])
        self.assertEqual(self.store.list_source_previews()[0]['status'], 'stale')
        self.assertFalse(self.store.list_source_previews()[0]['planner_current'])
        health = read_connection_state(self.store.db, NOW)['sources']
        self.assertEqual(health['preview_count'], 0)
        self.assertEqual(health['kinds']['announcement']['pending'], 1)
        self.assertEqual(before, self.protected())

    def test_stale_virtual_plan_does_not_supply_a_matching_target(self):
        first = self.preview()
        old_action = self.store.list_source_previews()[0]['outcomes'][0]['action_id']
        self.make_old('older-planner')
        second = self.ingest(source('second'))
        runtime = Runtime()
        result = SourcePlanner(self.store, runtime).analyze(second, NOW, preview=True)
        self.assertNotEqual(result['outcomes'][0]['action_id'], old_action)
        self.assertEqual([call[0] for call in runtime.calls], ['analyze'])
        self.assertEqual([item['id'] for item in self.store.pending_sources(preview=True)], [first['id']])

    def test_failed_refresh_replaces_old_ready_without_reusing_old_outcomes(self):
        src = self.preview()
        preview_id = self.make_old()
        before = self.protected()
        self.store.fail_source(src['id'], preview=True)
        failed = self.store.list_source_previews()[0]
        self.assertEqual((failed['id'], failed['status']), (preview_id, 'failed'))
        self.assertTrue(failed['planner_current'])
        self.assertNotIn('outcomes', failed)
        self.assertEqual(len(self.store.pending_sources(preview=True)), 1)
        SourcePlanner(self.store, Runtime()).analyze(src, NOW, preview=True)
        ready = self.store.list_source_previews()[0]
        self.assertEqual((ready['id'], ready['status']), (preview_id, 'ready'))
        self.assertEqual(ready['planner_version'], SOURCE_PREVIEW_PLANNER_VERSION)
        self.assertEqual(self.store.pending_sources(preview=True), [])
        self.assertEqual(before, self.protected())

    def test_old_failed_preview_is_stale_and_not_a_current_error(self):
        src=self.preview();self.store.fail_source(src['id'],preview=True);self.make_old()
        self.assertEqual(self.store.list_source_previews()[0]['status'],'stale')
        self.assertEqual(self.store.list_source_previews(status='failed'),[])
        self.assertEqual(self.store.source_status()['preview_failed'],0)
        self.assertEqual(self.store.source_status()['preview_pending'],1)
        health=read_connection_state(self.store.db,NOW)['sources']['kinds']['announcement']
        self.assertEqual((health['pending'],health['failed']),(1,0))

    def test_activation_rejects_old_previews_and_old_plan_before_collection(self):
        self.preview()
        self.make_old()
        plan = self.path.parent / 'activation.json'
        before = self.protected()
        with self.assertRaisesRegex(ValueError, 'ready preview'):
            activate_sources(self.store, Runtime(), plan_path=plan)
        self.assertFalse(plan.exists())
        src = self.store.pending_sources(preview=True)[0]
        SourcePlanner(self.store, Runtime()).analyze(src, NOW, preview=True)
        activate_sources(self.store, Runtime(), plan_path=plan)
        document = json.loads(plan.read_text())
        document['planner_version'] = 'old-planner'
        document['fingerprint'] = digest({key: value for key, value in document.items() if key != 'fingerprint'})
        plan.write_text(json.dumps(document))
        self.store.config = replace(self.store.config, multi_source_mode='active')
        factory = Mock()
        with self.assertRaisesRegex(ValueError, 'planner changed'):
            activate_sources(self.store, Runtime(), plan_path=plan, apply=True, client_factory=factory, current=NOW)
        factory.assert_not_called()
        self.assertEqual(before, self.protected())

    def test_decision_snapshot_binds_planner_version_without_rewriting_authority(self):
        self.preview()
        before = self.protected()
        snapshot = self.store.source_decision_snapshot()
        with patch('app.source_store.SOURCE_PREVIEW_PLANNER_VERSION', 'future-planner'):
            self.assertNotEqual(self.store.source_decision_snapshot(), snapshot)
        self.assertEqual(before, self.protected())

    def test_stale_filter_and_status_are_computed_before_pagination(self):
        self.preview(); self.make_old()
        current = self.ingest(source('new-current'))
        SourcePlanner(self.store, Runtime()).analyze(current, NOW, preview=True)
        ready = self.store.list_source_previews(status='ready', limit=1)
        stale = self.store.list_source_previews(status='stale', limit=1)
        self.assertEqual((len(ready), len(stale)), (1, 1))
        self.assertEqual(ready[0]['source_id'], current['id'])
        self.assertEqual(stale[0]['status'], 'stale')

    def test_current_sql_and_python_contract_fail_closed_on_malformed_unknown_values(self):
        values = ['broken json', 'null', '[]', '{}', '{"planner_version":null}',
                  '{"planner_version":{}}', json.dumps({'planner_version': 'older'}),
                  json.dumps({'planner_version': SOURCE_PREVIEW_PLANNER_VERSION})]
        db = sqlite3.connect(':memory:')
        try:
            db.execute('CREATE TABLE previews(payload TEXT)')
            db.executemany('INSERT INTO previews VALUES (?)', [(value,) for value in values])
            self.assertEqual([row[0] for row in db.execute('SELECT ' + current_preview_sql() + ' FROM previews p')], [0]*7+[1])
            self.assertEqual([preview_is_current(value) for value in values], [False]*7+[True])
        finally:
            db.close()

    @patch('app.core.utcnow', return_value=NOW.isoformat())
    def test_version_refresh_preserves_thirty_one_call_budget_and_resumes_remaining_sources(self, _clock):
        self.store.config = replace(self.store.config, multi_source_mode='preview')
        sources = [source('fictional-' + str(index)) for index in range(33)]
        self.store.ingest_sources({'sources': sources, 'complete_kinds': KINDS, 'learner': LEARNER, 'status': 'ok'}, NOW)
        for src in self.store.pending_sources(preview=True):
            self.store.save_source_preview(src['id'], [])
        with self.store.db:
            self.store.db.execute("UPDATE source_previews SET payload='{}'")
        before = self.protected()
        runtime = SourceRuntime()
        result = SyncController(self.store, SourceClient(sources, count=0), runtime).run(current=NOW)
        self.assertEqual(result['sources']['model_calls'], 31)
        self.assertEqual(len(self.store.list_source_previews(status='ready')), 31)
        self.assertEqual(len(self.store.pending_sources(preview=True)), 2)
        second = SyncController(self.store, SourceClient(sources, count=0), SourceRuntime()).run(current=NOW)
        self.assertEqual(second['sources']['model_calls'], 2)
        self.assertEqual(len(self.store.list_source_previews(status='ready')), 33)
        self.assertEqual(self.store.pending_sources(preview=True), [])
        self.assertEqual(before, self.protected())


if __name__ == '__main__':
    unittest.main()

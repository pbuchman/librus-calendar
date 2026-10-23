"""Fictional selection/CAS contracts before production planner integration."""
import copy
import hashlib
import json
from contextlib import closing
from datetime import timedelta
import sqlite3

from app.agenda import agenda_prompt, validate_agenda_plan, MAX_INPUT_BYTES
from app.agenda_incremental import assemble_incremental_snapshot, LEDGER_SCHEMA, source_version
from app.codex_runtime import RuntimeFailure
from app.core import digest
from test_agenda import AgendaTests, NOW, action, item, plan


class IncrementalSelectionTests(AgendaTests):
    def incremental(self, calendar=None):
        return assemble_incremental_snapshot(self.store, calendar or {}, current=NOW,
                                             owner_email=self.store.config.calendar_owner)

    def bootstrap(self):
        stamp = NOW.isoformat()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET status='analyzed',analyzed_at=?", (stamp,))
            self.store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,NULL)',
                ('prior-full-plan', 'fictional-full-hash', '{}', json.dumps({
                 'model': 'gpt-6.1-sol', 'effort': 'high', 'prompt_version': 'simple-agenda-v4-shared-learner'}), 'applied', stamp))

    def checkpoint(self, snapshot, contract):
        self.store.db.executescript(LEDGER_SCHEMA)
        with self.store.db:
            for version in contract['processed_versions']:
                self.store.db.execute('INSERT OR REPLACE INTO agenda_processed_sources VALUES (?,?,?,?,?)',
                    (version['source_id'], digest(version), json.dumps(version), 'fictional-plan', NOW.isoformat()))

    def test_read_only_new_three_sources_one_obligation_and_distinct_hashes(self):
        before = self.store.db.total_changes
        snapshot, contract = self.incremental()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            other, other_contract = assemble_incremental_snapshot(db, {}, current=NOW)
        self.assertEqual((snapshot, contract), (other, other_contract))
        self.assertEqual(len(snapshot['sources']), 3)
        self.assertEqual(len(validate_agenda_plan(plan(snapshot, [item(snapshot)]), snapshot)['items']), 1)
        self.assertEqual(contract['request_hash'], hashlib.sha256(agenda_prompt(snapshot).encode()).hexdigest())
        self.assertNotEqual(contract['request_hash'], contract['local_state_hash'])
        self.assertEqual(before, self.store.db.total_changes)

    def test_bootstrap_only_compatible_applied_analyzed_not_deleted_pending_failed(self):
        self.bootstrap()
        snapshot, contract = self.incremental()
        self.assertEqual(snapshot['sources'], [])
        self.assertEqual(len(contract['bootstrap_versions']), 3)
        with self.store.db:
            self.store.db.execute("UPDATE sources SET status='pending' WHERE kind='message'")
            self.store.db.execute("UPDATE sources SET status='failed' WHERE kind='announcement'")
            self.store.db.execute("UPDATE sources SET deleted=1 WHERE kind='school_event'")
        snapshot, contract = self.incremental()
        self.assertEqual(len(snapshot['sources']), 3)
        self.assertEqual(contract['bootstrap_versions'], {})
        with self.store.db:
            self.store.db.execute("UPDATE sources SET status='analyzed',deleted=0")
            self.store.db.execute("UPDATE agenda_plans SET status='preview'")
        self.assertEqual(len(self.incremental()[0]['sources']), 3)

    def test_deletion_retention_same_hash_processed_once_restart(self):
        snapshot, contract = self.incremental()
        self.checkpoint(snapshot, contract)
        self.assertEqual(self.incremental()[0]['sources'], [])
        with self.store.db:
            self.store.db.execute("UPDATE sources SET deleted=1 WHERE kind='announcement'")
            self.store.db.execute("UPDATE sources SET status='retained',payload=json_set(payload,'$.text','') WHERE kind='message'")
        changed, changed_contract = self.incremental()
        self.assertEqual(len(changed['sources']), 2)
        self.assertFalse(any(source['body_available'] for source in changed['sources']))
        self.assertEqual({source['id']: source['content_hash'] for source in snapshot['sources']},
                         {source['id']: source['content_hash'] for source in changed['source_catalog']})
        self.checkpoint(changed, changed_contract)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(assemble_incremental_snapshot(db, {}, current=NOW)[0]['sources'], [])

    def test_changed_source_and_external_decision_include_available_linked_bodies(self):
        self.bootstrap()
        event = self.inventory()
        sid = self.store.db.execute("SELECT id,content_hash FROM sources WHERE kind='announcement'").fetchone()
        with self.store.db:
            self.store.db.execute('INSERT INTO action_sources VALUES (?,?,?,?,?,?)',
                ('existing-action', sid['id'], sid['content_hash'], '', 'supports', NOW.isoformat()))
            self.store.db.execute('INSERT INTO canonical_decisions VALUES (?,?,?)',
                ('existing-action', '{"user_approved":true}', NOW.isoformat()))
        snapshot, contract = self.incremental({event['id']: event})
        self.assertEqual([source['id'] for source in snapshot['sources']], [sid['id']])
        self.assertEqual(contract['selection_reasons'][sid['id']], ['human_or_calendar_context'])
        with self.store.db:
            self.store.db.execute('DELETE FROM canonical_decisions')
        # inventory's saved fingerprint differs, making this an external edit.
        self.assertEqual(len(self.incremental({event['id']: event})[0]['sources']), 1)

    def test_local_cas_changes_when_omitted_old_body_changes_even_without_hash(self):
        self.bootstrap()
        before, contract = self.incremental()
        self.assertEqual(before['sources'], [])
        with self.store.db:
            self.store.db.execute("UPDATE sources SET payload=json_set(payload,'$.text','Changed fictional old body') WHERE kind='announcement'")
        after, changed = self.incremental()
        self.assertEqual(after['sources'], [])
        self.assertNotEqual(contract['local_state_hash'], changed['local_state_hash'])

    def test_catalog_refs_link_allowed_but_catalog_only_create_blocked(self):
        self.bootstrap()
        event = self.inventory()
        snapshot, _ = self.incremental({event['id']: event})
        self.assertEqual(snapshot['sources'], [])
        refs = [{'source_id': source['id'], 'source_hash': source['content_hash']} for source in snapshot['source_catalog']]
        linked = item(snapshot, 'link', action=None, action_id='existing-action', sources=refs)
        validate_agenda_plan(plan(snapshot, [linked]), snapshot)
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_source_body_required'):
            validate_agenda_plan(plan(snapshot, [item(snapshot, sources=refs)]), snapshot)

    def test_growth_86_sources_46_agenda_keep_all_state_under_unchanged_cap(self):
        with self.store.db:
            source = dict(self.store.db.execute('SELECT * FROM sources LIMIT 1').fetchone())
            for index in range(83):
                row = {**source, 'id': 'fictional-growth-%d' % index, 'upstream_id': 'growth-%d' % index}
                payload = json.loads(row['payload']); payload['text'] = 'Fictional ignored history. ' * 180
                row['payload'] = json.dumps(payload)
                self.store.db.execute('INSERT INTO sources (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            for index in range(46):
                self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                    ('agenda-%d' % index, 'proposal-%d' % index, json.dumps(action()), 'applied', NOW.isoformat()))
        self.bootstrap()
        with self.store.db:
            self.store.db.execute("UPDATE sources SET deleted=1 WHERE id IN (SELECT id FROM sources ORDER BY id LIMIT 6)")
            self.store.db.execute("UPDATE sources SET status='pending',analyzed_at=NULL WHERE id IN (SELECT id FROM sources ORDER BY id DESC LIMIT 2)")
        snapshot, contract = self.incremental()
        self.assertEqual((len(snapshot['source_catalog']), len(snapshot['sources']), len(snapshot['agenda'])), (86, 8, 46))
        self.assertLess(contract['input_bytes'], MAX_INPUT_BYTES)
        self.assertEqual(len(contract['bootstrap_versions']), 78)
        # Agenda itself may overflow: selection never truncates it to fit.
        with self.store.db:
            self.store.db.execute("UPDATE canonical_actions SET payload=json_set(payload,'$.description',?)", ('x' * 5000,))
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow'):
            self.incremental()

    def test_old_decisions_do_not_select_bodies_new_decision_does_then_checkpoint_quiets(self):
        from app.codex_runtime import event_fingerprint
        event = self.inventory()
        sid = self.store.db.execute("SELECT id,content_hash FROM sources WHERE kind='announcement'").fetchone()
        with self.store.db:
            self.store.db.execute('UPDATE events SET fingerprint=?', (event_fingerprint(event),))
            self.store.db.execute('INSERT INTO action_sources VALUES (?,?,?,?,?,?)',
                ('existing-action', sid['id'], sid['content_hash'], '', 'supports', NOW.isoformat()))
            self.store.db.execute('INSERT INTO canonical_decisions VALUES (?,?,?)',
                ('existing-action', '{"user_approved":true}', NOW.isoformat()))
        self.bootstrap()
        self.assertEqual(self.incremental({event['id']: event})[0]['sources'], [])
        with self.store.db:
            self.store.db.execute("UPDATE canonical_decisions SET updated_at='2030-03-01T13:00:00+01:00'")
        snapshot, contract = self.incremental({event['id']: event})
        self.assertEqual(len(snapshot['sources']), 1)
        self.assertEqual(snapshot['sources'][0]['source_role'], 'context')
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_source_body_required'):
            validate_agenda_plan(plan(snapshot, [item(snapshot)]), snapshot)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                ('agenda_authority_baseline', json.dumps(contract['authority_versions'])))
        self.assertEqual(self.incremental({event['id']: event})[0]['sources'], [])

    def test_retention_after_compatible_full_plan_cannot_bootstrap_old_timestamp(self):
        self.bootstrap()
        self.assertEqual(self.incremental()[0]['sources'], [])
        before = {row['id']: (row['content_hash'], row['analyzed_at'])
                  for row in self.store.db.execute('SELECT id,content_hash,analyzed_at FROM sources')}
        self.store.prune_source_content(current=NOW + timedelta(days=45), apply=True)
        snapshot, contract = self.incremental()
        self.assertEqual(len(snapshot['sources']), 3)
        self.assertEqual(contract['bootstrap_versions'], {})
        for source in snapshot['sources']:
            self.assertEqual(source['status'], 'retained')
            self.assertFalse(source['body_available'])
            row = self.store.db.execute('SELECT content_hash,analyzed_at FROM sources WHERE id=?', (source['id'],)).fetchone()
            self.assertEqual(tuple(row), before[source['id']])
        self.checkpoint(snapshot, contract)
        self.assertEqual(self.incremental()[0]['sources'], [])

    def test_selected_sources_require_explicit_coverage_grouped_ignore_allowed(self):
        snapshot, _ = self.incremental()
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_sources_unprocessed'):
            validate_agenda_plan(plan(snapshot, []), snapshot)
        partial = item(snapshot)
        partial['sources'] = partial['sources'][:1]
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_sources_unprocessed'):
            validate_agenda_plan(plan(snapshot, [partial]), snapshot)
        ignored = item(snapshot, 'ignore', action=None)
        result = validate_agenda_plan(plan(snapshot, [ignored]), snapshot)
        self.assertEqual(len(result['items']), 1)
        self.assertIsNone(result['items'][0]['action_id'])
        self.assertEqual(len(result['items'][0]['sources']), 3)
        self.bootstrap()
        catalog_only, _ = self.incremental()
        validate_agenda_plan(plan(catalog_only, []), catalog_only)

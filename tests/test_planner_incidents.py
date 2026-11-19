"""Planner incident evidence against fictional input and a local journal."""
import json
import unittest
from unittest.mock import patch

from app.agenda import AgendaPlanner, agenda_prompt, validate_agenda_plan
from app.codex_runtime import RuntimeFailure
from app.incidents import capture
from app.planner_incidents import incident_error_code, validate_event
import test_agenda as fixtures
import test_agenda_integration as integration


class PlannerIncidentTests(unittest.TestCase):
    setUp = fixtures.AgendaTests.setUp
    tearDown = fixtures.AgendaTests.tearDown
    ready = integration.AgendaIntegrationTests.ready

    def test_budget_captures_exact_bytes_before_provider_call(self):
        self.ready()
        runtime = integration.Calendar(lambda snapshot: [fixtures.item(snapshot)])
        events = []
        with capture(events.append), patch('app.agenda.MAX_INPUT_BYTES', 128):
            with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow') as result:
                AgendaPlanner(self.store, runtime).run(fixtures.NOW)
        event = next((entry for entry in events if entry['operation'] == 'input_budget'), None)
        self.assertIsNotNone(event, events)
        self.assertEqual((event['reason'], event['decision']), ('overflow', 'blocked'))
        evidence = event['evidence']
        self.assertEqual((evidence['input_bytes'], evidence['limit_bytes']), (result.exception.input_bytes, 128))
        self.assertEqual(sum(evidence['section_bytes'].values()), evidence['input_bytes'])
        self.assertEqual((evidence['source_count'], evidence['catalog_count']), (3, 3))
        self.assertEqual(runtime.calls, 0)

    def test_distinguishes_bad_schema_from_stale_source_version_at_position(self):
        snapshot = fixtures.AgendaTests.snapshot(self)
        sensitive = 'fictional-private-value'
        events = []
        invalid = fixtures.item(snapshot)
        invalid['action']['all_day'] = sensitive
        obsolete = fixtures.item(snapshot)
        obsolete['sources'][1]['source_hash'] = sensitive
        for bad_item, code in ((invalid, 'agenda_plan_invalid'), (obsolete, 'agenda_source_reference_invalid')):
            with capture(events.append), self.assertRaisesRegex(RuntimeFailure, code):
                validate_agenda_plan(fixtures.plan(snapshot, [bad_item]), snapshot)
        failures = [entry for entry in events if entry['operation'] == 'plan_validation']
        self.assertEqual([entry['reason'] for entry in failures], ['schema_shape', 'source_version_mismatch'])
        self.assertEqual(failures[0]['evidence']['path'], ['items', 0, 'action', 'all_day'])
        self.assertEqual(failures[1]['evidence']['ref_position'], 2)
        self.assertNotIn(sensitive, json.dumps(events))

    def test_wire_shape_includes_missing_columns_without_unknown_field_names(self):
        from test_agenda_wire import fictional_snapshot

        snapshot = fictional_snapshot()
        row = snapshot['source_catalog'][1]
        row['fictional-private-column'] = row.pop('body_available')
        events = []
        with capture(events.append), self.assertRaisesRegex(RuntimeFailure, 'agenda_snapshot_invalid'):
            agenda_prompt(snapshot)
        failure, = events
        self.assertEqual(failure['reason'], 'catalog_wire_shape')
        self.assertEqual(failure['evidence'], {'row_position': 2, 'received_type': 'object',
                                              'missing_fields': ['body_available'], 'unexpected_field_count': 1})
        self.assertNotIn('fictional-private-column', json.dumps(events))

    def test_source_change_rejects_specific_cas_fields_and_preserves_ledger(self):
        self.ready()
        runtime = integration.Calendar(lambda snapshot: [fixtures.item(snapshot)])
        def change_source():
            with self.store.db:
                self.store.db.execute("UPDATE sources SET content_hash='changed-private-version' WHERE kind='announcement'")
        runtime.on_plan = change_source
        events = []
        with capture(events.append), self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_stale'):
            AgendaPlanner(self.store, runtime).run(fixtures.NOW)
        event = next((entry for entry in events if entry['operation'] == 'materialization_cas'), None)
        self.assertIsNotNone(event, events)
        self.assertEqual(event['evidence']['failed_checks'], ['local_state_hash', 'processed_versions'])
        self.assertEqual(event['evidence']['changed_sections'], ['sources', 'source_catalog'])
        self.assertEqual(event['evidence']['source_position'], 1)
        self.assertNotIn('changed-private-version', json.dumps(events))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM agenda_processed_sources').fetchone()[0], 0)
        self.assertEqual(self.store.list_proposals(), [])

    def test_reports_committed_downgrade_and_later_reconciliation(self):
        self.ready()
        runtime = integration.Calendar(lambda snapshot: [fixtures.item(snapshot)])
        planner = AgendaPlanner(self.store, runtime)
        planner.run(fixtures.NOW)
        self.store.set_writes_paused(False)
        operation, = self.store.queue_operations(fixtures.NOW)
        events = []
        with capture(events.append):
            self.assertTrue(self.store.claim_operation(operation['operation_id'], expected_operation=operation))
            applied = runtime.execute(operation)
            self.store.record_result(operation['operation_id'], {'status': 'applied'}, agenda_calendar=planner.calendar,
                                     current=fixtures.NOW)
            self.assertEqual(self.store.db.execute('SELECT status FROM operations').fetchone()[0], 'unknown')
            reconciliation, = self.store.reconcile_operations()
            self.assertTrue(reconciliation['reconcile_only'])
            self.store.record_result(operation['operation_id'], runtime.execute(reconciliation),
                                     agenda_calendar=planner.calendar, current=fixtures.NOW)
        journal = [entry for entry in events if entry['operation'] == 'journal']
        self.assertEqual([entry['reason'] for entry in journal], ['missing_verified_event', 'reconciled'])
        self.assertEqual([entry['evidence']['journal_status'] for entry in journal], ['unknown', 'applied'])
        self.assertEqual(journal[0]['evidence']['runtime_status'], 'applied')
        self.assertNotIn(applied['event_id'], json.dumps(journal))
        self.assertEqual(len(runtime.writes), 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM operations').fetchone()[0], 'applied')

    def test_claim_reports_frozen_payload_mismatch_without_attempt(self):
        self.ready()
        runtime = integration.Calendar(lambda snapshot: [fixtures.item(snapshot)])
        AgendaPlanner(self.store, runtime).run(fixtures.NOW)
        self.store.set_writes_paused(False)
        operation, = self.store.queue_operations(fixtures.NOW)
        stale = {**operation, 'event': {**operation['event'], 'summary': 'fictional-obsolete-title'}}
        events = []
        with capture(events.append):
            self.assertFalse(self.store.claim_operation(operation['operation_id'], expected_operation=stale))
        self.assertEqual([(entry['operation'], entry['reason']) for entry in events], [('claim', 'payload_changed')])
        self.assertEqual(self.store.db.execute('SELECT status,attempts FROM operations').fetchone()[:], ('pending', 0))
        self.assertNotIn('fictional-obsolete-title', json.dumps(events))

    def test_timeout_report_has_dispatch_and_committed_journal_then_read_only_recovery(self):
        from app.core import AppStore
        from app.incidents import read_incident
        from app.sync import SyncController
        from test_core import NOW
        from test_sync import FakeClient
        from test_support import TEST_CONFIG

        path = self.path.with_name('controller.sqlite3')
        store = AppStore(path, config=TEST_CONFIG)
        self.addCleanup(store.close)
        store.set_writes_paused(False)
        runtime = integration.Calendar(lambda snapshot: [fixtures.item(snapshot)])
        runtime.timeout_after_write = True
        failed = SyncController(store, FakeClient(), runtime).run(current=NOW)
        recovered = SyncController(store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual((failed['status'], recovered['status']), ('partial', 'ok'))
        failure = read_incident(path)
        self.assertEqual(failure['run']['id'], failed['run_id'])
        report = failure['report']['events']
        self.assertTrue(any(row['operation'] == 'claim' and row['decision'] == 'committed' for row in report))
        first, = [row['evidence'] for row in report if row['operation'] == 'dispatch']
        self.assertEqual((first['dispatch_mode'], first['runtime_status']), ('write_intent', 'unknown'))
        first_journal, = [row['evidence'] for row in report if row['operation'] == 'journal']
        self.assertEqual(first_journal['journal_status'], 'unknown')
        report = read_incident(path, run=recovered['run_id'])['report']['events']
        second, = [row['evidence'] for row in report if row['operation'] == 'dispatch']
        self.assertEqual((second['dispatch_mode'], second['runtime_status']), ('read_only', 'applied'))
        settled, = [row for row in report if row['operation'] == 'journal']
        self.assertEqual((settled['kind'], settled['reason'], settled['evidence']['journal_status']),
                         ('observation', 'reconciled', 'applied'))
        self.assertEqual(len(runtime.writes), 1)

    def test_rejects_unknown_or_unhashable_incident_fields(self):
        for operation, reason, evidence in (
                ([], 'payload_changed', {'position': 1, 'action': 'create'}),
                ('claim', [], {'position': 1, 'action': 'create'}),
                ('claim', 'payload_changed', {'position': 1, 'action': ['fictional-canary']}),
                ('claim', 'payload_changed', {'position': 1, 'action': 'fictional-canary'}),
                ('claim', 'payload_changed', {'position': 1, 'action': 'create', 'raw': 'fictional-canary'})):
            with self.subTest(operation=operation, reason=reason), self.assertRaises(ValueError):
                validate_event(operation, reason, 'skipped', evidence)
        error = RuntimeError('fictional-canary')
        error.code = ['fictional-canary']
        self.assertEqual(incident_error_code(error), 'operation_failed')

from test_support import TEST_CONFIG
from datetime import datetime, timedelta
import fcntl
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.core import AppStore, WARSAW
from app.sync import SyncController, SyncInterrupted
from test_core import NOW, QUOTE, proposal
import test_runtime as runtime_fixtures
from app.codex_runtime import CodexRuntime
import json


class FakeClient:
    def __init__(self, count=1, fail_ids=(), old_after=None):
        self.count = count
        self.fail_ids = set(fail_ids)
        self.old_after = old_after
        self.pages = []
        self.details = []

    def messages(self, limit=10, page=1):
        self.pages.append(page)
        ids = range((page-1)*limit, min(page*limit, self.count))
        return [{'messageId': str(i), 'topic': 'School', 'senderName': 'Teacher', 'readDate': 'already read',
                 'sendDate': '2026-08-01T10:00:00+02:00' if self.old_after is not None and i >= self.old_after else '2026-10-08T10:00:00+02:00'} for i in ids], self.count

    def message(self, mid):
        self.details.append(mid)
        if mid in self.fail_ids:
            raise RuntimeError('Simulated private failure')
        return {'message_id': mid, 'subject': 'School '+mid, 'sender': 'Teacher', 'sent_at': '2026-10-08T10:00:00+02:00', 'text': QUOTE, 'read_date_returned': 'already read'}

    def collect_sources(self, **kwargs):
        return {'learner': {'id': 'fictional-learner', 'class_id': '1x'}, 'sources': [],
                'complete_kinds': ['announcement','school_event','substitution'], 'status':'ok'}


class FakeRuntime:
    def __init__(self, propose=True, fail_analysis=False, outcome='applied'):
        self.propose, self.fail_analysis, self.outcome = propose, fail_analysis, outcome
        self.analyzed = []
        self.executed = []

    def probe(self):
        return {'calendar_id': 'calendar@example.test', 'access_role': 'owner'}

    def analyze(self, message, events):
        self.analyzed.append(message['id'])
        if self.fail_analysis:
            raise RuntimeError('Simulated model failure')
        return [proposal(title='Trip '+message['id'], start='2030-11-10T%02d:00:00+01:00' % (9 + int(message['id']) % 10))] if self.propose else []

    def plan_agenda(self, snapshot):
        from test_agenda import item, plan
        self.agenda_calls = getattr(self, 'agenda_calls', 0) + 1
        result = []
        for source in snapshot['sources']:
            if source['kind'] != 'message' or source['status'] not in ('pending','failed'):
                continue
            message = {'id': source['id'].removeprefix('message:'), 'text': source['text']}
            for raw in self.analyze(message, []):
                projected = {key: raw.get(key) for key in ('title','description','start','end','all_day','temporal_kind','due_at','activity_scope')}
                projected.update(optional=False, temporal_kind=raw.get('temporal_kind', 'event'), activity_scope=raw.get('activity_scope', 'school'))
                result.append(item(snapshot, 'review' if raw.get('needs_review') or raw.get('confidence', 'high') == 'low' or raw.get('activity_scope', 'school') != 'school' else 'create', review_change='create' if raw.get('needs_review') or raw.get('confidence', 'high') == 'low' or raw.get('activity_scope', 'school') != 'school' else None, action=projected, sources=[{'source_id':source['id'],'source_hash':source['content_hash']}]))
        self.last_agenda_metadata = {key: value for key, value in getattr(self, 'last_analysis_metadata', {}).items() if key in ('model','effort','prompt_version')}
        return plan(snapshot, result)

    def read_event(self, eid, expected=None):
        found = next((operation for operation in reversed(self.executed)
                      if eid == 'event-' + operation['message_id']), None)
        if found:
            return {**found['event'], 'id': eid}
        return dict(expected or {}, id=eid)

    def execute(self, op):
        self.executed.append(op)
        return {'status': self.outcome, 'event_id': 'event-'+op['message_id'], 'snapshot': {**op['event'], 'id': 'event-'+op['message_id']}, 'fingerprint': 'fp'}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name)/'data/state.sqlite3', config=TEST_CONFIG)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pagination_bounded_and_resumes_without_losing_tail(self):
        client, runtime = FakeClient(count=125), FakeRuntime(propose=False)
        result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(result['fetched'], 100)
        self.assertEqual(self.store._setting('discovery_page'), '11')
        self.assertLessEqual(len(set(client.details)), 100)
        self.assertIsNone(self.store.get_message('124'))
        result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertIsNotNone(self.store.get_message('100'))
        self.assertIsNotNone(self.store.get_message('109'))
        self.assertIsNotNone(self.store.get_message('124'))
        self.assertEqual(self.store.status()['messages'], 125)
        self.assertEqual(len(runtime.analyzed), 125)

    def test_limit_one_cursor_and_page_size_change_fill_history_in_same_run(self):
        client, runtime = FakeClient(count=25), FakeRuntime(propose=False)
        first = SyncController(self.store, client, runtime, max_messages=1).run(current=NOW)
        self.assertEqual(first['fetched'], 1)
        self.assertEqual(self.store._setting('discovery_page'), '2')
        self.assertEqual(self.store._setting('discovery_page_size'), '1')
        # Even an older persisted cursor must not be interpreted under a new size.
        self.store.set_setting('discovery_page', '3')
        client.pages.clear()
        full = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(full['fetched'], 25)
        self.assertEqual(client.pages, [1, 2, 3])
        self.assertEqual(self.store.status()['messages'], 25)
        self.assertEqual(self.store._setting('discovery_page_size'), '10')
        self.assertEqual(self.store._setting('discovery_page'), '2')

    def test_30day_history_ends_at_old_page(self):
        client, runtime = FakeClient(count=60, old_after=12), FakeRuntime(propose=False)
        result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(result['fetched'], 12)
        self.assertEqual(client.pages, [1, 2, 3])
        self.assertIsNone(self.store.get_message('12'))

    def test_per_message_fetch_failure_retry_without_reanalysis_or_duplicate_writes(self):
        client, runtime = FakeClient(count=3, fail_ids={'1'}), FakeRuntime()
        self.store.set_writes_paused(False)
        first = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(first['fetch_failures'], 1)
        self.assertEqual(self.store.get_message('1')['status'], 'fetch_failed')
        client.fail_ids.clear()
        second = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(second['fetch_failures'], 0)
        self.assertEqual(sorted(runtime.analyzed), ['0','1','2'])
        self.assertEqual(len(runtime.executed), 3)
        self.assertEqual(self.store.status()['events'], 3)

    def test_analysis_failure_retries_from_durable_queue(self):
        client, runtime = FakeClient(), FakeRuntime(fail_analysis=True)
        first = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(first['analysis_failures'], 1)
        self.assertEqual(self.store.get_message('0')['status'], 'analysis_pending')
        self.assertEqual(self.store.agenda_status()['latest_plan']['status'], 'failed')
        runtime.fail_analysis = False
        second = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(second['analyzed'], 1)
        self.assertEqual(self.store.get_message('0')['status'], 'analyzed')

    def test_successful_empty_plan_persists_current_call_provenance(self):
        runtime = FakeRuntime(propose=False)
        runtime.last_analysis_metadata = {'model':'gpt-6.1-sol','effort':'high','prompt_version':'fictional-shared-plan'}
        result = SyncController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(result['analyzed'], 1)
        metadata = self.store.agenda_status()['latest_plan']['metadata']
        self.assertEqual(metadata['model'], 'gpt-6.1-sol')
        self.assertEqual(metadata['effort'], 'high')
        self.assertEqual(metadata['prompt_version'], 'fictional-shared-plan')
        self.assertEqual(self.store.get_message('0')['status'], 'analyzed')
        self.assertEqual(self.store.get_analysis_history('0'), [])  # No second per-message analysis journal.

    def test_dry_run_and_default_pause_skip_all_execution(self):
        client, runtime = FakeClient(), FakeRuntime()
        SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(runtime.executed, [])
        self.store.set_writes_paused(False)
        before = list(self.store.db.execute('SELECT * FROM operations'))
        result = SyncController(self.store, client, runtime).run(dry_run=True, current=NOW)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(runtime.executed, [])
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM operations')))
        self.assertEqual(len(self.store.pending_operations()), 1)

    def test_unknown_operation_only_reconciles_never_new_create(self):
        client, runtime = FakeClient(), FakeRuntime(outcome='unknown')
        self.store.set_writes_paused(False)
        SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(self.store.status()['operations_unknown'], 1)
        runtime.executed.clear()
        SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(len(runtime.executed), 1)
        self.assertTrue(runtime.executed[0]['reconcile_only'])
        self.assertEqual(self.store.pending_operations(), [])

    def test_empty_inbox_full_success_requires_verified_calendar_probe(self):
        self.store.set_writes_paused(False)
        runtime = FakeRuntime()
        result = SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'ok')
        success = self.store.status()['last_successful_sync']
        self.assertIsNotNone(success)
        runtime.probe = lambda: {'calendar_id': 'wrong', 'access_role': 'owner'}
        failed = SyncController(self.store, FakeClient(count=0), runtime).run(current=NOW)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error_code'], 'calendar_write_access_missing')
        self.assertEqual(failed['state']['last_stage'], 'calendar_probe')
        self.assertEqual(failed['state']['last_successful_sync'], success)

    def test_review_queue_does_not_prevent_full_success(self):
        self.store.set_writes_paused(False)
        runtime = FakeRuntime()
        runtime.analyze = lambda message, events: [proposal(confidence='low')]
        result = SyncController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['state']['review_count'], 1)
        self.assertEqual(result['state']['operations_pending'], 0)
        self.assertIsNotNone(result['state']['last_successful_sync'])

    def test_dry_run_and_pause_never_advance_last_success(self):
        self.store.set_writes_paused(False)
        SyncController(self.store, FakeClient(count=0), FakeRuntime()).run(current=NOW)
        success = self.store.status()['last_successful_sync']
        result = SyncController(self.store, FakeClient(count=0), FakeRuntime()).run(dry_run=True, current=NOW)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(result['state']['last_successful_sync'], success)
        self.store.set_writes_paused(True)
        result = SyncController(self.store, FakeClient(count=0), FakeRuntime()).run(current=NOW)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['state']['last_successful_sync'], success)

    def test_lock_and_run_record_precede_credentials_and_login(self):
        calls = []
        def initialize(stage):
            state = self.store.status()
            self.assertEqual(state['last_result'], 'running')
            self.assertEqual(state['last_stage'], 'credentials')
            before = list(self.store.db.execute('SELECT * FROM sync_runs'))
            other = SyncController(self.store, client_factory=lambda stage: calls.append('bad')).run()
            self.assertEqual(other['status'], 'already_running')
            self.assertEqual(before, list(self.store.db.execute('SELECT * FROM sync_runs')))
            self.assertEqual(calls, [])
            stage('login')
            raise RuntimeError('dummy-private-password API BODY')
        result = SyncController(self.store, client_factory=initialize, runtime_factory=FakeRuntime).run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'login_failed')
        self.assertEqual(result['state']['last_stage'], 'login')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['state']['consecutive_failures'], 1)
        SyncController(self.store, client_factory=initialize, runtime_factory=FakeRuntime).run()
        self.assertEqual(self.store.status()['consecutive_failures'], 2)

    def test_failure_before_credentials_records_attempt(self):
        def initialize(stage):
            raise FileNotFoundError('PRIVATE-FILE')
        result = SyncController(self.store, client_factory=initialize).run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'missing_private_file')
        self.assertIsNotNone(result['state']['last_attempt_at'])
        self.assertIsNone(result['state']['last_successful_sync'])

    def test_discovery_failure_and_model_limit_never_publish_success(self):
        self.store.set_writes_paused(False)
        client = FakeClient()
        def broken_list(**kwargs):
            raise RuntimeError('PRIVATE-API-BODY')
        client.messages = broken_list
        first = SyncController(self.store, client, FakeRuntime()).run(current=NOW)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['error_code'], 'discovery_failed')
        self.assertEqual(first['state']['last_stage'], 'discovery')
        self.assertNotIn('PRIVATE', str(first))
        class ModelLimit(Exception):
            code = 'codex_limit'
        runtime = FakeRuntime()
        def limited_analysis(message, events):
            raise ModelLimit('PRIVATE-MODEL-RESPONSE')
        runtime.analyze = limited_analysis
        second = SyncController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(second['status'], 'partial')
        self.assertEqual(second['state']['last_error_code'], 'codex_limit')
        self.assertEqual(second['state']['last_stage'], 'analysis')
        self.assertEqual(second['state']['consecutive_failures'], 2)
        self.assertIsNone(second['state']['last_successful_sync'])

    def test_initial_budget_exhaustion_is_partial_then_known_audit_can_succeed(self):
        self.store.set_writes_paused(False)
        client, runtime = FakeClient(count=125), FakeRuntime(propose=False)
        first = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['error_code'], 'discovery_incomplete')
        self.assertIsNone(first['state']['last_successful_sync'])
        second = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(second['status'], 'ok')
        third = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(third['status'], 'ok')

    def test_full_snapshot_replaces_per_message_analysis_budget(self):
        self.store.set_writes_paused(False)
        for index in range(3):
            self.store.save_message({'id':str(index),'text':QUOTE})
        runtime = FakeRuntime(propose=False)
        result = SyncController(self.store, FakeClient(count=0), runtime, max_messages=1).run(current=NOW)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['state']['messages_pending'], 0)
        self.assertEqual(runtime.agenda_calls, 1)
        self.assertEqual(len(runtime.analyzed), 3)

    def test_failed_write_and_unknown_write_prevent_success(self):
        self.store.set_writes_paused(False)
        failed = SyncController(self.store, FakeClient(), FakeRuntime(outcome='failed')).run(current=NOW)
        self.assertEqual(failed['status'], 'partial')
        self.assertEqual(failed['error_code'], 'operations_pending')
        self.assertIsNone(failed['state']['last_successful_sync'])
        unknown = SyncController(self.store, FakeClient(), FakeRuntime(outcome='unknown')).run(current=NOW)
        self.assertEqual(unknown['status'], 'partial')
        self.assertEqual(unknown['error_code'], 'operations_unknown')
        self.assertIsNone(unknown['state']['last_successful_sync'])

    def test_whole_run_timeout_and_interrupted_write_are_recorded(self):
        runtime = FakeRuntime()
        runtime.probe = lambda: time.sleep(0.1)
        timed = SyncController(self.store, FakeClient(count=0), runtime, timeout_seconds=0.02).run(current=NOW)
        self.assertEqual(timed['status'], 'interrupted')
        self.assertEqual(timed['error_code'], 'sync_timeout')
        self.assertIsNone(timed['state']['running_since'])
        self.store.set_writes_paused(False)
        runtime = FakeRuntime()
        def interrupted(operation):
            raise SyncInterrupted('sync_interrupted')
        runtime.execute = interrupted
        result = SyncController(self.store, FakeClient(), runtime).run(current=NOW)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['state']['operations_unknown'], 1)
        self.assertEqual(self.store.pending_operations(), [])
        self.assertTrue(self.store.reconcile_operations()[0]['reconcile_only'])

    def test_cli_returns_nonzero_for_partial_and_records_login_failure(self):
        from app.cli import main
        from contextlib import redirect_stdout
        self.store.set_writes_paused(False)
        with patch('app.librus_client.load_credentials', return_value={'login': 'synthetic-login', 'password': 'dummy-password'}), \
                patch('app.librus_client.LibrusClient', return_value=FakeClient()), \
                patch('app.codex_runtime.CodexRuntime', return_value=FakeRuntime(outcome='failed')):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['--state', str(self.store.path), 'sync'], config=TEST_CONFIG)
            self.assertEqual(code, 1)
            self.assertNotIn('PRIVATE', output.getvalue())
        with patch('app.librus_client.load_credentials', side_effect=FileNotFoundError('PRIVATE-FILE')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--state', str(self.store.path), 'sync'], config=TEST_CONFIG), 1)
        self.assertEqual(self.store.status()['last_error_code'], 'missing_private_file')


class ProbeAuditSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name) / 'state.sqlite3', config=TEST_CONFIG)
        self.store.set_writes_paused(False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def runtime(self, outputs):
        remaining = iter(outputs)
        self.process_calls = []
        def process(args, prompt, timeout):
            self.process_calls.append(args)
            return next(remaining)
        return CodexRuntime(TEST_CONFIG, run_process=process)

    def test_probe_recovery_is_persisted_before_discovery_and_full_success(self):
        runtime = self.runtime([runtime_fixtures.CalendarProbeTests.output(), runtime_fixtures.CalendarProbeTests.output(runtime_fixtures.CalendarProbeTests.call())])
        client = FakeClient(count=0)
        original = client.messages
        def messages(*args, **kwargs):
            rows = self.store.db.execute('SELECT result,attempt_number FROM calendar_probe_attempts ORDER BY attempt_number').fetchall()
            self.assertEqual([tuple(row) for row in rows], [('failed', 1), ('ok', 2)])
            return original(*args, **kwargs)
        client.messages = messages
        with patch('app.codex_runtime.time.sleep') as sleep:
            result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual(result['status'], 'ok')
        self.assertIsNotNone(result['state']['last_successful_sync'])
        self.assertEqual(result['state']['calendar_probe_attempts'], 2)
        self.assertTrue(result['state']['calendar_probe_recovered'])
        self.assertEqual(result['state']['calendar_probe_retries_24h'], 1)
        self.assertEqual(result['state']['calendar_probe_recoveries_24h'], 1)
        sleep.assert_called_once_with(5)
        self.assertEqual(len(self.process_calls), 2)

    def test_recovered_probe_followed_by_discovery_failure_is_not_full_success(self):
        import requests
        previous = self.store.start_sync_run()
        self.store.finish_sync_run(previous, 'ok')
        last_success = self.store.status()['last_successful_sync']
        runtime = self.runtime([runtime_fixtures.CalendarProbeTests.output(), runtime_fixtures.CalendarProbeTests.output(runtime_fixtures.CalendarProbeTests.call())])
        client = FakeClient(count=0)
        def failed(*_, **__):
            raise requests.exceptions.Timeout('PRIVATE discovery timeout')
        client.messages = failed
        with patch('app.codex_runtime.time.sleep'):
            result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual((result['status'], result['error_code']), ('partial', 'discovery_timeout'))
        self.assertEqual(result['state']['last_successful_sync'], last_success)
        self.assertEqual(result['state']['consecutive_failures'], 1)
        self.assertEqual(result['state']['calendar_probe_recoveries_24h'], 1)
        self.assertTrue(result['state']['calendar_probe_recovered'])

    def test_failed_probe_and_audit_failure_block_all_discovery_and_writes(self):
        previous = self.store.start_sync_run()
        self.store.finish_sync_run(previous, 'ok')
        last_success = self.store.status()['last_successful_sync']
        variants = [('count', None), ('start_audit', 1), ('finish_audit', 2)]
        for variant, fail_on in variants:
            client = FakeClient()
            runtime = self.runtime([runtime_fixtures.CalendarProbeTests.output(), runtime_fixtures.CalendarProbeTests.output()])
            persistence_calls = []
            original = self.store.record_probe_attempt
            def persist(run_id, metadata):
                persistence_calls.append(metadata)
                if len(persistence_calls) == fail_on:
                    raise OSError('PRIVATE persistence error')
                return original(run_id, metadata)
            with patch.object(self.store, 'record_probe_attempt', side_effect=persist), patch('app.codex_runtime.time.sleep'):
                result = SyncController(self.store, client, runtime).run(current=NOW)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'unexpected_tool_count' if variant == 'count' else 'calendar_probe_audit_failed')
            self.assertEqual(result['state']['last_successful_sync'], last_success)
            self.assertEqual(client.pages, [])
            self.assertEqual(client.details, [])
            self.assertFalse(any('.tools.create_event.' in arg for args in self.process_calls for arg in args))
            rows = self.store.db.execute('SELECT * FROM calendar_probe_attempts').fetchall()
            self.assertNotIn('PRIVATE', json.dumps([dict(row) for row in rows]))

    def test_interrupted_probe_records_attempt_and_preserves_last_success(self):
        previous = self.store.start_sync_run()
        self.store.finish_sync_run(previous, 'ok')
        last_success = self.store.status()['last_successful_sync']
        def interrupted(*_):
            raise SyncInterrupted('sync_timeout')
        runtime = CodexRuntime(TEST_CONFIG, run_process=interrupted)
        client = FakeClient(count=0)
        result = SyncController(self.store, client, runtime).run(current=NOW)
        self.assertEqual((result['status'], result['error_code']), ('interrupted', 'sync_timeout'))
        self.assertEqual(result['state']['calendar_probe_result'], 'interrupted')
        self.assertEqual(result['state']['last_successful_sync'], last_success)
        self.assertEqual(client.pages, [])

    def test_discovery_error_categories_have_no_retry_and_no_false_success(self):
        import requests
        from app.librus_client import DiscoveryParsingError
        variants = ((requests.exceptions.Timeout('PRIVATE'), 'discovery_timeout'),
                    (requests.exceptions.HTTPError('PRIVATE'), 'discovery_http_failed'),
                    (requests.exceptions.ConnectionError('PRIVATE'), 'discovery_connection_failed'),
                    (DiscoveryParsingError('PRIVATE'), 'discovery_parsing_failed'),
                    (ValueError('PRIVATE'), 'discovery_parsing_failed'),
                    (RuntimeError('PRIVATE'), 'discovery_failed'))
        for error, expected in variants:
            client = FakeClient(count=0)
            calls = []
            def failed(*args, **kwargs):
                calls.append(True)
                raise error
            client.messages = failed
            result = SyncController(self.store, client, FakeRuntime()).run(current=NOW)
            self.assertEqual((result['status'], result['error_code']), ('partial', expected))
            self.assertEqual(len(calls), 1)
            self.assertIsNone(result['state']['last_successful_sync'])
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_actual_discovery_json_and_schema_failures_have_safe_parsing_code(self):
        from app.librus_client import LibrusClient, DiscoveryParsingError
        from unittest.mock import Mock
        for response in (Mock(is_redirect=False, json=Mock(side_effect=ValueError('PRIVATE'))),
                         Mock(is_redirect=False, json=Mock(return_value={'data': 'PRIVATE'})),
                         Mock(is_redirect=False, json=Mock(return_value=['PRIVATE']))):
            client = object.__new__(LibrusClient)
            client.request = Mock(return_value=response)
            with self.assertRaises(DiscoveryParsingError) as caught:
                client.messages()
            self.assertEqual(caught.exception.code, 'discovery_parsing_failed')
            self.assertNotIn('PRIVATE', str(caught.exception))
            client.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()

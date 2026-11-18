"""Offline watchdog retries and durable Calendar outcomes with fictional data."""
from contextlib import closing
from itertools import repeat
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from app.codex_runtime import CodexRuntime, RuntimeFailure
from app.core import AppStore
from app.sync import SyncController, SyncInterrupted
from test_agenda import plan
from test_core import NOW
import test_runtime as runtime_fixtures
from test_runtime import operation, sample_event
from test_support import TEST_CONFIG
from test_sync import FakeClient, FakeRuntime


def completed(tool, arguments, data):
    return {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'id': 'fictional-call',
            'tool': 'google_calendar.' + tool, 'arguments': arguments,
            'result': {'structured_content': data}}}


def output(*records):
    return 0, '\n'.join(map(json.dumps, records)), ''


class TimeoutRetryTests(TestCase):
    def runtime(self, responses):
        self.calls, self.records = [], []
        pending = iter(responses)

        def run(args, prompt, timeout):
            self.calls.append((args, prompt, timeout))
            response = next(pending)
            if isinstance(response, BaseException):
                raise response
            return response(runtime, prompt) if callable(response) else response

        runtime = CodexRuntime(TEST_CONFIG, run_process=run)
        runtime.invocation_sink = self.records.append
        return runtime

    def finished(self):
        return [record for record in self.records if record['state'] != 'running']

    @staticmethod
    def reply(data, *, child=False, stall=False):
        def respond(runtime, prompt):
            request = json.loads(prompt.rsplit('\n', 1)[1])
            record = completed(request['tool'], request['arguments'], data)
            if child or stall:
                return TimeoutRetryTests.child(runtime, prompt, record, stall=stall)
            return output(record)
        return respond

    @staticmethod
    def child(runtime, prompt, *records, stall=True):
        if not stall:
            records += ({'type': 'turn.completed'},)
        stdout = '\n'.join(map(json.dumps, records))
        code = 'import sys, time; sys.stdin.read(); print(' + repr(stdout) + ', flush=True)'
        if stall:
            code += '; time.sleep(20)'
        return runtime._run_process([sys.executable, '-u', '-c', code], prompt, .35)

    @patch('app.codex_runtime.time.sleep')
    def test_real_watchdog_retries_both_read_stall_shapes_with_a_fresh_process(self, sleep):
        event = dict(sample_event(), id='fictional-event')
        silent_read = lambda runtime, prompt: self.child(runtime, prompt, {'type': 'turn.started'})
        completed_read = self.reply({'event': event}, stall=True)
        for first, stage, calls in ((silent_read, 'turn_started', 0), (completed_read, 'tool_completed', 1)):
            with self.subTest(stage=stage):
                runtime = self.runtime([first, self.reply({'event': event}, child=True)])
                self.assertEqual(runtime.read_event(event['id']), event)
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.calls[0][1:], self.calls[1][1:])
                self.assertNotEqual(self.calls[0][0][self.calls[0][0].index('-C')+1],
                                    self.calls[1][0][self.calls[1][0].index('-C')+1])
                failed, recovered = self.finished()
                self.assertEqual(failed['process_observation']['last_stage'], stage)
                self.assertEqual(failed['process_observation']['stop_cause'], 'app_timeout')
                self.assertEqual((failed['completed_count'], failed['turn_completed']), (calls, False))
                self.assertEqual((failed['operation_kind'], failed['error_code'], failed['retry_decision'],
                                  failed['retry_reason']), ('read_event', 'codex_timeout', 'retry', 'app_timeout'))
                self.assertEqual(failed['logical_call_index'], recovered['logical_call_index'])
                self.assertEqual((failed['attempt_number'], recovered['attempt_number']), (1, 2))
                self.assertEqual(recovered['process_observation']['stop_cause'], 'normal_exit')
                self.assertIn('attempt_started_at', recovered)
                self.assertIn('attempt_finished_at', recovered)
                self.assertTrue(recovered['recovered'])
        self.assertEqual([call.args for call in sleep.call_args_list], [(5,), (5,)])

    @patch('app.codex_runtime.time.sleep')
    def test_tool_free_planner_has_its_own_budget_and_reports_later_error(self, sleep):
        snapshot = {'snapshot_hash': 'a' * 64, 'sources': [], 'agenda': [], 'offer_groups': []}
        expected = plan(snapshot, [])
        valid = output({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(expected)}})
        timeout = RuntimeFailure('codex_timeout')
        runtime = self.runtime([timeout, valid, timeout, output({'type': 'turn.failed', 'message': 'Permission denied'})])
        self.assertEqual(runtime.plan_agenda(snapshot), expected)
        with self.assertRaisesRegex(RuntimeFailure, 'codex_permission_denied'):
            runtime.plan_agenda(snapshot)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len({prompt for _, prompt, _ in self.calls}), 1)
        for args, _, _ in self.calls:
            self.assertIn('apps', args)
            self.assertFalse(any('.tools.' in arg for arg in args))
        attempts = self.finished()
        self.assertEqual([item['logical_call_index'] for item in attempts], [1, 1, 2, 2])
        self.assertEqual([item['operation_kind'] for item in attempts], ['agenda_plan'] * 4)
        self.assertEqual([item['recovered'] for item in attempts], [False, True, False, False])
        self.assertEqual(attempts[-1]['retry_reason'], 'other_failure')
        self.assertEqual(sleep.call_count, 2)

    @patch('app.codex_runtime.time.sleep')
    def test_timeout_followed_by_wrong_event_does_not_mark_recovery(self, sleep):
        event = dict(sample_event(), id='another-fictional-event')
        runtime = self.runtime([RuntimeFailure('codex_timeout'), self.reply({'event': event})])
        with self.assertRaisesRegex(RuntimeFailure, 'calendar_event_id_mismatch'):
            runtime.read_event('fictional-event')
        self.assertEqual(self.finished()[-1]['state'], 'failed')
        self.assertFalse(self.finished()[-1]['recovered'])
        sleep.assert_called_once_with(5)

    @patch('app.codex_runtime.time.sleep')
    def test_timeout_followed_by_unverified_probe_does_not_mark_recovery(self, sleep):
        runtime = self.runtime([RuntimeFailure('codex_timeout'), self.reply({'calendars': []})])
        with self.assertRaisesRegex(RuntimeFailure, 'calendar_write_access_missing'):
            runtime.probe()
        self.assertEqual(self.finished()[-1]['state'], 'failed')
        self.assertFalse(self.finished()[-1]['recovered'])
        self.assertEqual(runtime.last_probe_attempts[-1]['result'], 'failed')
        sleep.assert_called_once_with(5)

    @patch('app.codex_runtime.time.sleep')
    def test_probe_and_batch_mixed_failures_share_one_attempt_limit(self, sleep):
        timeout = RuntimeFailure('codex_timeout')
        event = dict(sample_event(), id='fictional-event')
        valid_batch = self.reply({'responses': [event]})
        for first, second in ((timeout, output()), (output(), timeout), (timeout, timeout)):
            for kind in ('probe', 'batch'):
                with self.subTest(first=first, second=second, kind=kind):
                    runtime = self.runtime([first, second, valid_batch])
                    call = runtime.probe if kind == 'probe' else lambda: runtime.read_events({event['id']: event})
                    code = 'codex_timeout' if isinstance(second, RuntimeFailure) else 'unexpected_tool_count'
                    with self.assertRaisesRegex(RuntimeFailure, code):
                        call()
                    self.assertEqual(len(self.calls), 2)
                    self.assertEqual([a['retry_decision'] for a in self.finished()], ['retry', 'exhausted'])
        self.assertEqual(sleep.call_count, 6)

    @patch('app.codex_runtime.time.sleep')
    def test_batch_watchdog_retry_preserves_the_run_wide_clean_batch_budget(self, sleep):
        timeout = RuntimeFailure('codex_timeout')
        event = dict(sample_event(), id='fictional-event')
        valid = self.reply({'responses': [event]})
        runtime = self.runtime([timeout, output(), output(), valid, timeout, valid])
        with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_count'):
            runtime.read_events({event['id']: event})
        self.assertEqual(runtime.read_events({event['id']: event}), {event['id']: event})
        self.assertEqual(runtime.read_events({event['id']: event}), {event['id']: event})
        self.assertEqual([a['retry_reason'] for a in self.finished()[::2]],
                         ['app_timeout', 'clean_batch_zero_calls', 'app_timeout'])
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(sleep.call_count, 3)

    @patch('app.codex_runtime.time.sleep')
    def test_probe_timeout_allows_durable_second_attempt(self, sleep):
        timeout = RuntimeFailure('codex_timeout')
        runtime = self.runtime([timeout, output(runtime_fixtures.CalendarProbeTests.call())])
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory) / 'state.sqlite3', config=TEST_CONFIG)
            try:
                run_id = store.start_sync_run()
                runtime.probe_attempt_sink = lambda attempt: store.record_probe_attempt(run_id, attempt)
                self.assertTrue(runtime.probe()['recovered'])
                rows = store.db.execute('SELECT result, safe_code FROM calendar_probe_attempts ORDER BY attempt_number')
                self.assertEqual([tuple(row) for row in rows], [('failed', 'codex_timeout'), ('ok', None)])
            finally:
                store.close()
        sleep.assert_called_once_with(5)

    def test_deadline_prevents_another_process_and_bounds_its_watchdog(self):
        runtime = self.runtime([RuntimeFailure('codex_timeout')])
        runtime.reset_batch_retry_budget(deadline=10)
        now = [8]
        with patch('app.codex_runtime.time.monotonic', side_effect=lambda: now[0]), \
                patch('app.codex_runtime.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)) as sleep:
            with self.assertRaises(SyncInterrupted) as caught:
                runtime.read_event('fictional-event')
        self.assertEqual(caught.exception.code, 'sync_timeout')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], 2)
        self.assertEqual(self.finished()[0]['retry_decision'], 'deadline_exhausted')
        sleep.assert_called_once_with(2)

    @patch('app.codex_runtime.time.sleep')
    def test_interruption_never_retries_and_unsafe_read_evidence_is_preserved(self, sleep):
        for error in (SyncInterrupted('sync_interrupted'), RuntimeFailure('codex_timeout', possible_write=True)):
            with self.subTest(error=error):
                runtime = self.runtime([error])
                with self.assertRaises(type(error)) as caught:
                    runtime.read_event('fictional-event')
                self.assertEqual(len(self.calls), 1)
                if isinstance(error, RuntimeFailure):
                    self.assertTrue(caught.exception.possible_write)
                    self.assertEqual(self.finished()[0]['retry_reason'], 'possible_write')
                else:
                    self.assertEqual(self.finished()[0]['retry_reason'], 'interrupted')
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_watchdog_read_with_unexpected_write_evidence_is_not_retried(self, sleep):
        def wrong_tool(runtime, prompt):
            return self.child(runtime, prompt, {'type': 'item.started', 'item': {
                'type': 'mcp_tool_call', 'tool': 'google_calendar.create_event', 'arguments': {}}})
        runtime = self.runtime([wrong_tool])
        with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout') as caught:
            runtime.read_event('fictional-event')
        self.assertTrue(caught.exception.possible_write)
        failure, = self.finished()
        self.assertEqual((failure['retry_decision'], failure['retry_reason']), ('not_eligible', 'possible_write'))
        self.assertEqual(len(self.calls), 1)
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_watchdog_after_create_dispatch_reconciles_read_only_without_duplicate_write(self, sleep):
        event = dict(sample_event(), id='fictional-event')
        runtime = self.runtime([self.reply({'events': []}), self.reply({'event': event}, stall=True),
                                self.reply({'events': [event]}), self.reply({'event': event})])
        result = runtime.execute(operation())
        self.assertEqual((result['status'], result['error']), ('unknown', 'codex_timeout'))
        result = runtime.execute(dict(operation(), reconcile_only=True))
        self.assertEqual((result['status'], result['event_id']), ('applied', event['id']))
        attempts = self.finished()
        self.assertEqual([a['operation_kind'] for a in attempts],
                         ['search_events', 'create_event', 'search_events', 'read_event'])
        self.assertEqual((attempts[1]['retry_decision'], attempts[1]['retry_reason'], attempts[1]['completed_count']),
                         ('not_eligible', 'possible_write', 1))
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_timeout_followed_by_malformed_search_does_not_authorize_create(self, sleep):
        for malformed in ({'wrong_shape': []}, {'events': [{}]},
                          {'events': [], 'next_page_token': ['fictional-token']},
                          {'items': [dict(sample_event(), id=str(index)) for index in range(100)]}):
            with self.subTest(keys=malformed.keys()):
                event = dict(sample_event(), id='fictional-event')
                runtime = self.runtime([RuntimeFailure('codex_timeout'), self.reply(malformed),
                                        self.reply({'event': event}), self.reply({'event': event})])
                result = runtime.execute(operation())
                self.assertEqual((result['status'], result['error']), ('failed', 'calendar_search_incomplete'))
                self.assertEqual([item['operation_kind'] for item in self.finished()], ['search_events', 'search_events'])
                self.assertFalse(self.finished()[-1]['recovered'])
        self.assertEqual(sleep.call_count, 4)

    @patch('app.codex_runtime.time.sleep')
    def test_search_retry_checks_supported_wrappers_and_follows_continuation(self, sleep):
        event = dict(sample_event(), id='fictional-event')
        runtime = self.runtime([RuntimeFailure('codex_timeout'),
                                self.reply({'events': [], 'next_page_token': 'fictional-token'}),
                                self.reply({'items': [event], 'nextPageToken': None}),
                                self.reply({'event': event})])
        result = runtime.execute(operation())
        self.assertEqual((result['status'], result['event_id']), ('applied', event['id']))
        self.assertEqual([item['operation_kind'] for item in self.finished()],
                         ['search_events', 'search_events', 'search_events', 'read_event'])
        page_request = json.loads(self.calls[2][1].rsplit('\n', 1)[1])
        self.assertEqual(page_request['arguments']['next_page_token'], 'fictional-token')
        sleep.assert_called_once_with(5)

    @patch('app.codex_runtime.time.sleep')
    def test_search_retry_rejects_a_continuation_cycle_before_marking_recovery(self, sleep):
        first = self.reply({'events': [], 'next_page_token': 'first-token'})
        second = self.reply({'events': [], 'nextPageToken': 'second-token'})
        runtime = self.runtime([first, second, RuntimeFailure('codex_timeout'), first])
        result = runtime.execute(operation())
        self.assertEqual((result['status'], result['error']), ('failed', 'calendar_search_incomplete'))
        attempts = self.finished()
        self.assertEqual([item['operation_kind'] for item in attempts], ['search_events'] * 4)
        self.assertEqual(attempts[-1]['state'], 'failed')
        self.assertFalse(attempts[-1]['recovered'])
        sleep.assert_called_once_with(5)

    @patch('app.codex_runtime.time.sleep')
    def test_planner_timeout_with_unexpected_tool_activity_is_not_retried(self, sleep):
        snapshot = {'snapshot_hash': 'a' * 64, 'sources': [], 'agenda': [], 'offer_groups': []}
        for item in ({'type': 'mcp_tool_call', 'tool': 'google_calendar.create_event', 'arguments': {}},
                     {'type': 'command_execution'}):
            with self.subTest(item=item['type']):
                def wrong_tool(runtime, prompt):
                    return self.child(runtime, prompt, {'type': 'item.started', 'item': item})
                runtime = self.runtime([wrong_tool])
                with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout') as caught:
                    runtime.plan_agenda(snapshot)
                self.assertEqual(caught.exception.possible_write, item['type'] == 'mcp_tool_call')
                failure, = self.finished()
                self.assertEqual(failure['retry_decision'], 'not_eligible')
                self.assertEqual(len(self.calls), 1)
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_definitive_turn_failure_survives_a_later_watchdog_and_is_not_retried(self, sleep):
        for message, error_code in (('Permission denied', 'codex_permission_denied'),
                                    ('Invalid configuration', 'codex_configuration_failed'),
                                    ('Usage limit exceeded', 'codex_limit')):
            with self.subTest(error_code=error_code):
                def failed_then_stalled(runtime, prompt):
                    return self.child(runtime, prompt, {'type': 'turn.failed', 'message': message})
                runtime = self.runtime([failed_then_stalled])
                with self.assertRaisesRegex(RuntimeFailure, error_code):
                    runtime.read_event('fictional-event')
                failure, = self.finished()
                self.assertEqual((failure['error_code'], failure['retry_decision']), (error_code, 'not_eligible'))
                self.assertEqual(failure['process_observation']['stop_cause'], 'app_timeout')
                self.assertEqual(len(self.calls), 1)
        sleep.assert_not_called()

    def sync_runtime(self, store, *, timeout_before_write=False):
        remote = {}
        self.calendar_tools = []

        def calendar(runtime, prompt):
            request = json.loads(prompt.rsplit('\n', 1)[1])
            tool = request['tool']
            self.calendar_tools.append(tool)
            if tool == 'list_calendars':
                return output(runtime_fixtures.CalendarProbeTests.call())
            if tool == 'search_events':
                if timeout_before_write:
                    raise RuntimeFailure('codex_timeout')
                return self.reply({'events': list(remote.values())})(runtime, prompt)
            if tool == 'create_event':
                row, = store.db.execute('SELECT payload FROM operations WHERE status="inflight"')
                event = dict(json.loads(row['payload'])['event'], id='fictional-synced-event')
                self.assertNotIn(event['id'], remote)
                remote[event['id']] = event
                return self.reply({'event': event}, stall=True)(runtime, prompt)
            if tool == 'read_event':
                return self.reply({'event': remote[request['arguments']['event_id']]})(runtime, prompt)
            if tool == 'batch_read_event':
                return self.reply({'responses': [remote[eid] for eid in request['arguments']['event_ids']]})(runtime, prompt)
            self.fail(f'Unexpected Calendar tool {tool!r}')

        runtime = self.runtime(repeat(calendar))
        runtime.plan_agenda = FakeRuntime().plan_agenda
        return runtime

    @patch('app.codex_runtime.time.sleep')
    def test_sync_reconciles_unknown_write_from_sqlite_before_any_new_create(self, sleep):
        with tempfile.TemporaryDirectory() as directory, closing(AppStore(
                Path(directory) / 'state.sqlite3', config=TEST_CONFIG)) as store:
            store.set_writes_paused(False)
            runtime = self.sync_runtime(store)
            first = SyncController(store, FakeClient(), runtime).run(current=NOW)
            self.assertEqual((first['status'], first['error_code']), ('partial', 'codex_timeout'))
            self.assertEqual(first['state']['operations_unknown'], 1)
            op, = store.reconcile_operations()
            self.assertTrue(op['reconcile_only'])
            self.assertFalse(store.pending_operations())
            self.assertEqual(self.calendar_tools, ['list_calendars', 'search_events', 'create_event'])
            write = first['state']['sync_diagnostics']['invocations'][-1]
            self.assertEqual((write['operation_kind'], write['attempt_number'], write['retry_reason'],
                              write['recovered']), ('create_event', 1, 'possible_write', False))

            boundary = len(self.calendar_tools)
            second = SyncController(store, FakeClient(), runtime).run(current=NOW)
            self.assertEqual(second['status'], 'ok')
            self.assertEqual(second['state']['operations_unknown'], 0)
            self.assertEqual(second['calendar_writes'], 0)
            self.assertEqual(store.list_events()[0]['id'], 'fictional-synced-event')
            self.assertEqual(self.calendar_tools[boundary:boundary+3],
                             ['list_calendars', 'search_events', 'read_event'])
            status, attempts = store.db.execute('SELECT status, attempts FROM operations').fetchone()
            self.assertEqual((status, attempts), ('applied', 1))
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_sync_does_not_claim_success_after_prewrite_read_exhausts_retry(self, sleep):
        with tempfile.TemporaryDirectory() as directory, closing(AppStore(
                Path(directory) / 'state.sqlite3', config=TEST_CONFIG)) as store:
            store.set_writes_paused(False)
            runtime = self.sync_runtime(store, timeout_before_write=True)
            result = SyncController(store, FakeClient(), runtime).run(current=NOW)
            self.assertEqual((result['status'], result['error_code']), ('partial', 'codex_timeout'))
            self.assertEqual(result['state']['last_stage'], 'write')
            self.assertIsNone(result['state']['last_successful_sync'])
            self.assertEqual(result['state']['operations_pending'], 1)
            self.assertEqual(result['state']['operations_unknown'], 0)
            self.assertEqual(self.calendar_tools, ['list_calendars', 'search_events', 'search_events'])
            attempts = result['state']['sync_diagnostics']['invocations'][-2:]
            self.assertEqual([attempt['retry_decision'] for attempt in attempts], ['retry', 'exhausted'])
            self.assertTrue(all(attempt['operation_kind'] == 'search_events' for attempt in attempts))
            self.assertFalse(any(attempt['recovered'] for attempt in attempts))
        sleep.assert_called_once_with(5)

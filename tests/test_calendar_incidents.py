"""Recover Calendar rejection and verification branches from fictional evidence."""
import json
from unittest import TestCase
from unittest.mock import patch

from app.calendar_incidents import LIMIT, validate_event
from app.codex_runtime import CodexRuntime, RuntimeFailure, event_fingerprint
from app.incidents import capture
from test_runtime import operation, sample_event
from test_support import TEST_CONFIG


def record(lifecycle, method, arguments, data=None):
    item = {'id': 'PRIVATE-ITEM', 'type': 'mcp_tool_call', 'tool': method, 'arguments': arguments}
    if data is not None:
        item['result'] = {'structured_content': data}
    return {'type': f'item.{lifecycle}', 'item': item}


def output(*records):
    return 0, '\n'.join(map(json.dumps, records)), ''


def event(eid):
    return dict(sample_event(), id=eid, summary='PRIVATE-TITLE')


class CalendarIncidentTests(TestCase):
    def setUp(self):
        self.events = []
        self.enterContext(capture(self.events.append))

    def evidence(self, reason):
        return [row['evidence'] for row in self.events if row['reason'] == reason]

    def assert_private(self):
        report = json.dumps(self.events)
        self.assertNotIn('PRIVATE', report)
        self.assertNotIn(TEST_CONFIG.calendar_id, report)
        self.assertNotIn('[Szkoła]', report)

    def test_request_comparison_can_reconstruct_rejected_wire_order(self):
        expected = {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': ['PRIVATE-FIRST', 'PRIVATE-SECOND']}
        reordered = dict(expected, event_ids=list(reversed(expected['event_ids'])))
        incompatible = {'calendar_id': 'PRIVATE-SECOND-CALENDAR',
                        'event_ids': ['PRIVATE-FIRST', 'PRIVATE-EXTRA', 'PRIVATE-EXTRA'],
                        'max_results': 'PRIVATE-VALUE', 'PRIVATE-KEY': True}
        different = dict(expected, event_ids=['PRIVATE-FIRST', 'PRIVATE-ANOTHER-EXTRA'])
        records = (record('started', 'google_calendar.batch_read_event', reordered),
                   record('updated', 'PRIVATE-METHOD', incompatible),
                   record('completed', 'google_calendar.batch_read_event', different, {'responses': []}))
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *args: output(*records))
        with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_arguments'):
            runtime._tool('batch_read_event', expected)

        started, updated, completed = self.evidence('request_mismatch')
        self.assertEqual([row['lifecycle'] for row in (started, updated, completed)], ['started', 'updated', 'completed'])
        ids = started['event_ids']
        self.assertEqual((ids['expected'], ids['observed'], ids['order']), (['E1', 'E2'], ['E2', 'E1'], 'permuted'))
        self.assertEqual(updated['method'], 'other')
        self.assertEqual(updated['calendar'], 'different')
        self.assertEqual(updated['observed_fields'], {'calendar_id': 'string', 'event_ids': 'array', 'max_results': 'string'})
        self.assertEqual(updated['unknown_fields'], 1)
        self.assertEqual(updated['event_ids']['observed'], ['E1', 'E3', 'E3'])
        self.assertEqual(completed['event_ids']['observed'], ['E1', 'E4'])
        self.assertEqual([updated['event_ids'][key] for key in ('missing_count', 'extra_count', 'duplicate_count')], [1, 1, 1])

        # Replay with only aliases and the test's known operation contract.
        arguments = {'calendar_id': TEST_CONFIG.calendar_id}
        fixture = output(record('completed', 'google_calendar.batch_read_event',
                                dict(arguments, event_ids=ids['observed']), {'responses': []}))
        with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_arguments'):
            CodexRuntime(TEST_CONFIG, run_process=lambda *args: fixture)._tool(
                'batch_read_event', dict(arguments, event_ids=ids['expected']))
        self.assert_private()

    def test_malformed_lifecycle_and_invalid_result_record_different_shapes(self):
        arguments = {'calendar_id': TEST_CONFIG.calendar_id, 'event_id': 'PRIVATE-EVENT'}
        malformed = record('started', 'google_calendar.read_event', 'PRIVATE-ARGUMENTS')
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *args: output(malformed))
        with self.assertRaisesRegex(RuntimeFailure, 'codex_protocol_invalid'):
            runtime._tool('read_event', arguments)
        self.assertEqual(self.evidence('protocol_invalid')[0]['problems'], ['tool_record'])
        self.assertEqual(self.evidence('request_mismatch')[0]['arguments_type'], 'string')

        broken = record('completed', 'google_calendar.read_event', arguments)
        broken['item']['result'] = {'content': [{'type': 'text', 'text': 'PRIVATE-INVALID-JSON'}]}
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *args: output(broken))
        with self.assertRaisesRegex(RuntimeFailure, 'calendar_result_unreadable'):
            runtime._tool('read_event', arguments)
        self.assertEqual(self.evidence('tool_result_rejected')[0]['shape'], 'text_invalid_json')
        self.assert_private()

    def test_mixed_batch_rejection_records_anonymous_status_and_positional_dto(self):
        requested = {eid: event(eid) for eid in ('PRIVATE-A', 'PRIVATE-B', 'PRIVATE-C')}
        response = [
            {'error': 'Failed to read event', 'detail': '404: HTTP request failed (Response: ' + json.dumps(
                {'error': {'code': 404, 'message': 'PRIVATE-404', 'errors': [
                    {'domain': 'PRIVATE-DOMAIN', 'reason': 'PRIVATE-REASON', 'message': 'PRIVATE-DETAIL'}]}}) + ')'},
            requested['PRIVATE-C'], requested['PRIVATE-B'],
        ]
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *args: output(record(
            'completed', 'google_calendar.batch_read_event',
            {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': list(requested)}, {'responses': response})))
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_calendar_snapshot_invalid'):
            runtime.read_events(requested)
        rejected = self.evidence('batch_rejected')[0]
        self.assertEqual(rejected['rule'], 'anonymous_order')
        self.assertEqual(rejected['event_ids']['expected'], ['E1', 'E2', 'E3'])
        self.assertEqual(rejected['event_ids']['observed'], ['not_found_404', 'E3', 'E2'])
        self.assertEqual(rejected['position'], 2)
        self.assert_private()

    def test_batch_rejection_records_count_and_known_dto_fields(self):
        requested = {'PRIVATE-A': event('PRIVATE-A')}
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *args: output(record(
            'completed', 'google_calendar.batch_read_event',
            {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': list(requested)}, {'responses': []})))
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_calendar_snapshot_incomplete'):
            runtime.read_events(requested)
        evidence = self.evidence('batch_rejected')[0]
        self.assertEqual((evidence['rule'], evidence['event_ids']['missing_count']), ('response_count', 1))

        malformed = {'id': 'PRIVATE-A', 'start_time': None, 'PRIVATE-DTO-KEY': True, 'error': True}
        with self.assertRaisesRegex(RuntimeFailure, 'calendar_result_unreadable'):
            runtime._validate_read_batch({'responses': [malformed]}, list(requested), requested)
        evidence = self.evidence('batch_rejected')[1]
        self.assertEqual(evidence['rule'], 'unreadable_dto')
        self.assertEqual(evidence['fields'], {'id': 'string', 'start_time': 'null', 'error': 'boolean'})
        self.assert_private()

    def test_anonymous_error_with_disagreeing_statuses_records_each_side(self):
        requested = {'PRIVATE-ID': event('PRIVATE-ID')}
        wrong_status = {'error': 'Failed to read event', 'detail': '404: HTTP request failed (Response: '
                        + json.dumps({'error': {'code': 410, 'message': 'PRIVATE-MESSAGE',
                                               'errors': [{'domain': 'PRIVATE-DOMAIN', 'reason': 'PRIVATE-REASON',
                                                           'message': 'PRIVATE-DETAIL'}]}}) + ')'}
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_calendar_snapshot_invalid'):
            CodexRuntime(TEST_CONFIG)._validate_read_batch({'responses': [wrong_status]}, list(requested), requested)
        rejected = self.evidence('batch_rejected')[0]
        self.assertEqual(rejected['anonymous_shapes'], ['code_mismatch'])
        self.assertEqual(rejected['not_found_statuses'], [{'position': 1, 'envelope': '404', 'body': '410'}])
        self.assert_private()

    def test_tool_free_failures_distinguish_missing_json_invalid_json_and_schema(self):
        def answer(text):
            return output({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})
        for response in (output(), answer('PRIVATE-INVALID'), answer('{"items":"PRIVATE-WRONG-TYPE"}')):
            with self.assertRaisesRegex(RuntimeFailure, 'invalid_analysis_json'):
                CodexRuntime(TEST_CONFIG, run_process=lambda *args: response)._invoke('PRIVATE-PROMPT', 'agenda_plan.json')
        self.assertEqual([event['reason'] for event in self.events if event['reason'].startswith('analysis_')],
                         ['analysis_missing', 'analysis_json', 'analysis_schema'])
        self.assert_private()

    def test_safe_timeout_records_retry_and_verified_recovery(self):
        value = event('PRIVATE-ID')
        attempts = iter([RuntimeFailure('codex_timeout'),
                         output(record('completed', 'google_calendar.read_event',
                                       {'event_id': value['id'], 'calendar_id': TEST_CONFIG.calendar_id}, value))])

        def runner(*args):
            result = next(attempts)
            if isinstance(result, RuntimeFailure):
                raise result
            return result

        with patch('app.codex_runtime.time.sleep'):
            self.assertEqual(CodexRuntime(TEST_CONFIG, run_process=runner).read_event(value['id']), value)
        retry, recovery = [row for row in self.events if row['reason'] == 'invocation']
        self.assertEqual((retry['decision'], retry['evidence']['trigger']), ('retry', 'app_timeout'))
        self.assertEqual((recovery['decision'], recovery['evidence']['attempt']), ('recovered', 2))
        self.assertEqual(retry['evidence']['call'], recovery['evidence']['call'])
        self.assert_private()

    def test_completed_write_followed_by_failed_read_remains_unknown(self):
        calls = []
        desired = operation()
        value = event('PRIVATE-EVENT')
        def runner(args, prompt, timeout):
            request = json.loads(prompt.rsplit('\n', 1)[1])
            method = request['tool']
            calls.append(method)
            if method == 'read_event':
                raise RuntimeFailure('codex_timeout')
            data = {'events': []} if method == 'search_events' else value
            return output(record('completed', 'google_calendar.' + method, request['arguments'], data))

        with patch('app.codex_runtime.time.sleep'):
            result = CodexRuntime(TEST_CONFIG, run_process=runner).execute(desired)
        self.assertEqual((result['status'], result['error']), ('unknown', 'codex_timeout'))
        self.assertEqual(calls, ['search_events', 'create_event', 'read_event', 'read_event'])
        self.assertEqual([(row['reason'], row['decision']) for row in self.events if row['operation'] == 'create_event'],
                         [('write_dispatch', 'acknowledged'), ('write_verification', 'unknown')])
        self.assert_private()

    def test_mismatched_write_reports_possible_dispatch_without_acknowledgment(self):
        calls = []

        def runner(args, prompt, timeout):
            request = json.loads(prompt.rsplit('\n', 1)[1])
            method = request['tool']
            calls.append(method)
            if method == 'search_events':
                return output(record('completed', 'google_calendar.' + method, request['arguments'], {'events': []}))
            actual = dict(request['arguments'], calendar_id='PRIVATE-OTHER-CALENDAR')
            return output(record('started', 'google_calendar.' + method, actual))

        result = CodexRuntime(TEST_CONFIG, run_process=runner).execute(operation())
        self.assertEqual((result['status'], result['error']), ('unknown', 'unexpected_tool_arguments'))
        self.assertEqual(calls, ['search_events', 'create_event'])
        self.assertEqual(self.evidence('write_dispatch'), [])
        self.assertEqual(self.evidence('write_verification')[0]['check'], 'dispatch_uncertain')
        evidence = self.evidence('invocation')[0]
        self.assertEqual((evidence['observed_write'], evidence['possible_write']), (True, True))
        self.assert_private()

    def test_delete_followed_by_verified_absence_records_observations(self):
        before = event('PRIVATE-EVENT')
        desired = dict(operation('cancel'), event_id=before['id'], deletion_approved=True)
        dispatch = []

        def runner(args, prompt, timeout):
            request = json.loads(prompt.rsplit('\n', 1)[1])
            method = request['tool']
            dispatch.append(method)
            result = record('completed', 'google_calendar.' + method, request['arguments'],
                            {'result': None} if method == 'delete_event' else before)
            if len(dispatch) == 3:
                result['item']['error'] = '404 PRIVATE-DETAIL'
            return output(result)

        runtime = CodexRuntime(TEST_CONFIG, run_process=runner)
        desired['expected_fingerprint'] = event_fingerprint(before, TEST_CONFIG.calendar_owner)
        rejected, observations = [], []
        with capture(rejected.append, observation=observations.append):
            self.assertEqual(runtime.execute(desired)['status'], 'applied')
        self.assertEqual(rejected, [])
        self.assertEqual([(row['reason'], row['decision']) for row in observations if row['operation'] == 'delete_event'],
                         [('write_dispatch', 'acknowledged'), ('write_verification', 'applied')])
        self.assertNotIn('PRIVATE', json.dumps(observations))

    def test_schema_rejects_an_unaliased_raw_identifier(self):
        wanted = {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': ['PRIVATE-ID']}
        emitted = output(record('completed', 'google_calendar.batch_read_event',
                                dict(wanted, event_ids=['PRIVATE-OTHER-ID']), {'responses': []}))
        with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_arguments'):
            CodexRuntime(TEST_CONFIG, run_process=lambda *args: emitted)._tool('batch_read_event', wanted)
        evidence = self.evidence('request_mismatch')[0]
        with self.assertRaises(ValueError):
            validate_event('batch_read_event', 'request_mismatch', 'rejected',
                           dict(evidence, event_ids=dict(evidence['event_ids'], observed=['PRIVATE-OTHER-ID'])))

    def test_request_evidence_marks_both_record_and_id_limits(self):
        wanted = {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': ['PRIVATE-ID']}
        actual = dict(wanted, event_ids=[f'PRIVATE-ID-{index}' for index in range(LIMIT + 5)])
        wire = output(
            record('started', 'google_calendar.batch_read_event', actual),
            *[record('updated', 'google_calendar.batch_read_event', actual) for unused in range(LIMIT)],
            record('completed', 'google_calendar.batch_read_event', dict(wanted, event_ids=['PRIVATE-ID', 'PRIVATE-FINAL']),
                   {'responses': []}),
        )
        with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_arguments'):
            CodexRuntime(TEST_CONFIG, run_process=lambda *args: wire)._tool('batch_read_event', wanted)
        detailed = self.evidence('request_mismatch')
        self.assertEqual(len(detailed), LIMIT)
        ids = detailed[0]['event_ids']
        self.assertEqual((len(ids['observed']), ids['observed_count'], ids['order']), (LIMIT, LIMIT + 5, 'truncated'))
        self.assertEqual((detailed[0]['lifecycle'], detailed[-1]['lifecycle']), ('started', 'completed'))
        self.assertEqual(detailed[-1]['event_ids']['observed'], ['E1', f'E{LIMIT + 2}'])
        self.assertEqual(self.evidence('request_mismatch_limit')[0]['omitted'], 2)
        self.assert_private()

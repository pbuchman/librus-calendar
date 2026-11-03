"""Fictional bounded diagnostics at collection and process boundaries."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import requests

from app.codex_runtime import CodexRuntime, RuntimeFailure
from app.core import AppStore
from app.health_state import sync_diagnostics
from app.librus_client import LibrusClient
from app.source_collectors import Document, SourceCollectionError, _calendar_form, collect_sources
from app.sync import SyncController, SyncInterrupted
from test_source_collectors import CURRENT, FakeClient as SchoolClient, calendar_page
from test_support import TEST_CONFIG
from test_sync import FakeClient, FakeRuntime
from test_core import NOW


def diagnostic(**changes):
    return dict(dict(phase='fetch', state='failed', requested_count=1, received_count=0,
                     duration_seconds=0.1, process_code=None, started_count=0, completed_count=0), **changes)


class FormDiagnosticsTests(unittest.TestCase):
    def test_each_form_invariant_has_a_distinct_safe_cause(self):
        valid = calendar_page()
        mutations = [
            ('calendar_form_count', valid + '<form></form>'),
            ('calendar_form_id', valid.replace('scheduleForm', 'other')),
            ('calendar_form_target', valid.replace('action="/terminarz"', 'action="https://external.example/"')),
            ('calendar_form_method', valid.replace('method="post"', 'method="get"')),
            ('calendar_form_textarea', valid + '<textarea>PRIVATE</textarea>'),
            ('calendar_form_button', valid + '<button>PRIVATE</button>'),
            ('calendar_form_external_control', valid + '<input name="PRIVATE">'),
            ('calendar_form_language_id', valid + '<select id="PRIVATE" name="Język"></select>'),
            ('calendar_form_language_name', valid + '<select id="language" name="PRIVATE"></select>'),
            ('calendar_form_language_empty', valid + '<select id="language" name="Język"></select>'),
            ('calendar_form_language_limit', valid + '<select id="language" name="Język">' + '<option>1</option>'*33 + '</select>'),
            ('calendar_form_language_duplicate', valid + '<select id="language" name="Język"><option>1</option></select>'*2),
            ('calendar_form_learner_switch', valid + '<a onclick="switchStudent()">PRIVATE</a>'),
            ('calendar_form_select_name', valid.replace('name="rok"', 'name="PRIVATE"')),
            ('calendar_form_select_duplicate', valid.replace('</form>', '<select name="rok"></select></form>')),
            ('calendar_form_selected_count', valid.replace(' selected="selected"', '')),
            ('calendar_form_selected_count', valid.replace('value="4"', 'selected="selected" value="4"')),
            ('calendar_form_selected_value', valid.replace('value="5" selected', 'value="PRIVATE" selected')),
            ('calendar_form_month_missing', valid.replace('<select name="miesiac">', '<span>').replace('</select>', '</span>', 1)),
            ('calendar_form_year_missing', valid.replace('<select name="rok">', '<span>').replace('</select></form>', '</span></form>')),
            ('calendar_form_month_range', valid.replace('value="5" selected', 'value="99" selected')),
            ('calendar_form_year_range', valid.replace('value="2028" selected', 'value="9999" selected')),
            ('calendar_form_hidden_name', valid.replace('name="requestkey"', '')),
            ('calendar_form_hidden_type', valid.replace('type="hidden"', 'type="text"')),
            ('calendar_form_hidden_field', valid.replace('name="requestkey"', 'name="PRIVATE"')),
            ('calendar_form_hidden_duplicate', valid.replace('</form>', '<input type="hidden" name="requestkey" value="synthetic-key"></form>')),
            ('calendar_form_token_missing', valid.replace('value="synthetic-form-key"', 'value=""')),
            ('calendar_form_token_missing', valid.replace('<input type="hidden" name="requestkey" value="synthetic-form-key">', '')),
        ]
        self.assertEqual(_calendar_form(Document(valid))['month'], 5)
        for code, html in mutations:
            with self.subTest(code=code), self.assertRaises(SourceCollectionError) as caught:
                _calendar_form(Document(html))
            self.assertEqual(caught.exception.code, 'calendar_form_invalid')
            self.assertEqual(getattr(caught.exception, 'diagnostic_code', None), code)

    def test_initial_selection_and_restoration_causes_are_sequential_and_primary_survives(self):
        for failure_step in ('calendar_initial', 'calendar_select', 'calendar_restore', 'both'):
            client = SchoolClient()
            records = []
            client.collection_diagnostic_sink = lambda item: records.append(dict(item))
            original_calendar, original_request = client.calendar, client.request
            def calendar():
                text = original_calendar()
                return text.replace('scheduleForm', 'PRIVATE') if failure_step == 'calendar_initial' else text
            def request(method, url, *, data):
                previous_month = client.month
                text = original_request(method, url, data=data)
                selecting = data['miesiac'] == '4'
                restoring = data['miesiac'] == '5' and (previous_month == 7 or failure_step == 'both')
                if (selecting and failure_step in ('calendar_select', 'both')
                        or restoring and failure_step in ('calendar_restore', 'both')):
                    text.text = text.text.replace('scheduleForm', 'PRIVATE')
                return text
            client.calendar, client.request = calendar, request
            batch = collect_sources(client, current=CURRENT)
            failures = [item for item in records if item['state'] == 'failed']
            with self.subTest(step=failure_step):
                self.assertTrue(failures)
                self.assertEqual(failures[0]['error_code'], 'calendar_form_id')
                self.assertNotIn('source_kind', failures[0])
                self.assertEqual(failures[0]['collection_step'], 'calendar_select' if failure_step == 'both' else failure_step)
                self.assertEqual(batch['errors'], ['calendar_form_invalid'] if failure_step != 'calendar_restore' else ['calendar_restore_failed'])
                if failure_step == 'both':
                    self.assertEqual(failures[-1]['collection_step'], 'calendar_restore')
                self.assertNotIn('PRIVATE', json.dumps(records))


class MessageDiagnosticsTests(unittest.TestCase):
    def test_source_failure_remains_on_run_after_source_health_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory)/'state.sqlite3', config=TEST_CONFIG)
            try:
                school = SchoolClient()
                original = school.calendar
                school.calendar = lambda: original().replace('scheduleForm', 'PRIVATE')
                client = FakeClient(count=0)
                client.follow, client.request = school.follow, school.request
                client.collect_sources = lambda **_: collect_sources(client, current=CURRENT)
                SyncController(store, client, FakeRuntime(propose=False)).run(current=NOW)
                saved = store.status()['sync_diagnostics']
                self.assertTrue(any(item.get('error_code') == 'calendar_form_id' for item in saved['invocations']))
                self.assertIsNone(getattr(client, 'collection_diagnostic_sink', None))
                store.ingest_sources(collect_sources(SchoolClient(), current=CURRENT), NOW)
                self.assertEqual(store.source_status()['health']['school_event']['last_result'], 'ok')
                self.assertEqual(store.status()['sync_diagnostics'], saved)
                self.assertNotIn('PRIVATE', json.dumps(saved))
            finally:
                store.close()

    def test_fetch_failure_categories_status_and_private_text_are_saved_to_the_run(self):
        response = requests.Response()
        response.status_code = 503
        failures = [(requests.Timeout('PRIVATE quota'), 'message_fetch_timeout', None),
                    (requests.ConnectionError('PRIVATE quota'), 'message_fetch_connection', None),
                    (requests.HTTPError('PRIVATE quota', response=response), 'message_fetch_http', 503),
                    (ValueError('PRIVATE quota'), 'message_fetch_parsing', None),
                    (RuntimeError('PRIVATE quota'), 'message_fetch_unknown', None)]
        for error, expected, status in failures:
            with self.subTest(code=expected), tempfile.TemporaryDirectory() as directory:
                store = AppStore(Path(directory)/'state.sqlite3', config=TEST_CONFIG)
                try:
                    client = FakeClient()
                    client.message = Mock(side_effect=error)
                    result = SyncController(store, client, FakeRuntime(propose=False)).run(current=NOW)
                    self.assertEqual(result['error_code'], 'fetch_failed')
                    saved = store.status()['sync_diagnostics']
                    item, = [item for item in saved['invocations'] if item.get('collection_step') == 'message_body']
                    self.assertEqual(item['error_code'], expected)
                    self.assertEqual(item['source_kind'], 'message')
                    self.assertEqual(item.get('http_status'), status)
                    self.assertNotIn('PRIVATE', json.dumps(saved))
                finally:
                    store.close()

    def test_real_message_parser_has_structured_failure_evidence(self):
        client = object.__new__(LibrusClient)
        for payload in ([], None, {'data': []}, {'data': {'messageId': 'fictional', 'Message': '!'}}):
            client.request = Mock(return_value=Mock(is_redirect=False, json=lambda: payload))
            with self.assertRaises(Exception) as caught:
                client.message('fictional')
            self.assertEqual(getattr(caught.exception, 'diagnostic_code', None), 'message_fetch_parsing')


class ProcessDiagnosticsTests(unittest.TestCase):
    def test_completed_process_with_invalid_analysis_keeps_safe_cause_and_exit_evidence(self):
        runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *_: (0, json.dumps({
            'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'PRIVATE'}}), ''))
        saved = []
        runtime.invocation_sink = lambda item: saved.append(dict(item))
        with self.assertRaisesRegex(RuntimeFailure, 'invalid_analysis_json'):
            runtime._invoke('PRIVATE', 'agenda_plan.json')
        self.assertEqual(saved[-1]['error_code'], 'invalid_analysis_json')
        self.assertEqual(saved[-1]['process_code'], 0)
        self.assertEqual((saved[-1]['started_count'], saved[-1]['completed_count']), (0, 0))
        self.assertEqual(saved[-1]['received_count'], 0)

    def test_timeout_and_interrupt_before_during_after_tool_retain_cause_and_counts(self):
        args = {'calendar_id': TEST_CONFIG.calendar_id, 'event_id': 'fictional'}
        def record(kind):
            item = {'type': 'mcp_tool_call', 'id': 'fictional-call', 'tool': 'google_calendar.read_event',
                    'arguments': args, 'status': 'completed' if kind == 'item.completed' else 'in_progress'}
            if kind == 'item.completed': item['result'] = {'structured_content': {'id': 'fictional'}}
            return json.dumps({'type': kind, 'item': item})
        for output, counts in (('', (0, 0)), (record('item.started'), (1, 0)),
                               (record('item.started')+'\n'+record('item.completed'), (1, 1))):
            for error, code in ((subprocess.TimeoutExpired('synthetic', 1), 'codex_timeout'),
                                (SyncInterrupted('sync_interrupted'), 'sync_interrupted')):
                process = MagicMock(pid=12345, returncode=-9)
                process.communicate.side_effect = [error, (output, 'PRIVATE quota')]
                runtime = CodexRuntime(TEST_CONFIG)
                saved = []
                runtime.invocation_sink = lambda item: saved.append(dict(item))
                with self.subTest(counts=counts, code=code), patch('app.codex_runtime.subprocess.Popen', return_value=process), patch('app.codex_runtime.os.killpg'):
                    with self.assertRaises((RuntimeFailure, SyncInterrupted)):
                        runtime._invoke('PRIVATE', 'tool.json', ('read_event',), 'read_event', args)
                self.assertEqual(saved[-1]['error_code'], code)
                self.assertEqual((saved[-1]['started_count'], saved[-1]['completed_count']), counts)
                self.assertEqual(saved[-1]['process_code'], -9)
                self.assertNotIn('PRIVATE', json.dumps(saved))

    def test_planner_and_calendar_only_classify_protocol_errors_and_stderr(self):
        args = {'calendar_id': TEST_CONFIG.calendar_id, 'event_id': 'fictional'}
        call = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'tool': 'google_calendar.read_event',
                'arguments': args, 'result': {'structured_content': {'description': 'PRIVATE quota forbidden'}}}}
        for tools in (False, True):
            for origin in ('stderr', 'turn', 'item', 'agent', 'tool'):
                for text, expected in (('usage limit PRIVATE', 'codex_limit'),
                                       ('permission denied PRIVATE', 'codex_permission_denied'),
                                       ('invalid configuration PRIVATE', 'codex_configuration_failed')):
                    records = [call] if tools else []
                    stderr = text if origin == 'stderr' else ''
                    if origin == 'turn': records.append({'type': 'turn.failed', 'error': {'message': text}})
                    if origin == 'item': records.append({'type': 'item.completed', 'item': {'type': 'error', 'message': text}})
                    if origin == 'agent': records.append({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})
                    if origin == 'tool' and not tools:
                        records.append(call)
                    runtime = CodexRuntime(TEST_CONFIG, run_process=lambda *_: (1, '\n'.join(map(json.dumps, records)), stderr))
                    saved = []
                    runtime.invocation_sink = lambda item: saved.append(dict(item))
                    with self.subTest(tools=tools, origin=origin, text=text), self.assertRaises(RuntimeFailure) as caught:
                        runtime._invoke('PRIVATE quota', 'agenda_plan.json', ('read_event',) if tools else (), 'read_event' if tools else None, args if tools else None)
                    code = expected if origin in ('stderr', 'turn', 'item') else 'codex_failed'
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(saved[-1].get('error_code'), code)
                    self.assertEqual(saved[-1]['process_code'], 1)
                    self.assertNotIn('PRIVATE', json.dumps(saved))


class DiagnosticStorageTests(unittest.TestCase):
    def test_optional_fields_closed_values_and_old_records(self):
        old = {'invocations': [diagnostic()], 'omitted_count': 0}
        self.assertEqual(sync_diagnostics(old), old)
        valid = diagnostic(error_code='calendar_form_id', source_kind='school_event', collection_step='calendar_initial', http_status=503)
        self.assertEqual(sync_diagnostics({'invocations': [valid], 'omitted_count': 0})['invocations'][0], valid)
        for changes in ({'error_code': 'PRIVATE'}, {'source_kind': 'PRIVATE'}, {'collection_step': 'PRIVATE'},
                        {'error_code': {}}, {'source_kind': []}, {'collection_step': {}},
                        {'http_status': 99}, {'http_status': 600}, {'http_status': True}, {'raw_text': 'PRIVATE'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sync_diagnostics({'invocations': [diagnostic(**changes)], 'omitted_count': 0})

    def test_enriched_history_evicts_to_byte_budget_and_interruption_has_a_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory)/'state.sqlite3', config=TEST_CONFIG)
            try:
                run = store.start_sync_run()
                for _ in range(520):
                    entry = diagnostic(state='running', source_kind='school_event', collection_step='calendar_restore')
                    store.record_sync_diagnostic(run, entry)
                    store.record_sync_diagnostic(run, dict(entry, state='failed', error_code='calendar_form_hidden_type', http_status=503))
                store.record_sync_diagnostic(run, diagnostic(state='running'))
                store.start_sync_run()
                saved = json.loads(store.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?', (run,)).fetchone()[0])
                sync_diagnostics(saved)
                self.assertLessEqual(len(json.dumps(saved)), 131072)
                self.assertGreater(saved['omitted_count'], 0)
                self.assertEqual(saved['invocations'][-1]['error_code'], 'worker_interrupted')
            finally:
                store.close()

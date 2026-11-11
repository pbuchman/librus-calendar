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
from app.health_state import failure_diagnostic, sync_diagnostics
from app.librus_client import LibrusClient
from app.source_collectors import Document, SourceCollectionError, _calendar_form, collect_sources
from app.sync import SyncController, SyncInterrupted
from test_source_collectors import CURRENT, FakeClient as SchoolClient, calendar_page, cancellation_row
from test_support import TEST_CONFIG
from test_sync import FakeClient, FakeRuntime
from test_core import NOW


def diagnostic(**changes):
    return dict(dict(phase='fetch', state='failed', requested_count=1, received_count=0,
                     duration_seconds=0.1, process_code=None, started_count=0, completed_count=0), **changes)


class FormDiagnosticsTests(unittest.TestCase):
    def form_evidence(self, html):
        with self.assertRaises(SourceCollectionError) as caught:
            _calendar_form(Document(html))
        self.assertEqual(caught.exception.code, 'calendar_form_invalid')
        self.assertEqual(caught.exception.diagnostic_code, 'calendar_form_count')
        evidence = failure_diagnostic(caught.exception)['evidence']
        self.assertIn('forms', evidence)
        return evidence

    def test_duplicate_forms_describe_calendar_and_unknown_without_private_content(self):
        extra = ('<div id="PRIVATE-location"><form id="PRIVATE-id" action="/PRIVATE-route?secret=PRIVATE-query" '
                 'method="get"><input name="PRIVATE-name" value="PRIVATE-value">'
                 '<input type="password" name="PRIVATE-password"><input type="hidden" name="PRIVATE-token" '
                 'value="PRIVATE-secret"><select name="PRIVATE-choice"><option>PRIVATE-person</option></select>'
                 '<button>PRIVATE-text</button><textarea>PRIVATE-body</textarea></form></div>')
        evidence = self.form_evidence(calendar_page().replace('</body>', extra + '</body>'))
        self.assertEqual(evidence['form_count'], 2)
        self.assertEqual(evidence['omitted_form_count'], 0)
        first, second = evidence['forms']
        self.assertEqual(first, dict(position=1, identity_category='expected_calendar_id',
            action_category='calendar', method='post', tag_path=['html', 'body', 'form'],
            control_count=3, control_types={'input_hidden': 1, 'select': 2},
            field_categories={'request_token': 1, 'month': 1, 'year': 1}))
        self.assertEqual(second, dict(position=2, identity_category='other_id',
            action_category='same_origin_other', method='get', tag_path=['html', 'body', 'div', 'form'],
            control_count=6, control_types={'input_text': 1, 'input_password': 1, 'input_hidden': 1,
                                          'select': 1, 'button': 1, 'textarea': 1},
            field_categories={'other': 6}))
        self.assertNotIn('PRIVATE', json.dumps(evidence))
        self.assertNotIn('synthetic-form-key', json.dumps(evidence))
        self.assertNotIn('scheduleForm', json.dumps(evidence))
        self.assertNotIn('https:', json.dumps(evidence))
        sync_diagnostics({'invocations': [diagnostic(evidence=evidence)], 'omitted_count': 0})

    def test_form_descriptors_classify_only_observed_routes_fields_and_types(self):
        extras = [
            ('<form action="/ogloszenia" method="POST"><select id="language" name="Język"></select></form>',
             'known_source', 'post', {'language': 1}),
            ('<form action="https://external.example/PRIVATE" method="PRIVATE"><input type="PRIVATE"></form>',
             'untrusted', 'other', {'other': 1}),
            ('<form action="https://[PRIVATE" method="dialog"></form>', 'invalid', 'dialog', {}),
            ('<form action="/terminarz?PRIVATE"><input type="text" name="requestkey"></form>',
             'same_origin_other', 'get', {'other': 1}),
            ('<form><select name="miesiac"></select><select name="rok"></select></form>',
             'calendar', 'get', {'month': 1, 'year': 1}),
        ]
        for extra, action, method, fields in extras:
            with self.subTest(action=action):
                evidence = self.form_evidence(calendar_page().replace('</body>', extra + '</body>'))
                descriptor = evidence['forms'][1]
                self.assertEqual(descriptor['identity_category'], 'missing_id')
                self.assertEqual(descriptor['action_category'], action)
                self.assertEqual(descriptor['method'], method)
                self.assertEqual(descriptor['field_categories'], fields)
                self.assertNotIn('PRIVATE', json.dumps(evidence))
        self.assertEqual(descriptor['control_types'], {'select': 2})

    def test_form_evidence_has_explicit_bounds_and_nearest_form_control_ownership(self):
        html = calendar_page().replace('</body>', '<div>' * 12 + '<form><input><form><textarea></textarea></form>'
                                       '</form>' + '</div>' * 12 + '<form></form>' * 20 + '</body>')
        evidence = self.form_evidence(html)
        self.assertEqual(evidence['form_count'], 23)
        self.assertEqual(len(evidence['forms']), 16)
        self.assertEqual(evidence['omitted_form_count'], 7)
        outer, nested = evidence['forms'][1:3]
        self.assertEqual(outer['control_count'], 1)
        self.assertEqual(nested['control_count'], 1)
        self.assertEqual(outer['tag_path'], ['div'] * 7 + ['form'])
        self.assertEqual(nested['tag_path'], ['div'] * 6 + ['form', 'form'])

    def test_valueless_attributes_cannot_replace_form_count_failure(self):
        extra = '<form id action method><input type name value><select name></select></form>'
        evidence = self.form_evidence(calendar_page().replace('</body>', extra + '</body>'))
        second = evidence['forms'][1]
        self.assertEqual(second['method'], 'other')
        self.assertEqual(second['action_category'], 'invalid')
        self.assertEqual(second['control_types'], {'input_other': 1, 'select': 1})
        self.assertEqual(second['field_categories'], {'other': 2})

    def test_form_count_records_actual_count(self):
        for count in (0, 2, 3):
            html = calendar_page()
            if count == 0:
                html = html[:html.index('<form')] + html[html.index('</form>') + 7:]
            else:
                html = html.replace('</body>', '<form></form>' * (count - 1) + '</body>')
            with self.subTest(count=count), self.assertRaises(SourceCollectionError) as caught:
                _calendar_form(Document(html))
            evidence = failure_diagnostic(caught.exception)['evidence']
            self.assertEqual(evidence['form_count'], count)
            self.assertEqual(len(evidence['forms']), count)
            self.assertEqual(evidence['omitted_form_count'], 0)

    def test_cell_structure_is_safe_and_primary_survives_restore_failure(self):
        for failure_step in ('calendar_initial', 'calendar_select', 'both'):
            client = SchoolClient()
            records = []
            client.collection_diagnostic_sink = lambda item: records.append(dict(item))
            original_calendar, original_request = client.calendar, client.request
            # Unknown names/values and private text must collapse to enum evidence.
            row = cancellation_row().replace('<br>', '<br><private-tag private-attribute="PRIVATE">PRIVATE</private-tag>')
            bad = lambda text: text.replace('<table></table>', '<table><tbody>' + row + '</tbody></table>', 1)
            def calendar():
                text = original_calendar()
                return bad(text) if failure_step == 'calendar_initial' else text
            def request(method, url, *, data):
                previous = client.month
                response = original_request(method, url, data=data)
                if data['miesiac'] == '5' and previous == 4 and failure_step == 'both':
                    response.text = response.text.replace('</body>', '<form></form></body>')
                return response
            client.calendar, client.request = calendar, request
            # Put bad rows on an in-window date even in the earlier month.
            if failure_step in ('calendar_select', 'both'):
                client.events[(2028, 4)] = {16: row}
            batch = collect_sources(client, current=CURRENT)
            failures = [entry for entry in records if entry['state'] == 'failed']
            with self.subTest(step=failure_step):
                self.assertTrue(failures)
                expected = 'calendar_select' if failure_step == 'both' else failure_step
                self.assertEqual(failures[0]['collection_step'], expected)
                self.assertEqual(failures[0]['error_code'], 'calendar_identity_unverified')
                evidence = failures[0]['evidence']
                self.assertEqual(evidence['provider_type'], 'cancelled_lessons')
                self.assertEqual(evidence['child_tags'], ['br', 'other'])
                self.assertEqual(evidence['tag_path'], ['div', 'table', 'tbody', 'tr', 'td'] if failure_step == 'calendar_initial' else ['div', 'table', 'tr', 'td'])
                self.assertEqual(evidence['attribute_categories'], ['other', 'style'])
                self.assertEqual(evidence['row_cell_count'], 1)
                self.assertEqual(evidence['child_count'], 2)
                self.assertEqual(batch['errors'], ['calendar_identity_unverified'])
                if failure_step == 'both':
                    self.assertEqual(failures[-1]['collection_step'], 'calendar_restore')
                    self.assertEqual(failures[-1]['evidence']['form_count'], 2)
                    self.assertEqual(len(failures[-1]['evidence']['forms']), 2)
                for private in ('PRIVATE', 'private-tag', 'private-attribute', 'color', 'Example Teacher'):
                    self.assertNotIn(private, json.dumps(records))

    def test_restore_form_evidence_survives_wrapping_without_primary_error(self):
        client = SchoolClient()
        records = []
        client.collection_diagnostic_sink = lambda item: records.append(dict(item))
        original = client.request
        def request(method, url, *, data):
            previous = client.month
            response = original(method, url, data=data)
            if data['miesiac'] == '5' and previous == 7:
                response.text = response.text.replace('</body>', '<form></form></body>')
            return response
        client.request = request
        batch = collect_sources(client, current=CURRENT)
        self.assertEqual(batch['errors'], ['calendar_restore_failed'])
        failure, = [entry for entry in records if entry['state'] == 'failed']
        self.assertEqual(failure['collection_step'], 'calendar_restore')
        self.assertEqual(failure['error_code'], 'calendar_form_count')
        self.assertEqual(failure['evidence']['form_count'], 2)
        self.assertEqual(len(failure['evidence']['forms']), 2)

    def test_restore_http_status_survives_wrapping_and_primary_failure_wins(self):
        for primary_failure in (False, True):
            client = SchoolClient()
            saved = []
            client.collection_diagnostic_sink = lambda item: saved.append(dict(item))
            original = client.request
            response = requests.Response()
            response.status_code = 503
            def request(method, url, *, data):
                previous_month = client.month
                if data['miesiac'] == '5' and (previous_month == 7 or primary_failure):
                    raise requests.HTTPError('PRIVATE response URL', response=response)
                result = original(method, url, data=data)
                if primary_failure and data['miesiac'] == '4':
                    result.text = result.text.replace('scheduleForm', 'PRIVATE')
                return result
            client.request = request
            batch = collect_sources(client, current=CURRENT)
            with self.subTest(primary_failure=primary_failure):
                self.assertEqual(batch['errors'], ['calendar_form_invalid' if primary_failure else 'calendar_restore_failed'])
                restore, = [item for item in saved if item['state'] == 'failed' and item.get('collection_step') == 'calendar_restore']
                self.assertEqual(restore['error_code'], 'calendar_restore_failed')
                self.assertEqual(restore.get('http_status'), 503)
                self.assertNotIn('PRIVATE', json.dumps(saved))

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
                school.calendar = lambda: original().replace('</body>', '<form id="PRIVATE"></form></body>')
                client = FakeClient(count=0)
                client.follow, client.request = school.follow, school.request
                client.collect_sources = lambda **_: collect_sources(client, current=CURRENT)
                SyncController(store, client, FakeRuntime(propose=False)).run(current=NOW)
                saved = store.status()['sync_diagnostics']
                failure, = [item for item in saved['invocations'] if item.get('error_code') == 'calendar_form_count']
                self.assertEqual(failure['evidence']['form_count'], 2)
                self.assertEqual(len(failure['evidence']['forms']), 2)
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
    def test_form_evidence_sanitizer_checks_nested_enums_counts_and_consistency(self):
        form = dict(position=1, identity_category='expected_calendar_id', action_category='calendar',
                    method='post', tag_path=['html', 'body', 'form'], control_count=3,
                    control_types={'input_hidden': 1, 'select': 2},
                    field_categories={'request_token': 1, 'month': 1, 'year': 1})
        valid = dict(form_count=2, forms=[form, dict(form, position=2)], omitted_form_count=0)
        def validate(evidence):
            return sync_diagnostics({'invocations': [diagnostic(evidence=evidence)], 'omitted_count': 0})
        self.assertEqual(validate(valid)['invocations'][0]['evidence'], valid)
        # Legacy count-only records remain readable.
        self.assertEqual(validate({'form_count': 2})['invocations'][0]['evidence'], {'form_count': 2})
        bad_forms = [dict(form, raw='PRIVATE'), dict(form, identity_category='PRIVATE'),
            dict(form, identity_category=[]), dict(form, action_category='PRIVATE'),
            dict(form, method='PRIVATE'), dict(form, method={}), dict(form, tag_path=['PRIVATE']),
            dict(form, tag_path=[]), dict(form, tag_path=['div'] * 8 + ['form']),
            dict(form, tag_path=['html', 'body']), dict(form, position=True), dict(form, position=2),
            dict(form, control_count=True), dict(form, control_count=1000001),
            dict(form, control_types={'PRIVATE': 3}), dict(form, control_types={'input_text': True}),
            dict(form, control_types={'input_text': 0}), dict(form, control_types={'input_text': 2}),
            dict(form, control_types=[]), dict(form, field_categories={'PRIVATE': 3}),
            dict(form, field_categories={'month': -1}), dict(form, field_categories={'month': 2}),
            dict(form, field_categories={'month': 1000001}), dict(form, field_categories='PRIVATE')]
        bad = [dict(valid, forms=[entry, dict(form, position=2)]) for entry in bad_forms]
        bad += [dict(valid, raw='PRIVATE'), dict(valid, forms={}), dict(valid, forms=[None]),
                dict(valid, omitted_form_count=True), dict(valid, omitted_form_count=-1),
                dict(valid, omitted_form_count=1000001), dict(valid, omitted_form_count=1),
                dict(valid, forms=[dict(form, position=i+1) for i in range(17)], form_count=17),
                dict(valid, forms=[form], form_count=2, omitted_form_count=1)]
        for evidence in bad:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate(evidence)
            error = SourceCollectionError('calendar_form_invalid', evidence=evidence)
            self.assertNotIn('evidence', failure_diagnostic(error))

    def test_structure_evidence_is_closed_bounded_and_rejects_private_injection(self):
        valid = {'tag_path': ['div', 'table', 'tr', 'td'], 'child_tags': ['br', 'other'],
                 'attribute_categories': ['style', 'other'], 'provider_type': 'cancelled_lessons',
                 'child_count': 2, 'row_cell_count': 1}
        def validate(evidence):
            return sync_diagnostics({'invocations': [diagnostic(evidence=evidence)], 'omitted_count': 0})
        self.assertEqual(validate(valid)['invocations'][0]['evidence'], valid)
        bad = [None, [], {}, {'raw': 'PRIVATE'}, dict(valid, raw='PRIVATE'),
               dict(valid, tag_path=['PRIVATE']), dict(valid, tag_path=[]), dict(valid, tag_path=['td'] * 9),
               dict(valid, tag_path=[{}]), dict(valid, child_tags=['PRIVATE']),
               dict(valid, child_tags=['br', 'br']), dict(valid, child_tags=[{'tag': 'br'}]),
               dict(valid, attribute_categories=['PRIVATE']), dict(valid, attribute_categories=['style', 'style']),
               dict(valid, provider_type='PRIVATE'),
               dict(valid, provider_type={}), dict(valid, child_count=-1), dict(valid, child_count=True),
               dict(valid, child_count=1000001), dict(valid, row_cell_count=True),
               dict(valid, row_cell_count=-1), dict(valid, row_cell_count=1000001),
               {'form_count': True}, {'form_count': -1}, {'form_count': 1000001}, {'form_count': 'PRIVATE'},
               {'evidence': {'form_count': 1}}]
        for evidence in bad:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate(evidence)
        error = SourceCollectionError('calendar_identity_unverified')
        error.evidence = dict(valid, raw='PRIVATE')
        self.assertNotIn('evidence', failure_diagnostic(error))

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
                    store.record_sync_diagnostic(run, dict(entry, state='failed', error_code='calendar_form_count',
                                                          http_status=503, evidence={'form_count': 2}))
                store.record_sync_diagnostic(run, diagnostic(state='running'))
                store.start_sync_run()
                saved = json.loads(store.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?', (run,)).fetchone()[0])
                sync_diagnostics(saved)
                self.assertLessEqual(len(json.dumps(saved)), 131072)
                self.assertGreater(saved['omitted_count'], 0)
                self.assertEqual(saved['invocations'][-1]['error_code'], 'worker_interrupted')
            finally:
                store.close()

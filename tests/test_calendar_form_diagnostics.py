"""Private form diagnostics use fictional inert markup only."""
from datetime import date
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from app.source_collectors import SourceCollectionError, parse_calendar_month
from test_source_collectors import calendar_page


class PrivateFormCaptureTests(unittest.TestCase):
    def test_same_failure_captures_static_structure_without_values_or_text(self):
        self.assertIsNotNone(importlib.util.find_spec("app.calendar_form_diagnostics"), "Private capture is not implemented")
        from app.calendar_form_diagnostics import PrivateFormCapture
        with tempfile.TemporaryDirectory() as tmp:
            sink = PrivateFormCapture(Path(tmp) / 'state.sqlite')
            private_marker = 'DO_NOT_RETAIN_741936'
            extra = ('<div id="integrationPanel" class="integration-container" role="region">'
                     '<form id="integrationForm" name="integrationRequest" method="post" '
                     f'action="/integration/dispatch?token={private_marker}#{private_marker}" onclick="{private_marker}">'
                     f'<input id="integrationKey" name="integration_key" type="hidden" value="{private_marker}">'
                     f'<span style="{private_marker}">{private_marker}</span><script>{private_marker}</script></form></div>'
                     '<input form="integrationForm" name="outside_field" type="hidden" value="'+private_marker+'">')
            html = calendar_page().replace('<body>', '<body>'+extra)
            with self.assertRaises(SourceCollectionError) as caught:
                parse_calendar_month(html, start=date(2028,5,1), end=date(2028,5,31), private_form_sink=sink)
            error = caught.exception
            self.assertEqual(error.diagnostic_code, 'calendar_form_count')
            self.assertEqual(error.private_capture_status, 'written')
            path = Path(tmp)/'calendar-form-diagnostics'/('form-'+error.private_capture_id+'.json')
            raw = path.read_text()
            artifact = json.loads(raw)
            self.assertNotIn(private_marker, raw)
            self.assertNotIn('token=', raw)
            form = artifact['forms'][0]
            self.assertEqual(form['id'], 'integrationForm')
            self.assertEqual(form['name'], 'integrationRequest')
            self.assertEqual(form['action_path'], '/integration/dispatch')
            self.assertEqual(form['controls'][0]['name'], 'integration_key')
            self.assertEqual(form['controls'][1]['association'], 'explicit')
            self.assertEqual(form['ancestry'][-2]['id'], 'integrationPanel')
            self.assertTrue(form['handler_present'])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_unassociated_control_has_bounded_technical_descriptor(self):
        from app.calendar_form_diagnostics import describe_forms
        from app.source_collectors import Document
        html = calendar_page().replace('</body>', '<input id="externalControl" name="outside_field" type="hidden"></body>')
        artifact = describe_forms(Document(html), 'calendar_form_external_control')
        self.assertEqual(artifact.get('unassociated_controls', [{}])[0].get('id'), 'externalControl')

    def test_failures_do_not_mask_parser_error_and_normal_page_does_not_capture(self):
        from unittest.mock import Mock
        sink = Mock(side_effect=OSError('PRIVATE FAILURE'))
        args = dict(start=date(2028,5,1), end=date(2028,5,31), private_form_sink=sink)
        parse_calendar_month(calendar_page(), **args)
        sink.assert_not_called()
        for callback, status in ((sink, 'capture_failed'), (None, 'not_configured'), (lambda *args:'private-id', 'capture_failed')):
            with self.subTest(status=status), self.assertRaises(SourceCollectionError) as caught:
                parse_calendar_month(calendar_page().replace('<body>', '<body><form></form>'),
                                     **dict(args, private_form_sink=callback))
            self.assertEqual(caught.exception.diagnostic_code, 'calendar_form_count')
            self.assertEqual(caught.exception.private_capture_status, status)
            self.assertIsNone(caught.exception.private_capture_id)

    def test_unsafe_storage_is_rejected_without_changing_original_error(self):
        from app.calendar_form_diagnostics import PrivateFormCapture
        for unsafe in ('directory_symlink', 'parent_symlink', 'public_directory', 'file_symlink'):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                from unittest.mock import patch
                parent = Path(tmp)
                directory = parent/'calendar-form-diagnostics'
                outside = parent/'outside'; outside.mkdir(mode=0o700)
                state = parent/'state.sqlite'
                if unsafe == 'directory_symlink': directory.symlink_to(outside, target_is_directory=True)
                if unsafe == 'parent_symlink':
                    (parent/'link').symlink_to(outside, target_is_directory=True)
                    state = parent/'link'/'state.sqlite'
                if unsafe == 'public_directory':
                    directory.mkdir(mode=0o700); directory.chmod(0o755)
                if unsafe == 'file_symlink':
                    directory.mkdir(mode=0o700)
                    target = outside/'target'; target.write_text('UNTOUCHED')
                    (directory/('form-'+'a'*32+'.json')).symlink_to(target)
                with patch('app.calendar_form_diagnostics.secrets.token_hex', return_value='a'*32):
                    with self.assertRaises(SourceCollectionError) as caught:
                        parse_calendar_month(calendar_page().replace('<body>', '<body><form></form>'),
                            start=date(2028,5,1), end=date(2028,5,31), private_form_sink=PrivateFormCapture(state))
                self.assertEqual(caught.exception.diagnostic_code, 'calendar_form_count')
                self.assertEqual(caught.exception.private_capture_status, 'capture_failed')
                if unsafe == 'file_symlink': self.assertEqual(target.read_text(), 'UNTOUCHED')

    def test_retention_only_removes_owned_matching_regular_artifacts(self):
        from unittest.mock import patch
        from app.calendar_form_diagnostics import PrivateFormCapture
        from app.source_collectors import Document
        with tempfile.TemporaryDirectory() as tmp:
            sink = PrivateFormCapture(Path(tmp)/'state.sqlite')
            doc = Document(calendar_page())
            first = sink(doc, 'calendar_form_count')
            directory = Path(tmp)/'calendar-form-diagnostics'
            unrelated = directory/'keep.json'; unrelated.write_text('keep')
            linked = directory/('form-'+'a'*32+'.json'); linked.symlink_to(unrelated)
            with patch('app.calendar_form_diagnostics.MAX_FILES', 3):
                for unused in range(5): sink(doc, 'calendar_form_count')
            regular = [path for path in directory.glob('form-*.json') if not path.is_symlink()]
            self.assertEqual(len(regular), 3)
            self.assertFalse((directory/('form-'+first+'.json')).exists())
            self.assertTrue(linked.is_symlink())
            self.assertEqual(unrelated.read_text(), 'keep')

    def test_identifier_route_and_size_bounds_report_redactions_and_omissions(self):
        from unittest.mock import patch
        from app.calendar_form_diagnostics import describe_forms
        from app.source_collectors import Document
        private_marker = 'credentialSentinel_564738'
        form = '<form id="integrationForm" class="a b c d e" action="https://'+private_marker+'@external.invalid/integration/12345/%61bc/'+private_marker+'?'+private_marker+'#'+private_marker+'">'
        form += ''.join('<input name="field_name" value="'+private_marker+'">' for unused in range(40))+'</form>'
        with patch('app.calendar_form_diagnostics.MAX_BYTES', 5000):
            artifact = describe_forms(Document('<html><body>'+form*20+'</body></html>'), 'calendar_form_count')
        raw = json.dumps(artifact)
        self.assertLessEqual(len(raw.encode()), 5000)
        self.assertNotIn(private_marker, raw)
        self.assertNotIn('external.invalid', raw)
        self.assertNotIn('12345', raw)
        self.assertNotIn('%61bc', raw)
        self.assertGreater(artifact['redacted_identifier_count'], 0)
        self.assertGreater(artifact['omitted_class_count'], 0)
        self.assertEqual(artifact['form_count'], len(artifact['forms']) + artifact['omitted_form_count'])
        for item in artifact['forms']:
            self.assertEqual(item['control_count'], len(item['controls']) + item['omitted_control_count'])

    def test_initial_select_collect_and_restore_use_their_actual_response(self):
        from unittest.mock import patch
        from app.calendar_form_diagnostics import PrivateFormCapture
        from app.source_collectors import collect_calendar
        from app.health_state import sync_diagnostics
        from test_source_collectors import FakeClient, FETCHED, ORIGIN
        for step in ('calendar_initial', 'calendar_select', 'calendar_collect', 'calendar_restore', 'restore_post'):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                client.events = {}
                client.private_form_diagnostic_sink = PrivateFormCapture(Path(tmp)/'state.sqlite')
                records=[]; client.collection_diagnostic_sink=records.append
                original_calendar, original_request = client.calendar, client.request
                def bad(html): return html.replace('<body>', '<body><form id="failureForm" action="/integration/dispatch"></form>')
                if step == 'calendar_initial': client.calendar=lambda:bad(original_calendar())
                if step in {'calendar_select', 'restore_post'}:
                    def request(method,url,*,data):
                        response=original_request(method,url,data=data)
                        if (step == 'calendar_select' and data['miesiac']=='6') or (step == 'restore_post' and data['miesiac']=='5'):
                            response.text=bad(response.text)
                        return response
                    client.request=request
                if step == 'calendar_restore':
                    original_follow=client.follow
                    def follow(url):
                        response=original_follow(url)
                        if url==ORIGIN+'/terminarz' and client.month==6: response.text=bad(response.text)
                        return response
                    client.follow=follow
                args=dict(learner_id='fictional', fetched_at=FETCHED, start=date(2028,5,1), end=date(2028,6,1))
                if step == 'calendar_collect':
                    from app.source_collectors import _select_month
                    def selection(*args):
                        html, form=_select_month(*args)
                        return bad(html), form
                    context=patch('app.source_collectors._select_month', side_effect=selection)
                else:
                    from contextlib import nullcontext
                    context=nullcontext()
                with context, self.assertRaises(SourceCollectionError): collect_calendar(client, **args)
                failed=[item for item in records if item.get('private_capture_status')=='written']
                self.assertEqual(len(failed), 1, records)
                expected='calendar_restore' if step=='restore_post' else ('calendar_select' if step=='calendar_collect' else step)
                self.assertEqual(failed[0]['collection_step'], expected)
                sync_diagnostics(dict(invocations=[dict(item,phase='fetch') for item in records], omitted_count=0))
                files=list((Path(tmp)/'calendar-form-diagnostics').glob('*.json'))
                self.assertEqual(len(files), 1)
                self.assertIn('failureForm', files[0].read_text())
                self.assertEqual(client.calls.count(('GET',ORIGIN+'/terminarz')), 1 if step=='calendar_initial' else 2)

    def test_sync_configures_restores_sink_and_persists_opaque_reference(self):
        from app.core import AppStore
        from app.sync import SyncController
        from app.source_collectors import collect_sources
        from test_source_collectors import FakeClient, CURRENT
        from test_support import TEST_CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            store=AppStore(Path(tmp)/'state.sqlite',config=TEST_CONFIG)
            self.addCleanup(store.close)
            client=FakeClient(); original=client.calendar
            client.calendar=lambda:original().replace('<body>','<body><form id="failureForm"></form>')
            client.collect_sources=lambda **args:collect_sources(client,**args)
            previous=object(); client.private_form_diagnostic_sink=previous
            controller=SyncController(store,client)
            controller.run_id=store.start_sync_run(); controller.stage='fetch'
            controller._source_collection(CURRENT)
            self.assertIs(client.private_form_diagnostic_sink,previous)
            raw=store.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?',(controller.run_id,)).fetchone()[0]
            records=json.loads(raw)['invocations']
            failed=next(item for item in records if item.get('private_capture_status')=='written')
            self.assertRegex(failed['private_capture_id'], '^[a-f0-9]{32}$')
            self.assertNotIn('failureForm',raw)
            self.assertNotIn('scheduleForm',raw)

    def test_public_validator_rejects_private_details_and_malformed_references(self):
        from app.health_state import sync_diagnostics, failure_diagnostic
        base=dict(phase='fetch',state='failed',requested_count=1,received_count=0,duration_seconds=0.1,
                  process_code=None,started_count=0,completed_count=0)
        for extras in (dict(private_capture_status='written',private_capture_id='../private'),
                       dict(private_capture_status='written'), dict(private_capture_status=[]),
                       dict(private_capture_status='capture_failed',private_capture_id='a'*32),
                       dict(private_capture_id='a'*32), dict(private_capture_status='PRIVATE'),
                       dict(private_capture_status='written',private_capture_id='a'*32,form_id='integrationForm'),
                       dict(evidence={'form_count':2,'form_id':'integrationForm'})):
            with self.subTest(extras=extras), self.assertRaises(ValueError):
                sync_diagnostics(dict(invocations=[dict(base,**extras)],omitted_count=0))
        error=SourceCollectionError('calendar_form_invalid',private_capture_status='written',private_capture_id='bad')
        self.assertNotIn('private_capture_id',failure_diagnostic(error))

    def test_normal_full_collection_does_not_create_private_directory(self):
        from app.calendar_form_diagnostics import PrivateFormCapture
        from app.source_collectors import collect_sources
        from test_source_collectors import FakeClient, CURRENT
        with tempfile.TemporaryDirectory() as tmp:
            client=FakeClient()
            client.private_form_diagnostic_sink=PrivateFormCapture(Path(tmp)/'state.sqlite')
            self.assertEqual(collect_sources(client,current=CURRENT)['status'],'ok')
            self.assertFalse((Path(tmp)/'calendar-form-diagnostics').exists())

    def test_identifiers_cannot_repeat_response_values_or_obvious_random_tokens(self):
        from app.calendar_form_diagnostics import describe_forms
        from app.source_collectors import Document
        # These fictional alphabetic markers deliberately pass a plain identifier regex.
        markers = ['QrStUvWxYzAbCdGh', 'fieldvaluemarker', 'queryvaluemarker', 'fragmentmarker']
        markup = ('<form id="integrationForm" name="integrationRequest" action="/integration/'+markers[2]+
                  '?opaque=%71'+markers[2][1:]+'#%66'+markers[3][1:]+'">'
                  '<input name="technical_field" type="hidden" value="'+markers[1]+'">'
                  + ''.join('<input id="'+marker+'" name="'+marker+'" value="">' for marker in markers)+
                  '</form><form action="/integration/'+markers[1]+'/'+markers[3]+'/'+markers[0]+'"></form>')
        artifact = describe_forms(Document('<html><body>'+markup+'</body></html>'), 'calendar_form_count')
        raw = json.dumps(artifact)
        for marker in markers: self.assertNotIn(marker, raw)
        self.assertEqual(artifact['forms'][0]['id'], 'integrationForm')
        self.assertEqual(artifact['forms'][0]['name'], 'integrationRequest')
        self.assertEqual(artifact['forms'][0]['controls'][0]['name'], 'technical_field')

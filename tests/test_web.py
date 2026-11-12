from test_support import TEST_CONFIG
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from app.web import create_app


class FakeStore:
    def __init__(self):
        self.paused = True
        self.decisions = []
        self.requested = False
    def status(self):
        return dict(messages=1, events=0, proposals_review=1, proposals_pending=0,
                    operations_unknown=0, writes_paused=self.paused, last_sync=None, last_error=None,
                    last_successful_sync='2026-10-08T08:00:00+00:00', last_attempt_at='2026-10-08T09:00:00+00:00',
                    last_result='partial', last_duration_seconds=15, messages_pending=2, operations_pending=1)
    def list_messages(self):
        return [dict(id='m1',subject='<script>alert(1)</script>',sender='Nauczyciel',
                     sent_at='2026-10-08T09:00:00+02:00',text='<img src=x onerror=alert(1)>',
                     status='pending',attachment_count=0)]
    def get_message(self, key):
        return self.list_messages()[0] if key == 'm1' else None
    def list_proposal_groups(self, **kwargs):
        return []
    def get_proposal_group_for_proposal(self, key, **kwargs):
        return None
    def list_events(self):
        return []
    def list_proposals(self):
        return [dict(id='p1',message_id='m1',kind='cancel',title='Wycieczka',
                     status='review',review_reason='Odwołano',source_quote='Nie jedziemy',start='',end='')]
    def set_writes_paused(self, paused):
        self.paused = paused
    def resolve_proposal(self, key, action, changes):
        self.decisions.append((key,action,changes))
    def request_sync(self):
        self.requested=True


class WebTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.calls=[]
        self.app=create_app(self.store, lambda:self.calls.append('sync'), owner='owner@example.test',
                            hosts={'home.test:8445'}, config=TEST_CONFIG)
        self.client=TestClient(self.app,base_url='https://home.test:8445',client=('127.0.0.1',1234))
        self.client.headers['Tailscale-User-Login']='owner@example.test'
    def submit(self,path,data,origin='https://home.test:8445'):
        self.client.get('/')
        return self.client.post(path,data={'csrf':self.client.cookies['__Host-librus_csrf'],**data},
                                headers={'origin':origin},follow_redirects=False)
    def test_additive_technical_diagnostics_are_readable_in_panel_and_status(self):
        original=self.store.status
        legacy=dict(phase='reconcile',state='failed',requested_count=10,
            received_count=7,duration_seconds=3.25,process_code=-9,started_count=1,completed_count=0)
        for details in ({},dict(error_code='message_fetch_http',source_kind='message',collection_step='message_body',http_status=503)):
            with self.subTest(enriched=bool(details)):
                diagnostics={'invocations':[dict(legacy,**details)],'omitted_count':0}
                self.store.status=lambda:dict(original(),sync_diagnostics=diagnostics)
                self.assertEqual(self.client.get('/api/status').json()['sync_diagnostics'],diagnostics)
                response=self.client.get('/')
                self.assertEqual(response.status_code,200)
                self.assertIn('Diagnostyka ostatniej synchronizacji',response.text)
                self.assertIn('10 / 7',response.text)
                self.assertIn('reconcile',response.text)
                if details:
                    self.assertIn('message_fetch_http',response.text)
                    self.assertIn('message_body',response.text)
                    self.assertIn('HTTP 503',response.text)

    def test_batch_retry_attempts_and_recovery_render_in_existing_diagnostics(self):
        original=self.store.status
        base=dict(phase='reconcile',requested_count=1,received_count=0,duration_seconds=1.0,
                  process_code=0,started_count=0,completed_count=0,batch_index=2,
                  parse_error_count=0,unknown_event_count=0,incomplete_count=0,
                  turn_completed=True,final_answer_classification='exact_ok_true')
        diagnostics={'invocations':[
            dict(base,state='failed',attempt_number=1,retry_decision='retry',recovered=False,error_code='unexpected_tool_count'),
            dict(base,state='completed',attempt_number=2,retry_decision='not_needed',recovered=True,received_count=1,completed_count=1)],'omitted_count':0}
        self.store.status=lambda:dict(original(),sync_diagnostics=diagnostics)
        self.assertEqual(self.client.get('/api/status').json()['sync_diagnostics'],diagnostics)
        text=self.client.get('/').text
        for expected in ('Partia 2 · próba 1','Partia 2 · próba 2','Odzyskano po ponowieniu',
                         'retry','not_needed','exact_ok_true','Parser: 0 / 0 / 0','Tura zakończona: tak'):
            self.assertIn(expected,text)

    def test_structural_diagnostics_render_in_existing_table(self):
        original = self.store.status
        evidence = {'tag_path': ['div', 'table', 'tr', 'td'], 'child_tags': ['br', 'other'],
                    'attribute_categories': ['style', 'other'], 'provider_type': 'cancelled_lessons',
                    'child_count': 2, 'row_cell_count': 1}
        for outline in (evidence, {'form_count': 3}):
            diagnostic = dict(phase='fetch', state='failed', requested_count=1, received_count=0,
                duration_seconds=0.1, process_code=None, started_count=0, completed_count=0,
                error_code='calendar_identity_unverified', evidence=outline)
            self.store.status = lambda: dict(original(), sync_diagnostics={'invocations': [diagnostic], 'omitted_count': 0})
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Struktura:', response.text)
            if 'form_count' in outline:
                self.assertIn('formularze: 3', response.text)
            else:
                self.assertIn('div / table / tr / td', response.text)
                self.assertIn('cancelled_lessons', response.text)
                self.assertIn('br, other', response.text)
                self.assertIn('style, other', response.text)
                self.assertIn('komórki wiersza: 1', response.text)

    def test_form_descriptors_render_individually_with_unknown_purpose_and_omission(self):
        original = self.store.status
        forms = [dict(position=i+1, identity_category='expected_calendar_id' if i == 0 else 'other_id',
                      action_category='calendar' if i == 0 else 'same_origin_other', method='post',
                      tag_path=['html', 'body', 'div', 'form'], control_count=3,
                      control_types={'input_hidden': 1, 'select': 2},
                      field_categories={'request_token': 1, 'month': 1, 'year': 1} if i == 0 else {'other': 3})
                 for i in range(16)]
        outline = dict(form_count=17, forms=forms, omitted_form_count=1)
        item = dict(phase='fetch', state='failed', requested_count=1, received_count=0,
                    duration_seconds=0.1, process_code=None, started_count=0, completed_count=0,
                    error_code='calendar_form_count', evidence=outline)
        self.store.status = lambda: dict(original(), sync_diagnostics={'invocations': [item], 'omitted_count': 0})
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        for text in ('formularze: 17', 'Formularz 1', 'Formularz 2', 'expected_calendar_id',
                     'same_origin_other', 'html / body / div / form', 'input_hidden: 1',
                     'select: 2', 'request_token: 1', 'other: 3', 'Pominięte formularze: 1',
                     'Przeznaczenie nierozpoznanych formularzy pozostaje nieustalone.'):
            self.assertIn(text, response.text)
        self.assertEqual(self.client.get('/api/status').json()['sync_diagnostics']['invocations'][0]['evidence'], outline)

    def test_overflow_panel_renders_only_fixed_numeric_size_evidence(self):
        original = self.store.status
        sections = dict(sources=12000, source_catalog=10000, agenda=140000, offer_groups=200,
                        preferences=100, unsettled_operations=2, envelope=800)
        metadata = dict(input_bytes=163102, input_limit_bytes=160000, section_bytes=sections,
                        source_count=17, catalog_count=133, agenda_count=70, private_field='PRIVATE_VALUE')
        sections['PRIVATE_KEY'] = 'PRIVATE_VALUE'
        for evidence in (metadata, {}, dict(metadata, input_bytes='PRIVATE_VALUE', input_limit_bytes=True,
                                            section_bytes={'agenda': 'PRIVATE_VALUE', 'sources': True}, source_count={'text': 'PRIVATE_VALUE'}),
                         dict(metadata, input_bytes=-1, input_limit_bytes=2**63, source_count=True,
                              catalog_count=5001, agenda_count=1.5, section_bytes={'agenda': 2**63, 'sources': -1})):
            self.store.status = lambda: dict(original(), agenda={'latest_plan': {
                'status': 'failed', 'error_code': 'agenda_context_overflow', 'metadata': evidence}})
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            self.assertIn('agenda_context_overflow', response.text)
            self.assertNotIn('PRIVATE', response.text)
            if evidence is metadata:
                self.assertIn('163102 / 160000 B', response.text)
                for key, value in sections.items():
                    if key != 'PRIVATE_KEY':
                        self.assertIn('%s: %d B' % (key, value), response.text)
                self.assertIn('source_count: 17', response.text)
            elif evidence:
                self.assertNotIn('sources: True', response.text)
                self.assertNotIn('sources: -1', response.text)
                self.assertNotIn('catalog_count: 5001', response.text)
                self.assertNotIn(str(2**63), response.text)

    def test_identity_and_socket_are_required(self):
        self.client.headers.pop('Tailscale-User-Login')
        self.assertEqual(self.client.get('/').status_code,403)
        other=TestClient(self.app,base_url='https://home.test:8445',client=('100.1.2.3',1234))
        self.assertEqual(other.get('/',headers={'Tailscale-User-Login':'owner@example.test'}).status_code,403)
    def test_spoofed_host_and_wrong_owner(self):
        self.assertEqual(self.client.get('/',headers={'host':'evil.test'}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Tailscale-User-Login':'other@example.test'}).status_code,403)
    def test_message_html_is_escaped(self):
        response=self.client.get('/messages/m1')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('<img src=x',response.text)
        self.assertNotIn('<script>alert',response.text)
        self.assertIn('&lt;img',response.text)
        self.assertIn("script-src 'none'",response.headers['content-security-policy'])
        self.assertEqual(response.headers['referrer-policy'], 'same-origin')
    def test_mutations_require_origin_and_csrf(self):
        self.assertEqual(self.submit('/writes',{'paused':'false'},origin='https://evil.test').status_code,403)
        self.assertTrue(self.store.paused)
        self.assertEqual(self.client.post('/writes',data={'paused':'false'},headers={'origin':'https://home.test:8445'}).status_code,403)
        self.assertEqual(self.submit('/writes',{'paused':'false'}).status_code,303)
        self.assertFalse(self.store.paused)
    def test_sync_uses_only_fixed_callback(self):
        self.assertEqual(self.submit('/sync',{}).status_code,303)
        self.assertTrue(self.store.requested)
        self.assertEqual(self.calls,['sync'])
    def test_cancel_requires_explicit_confirmation(self):
        self.assertEqual(self.submit('/proposals/p1',{'action':'approve'}).status_code,400)
        self.assertEqual(self.store.decisions,[])
        self.assertEqual(self.submit('/proposals/p1',{'action':'confirm_cancel'}).status_code,303)
        self.assertEqual(self.store.decisions,[('p1','confirm_cancel',{})])
    def test_connector_and_google_event_shapes_render(self):
        self.store.list_events = lambda: [
            {'snapshot': {'title': 'Zebranie', 'start': '2030-10-23T15:30:00+00:00'}, 'message_id': 'm1'},
            {'snapshot': {'summary': 'Zgody', 'start': {'date': '2030-10-19'}}, 'message_id': 'm1'},
        ]
        response = self.client.get('/events')
        self.assertEqual(response.status_code,200)
        self.assertIn('23.10.2030, 17:30',response.text)
        self.assertIn('19.10.2030',response.text)
        self.assertIn('Zebranie',response.text)

    def test_review_renders_and_rejection_maps_to_ignore(self):
        self.assertEqual(self.client.get('/review').status_code,200)
        self.assertEqual(self.submit('/proposals/p1',{'action':'reject'}).status_code,303)
        self.assertEqual(self.store.decisions[0][1],'ignore')

    def test_status_shows_success_attempt_result_duration_and_backlog(self):
        response = self.client.get('/')
        self.assertIn('Ostatni pełny sukces', response.text)
        self.assertIn('08.10.2026, 10:00', response.text)
        self.assertIn('08.10.2026, 11:00', response.text)
        self.assertIn('Częściowo wykonano', response.text)
        self.assertIn('Czas: 15 s', response.text)
        self.assertIn('2 wiadomości, 1 zapisów', response.text)
        self.assertEqual(self.client.get('/api/status').json()['last_result'], 'partial')

    def test_legacy_probe_has_no_invented_success(self):
        response = self.client.get('/')
        probe = response.text.split('aria-label="Ostatnie sprawdzenie kalendarza"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(probe.count('Brak danych'), 3)
        self.assertNotIn('Dostęp potwierdzony', probe)

    def test_successful_probe_retry_is_neutral_status(self):
        status = self.store.status()
        status.update(last_result='ok', calendar_probe_result='ok', calendar_probe_attempts=2,
                      calendar_probe_recovered=True)
        self.store.status = lambda: status
        response = self.client.get('/')
        probe = response.text.split('aria-label="Ostatnie sprawdzenie kalendarza"', 1)[1].split('</section>', 1)[0]
        self.assertIn('Dostęp potwierdzony', probe)
        self.assertIn('Liczba prób</span><strong>2</strong>', probe)
        self.assertIn('Odzyskano po ponowieniu</span><strong>Tak</strong>', probe)
        self.assertNotIn('role="alert"', response.text)

    def test_probe_presentation_rejects_malformed_values(self):
        status = self.store.status()
        status.update(calendar_probe_result={'private': 'payload'}, calendar_probe_attempts=True,
                      calendar_probe_recovered='private response')
        self.store.status = lambda: status
        response = self.client.get('/')
        probe = response.text.split('aria-label="Ostatnie sprawdzenie kalendarza"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(probe.count('Brak danych'), 3)
        self.assertNotIn('private', probe)

class RealStoreIntegrationTests(unittest.TestCase):
    def test_panel_controls_persist_and_approval_survives_reload(self):
        from app.core import AppStore
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory) / 'state.sqlite3', config=TEST_CONFIG)
            store.save_message(dict(id='m-real', message_id='m-real', subject='Zebranie', sender='Wychowawca',
                                    sent_at='2026-10-08T09:00:00+02:00', text='Zebranie za tydzień'))
            start = (datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
            store.save_proposals('m-real', [dict(kind='create', title='Zebranie', description='',
                start=start, end=None, all_day=False, confidence='low', needs_review=True,
                review_reason='Sprawdź termin', source_quote='Zebranie za tydzień', event_id=None)])
            proposal=store.list_proposals()[0]
            client=TestClient(create_app(store,lambda:None,allow_local=True, config=TEST_CONFIG),
                              base_url='http://testserver',client=('127.0.0.1',1234))
            self.assertEqual(client.get('/review').status_code,200)
            token=client.cookies['librus_csrf']
            response=client.post('/proposals/'+proposal['id'],data={'csrf':token,'action':'approve'},
                                 headers={'origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(response.status_code,303)
            response=client.post('/writes',data={'csrf':token,'paused':'false'},
                                 headers={'origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(response.status_code,303)
            store.close()
            reopened=AppStore(Path(directory) / 'state.sqlite3', config=TEST_CONFIG)
            self.assertFalse(reopened.status()['writes_paused'])
            self.assertEqual(reopened.list_proposals()[0]['status'],'pending')
            reopened.close()

if __name__=='__main__':unittest.main()

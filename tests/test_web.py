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

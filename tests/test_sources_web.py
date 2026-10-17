"""Read-only source navigation remains private, escaped and separate from decisions."""
from test_web import FakeStore
from test_support import TEST_CONFIG
from app.web import create_app
from fastapi.testclient import TestClient
import unittest


class SourceStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.items = [dict(id=f's{i}', kind='announcement' if i % 2 else 'school_event',
            title=f'Źródło {i}', sender='Nauczyciel demo', text='<script>private</script>',
            published_at='2030-03-06T09:00:00+01:00', metadata={'attachment_count': 1},
            content_hash='current-hash', status='analyzed', outcome='linked', reason='Powiązano ten sam obowiązek.', action_ids=['a1']) for i in range(102)]
    def list_sources(self, limit=100, offset=0, kind=None, status=None):
        rows = [i for i in self.items if (kind is None or i['kind'] == kind) and (status is None or i['status'] == status)]
        return rows[offset:offset+limit]
    def get_source(self, key):
        return next((i for i in self.items if i['id'] == key), None)
    def get_action(self, key):
        if key == 'a2':
            return dict(id='a2',proposal_id='p2',status='applied',payload={'title':'Istniejące spotkanie','confidence':'high','activity_scope':'school','start':'2030-03-08T10:00:00+01:00'},source_links=[],decision={'proposal':{'user_approved':True}},event_ids=['event-demo'])
        if key != 'a1': return None
        return dict(id='a1', proposal_id='p1', status='review', payload={'title':'Wspólne działanie','confidence':'high','optional':True},
            source_links=[{'source_id':'s0','source_quote':'Literalny cytat','relation':'origin'},
                          {'source_id':'s1','source_quote':'Drugi cytat','relation':'enriched'}], decision={'action':'reject'},event_ids=[])
    def list_source_previews(self, limit=100, offset=0, status=None):
        planned={'id':'virtual-demo','payload':{'title':'Fikcyjny podgląd','confidence':'low','activity_scope':'school','optional':True},'decision':None}
        linked=self.get_action('a2')
        rejected=self.get_action('a1')
        changed={**linked,'payload':{**linked['payload'],'start':'2030-03-09T11:00:00+01:00'}}
        outcomes=[{'action_id':'virtual-demo','outcome':'added','reason':'Nowe planowane działanie','action':planned},
                  {'action_id':'a2','outcome':'linked','reason':'Ten sam obowiązek','action':linked},
                  {'action_id':'a1','outcome':'ignored','reason':'Zachowano wcześniejsze odrzucenie','action':rejected},
                  {'action_id':'a2','outcome':'review','reason':'Nowy termin wymaga sprawdzenia','action':changed}]
        return [dict(id='preview',source_id='s1',source_hash='current-hash',status='ready',outcomes=outcomes,
            proposals=[dict(title='Nieaktualny wynik ekstrakcji')])][offset:offset+limit]


class SourcesWebTests(unittest.TestCase):
    def setUp(self):
        self.store = SourceStore()
        self.app = create_app(self.store, lambda:None, config=TEST_CONFIG, owner='owner@example.test',hosts={'home.test:8445'})
        self.client = TestClient(self.app,base_url='https://home.test:8445',client=('127.0.0.1',1234))
        self.client.headers['Tailscale-User-Login']='owner@example.test'
    def test_source_pagination_and_filters_are_bounded(self):
        result = self.client.get('/api/sources?limit=100').json()
        self.assertEqual(len(result['items']),100)
        self.assertEqual(result['next_offset'],100)
        result = self.client.get('/api/sources?limit=100&offset=100').json()
        self.assertEqual(len(result['items']),2)
        self.assertIsNone(result['next_offset'])
        self.assertTrue(all(i['kind']=='school_event' for i in self.client.get('/api/sources?kind=school_calendar').json()['items']))
        for query in ('limit=0','limit=101','offset=-1','kind=secret'):
            self.assertIn(self.client.get('/api/sources?'+query).status_code,(400,422))
    def test_source_detail_and_action_show_provenance_without_new_decision_path(self):
        page = self.client.get('/sources/s1')
        self.assertEqual(page.status_code,200)
        self.assertIn('&lt;script&gt;private',page.text)
        self.assertIn('Podgląd gotowy',page.text)
        self.assertIn('Zachowano wcześniejsze odrzucenie',page.text)
        self.assertIn('Załącznik nieanalizowany',page.text)
        self.assertIn('/actions/a1',page.text)
        action = self.client.get('/actions/a1')
        self.assertIn('Wszystkie źródła działania',action.text)
        self.assertIn('Opcjonalne przypomnienie',action.text)
        self.assertIn('Literalny cytat',action.text)
        self.assertIn('Pewność: wysoka',action.text)
        self.assertIn('Źródło pierwotne',action.text)
        self.assertIn('Zachowana decyzja rodzica: odrzucono',action.text)
        self.assertIn('/sources/s0',action.text)
        self.assertIn('/sources/s1',action.text)
        self.assertNotIn('action="/proposals/',action.text)
        self.assertEqual(len(self.client.get('/api/actions/a1').json()['sources']),2)
        self.assertEqual(self.client.get('/actions/absent').status_code,404)
    def test_preview_has_no_activation_or_approval_control(self):
        page = self.client.get('/preview')
        self.assertEqual(page.status_code,200)
        self.assertIn('Podgląd — bez zapisu do kalendarza',page.text)
        self.assertIn('Fikcyjny podgląd',page.text)
        self.assertIn('Pewność: niska · wydarzenie szkolne',page.text)
        self.assertNotIn('Zatwierdź termin',page.text)
        self.assertNotIn('<form',page.text)
        self.assertNotIn('action="/proposals/',page.text)
        self.assertEqual(self.client.get('/api/source-previews').json()['items'][0]['status'],'ready')
        self.assertEqual(self.store.decisions,[])
        self.assertIn('Podgląd: Powiązano',page.text)
        self.assertIn('Podgląd: Pominięto',page.text)
        self.assertIn('Zachowana decyzja rodzica: odrzucono',page.text)
        self.assertIn('Planowana różnica wobec obecnego działania',page.text)
        self.assertIn('09.03.2030, 11:00',page.text)
        self.assertIn('/actions/a2',page.text)
        self.assertNotIn('/actions/virtual-demo',page.text)
        self.assertNotIn('Nieaktualny wynik ekstrakcji',page.text)
    def test_current_ready_preview_overrides_old_analysis_failure_but_stale_preview_does_not(self):
        self.store.items[1].update(status='failed',outcome='analysis_incomplete',reason='Old extraction failure')
        current=self.client.get('/sources/s1').text
        self.assertIn('Podgląd gotowy',current)
        self.assertNotIn('Old extraction failure',current)
        self.store.items[1]['content_hash']='new-version'
        stale=self.client.get('/sources/s1').text
        self.assertIn('Analiza niepełna',stale)
        self.assertIn('Old extraction failure',stale)
        self.assertNotIn('Zachowano wcześniejsze odrzucenie',stale)

    def test_unknown_presentation_values_are_not_exposed_as_internal_enums(self):
        original=self.store.get_action
        def unknown_action(key):
            action=original(key)
            if action and key == 'a1':
                action['payload']['confidence']='internal_confidence'
                action['status']='internal_status'
                action['source_links'][0]['relation']='internal_relation'
                action['decision']={'action':'internal_decision'}
            return action
        self.store.get_action=unknown_action
        page=self.client.get('/actions/a1')
        self.assertEqual(page.status_code,200)
        self.assertIn('Pewność: nieznana',page.text)
        self.assertIn('Powiązanie źródłowe',page.text)
        self.assertIn('Zachowana decyzja rodzica: zapisana',page.text)
        self.assertNotIn('internal_',page.text)

    def test_changed_offer_membership_has_no_standalone_approval(self):
        proposal=self.store.list_proposals()[0]
        proposal.update(kind='create',blocked_by_offer_group='original-group',alternative_group='new-option')
        self.store.list_proposals=lambda:[proposal]
        self.store.get_proposal_group=lambda key,**kw:{'message_id':'original-offer-message'}
        page=self.client.get('/review')
        self.assertEqual(page.status_code,200)
        self.assertIn('Nie można zatwierdzić jej osobno',page.text)
        self.assertIn('/messages/original-offer-message',page.text)
        self.assertNotIn('value="approve"',page.text)
        self.assertIn('value="reject"',page.text)
        proposal.pop('blocked_by_offer_group')
        orphan=self.client.get('/review')
        self.assertEqual(orphan.status_code,200)
        self.assertNotIn('value="approve"',orphan.text)

    def test_new_endpoints_require_owner(self):
        self.client.headers.pop('Tailscale-User-Login')
        for path in ('/sources','/preview','/api/sources','/api/actions/a1','/api/source-previews'):
            self.assertEqual(self.client.get(path).status_code,403)

if __name__ == '__main__': unittest.main()

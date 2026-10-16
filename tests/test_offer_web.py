"""Grouped offers remain complete and their decisions use the protected endpoint."""
import unittest
from copy import deepcopy
from fastapi.testclient import TestClient
from test_support import TEST_CONFIG
from test_web import FakeStore
from app.web import create_app


class OfferStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.group = dict(id='offer1', message_id='m1', status='open', revision=1, stale=False,
                          selected_proposal_id=None, source_evidence='Wybierz jeden termin', members=[
            dict(id=f'option{index}', message_id='m1', title='Szkolenie demo', status='review',
                 start=f'2030-03-{day}T10:00:00+01:00', end=f'2030-03-{day}T12:00:00+01:00',
                 all_day=False, description=f'Sala {index}', source_quote='Wybierz jeden termin',
                 eligible=index != 0, ineligible_reason='past' if index == 0 else '')
            for index, day in enumerate(('05', '09', '12'))])
    def status(self):
        return {**super().status(), 'proposals_review': 1 + sum(item['status'] == 'review' for item in self.group['members'])}
    def list_proposal_groups(self, **kwargs):
        return [deepcopy(self.group)]
    def get_proposal_group(self, key, **kwargs):
        return deepcopy(self.group) if key == 'offer1' else None
    def get_proposal_group_for_proposal(self, key, **kwargs):
        return deepcopy(self.group) if any(item['id'] == key for item in self.group['members']) else None
    def resolve_proposal_group(self, key, decision, proposal_id=None, expected_revision=None, **kwargs):
        if key != self.group['id'] or expected_revision != self.group['revision'] or self.group['status'] != 'open':
            raise ValueError('Changed group')
        if decision == 'select':
            option = next((item for item in self.group['members'] if item['id'] == proposal_id), None)
            if not option or not option['eligible']:
                raise ValueError('Unavailable option')
            for item in self.group['members']:
                item['status'] = 'ignored'
            self.group.update(status='selected', selected_proposal_id=proposal_id)
            option['status'] = 'pending'
        else:
            self.group['status'] = 'rejected'
            for item in self.group['members']:
                item['status'] = 'ignored'


class OfferWebTests(unittest.TestCase):
    def setUp(self):
        self.store = OfferStore()
        self.client = TestClient(create_app(self.store, lambda: None, config=TEST_CONFIG, allow_local=True),
                                 base_url='http://testserver', client=('127.0.0.1', 1234))
    def submit(self, data, **kwargs):
        self.client.get('/')
        return self.client.post('/api/proposal-groups/offer1/decision',
                                data={'csrf': self.client.cookies['librus_csrf'], 'expected_revision': '1', **data},
                                headers={'origin': 'http://testserver'}, follow_redirects=False, **kwargs)
    def test_groups_are_complete_independent_of_legacy_proposal_limit_and_no_default(self):
        self.store.list_proposals = lambda: [dict(id=f'noise{i}', message_id='m1', title='Other', status='ignored') for i in range(100)]
        page = self.client.get('/review').text
        self.assertEqual(page.count('data-offer-id='), 1)
        self.assertEqual(page.count('type="radio"'), 3)
        self.assertNotIn('checked', page)
        self.assertIn('value="option0" required disabled', page)
        self.assertIn('Termin minął', page)
        self.assertIn('Sala 2', page)
        self.assertIn('Do sprawdzenia <span>2</span>', page)
        self.assertEqual(len(self.client.get('/api/proposals').json()), 100)
        self.assertEqual(len(self.client.get('/api/proposal-groups').json()[0]['members']), 3)
        self.assertIn('data-offer-id=', self.client.get('/messages/m1').text)
    def test_selection_is_required_past_is_rejected_and_selection_closes_other_options(self):
        self.assertEqual(self.submit({'decision':'select'}).status_code, 400)
        self.assertEqual(self.submit({'decision':'select','proposal_id':'option0'}).status_code, 400)
        self.assertEqual(self.submit({'decision':'select','proposal_id':'option1'}).status_code, 303)
        self.assertNotIn('data-offer-id=', self.client.get('/review').text)
        page = self.client.get('/messages/m1').text
        self.assertIn('Wybrano termin', page)
        self.assertIn('Niewybrany — zamknięty', page)
        self.assertIn('Czeka na zapis', page)
        self.assertNotIn('type="radio"', page)
        self.assertNotIn('Dodaj wybrany termin', page)
    def test_selected_term_requiring_review_is_visible_and_actionable_beyond_legacy_limit(self):
        self.assertEqual(self.submit({'decision':'select','proposal_id':'option1'}).status_code, 303)
        self.store.group['members'][1]['status'] = 'review'
        self.store.list_proposals = lambda: [dict(id=f'noise{i}', message_id='m1', title='Other', status='ignored') for i in range(100)]
        status = self.store.status()
        self.store.status = lambda: {**status, 'proposals_review': 1}
        page = self.client.get('/review').text
        self.assertIn('Do sprawdzenia <span>1</span>', page)
        self.assertIn('Wybrany termin wymaga sprawdzenia', page)
        self.assertIn('action="/proposals/option1"', page)
        self.assertNotIn('action="/proposals/option0"', page)
        self.assertNotIn('action="/proposals/option2"', page)
        self.assertNotIn('Wszystko sprawdzone', page)
        self.assertNotIn('type="radio"', page)
        token = self.client.cookies['librus_csrf']
        result = self.client.post('/proposals/option1', data={'csrf':token, 'action':'approve'},
                                  headers={'origin':'http://testserver'}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.store.decisions, [('option1', 'approve', {})])

    def test_selected_pending_term_remains_closed_and_absent_from_review(self):
        self.assertEqual(self.submit({'decision':'select','proposal_id':'option1'}).status_code, 303)
        self.store.list_proposals = lambda: []
        status = self.store.status()
        self.store.status = lambda: {**status, 'proposals_review': 0}
        page = self.client.get('/review').text
        self.assertIn('Do sprawdzenia <span>0</span>', page)
        self.assertIn('Wszystko sprawdzone', page)
        self.assertNotIn('action="/proposals/option1"', page)
        self.assertNotIn('Wybrany termin wymaga sprawdzenia', page)

    def test_ambiguous_replacement_has_explanation_and_reject_but_no_approval(self):
        replacement = dict(id='replacement', message_id='m1', kind='create', title='Nowa propozycja',
                           status='review', start='2030-03-09T10:00:00+01:00', end=None,
                           description='Sala C', source_quote='Wybierz jeden termin', review_reason='',
                           blocked_by_group_decision='old-offer')
        self.store.list_proposals = lambda: [replacement]
        self.store.list_proposal_groups = lambda **kwargs: []
        page = self.client.get('/review').text
        self.assertIn('wymaga ręcznego sprawdzenia po wcześniejszej decyzji', page)
        self.assertIn('Sala C', page)
        self.assertIn('09.03.2030, 10:00', page)
        self.assertNotIn('value="approve"', page)
        self.assertNotIn('name="edit_dates"', page)
        self.assertIn('>Odrzuć propozycję</button>', page)
        token = self.client.cookies['librus_csrf']
        result = self.client.post('/proposals/replacement', data={'csrf':token, 'action':'reject'},
                                  headers={'origin':'http://testserver'}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.store.decisions, [('replacement', 'ignore', {})])

    def test_reject_whole_offer_requires_no_selection_and_returns_to_message(self):
        result = self.submit({'decision':'reject','return_to':'message'})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['location'], '/messages/m1?notice=resolved')
        page = self.client.get('/messages/m1').text
        self.assertIn('Odrzucono całą ofertę', page)
        self.assertNotIn('type="radio"', page)
    def test_group_decision_requires_origin_csrf_and_current_revision(self):
        endpoint = '/api/proposal-groups/offer1/decision'
        self.assertEqual(self.client.post(endpoint, data={'decision':'reject'}).status_code, 403)
        self.client.get('/')
        data = {'decision':'reject', 'csrf':self.client.cookies['librus_csrf'], 'expected_revision':'1'}
        self.assertEqual(self.client.post(endpoint,data=data,headers={'origin':'http://evil.test'}).status_code, 403)
        self.assertEqual(self.submit({'decision':'reject','expected_revision':'0'}).status_code, 400)
        self.assertEqual(self.submit({'decision':'reject','expected_revision':'bad'}).status_code, 400)
        self.assertEqual(self.store.group['status'], 'open')
    def test_no_eligible_dates_disables_add_but_keeps_whole_offer_rejection(self):
        for member in self.store.group['members']:
            member.update(eligible=False, ineligible_reason='past')
        page = self.client.get('/review').text
        self.assertIn('value="select" disabled', page)
        self.assertIn('Brak dostępnych terminów', page)
        self.assertIn('>Odrzuć całą ofertę</button>', page)
        self.assertEqual(self.submit({'decision':'reject'}).status_code, 303)

    def test_stale_offer_has_no_decision_controls(self):
        self.store.group['stale'] = True
        page = self.client.get('/review').text
        self.assertNotIn('Dodaj wybrany termin', page)
        self.assertNotIn('>Odrzuć całą ofertę</button>', page)
        self.assertIn('wymaga ponownej analizy', page)


if __name__ == '__main__':
    unittest.main()

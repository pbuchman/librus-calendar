from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.core import AppStore, WARSAW
from app.web import create_app
from test_core import NOW, message, proposal
from test_support import TEST_CONFIG


class ProposalInputs(HTMLParser):
    def __init__(self, endpoint, html):
        super().__init__()
        self.endpoint = endpoint
        self.active = False
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'form':
            self.active = attributes.get('action') == self.endpoint
        if self.active and tag == 'input' and 'name' in attributes:
            if attributes.get('type') != 'checkbox' or 'checked' in attributes:
                self.fields[attributes['name']] = attributes.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.active = False


class ReviewFormTests(unittest.TestCase):
    def prepare_form(self, **changes):
        directory = self.enterContext(TemporaryDirectory())
        store = self.enterContext(closing(AppStore(Path(directory) / 'state.sqlite3', config=TEST_CONFIG)))
        store.save_message(message())
        before, = store.save_proposals('1', [proposal(needs_review=True, **changes)], NOW)
        self.assertEqual(before['status'], 'review')
        client = self.enterContext(TestClient(
            create_app(store, lambda: None, allow_local=True, config=TEST_CONFIG, now=lambda: NOW),
            base_url='http://testserver', client=('127.0.0.1', 1234),
        ))
        page = client.get('/review')
        self.assertEqual(page.status_code, 200)
        endpoint = '/proposals/' + before['id']
        fields = ProposalInputs(endpoint, page.text).fields
        return store, client, before, endpoint, fields

    def submit(self, client, endpoint, fields):
        return client.post(endpoint, data={**fields, 'action': 'approve'},
                           headers={'origin': 'http://testserver'}, follow_redirects=False)

    def test_moves_exact_deadline_day_across_warsaw_offset_change(self):
        store, client, before, endpoint, fields = self.prepare_form(
            temporal_kind='deadline', all_day=True, start='2030-03-12',
            due_at='2030-03-12T16:30:00+01:00', description='Materiały do szkoły.',
        )
        response = self.submit(client, endpoint, {**fields, 'start': '2030-07-13'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(fields.get('due_time'), '16:30')
        saved, = store.list_proposals()
        self.assertEqual(saved['id'], before['id'])
        self.assertEqual(saved['due_at'], '2030-07-13T16:30:00+02:00')
        queued, = store.queue_operations(NOW)
        event = queued['event']
        self.assertEqual(event['start'], {'date': '2030-07-13'})
        self.assertEqual(event['end'], {'date': '2030-07-14'})
        self.assertEqual(event['summary'], '[Librus] Wycieczka — do 16:30')
        self.assertIn('Materiały do szkoły.', event['description'])
        self.assertIn('Termin: 2030-07-13 do 16:30 (Europe/Warsaw).', event['description'])
        self.assertNotIn('Termin: 2030-03-12', event['description'])

    def test_changes_exact_deadline_hour_and_replaces_generated_labels(self):
        store, client, _, endpoint, fields = self.prepare_form(
            temporal_kind='deadline', all_day=True, start='2030-03-12',
            due_at='2030-03-12T16:30:00+01:00',
        )
        response = self.submit(client, endpoint, {**fields, 'due_time': '17:45'})
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertEqual(saved['due_at'], '2030-03-12T17:45:00+01:00')
        queued, = store.queue_operations(NOW)
        event = queued['event']
        self.assertEqual(event['summary'], '[Librus] Wycieczka — do 17:45')
        self.assertIn('Termin: 2030-03-12 do 17:45 (Europe/Warsaw).', event['description'])
        self.assertNotIn('16:30', event['description'])
        self.assertEqual(event['start'], {'date': '2030-03-12'})
        self.assertEqual(event['end'], {'date': '2030-03-13'})

    def test_invalid_or_ambiguous_warsaw_deadline_hour_stays_in_review(self):
        for day in ('2030-03-31', '2030-10-27'):
            with self.subTest(day=day):
                store, client, before, endpoint, fields = self.prepare_form(
                    temporal_kind='deadline', all_day=True, start=day,
                    due_at=day + ('T16:30:00+02:00' if day[5:7] == '03' else 'T16:30:00+01:00'),
                )
                response = self.submit(client, endpoint, {**fields, 'due_time': '02:30'})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(store.list_proposals(), [before])
                self.assertEqual(store.queue_operations(NOW), [])

    def test_existing_deadline_in_repeated_hour_preserves_its_offset(self):
        store, client, before, endpoint, fields = self.prepare_form(
            temporal_kind='deadline', all_day=True, start='2030-10-27',
            due_at='2030-10-27T02:30:00+01:00',
        )
        self.assertEqual(fields.get('due_time'), '02:30')
        response = self.submit(client, endpoint, fields)
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertEqual(saved['due_at'], before['due_at'])
        self.assertFalse(saved['user_edited'])
        first_occurrence = datetime.fromisoformat('2030-10-27T02:45:00+02:00').astimezone(WARSAW)
        self.assertEqual(len(store.queue_operations(first_occurrence)), 1)

    def test_unchanged_rendered_form_preserves_default_duration_and_decision(self):
        store, client, before, endpoint, fields = self.prepare_form()
        self.assertTrue(before['default_duration'])
        self.assertEqual(fields['end'], '2030-11-10T10:00:00+01:00')
        response = self.submit(client, endpoint, fields)
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertTrue(saved['user_approved'])
        self.assertFalse(saved['user_edited'])
        self.assertTrue(saved['default_duration'])
        decision = store.get_action(saved['id'])['decision']['proposal']
        self.assertTrue(decision['user_approved'])
        self.assertFalse(decision['user_edited'])
        self.assertTrue(decision['default_duration'])
        queued, = store.queue_operations(NOW)
        self.assertIn('Czas trwania 60 minut przyjęty automatycznie', queued['event']['description'])

    def test_equivalent_title_and_times_are_not_manual_edits(self):
        store, client, _, endpoint, fields = self.prepare_form()
        response = self.submit(client, endpoint, {**fields, 'title': 'Wycieczka',
                               'start': '2030-11-10T08:00Z', 'end': '2030-11-10T09:00Z'})
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertFalse(saved['user_edited'])
        self.assertTrue(saved['default_duration'])

    def test_submitted_end_edit_replaces_default_and_counts_as_manual(self):
        store, client, _, endpoint, fields = self.prepare_form()
        response = self.submit(client, endpoint, {**fields, 'end': '2030-11-10T10:30:00+01:00'})
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertEqual(saved['end'], '2030-11-10T10:30:00+01:00')
        self.assertTrue(saved['user_edited'])
        self.assertFalse(saved['default_duration'])
        queued, = store.queue_operations(NOW)
        self.assertNotIn('Czas trwania 60 minut przyjęty automatycznie', queued['event']['description'])

    def test_missing_end_preserves_explicit_end_and_empty_end_clears_it(self):
        explicit_end = '2030-11-10T10:30:00+01:00'
        for clear in (False, True):
            with self.subTest(clear=clear):
                store, client, _, endpoint, fields = self.prepare_form(end=explicit_end)
                if clear:
                    fields['end'] = ''
                else:
                    del fields['end']
                response = self.submit(client, endpoint, fields)
                self.assertEqual(response.status_code, 303)
                saved, = store.list_proposals()
                self.assertEqual(saved['end'], '2030-11-10T10:00:00+01:00' if clear else explicit_end)
                self.assertEqual(saved['user_edited'], clear)
                self.assertEqual(saved['default_duration'], clear)
                queued, = store.queue_operations(NOW)
                self.assertEqual(queued['event']['end']['dateTime'], saved['end'])
                disclosed = 'Czas trwania 60 minut przyjęty automatycznie' in queued['event']['description']
                self.assertEqual(disclosed, clear)
                if clear:
                    self.assertNotIn('wiadomość nie podaje końca', queued['event']['description'])

    def test_moving_start_alone_recalculates_the_inferred_end(self):
        store, client, _, endpoint, fields = self.prepare_form()
        response = self.submit(client, endpoint, {**fields, 'start': '2030-11-10T11:15:00+01:00'})
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertTrue(saved['user_edited'])
        self.assertTrue(saved['default_duration'])
        self.assertEqual(saved['end'], '2030-11-10T12:15:00+01:00')

    def test_date_only_deadline_keeps_a_literal_title_suffix(self):
        store, client, before, endpoint, fields = self.prepare_form(
            title='Wycieczka — do 16:30', temporal_kind='deadline',
            all_day=True, start='2030-03-12',
        )
        response = self.submit(client, endpoint, fields)
        self.assertEqual(response.status_code, 303)
        saved, = store.list_proposals()
        self.assertEqual(saved['title'], before['title'])
        self.assertFalse(saved['user_edited'])
        self.assertIsNone(saved['due_at'])
        self.assertEqual(fields['due_time'], '')

"""Source incident evidence is classified through real collector failure paths."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from app import source_incidents
from app.config import AppConfig
from app.core import AppStore
from app.librus_client import DiscoveryParsingError, LibrusClient, MessageParsingError, PocError
from app.incidents import capture
from app.source_collectors import (Document, SourceCollectionError, _calendar_form, collect_sources,
                                   parse_announcements, parse_learner_context)
from test_source_collectors import CURRENT, FakeClient, announcement, calendar_page, detail, page, profile

PRIVATE = 'fictional-provider-value'


def response(payload=None, **overrides):
    defaults = dict(headers={}, is_redirect=False, status_code=200, json=lambda: payload,
                    raise_for_status=lambda: None, content=b'')
    return SimpleNamespace(**(defaults | overrides))


class SourceIncidentTests(unittest.TestCase):
    def setUp(self):
        self.events = []

    def incident(self, operation, reason):
        found = [event for event in self.events if event['operation'] == operation and event['reason'] == reason]
        self.assertEqual(len(found), 1, self.events)
        record = found[0]
        self.assertEqual(record['domain'], 'source')
        self.assertNotIn(PRIVATE, json.dumps(record))
        return record

    def fixture(self, event):
        fragment = source_incidents.fictional_fragment(
            event['operation'], event['reason'], event['evidence'])
        self.assertIsNotNone(fragment)
        self.assertNotIn(PRIVATE, json.dumps(fragment))
        return fragment

    def test_login_missing_redirect_identifies_the_expected_step(self):
        with patch('requests.Session') as session, capture(self.events.append):
            session.return_value.request.return_value = response()
            with self.assertRaisesRegex(PocError, 'expected OAuth redirect'):
                LibrusClient(PRIVATE, PRIVATE)
        event = self.incident('login_entry', 'login_redirect_missing')
        self.assertEqual(event['decision'], 'abort_login')
        self.assertEqual(event['evidence']['expected'], 'redirect_location')
        self.assertEqual(event['evidence']['actual'], 'missing')

    def test_login_transport_preserves_http_status_and_excludes_message(self):
        failure = requests.HTTPError(PRIVATE, response=SimpleNamespace(status_code=503))
        with patch('requests.Session') as session, capture(self.events.append):
            session.return_value.request.side_effect = failure
            with self.assertRaises(requests.HTTPError):
                LibrusClient(PRIVATE, PRIVATE)
        event = self.incident('login_entry', 'http_failure')
        self.assertEqual(event['evidence']['http_status'], 503)
        self.assertIsNone(source_incidents.fictional_fragment(event['operation'], event['reason'], event['evidence']))

    def test_login_invalid_oauth_target_preserves_type_error(self):
        redirect = response(headers={'Location': '/example-authorize'})
        oauth_result = response({'goTo': [PRIVATE]}, headers={'Content-Type': 'application/json; charset=' + PRIVATE})
        with patch('requests.Session') as session, capture(self.events.append):
            session.return_value.request.side_effect = [redirect, response(), oauth_result]
            with self.assertRaises(TypeError):
                LibrusClient(PRIVATE, PRIVATE)
        event = self.incident('login_submit', 'login_target_invalid')
        self.assertEqual(event['evidence']['expected'], 'oauth_target')
        self.assertEqual(event['evidence']['actual'], 'array')
        self.assertEqual(event['evidence']['method'], 'post')
        self.assertEqual(event['evidence']['http_status'], 200)
        self.assertEqual(event['evidence']['content_type'], 'json')

    def test_login_redirect_step_counts_the_observed_hop(self):
        responses = [
            response(headers={'Location': '/example-authorize'}, status_code=302),
            response(),
            response({'goTo': '/example-login'}),
            response(headers={'Location': 'https://synergia.librus.pl/' + PRIVATE}, status_code=302),
            response(headers={'Content-Type': 'text/html'}),
        ]
        with patch('requests.Session') as session, capture(self.events.append):
            session.return_value.request.side_effect = responses
            with self.assertRaisesRegex(PocError, 'Synergia OAuth session'):
                LibrusClient(PRIVATE, PRIVATE)
        event = self.incident('login_oauth_redirect', 'login_oauth_session_missing')
        self.assertEqual(event['evidence']['redirect_hop'], 1)
        self.assertEqual(event['evidence']['content_type'], 'html')

    def test_message_session_redirect_has_status_and_no_location(self):
        client = object.__new__(LibrusClient)
        redirect = response(headers={'Location': 'https://example.invalid/' + PRIVATE}, is_redirect=True, status_code=302)
        with patch.object(client, 'request', return_value=redirect):
            with capture(self.events.append), self.assertRaises(PocError):
                client.messages()
        event = self.incident('message_list', 'source_session_invalid')
        self.assertEqual(event['evidence']['http_status'], 302)
        self.assertEqual(event['evidence']['actual'], 'redirect_response')
        self.assertEqual(event['evidence']['redirect_category'], 'absolute_https')

    def test_message_list_type_drift_keeps_the_existing_discovery_failure(self):
        client = object.__new__(LibrusClient)
        drift = response({'data': {PRIVATE: PRIVATE}}, headers={'Content-Type': 'application/json'})
        with patch.object(client, 'request', return_value=drift):
            with capture(self.events.append), self.assertRaises(DiscoveryParsingError):
                client.messages()
        event = self.incident('message_list', 'message_page_shape')
        self.assertEqual(event['evidence']['expected'], 'array')
        self.assertEqual(event['evidence']['actual'], 'object')
        self.assertEqual(event['evidence']['method'], 'get')
        self.assertEqual(event['evidence']['http_status'], 200)
        self.assertEqual(event['evidence']['content_type'], 'json')
        self.assertEqual(event['evidence']['redirect_category'], 'no_location')
        self.assertEqual(event['evidence']['redirect_hop'], 0)
        fixture = self.fixture(event)
        self.assertEqual(fixture['format'], 'json')
        with patch.object(client, 'request', return_value=response(fixture['expected'])):
            self.assertEqual(client.messages(), ([], None))
        with patch.object(client, 'request', return_value=response(fixture['observed'])):
            with self.assertRaises(DiscoveryParsingError):
                client.messages()

    def test_body_identity_mismatch_uses_synthetic_aliases(self):
        client = object.__new__(LibrusClient)
        with patch.object(client, 'request', return_value=response({'data': {'messageId': PRIVATE}})):
            with capture(self.events.append), self.assertRaises(MessageParsingError):
                client.message(PRIVATE + '-requested')
        event = self.incident('message_body', 'message_identity_mismatch')
        self.assertEqual(event['evidence']['expected'], 'requested_identity')
        self.assertEqual(event['evidence']['actual'], 'other_identity')
        fixture = self.fixture(event)
        with patch.object(client, 'request', return_value=response(fixture['expected'])):
            self.assertEqual(client.message(fixture['message_id'])['text'], 'Example')
        with patch.object(client, 'request', return_value=response(fixture['observed'])):
            with self.assertRaisesRegex(MessageParsingError, 'identity mismatch'):
                client.message(fixture['message_id'])

    def test_bad_base64_excludes_the_raw_body(self):
        client = object.__new__(LibrusClient)
        with patch.object(client, 'request', return_value=response({'data': {'messageId': PRIVATE, 'Message': PRIVATE + '!'}})):
            with capture(self.events.append), self.assertRaises(MessageParsingError):
                client.message(PRIVATE)
        event = self.incident('message_body', 'message_body_encoding')
        self.assertEqual(event['evidence']['expected'], 'strict_base64')
        self.assertEqual(event['evidence']['actual'], 'invalid_base64')
        fixture = self.fixture(event)
        with patch.object(client, 'request', return_value=response(fixture['observed'])):
            with self.assertRaisesRegex(MessageParsingError, 'base64'):
                client.message(fixture['message_id'])

    def test_learner_diagnostic_exposes_roles_only(self):
        client = FakeClient()
        client.profiles = [profile(name=PRIVATE, class_name='', school=PRIVATE)]
        with capture(self.events.append):
            result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['errors'], ['learner_context_unverified'])
        event = self.incident('learner_profile', 'learner_context_unverified')
        self.assertEqual(event['evidence']['missing_fields'], ['learner_class'])
        fixture = self.fixture(event)
        self.assertEqual(fixture['format'], 'html')
        self.assertTrue(parse_learner_context(fixture['expected'])['selected_only'])
        with self.assertRaises(SourceCollectionError) as caught:
            parse_learner_context(fixture['observed'])
        self.assertEqual(caught.exception.code, 'learner_context_unverified')

    def test_announcement_unknown_field_cannot_appear_in_diagnostics(self):
        client = FakeClient()
        client.announcements = page(announcement(body=PRIVATE)).replace('Treść</th>', PRIVATE + '</th>')
        with capture(self.events.append):
            result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['complete_kinds'], ['school_event', 'substitution'])
        event = self.incident('announcement', 'announcement_markup_invalid')
        self.assertEqual(event['evidence']['missing_fields'], ['announcement_body'])
        fixture = self.fixture(event)
        self.assertEqual(fixture['format'], 'html')
        self.assertEqual(len(parse_announcements(fixture['expected'], learner_id='fixture', fetched_at='')), 1)
        with self.assertRaises(SourceCollectionError) as caught:
            parse_announcements(fixture['observed'], learner_id='fixture', fetched_at='')
        self.assertEqual(caught.exception.code, 'announcement_markup_invalid')

    def test_calendar_form_diff_uses_closed_structural_fragments(self):
        client = FakeClient()
        client.calendar = lambda: calendar_page().replace('scheduleForm', PRIVATE).replace('requestkey', PRIVATE)
        with capture(self.events.append):
            result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['errors'], ['calendar_form_invalid'])
        event = self.incident('calendar_initial', 'calendar_form_id')
        self.assertEqual(event['evidence']['expected'], 'calendar_form')
        form, = event['evidence']['structure']['forms']
        self.assertEqual(form['identity_category'], 'other_id')
        self.assertEqual(form['field_categories'], {'other': 1, 'month': 1, 'year': 1})
        fixture = self.fixture(event)
        self.assertEqual(fixture['format'], 'html')
        self.assertTrue(_calendar_form(Document(fixture['expected']))['hidden'])
        with self.assertRaises(SourceCollectionError) as caught:
            _calendar_form(Document(fixture['observed']))
        self.assertEqual(caught.exception.diagnostic_code, 'calendar_form_id')

    def test_calendar_detail_validation_is_distinct_from_month_scan(self):
        client = FakeClient()
        client.details['/terminarz/szczegoly/87001'] = detail().replace('Przedmiot', PRIVATE)
        with capture(self.events.append):
            result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['errors'], ['calendar_detail_markup_invalid'])
        event = self.incident('calendar_detail', 'calendar_detail_markup_invalid')
        self.assertEqual(event['evidence']['actual'], 'unknown_fields')

    def test_primary_select_and_restoration_failures_are_independent(self):
        client = FakeClient()
        original_request = client.request

        def fail_after_month_change(*args, **kwargs):
            original_request(*args, **kwargs)
            if client.month == 4:
                raise requests.Timeout(PRIVATE)
            raise requests.ConnectionError(PRIVATE)

        client.request = fail_after_month_change
        with capture(self.events.append):
            result = collect_sources(client, current=CURRENT)
        self.assertEqual(result['errors'], ['school_event_collection_failed'])
        self.assertEqual(result['complete_kinds'], ['announcement'])
        select = self.incident('calendar_select', 'timeout')
        restore = self.incident('calendar_restore', 'connection_failure')
        self.assertLess(self.events.index(select), self.events.index(restore))
        self.assertTrue(restore['evidence']['primary_failed'])

    def test_ingest_reports_binding_relation_before_any_mutation(self):
        with TemporaryDirectory() as directory:
            store = AppStore(Path(directory) / 'state.db', config=AppConfig(learner_id=PRIVATE))
            self.addCleanup(store.close)
            with capture(self.events.append), self.assertRaisesRegex(ValueError, 'could not be verified'):
                store.ingest_sources({'learner': {'id': PRIVATE + '-other'}, 'sources': []})
            self.assertEqual(store.list_sources(), [])
            self.assertFalse(store._setting('source_learner_verified'))
        event = self.incident('source_ingest', 'source_context_mismatch')
        self.assertEqual(event['evidence']['expected'], 'bound_learner')
        self.assertEqual(event['evidence']['actual'], 'other_learner')

    def test_ingest_sql_failure_preserves_rollback_and_omits_error_text(self):
        with TemporaryDirectory() as directory:
            store = AppStore(Path(directory) / 'state.db', config=AppConfig())
            self.addCleanup(store.close)
            with store.db:
                store.db.execute("CREATE TRIGGER reject_ingest BEFORE INSERT ON source_health "
                                 "BEGIN SELECT RAISE(ABORT, '" + PRIVATE + "'); END")
            with capture(self.events.append), self.assertRaises(sqlite3.IntegrityError):
                store.ingest_sources({'sources': [], 'learner': {'id': PRIVATE}})
            self.assertFalse(store._setting('source_learner_id'))
        event = self.incident('source_ingest', 'database_failure')
        self.assertEqual(event['decision'], 'reject_batch')

    def test_failing_sink_preserves_the_normal_client_error(self):
        client = object.__new__(LibrusClient)

        def fail_sink(event):
            raise RuntimeError(PRIVATE)

        with patch.object(client, 'request', return_value=response({'data': {}})):
            with self.assertLogs('app.incidents', level='WARNING') as logs:
                with capture(fail_sink), self.assertRaises(DiscoveryParsingError):
                    client.messages()
        self.assertNotIn(PRIVATE, str(logs.output))

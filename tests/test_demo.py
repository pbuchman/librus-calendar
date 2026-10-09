"""The public demo must remain deterministic and isolated from real accounts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from scripts.ui_fixture import CALENDAR_EVENTS, DEMO_LABEL, DEMO_NOW, make_demo_app, seed_store
from scripts.render_demo import calendar_illustration


class DemoTests(unittest.TestCase):
    def test_fixture_never_loads_home_config_or_initializes_real_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'LIBRUS_CONFIG_FILE': '/missing/private-config.json',
                                          'LIBRUS_CALENDAR_ID': 'must-not-use@example.test'}), \
                 patch('pathlib.Path.home', side_effect=AssertionError('Home access forbidden')), \
                 patch('app.config.load_config', side_effect=AssertionError('Config discovery forbidden')), \
                 patch('app.codex_runtime.CodexRuntime', side_effect=AssertionError('No runtime allowed')), \
                 patch('app.librus_client.LibrusClient', side_effect=AssertionError('No Librus allowed')), \
                 patch('requests.sessions.Session.request', side_effect=AssertionError('No external requests')):
                store = seed_store(Path(directory)/'state.sqlite3')
                try:
                    client = TestClient(make_demo_app(store), client=('127.0.0.1', 50000))
                    page = client.get('/')
                    self.assertEqual(page.status_code, 200)
                    self.assertIn(DEMO_LABEL, page.text)
                    self.assertIn('calendar@example.test', page.text)
                    self.assertIn('06.03.2030, 11:00', page.text)
                    self.assertNotIn('must-not-use', page.text)
                    self.assertEqual(store.status()['events'], len(CALENDAR_EVENTS))
                    self.assertEqual(store.status()['proposals_review'], 1)
                    csrf = client.cookies.get('librus_csrf')
                    result = client.post('/sync', data={'csrf': csrf}, headers={'origin': 'http://testserver'})
                    self.assertEqual(result.status_code, 200)
                    self.assertIn('Zlecono sprawdzenie', result.text)
                    self.assertEqual(len(store.list_events()), 3)
                finally:
                    store.close()

    def test_synthetic_snapshots_repeat_and_unapproved_activity_is_absent(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = seed_store(Path(one)/'state.sqlite3'), seed_store(Path(two)/'state.sqlite3')
            try:
                self.assertEqual(first.list_messages(), second.list_messages())
                self.assertEqual(first.list_proposals(), second.list_proposals())
                self.assertEqual(first.list_events(), second.list_events())
                self.assertEqual(first.status(), second.status())
                for event in first.list_events():
                    self.assertNotEqual(event['message_id'], 'demo-robotics')
                    self.assertIn('Synchronizacja rozpoczęta: 06.03.2030 10:15:00', event['snapshot']['description'])
                extra = next(p for p in first.list_proposals() if p['message_id'] == 'demo-robotics')
                self.assertEqual(extra['status'], 'review')
                self.assertFalse(extra['user_approved'])
            finally:
                first.close()
                second.close()

    def test_calendar_is_explicit_illustration_from_same_fixture(self):
        illustration = calendar_illustration()
        self.assertIn(DEMO_LABEL, illustration)
        self.assertIn('Not an app calendar feature or a Google Calendar screenshot.', illustration)
        for event in CALENDAR_EVENTS:
            self.assertIn(event['title'], illustration)
        self.assertNotIn('[Librus] Dodatkowe warsztaty robotyki', illustration)
        self.assertEqual(DEMO_NOW.year, 2030)


if __name__ == '__main__':
    unittest.main()

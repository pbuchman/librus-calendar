"""Storage/migration safety retained after replacing the per-source planner."""
from datetime import timedelta
import json
import unittest
import test_agenda as fixtures
from app.core import AppStore
from app.source_admin import cleanup_existing
from test_support import TEST_CONFIG


class SourceStoreContracts(unittest.TestCase):
    setUp = fixtures.AgendaTests.setUp
    tearDown = fixtures.AgendaTests.tearDown

    def batch(self, sources, **changes):
        result = {'learner': fixtures.LEARNER, 'sources': sources, 'complete_kinds': [], 'status': 'ok'}
        result.update(changes)
        return result

    def test_fetch_clock_and_window_do_not_change_content_version(self):
        original = next(source for source in self.store.list_sources() if source['kind'] == 'school_event')
        source = dict(original, metadata={'fetched_at': 'later', 'page': 8, 'window_start': '2030-03-01', 'window_end': '2030-05-01'})
        self.store.ingest_sources(self.batch([source]), fixtures.NOW + timedelta(hours=1))
        self.assertEqual(original['content_hash'], self.store.get_source(source['id'])['content_hash'])

    def test_learner_pin_cannot_switch_without_mutating_sources(self):
        before = self.store.list_sources()
        with self.assertRaises(ValueError):
            self.store.ingest_sources(self.batch([], learner={'id': 'other-fictional-child', 'class_id': '1x'}), fixtures.NOW)
        self.assertEqual(before, self.store.list_sources())

    def test_incomplete_or_outside_calendar_window_absence_never_deletes(self):
        self.store.ingest_sources(self.batch([]), fixtures.NOW)
        self.assertFalse(any(source['deleted'] for source in self.store.list_sources()))
        self.store.ingest_sources(self.batch([], complete_kinds=['school_event'], window_start='2030-04-01', window_end='2030-04-30'), fixtures.NOW)
        self.assertFalse(any(source['deleted'] for source in self.store.list_sources()))

    def test_complete_announcement_absence_does_not_delete_calendar(self):
        event = fixtures.AgendaTests.inventory(self)
        self.store.ingest_sources(self.batch([], complete_kinds=['announcement']), fixtures.NOW)
        self.assertTrue(next(source for source in self.store.list_sources() if source['kind'] == 'announcement')['deleted'])
        self.assertEqual(self.store.db.execute('SELECT id FROM events').fetchone()[0], event['id'])

    def test_expired_source_text_is_not_rehydrated_by_unchanged_ingestion(self):
        original = next(source for source in self.store.list_sources() if source['kind'] == 'announcement')
        with self.store.db:
            self.store.prune_source_content(fixtures.NOW + timedelta(days=31), apply=True)
        self.store.ingest_sources(self.batch([original]), fixtures.NOW + timedelta(days=32))
        retained = self.store.get_source(original['id'])
        self.assertEqual(retained['text'], '')
        self.assertEqual(retained['status'], 'retained')
        self.assertEqual(retained['content_hash'], original['content_hash'])

    def test_legacy_event_id_and_human_inventory_survive_repeated_initialization(self):
        event = fixtures.AgendaTests.inventory(self)
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM events')]
        self.store.close()
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.assertEqual(before, [tuple(row) for row in self.store.db.execute('SELECT * FROM events')])
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertEqual(self.store.list_actions()[0]['id'], 'existing-action')
        self.assertEqual(self.store.list_actions()[0]['event_ids'], [event['id']])

    def test_message_subject_change_versions_mirrored_source(self):
        before = self.store.get_source('message:fictional-message')
        message = self.store.get_message('fictional-message')
        self.store.save_message(dict(message, subject='Fikcyjna nowa wersja tytułu'))
        after = self.store.get_source(before['id'])
        self.assertNotEqual(before['content_hash'], after['content_hash'])
        self.assertEqual(before['id'], after['id'])

    def test_cleanup_stale_decision_cannot_retire_approved_intent(self):
        raw = dict(fixtures.action(), kind='create', confidence='low', needs_review=True,
                   source_quote='Klasa 1x przynosi papier do 12 marca.', review_reason='Do decyzji.')
        proposal = self.store.save_proposals('fictional-message', [raw], fixtures.NOW)[0]
        path = self.path.parent / 'fictional-cleanup.json'
        cleanup_existing(self.store, plan_path=path, request={'changes': [{'proposal_id': proposal['id'], 'reason': 'Fikcyjna korekta.'}]})
        self.store.resolve_proposal(proposal['id'], 'ignore')
        with self.assertRaisesRegex(ValueError, 'changed'):
            cleanup_existing(self.store, plan_path=path, apply=True)
        self.assertEqual(self.store.list_events(), [])

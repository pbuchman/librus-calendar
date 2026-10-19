"""Observed audience verification and learner pinning remain collector contracts."""
from copy import deepcopy
import unittest
import test_agenda as fixtures
from app.source_audience import AUDIENCE_KEY, mint_calendar_audience, valid_calendar_audience


class ClassAudienceTests(unittest.TestCase):
    setUp = fixtures.AgendaTests.setUp
    tearDown = fixtures.AgendaTests.tearDown

    def observed(self):
        source = {'kind': 'school_event', 'upstream_id': 'fictional-served-class', 'learner_id': fixtures.LEARNER['id'],
                  'title': 'Fikcyjne zadanie klasy', 'sender': 'Fikcyjna szkoła', 'published_at': '2030-03-01',
                  'updated_at': None, 'text': 'Przynieść papier do 12 marca.', 'url': '', 'deleted': False,
                  'metadata': {'event_start': '2030-03-12'}}
        source['metadata'][AUDIENCE_KEY] = mint_calendar_audience(source, fixtures.LEARNER)
        return source

    def ingest(self, source):
        return self.store.ingest_sources({'learner': fixtures.LEARNER, 'sources': [source],
                                         'complete_kinds': [], 'status': 'ok'}, fixtures.NOW)

    def test_valid_observed_class_audience_is_preserved_without_invented_quote(self):
        source = self.observed()
        self.assertTrue(valid_calendar_audience(source, fixtures.LEARNER))
        self.ingest(source)
        saved = next(row for row in self.store.list_sources() if row['upstream_id'] == source['upstream_id'])
        self.assertEqual(saved['metadata'][AUDIENCE_KEY], source['metadata'][AUDIENCE_KEY])
        self.assertEqual(saved['text'], source['text'])

    def test_context_hash_version_shape_and_kind_forgeries_are_rejected_atomically(self):
        original = self.observed()
        for mutate in (lambda row: row.update(text='Changed body'),
                       lambda row: row.update(learner_id='another-fictional-child'),
                       lambda row: row.update(kind='announcement'),
                       lambda row: row['metadata'][AUDIENCE_KEY].update(class_id='2y'),
                       lambda row: row['metadata'][AUDIENCE_KEY].update(version='forged'),
                       lambda row: row['metadata'][AUDIENCE_KEY].update(extra=True)):
            row = deepcopy(original); mutate(row)
            self.assertFalse(valid_calendar_audience(row, fixtures.LEARNER))
            with self.assertRaises(ValueError):
                self.ingest(row)
        self.assertEqual(len(self.store.list_sources()), 3)

    def test_visibility_never_establishes_different_learner_or_class(self):
        source = self.observed()
        self.assertFalse(valid_calendar_audience(source, dict(fixtures.LEARNER, class_id='2y')))
        self.assertFalse(valid_calendar_audience(source, dict(fixtures.LEARNER, id='different-child')))
        self.assertFalse(valid_calendar_audience(source, dict(fixtures.LEARNER, verified=False)))
        source['status'] = 'retained'
        self.assertFalse(valid_calendar_audience(source, fixtures.LEARNER))

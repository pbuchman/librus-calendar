"""A legacy cached-subset API cannot bypass the complete agenda snapshot."""
import tempfile
from pathlib import Path
import unittest
from app.core import AppStore
from app.sync import SyncController, reanalyze_cached
from test_support import TEST_CONFIG


class ReanalysisTests(unittest.TestCase):
    def test_legacy_subset_preview_and_apply_refuse_without_runtime_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory) / 'state.sqlite3', config=TEST_CONFIG)
            try:
                store.save_message({'id': 'fictional-message', 'text': 'Fikcyjna wiadomość.'})
                before = {table: [tuple(row) for row in store.db.execute('SELECT * FROM ' + table)]
                          for table in ('messages', 'proposals', 'operations', 'events', 'decision_intents', 'settings')}
                controller = SyncController(store, runtime_factory=lambda: self.fail('No provider for retired path'))
                for apply in (False, True):
                    self.assertEqual(controller.reanalyze(['fictional-message'], apply=apply)['status'], 'refused')
                    self.assertEqual(reanalyze_cached(store.path, ['fictional-message'], apply=apply,
                        runtime_factory=lambda: self.fail('No provider'), config=TEST_CONFIG)['status'], 'refused')
                self.assertEqual(before, {table: [tuple(row) for row in store.db.execute('SELECT * FROM ' + table)] for table in before})
            finally:
                store.close()

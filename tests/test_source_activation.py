"""Failed activation remains non-executable across process restart."""
from dataclasses import replace
import json
import sqlite3
import unittest

from app.core import AppStore
from app.source_admin import activate_sources
from app.source_planner import SourcePlanner
from app.sync import SyncController
import test_source_planner as fixtures


class ActivationAttemptTests(unittest.TestCase):
    setUp=fixtures.SourcesTests.setUp
    tearDown=fixtures.SourcesTests.tearDown
    ingest=fixtures.SourcesTests.ingest

    def prepare(self):
        self.rows=[fixtures.source('reactivation-a'),fixtures.source('reactivation-b')]
        for row in self.rows:
            src=self.ingest(row,mode='preview')
            SourcePlanner(self.store,fixtures.Runtime()).analyze(src,fixtures.NOW,preview=True)
        self.plan=self.path.parent/'first-activation.json'
        activate_sources(self.store,fixtures.Runtime(),plan_path=self.plan,current=fixtures.NOW)
        self.store.config=replace(self.store.config,multi_source_mode='active')

    def factory(self):
        owner=self
        class Client:
            def collect_sources(self,**kwargs):
                # The marker is committed before even creating a fresh client.
                with sqlite3.connect('file:'+str(owner.path)+'?mode=ro',uri=True)as db:
                    marker=db.execute("SELECT value FROM settings WHERE key='source_activation_incomplete'").fetchone()
                    owner.assertTrue(marker and marker[0])
                return {'sources':owner.rows,'learner':fixtures.LEARNER,'complete_kinds':fixtures.KINDS,'status':'ok'}
        return Client()

    def fail_second(self):
        class FailSecond(fixtures.Runtime):
            def analyze_source(self,src,context):
                if src['upstream_id']=='reactivation-b':raise RuntimeError('Fictional interruption')
                return super().analyze_source(src,context)
        with self.assertRaises(RuntimeError):
            activate_sources(self.store,FailSecond(),plan_path=self.plan,apply=True,
                current=fixtures.NOW,client_factory=self.factory)

    def reopen(self):
        config=self.store.config;self.store.close();self.store=AppStore(self.path,config=config)

    def test_partial_activation_restart_defers_queue_and_plan_does_not_clear(self):
        self.prepare();self.fail_second();marker=self.store._setting('source_activation_incomplete')
        self.assertTrue(marker);self.assertEqual(self.store._setting('source_activation_at'),'synthetic')
        self.assertEqual(len(self.store.list_proposals()),1)
        self.reopen();before=self.store.source_decision_snapshot()
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(self.store.source_decision_snapshot(),before)
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        retry=self.path.parent/'retry.json'
        activate_sources(self.store,fixtures.Runtime(),plan_path=retry,current=fixtures.NOW)
        self.assertEqual(self.store._setting('source_activation_incomplete'),marker)
        self.assertEqual(len(json.loads(retry.read_text())['sources']),2)

    def test_fresh_retry_reanalyzes_exact_failed_selection_then_clears_atomically(self):
        self.prepare();self.fail_second();self.reopen()
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        retry=self.path.parent/'retry.json';activate_sources(self.store,fixtures.Runtime(),plan_path=retry,current=fixtures.NOW)
        self.store.config=replace(self.store.config,multi_source_mode='active');runtime=fixtures.Runtime()
        result=activate_sources(self.store,runtime,plan_path=retry,apply=True,current=fixtures.NOW,client_factory=self.factory)
        self.assertEqual(result['sources'],2)
        self.assertEqual(sum(call[0]=='analyze'for call in runtime.calls),2)
        self.assertFalse(self.store._setting('source_activation_incomplete',''))
        self.assertEqual(self.store._setting('source_activation_plan'),result['plan_fingerprint'])
        self.assertEqual(len(self.store.queue_operations(fixtures.NOW)),1)

    def test_pending_claim_is_deferred_without_rewriting_operation_or_intent(self):
        src=self.ingest(fixtures.source('existing'))
        SourcePlanner(self.store,fixtures.Runtime()).analyze(src,fixtures.NOW)
        queued=self.store.queue_operations(fixtures.NOW);self.assertEqual(len(queued),1)
        existing=fixtures.source('existing');self.prepare();self.rows.append(existing);self.fail_second();self.reopen()
        self.store.set_writes_paused(False)
        before=[tuple(row)for row in self.store.db.execute('SELECT * FROM operations')]
        self.assertFalse(self.store.claim_operation(queued[0]['operation_id']))
        self.assertEqual(before,[tuple(row)for row in self.store.db.execute('SELECT * FROM operations')])

    def test_failed_receipt_transaction_keeps_old_receipt_and_incomplete_guard(self):
        self.prepare()
        keys=('source_activation_at','source_activation_learner','source_activation_binding','source_activation_plan')
        before={key:self.store._setting(key)for key in keys}
        with self.store.db:
            self.store.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON settings WHEN NEW.key='source_activation_plan' BEGIN SELECT RAISE(ABORT,'fictional receipt failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            activate_sources(self.store,fixtures.Runtime(),plan_path=self.plan,apply=True,current=fixtures.NOW,client_factory=self.factory)
        self.assertEqual(before,{key:self.store._setting(key)for key in keys})
        self.assertTrue(self.store._setting('source_activation_incomplete'))
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_incomplete_active_collection_does_not_collect_or_analyze(self):
        self.prepare();self.fail_second()
        class ForbiddenClient:
            def collect_sources(self,**kwargs):raise AssertionError('Incomplete activation must defer scheduled collection')
        result=SyncController(self.store,ForbiddenClient(),fixtures.Runtime())._source_collection(fixtures.NOW)
        self.assertTrue(result['activation_incomplete']);self.assertEqual(result['analyzed'],0)


if __name__=='__main__':unittest.main()

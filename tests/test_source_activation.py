"""Failed activation remains non-executable across process restart."""
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from app.core import AppStore
from app.source_admin import activate_sources
from app.source_planner import SourcePlanner
from app.health_state import read_connection_state
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
        self.assertEqual(self.store._setting('source_activation_class'),fixtures.LEARNER['class_id'])
        self.assertEqual(len(self.store.queue_operations(fixtures.NOW)),1)

    def test_planner_upgrade_refreshes_only_exact_incomplete_selection_and_retry_completes(self):
        ordinary_row=fixtures.source('ordinary-analyzed')
        ordinary=self.ingest(ordinary_row)
        SourcePlanner(self.store,fixtures.Runtime()).analyze(ordinary,fixtures.NOW)
        operation=self.store.queue_operations(fixtures.NOW)[0]
        with self.store.db:
            self.store.db.execute("UPDATE operations SET status='unknown',attempts=1 WHERE id=?",(operation['operation_id'],))
        self.prepare();self.rows.append(ordinary_row);self.fail_second();self.reopen()
        marker=self.store._setting('source_activation_incomplete')
        selected={item['id'] for item in json.loads(marker)['sources']}
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        self.store.set_setting('multi_source_mode','preview')
        with self.store.db:
            self.store.db.execute("UPDATE source_previews SET payload='{}'")
        before={table:[tuple(row)for row in self.store.db.execute('SELECT * FROM '+table+' ORDER BY 1')]
                for table in ('proposals','operations','events','decision_intents','canonical_actions','action_sources')}
        self.assertEqual({item['id']for item in self.store.pending_sources(preview=True)},selected)
        self.assertEqual(self.store.source_status()['preview_pending'],2)
        self.assertEqual(read_connection_state(self.store.db,fixtures.NOW)['sources']['kinds']['announcement']['pending'],2)
        activated=next(item for item in self.store.pending_sources(preview=True)if item['status']=='analyzed')
        self.store.fail_source(activated['id'],preview=True)
        self.assertEqual({item['id']for item in self.store.pending_sources(preview=True)},selected)
        for item in self.store.pending_sources(preview=True):
            SourcePlanner(self.store,fixtures.Runtime()).analyze(item,fixtures.NOW,preview=True)
        self.assertEqual(self.store.pending_sources(preview=True),[])
        self.assertEqual(before,{table:[tuple(row)for row in self.store.db.execute('SELECT * FROM '+table+' ORDER BY 1')]
                                 for table in before})
        self.assertEqual(self.store._setting('source_activation_incomplete'),marker)
        retry=self.path.parent/'version-retry.json'
        activate_sources(self.store,fixtures.Runtime(),plan_path=retry,current=fixtures.NOW)
        self.assertEqual({item['id']for item in json.loads(retry.read_text())['sources']},selected)
        self.store.config=replace(self.store.config,multi_source_mode='active')
        result=activate_sources(self.store,fixtures.Runtime(),plan_path=retry,apply=True,current=fixtures.NOW,client_factory=self.factory)
        self.assertEqual(result['sources'],2)
        self.assertFalse(self.store._setting('source_activation_incomplete',''))

    def test_retry_refresh_requires_valid_attempt_current_hash_context_and_existing_receipt(self):
        self.prepare();self.fail_second()
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        marker=self.store._setting('source_activation_incomplete')
        attempt=json.loads(marker)
        analyzed=next(item for item in self.store.list_sources()if item['status']=='analyzed')
        with self.store.db:self.store.db.execute("UPDATE source_previews SET payload='{}'")
        def queued_analyzed():
            return [item for item in self.store.pending_sources(preview=True)if item['status']=='analyzed']
        self.assertEqual(len(queued_analyzed()),1)
        for value in ('not json','[]',json.dumps({'token':'x','plan':'x','sources':[]}),
                      json.dumps(dict(attempt,sources=attempt['sources']+[attempt['sources'][0]])),
                      json.dumps(dict(attempt,sources=[dict(item,hash='changed')for item in attempt['sources']]))):
            with self.subTest(marker=value):
                self.store.set_setting('source_activation_incomplete',value)
                self.assertEqual(queued_analyzed(),[])
        self.store.set_setting('source_activation_incomplete',marker)
        self.store.set_setting('source_learner_verified','false')
        self.assertEqual(queued_analyzed(),[])
        self.store.set_setting('source_learner_verified','true')
        self.store.set_setting('source_learner_context',json.dumps(dict(fixtures.LEARNER,id='different-learner')))
        self.assertEqual(queued_analyzed(),[])
        self.store.set_setting('source_learner_context',json.dumps(dict(fixtures.LEARNER,class_id=['not-a-class-id'])))
        self.assertEqual(queued_analyzed(),[])
        self.store.set_setting('source_learner_context',json.dumps(fixtures.LEARNER))
        with self.store.db:self.store.db.execute('DELETE FROM source_previews WHERE source_id=?',(analyzed['id'],))
        self.assertEqual(queued_analyzed(),[])

    def test_changed_current_ready_version_cannot_replace_failed_selection_hash(self):
        self.prepare();self.fail_second()
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        selected=json.loads(self.store._setting('source_activation_incomplete'))['sources'][0]
        with self.store.db:
            self.store.db.execute('UPDATE sources SET content_hash=? WHERE id=?',('changed-current-version',selected['id']))
        self.store.save_source_preview(selected['id'],[])
        with self.assertRaisesRegex(ValueError,'incomplete activation source'):
            activate_sources(self.store,fixtures.Runtime(),plan_path=self.path.parent/'changed-retry.json',current=fixtures.NOW)

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
        keys=('source_activation_at','source_activation_learner','source_activation_class','source_activation_binding','source_activation_plan')
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

    def fail_after_final_analysis_context_change(self):
        self.store.set_setting('source_activation_class',fixtures.LEARNER['class_id'])
        self.prepare();original=SourcePlanner.analyze
        keys=('source_activation_at','source_activation_learner','source_activation_class','source_activation_binding','source_activation_plan')
        before={key:self.store._setting(key)for key in keys}
        def changed(planner,source,*args,**kwargs):
            result=original(planner,source,*args,**kwargs)
            if source['upstream_id']=='reactivation-b':
                self.store.set_setting('source_learner_context',json.dumps(dict(fixtures.LEARNER,class_id='2y')))
            return result
        with patch.object(SourcePlanner,'analyze',new=changed),self.assertRaises(ValueError):
            activate_sources(self.store,fixtures.Runtime(),plan_path=self.plan,apply=True,current=fixtures.NOW,client_factory=self.factory)
        self.assertEqual(before,{key:self.store._setting(key)for key in keys})
        self.assertTrue(self.store._setting('source_activation_incomplete'))

    def test_context_change_after_final_real_analysis_cannot_bless_new_class(self):
        self.fail_after_final_analysis_context_change()
        import test_source_audience as audience
        context=dict(fixtures.LEARNER,class_id='2y');row=audience.observed('fresh-new-class',context=context)
        self.store.ingest_sources({'sources':[row],'learner':context,'complete_kinds':fixtures.KINDS,'status':'ok'},fixtures.NOW)
        from app.source_store import source_identity
        source=self.store.get_source(source_identity(row['kind'],row['upstream_id'],row['learner_id']))
        raw=audience.school_action();raw['recipient']['class_id']='2y'
        SourcePlanner(self.store,fixtures.Runtime([raw],decision='distinct')).analyze(source,fixtures.NOW)
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(self.store._setting('source_activation_class'),'1x')

    def test_context_race_keeps_complete_selection_retryable_under_original_class(self):
        self.fail_after_final_analysis_context_change();self.reopen()
        self.store.set_setting('source_learner_context',json.dumps(fixtures.LEARNER))
        self.store.config=replace(self.store.config,multi_source_mode='preview')
        retry=self.path.parent/'context-retry.json'
        activate_sources(self.store,fixtures.Runtime(),plan_path=retry,current=fixtures.NOW)
        self.assertEqual(len(json.loads(retry.read_text())['sources']),2)
        self.store.config=replace(self.store.config,multi_source_mode='active')
        result=activate_sources(self.store,fixtures.Runtime(),plan_path=retry,apply=True,current=fixtures.NOW,client_factory=self.factory)
        self.assertEqual(result['sources'],2);self.assertFalse(self.store._setting('source_activation_incomplete',''))
        self.assertEqual(self.store._setting('source_activation_class'),'1x')
        self.assertEqual(len(self.store.queue_operations(fixtures.NOW)),1)


if __name__=='__main__':unittest.main()

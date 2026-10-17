"""Served class audience is observed context, never invented action evidence."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from app.source_audience import AUDIENCE_KEY,mint_calendar_audience,valid_calendar_audience
from app.source_planner import SourcePlanner,prepare_source_proposals
import test_source_planner as fixtures

BODY=fixtures.TEXT.removeprefix('Klasa 1x ')


def observed(upstream='served-school',context=fixtures.LEARNER,**changes):
    source=fixtures.source(upstream,kind='school_event',text=BODY,metadata={'event_start':'2030-03-12'})
    source.update(changes)
    source['metadata'][AUDIENCE_KEY]=mint_calendar_audience(source,context)
    return source


def school_action(**changes):
    raw=fixtures.action(source_quote=BODY,optional=False,
        recipient={'scope':'class','class_id':'1x','learner_id':None,'condition':'none','evidence':''},
        action_identity={'action_type':'submit','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':BODY})
    raw.update(changes);return raw


class ClassAudienceTests(unittest.TestCase):
    setUp=fixtures.SourcesTests.setUp
    tearDown=fixtures.SourcesTests.tearDown
    ingest=fixtures.SourcesTests.ingest

    def authorize_class(self):self.store.set_setting('source_activation_class',fixtures.LEARNER['class_id'])

    def test_valid_audience_without_class_quotation_adds_one_action_and_replays_no_work(self):
        self.authorize_class();src=self.ingest(observed());runtime=fixtures.Runtime([school_action()])
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        self.assertEqual(result['outcomes'][0]['outcome'],'added')
        proposal=self.store.list_proposals()[0];self.assertEqual(proposal['status'],'pending')
        self.assertEqual(proposal['recipient']['evidence'],'');self.assertEqual(proposal['source_quote'],BODY)
        self.assertEqual(proposal['recipient_basis']['kind'],AUDIENCE_KEY)
        self.assertEqual(proposal['recipient_basis']['source_hash'],src['content_hash'])
        self.assertEqual(len(self.store.queue_operations(fixtures.NOW)),1)
        unchanged=self.ingest(observed());self.assertEqual(unchanged['content_hash'],src['content_hash'])
        self.assertNotIn(src['id'],[x['id']for x in self.store.pending_sources()])
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_only_exact_class_condition_none_ordinary_school_can_use_view(self):
        src=self.ingest(observed())
        invalid=[{'recipient':{'scope':scope,'class_id':'1x','learner_id':fixtures.LEARNER['id'],'condition':'none','evidence':''}}
                 for scope in ('all','learner','unknown')]
        invalid += [{'recipient':{'scope':'class','class_id':'1x','learner_id':None,'condition':condition,'evidence':''}}
                    for condition in ('unknown','participation','role')]
        invalid += [{'activity_scope':'extracurricular'},
                    {'recipient':{'scope':'class','class_id':'2y','learner_id':None,'condition':'none','evidence':''}},
                    {'recipient':{'scope':'class','class_id':'1x','learner_id':'another-learner','condition':'none','evidence':''}},
                    {'source_quote':'invented evidence'},{'date_certainty':'uncertain'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                item=prepare_source_proposals(src,[school_action(**changes)],fixtures.LEARNER,fixtures.NOW)[0]
                self.assertNotEqual(item['status'],'pending');self.assertNotIn('recipient_basis',item)

    def test_model_basis_is_ignored_and_cannot_replace_missing_observation(self):
        src=self.ingest(fixtures.source(kind='school_event',text=BODY))
        item=prepare_source_proposals(src,[school_action(recipient_basis={'kind':AUDIENCE_KEY,'proof':'invented'})],fixtures.LEARNER,fixtures.NOW)[0]
        self.assertEqual(item['status'],'review');self.assertNotIn('recipient_basis',item)
        valid=self.ingest(observed())
        item=prepare_source_proposals(valid,[school_action(recipient_basis={'kind':'forged'})],fixtures.LEARNER,fixtures.NOW)[0]
        self.assertEqual(item['recipient_basis']['proof'],valid['metadata'][AUDIENCE_KEY])

    def test_source_hash_context_version_shape_and_kind_forgeries_are_refused(self):
        original=observed()
        variants=[]
        for key in ('source_sha256','view_sha256','version','class_id','learner_id'):
            row=deepcopy(original);row['metadata'][AUDIENCE_KEY][key]='wrong';variants.append(row)
        row=deepcopy(original);row['text']+=' Changed fact.';variants.append(row)
        row=deepcopy(original);row['kind']='announcement';variants.append(row)
        row=deepcopy(original);row['metadata'][AUDIENCE_KEY]['extra']=True;variants.append(row)
        for row in variants:
            with self.subTest(row=row['kind']):
                self.assertFalse(valid_calendar_audience(row,fixtures.LEARNER))
                with self.assertRaises(ValueError):self.ingest(row)
        self.assertEqual(self.store.list_sources(),[])

    def test_context_change_during_extraction_invalidates_preview_and_active(self):
        src=self.ingest(observed());case=self
        class Switching(fixtures.Runtime):
            def analyze_source(inner,*args):
                result=super().analyze_source(*args)
                case.store.set_setting('source_learner_context',json.dumps(dict(fixtures.LEARNER,class_id='2y')))
                return result
        for preview in (True,False):
            self.store.set_setting('source_learner_context',json.dumps(fixtures.LEARNER))
            with self.assertRaises(ValueError):SourcePlanner(self.store,Switching([school_action()])).analyze(src,fixtures.NOW,preview=preview)
        self.assertEqual(self.store.list_proposals(),[]);self.assertEqual(self.store.list_source_previews(),[])

    def test_context_and_proof_change_defer_existing_frozen_operation_without_rewriting(self):
        self.authorize_class();src=self.ingest(observed());SourcePlanner(self.store,fixtures.Runtime([school_action()])).analyze(src,fixtures.NOW)
        operation=self.store.queue_operations(fixtures.NOW)[0];self.store.set_writes_paused(False)
        protected=lambda:{table:[tuple(r)for r in self.store.db.execute('SELECT * FROM '+table)]for table in ('proposals','canonical_actions','decision_intents','operations')}
        before=protected();self.store.set_setting('source_learner_context',json.dumps(dict(fixtures.LEARNER,class_id='2y')))
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[]);self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.assertEqual(before,protected())

    def test_missing_class_receipt_cannot_authorize_audience_work(self):
        src=self.ingest(observed());SourcePlanner(self.store,fixtures.Runtime([school_action()])).analyze(src,fixtures.NOW)
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(self.store.list_proposals()[0]['status'],'pending')

    def test_removed_proof_defers_queued_intent_without_operation_rewrite(self):
        self.authorize_class();row=observed();src=self.ingest(row)
        SourcePlanner(self.store,fixtures.Runtime([school_action()])).analyze(src,fixtures.NOW)
        operation=self.store.queue_operations(fixtures.NOW)[0];self.store.set_writes_paused(False)
        before={table:[tuple(r)for r in self.store.db.execute('SELECT * FROM '+table)]for table in ('proposals','operations')}
        del row['metadata'][AUDIENCE_KEY];self.ingest(row)
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[]);self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.assertEqual(before,{table:[tuple(r)for r in self.store.db.execute('SELECT * FROM '+table)]for table in ('proposals','operations')})

    def test_fresh_new_class_proof_cannot_use_old_class_activation_receipt(self):
        self.authorize_class();context=dict(fixtures.LEARNER,class_id='2y')
        row=observed(context=context)
        self.store.ingest_sources({'sources':[row],'learner':context,'complete_kinds':fixtures.KINDS,'status':'ok'},fixtures.NOW)
        from app.source_store import source_identity
        src=self.store.get_source(source_identity(row['kind'],row['upstream_id'],row['learner_id']))
        raw=school_action();raw['recipient']['class_id']='2y'
        SourcePlanner(self.store,fixtures.Runtime([raw])).analyze(src,fixtures.NOW)
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(self.store.list_proposals()[0]['status'],'pending')

    def test_retention_removes_observation_but_preserves_guard_against_authority_upgrade(self):
        self.authorize_class();body='Przygotować papierowe serce na kiermasz wiosenny do 30 kwietnia.'
        row=observed(text=body);src=self.ingest(row)
        raw=school_action(source_quote=body,start='2030-04-30',action_identity={'action_type':'submit','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':body})
        result=SourcePlanner(self.store,fixtures.Runtime([raw])).analyze(src,fixtures.NOW)
        self.store.prune_source_content(fixtures.NOW+timedelta(days=31),apply=True)
        proposal=self.store.get_action(result['outcomes'][0]['action_id'])['payload']
        self.assertIn('recipient_basis',proposal)
        self.assertFalse(valid_calendar_audience(self.store.get_source(src['id']),fixtures.LEARNER))
        self.assertEqual(self.store.queue_operations(fixtures.NOW+timedelta(days=31)),[])

    def test_literal_path_does_not_gain_new_receipt_requirement(self):
        row=observed(text=fixtures.TEXT);src=self.ingest(row)
        SourcePlanner(self.store,fixtures.Runtime([fixtures.action()])).analyze(src,fixtures.NOW)
        self.assertNotIn('recipient_basis',self.store.list_proposals()[0]);self.assertEqual(len(self.store.queue_operations(fixtures.NOW)),1)

    def test_runtime_observes_audience_as_context_without_inventing_body_evidence(self):
        from app.codex_runtime import CodexRuntime,SOURCE_PROMPT_VERSION
        from test_support import TEST_CONFIG
        source=self.ingest(observed());runtime=CodexRuntime(TEST_CONFIG,read_only=True)
        with patch.object(runtime,'_invoke',return_value={'proposals':[school_action()],'decision_reason':'Fictional class task.'})as invoke:
            runtime.analyze_source(source,fixtures.LEARNER)
        prompt,schema=invoke.call_args.args;document=json.loads(prompt.split('\n',1)[1])
        self.assertEqual(schema,'source_analysis.json');self.assertEqual(document['source']['text'],BODY)
        self.assertIn(AUDIENCE_KEY,document['source']['metadata']);self.assertIn('condition=none',prompt)
        self.assertEqual(SOURCE_PROMPT_VERSION,'school-sources-v6-class-calendar-audience')


if __name__=='__main__':unittest.main()

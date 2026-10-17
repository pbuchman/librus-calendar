"""Independent verifier uses fictional original quotations and no providers."""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch
import unittest

from app.codex_runtime import CodexRuntime,RuntimeFailure,SOURCE_VERIFY_VERSION
from app.core import digest
from app.source_planner import SourcePlanner,prepare_source_proposals,verified_semantic_match
from app.sync import _PreviewBudget,_PreviewBudgetExceeded,_PreviewRuntime
import test_source_evidence as fixtures
from test_source_planner import Runtime,source,action,NOW,LEARNER
from test_support import TEST_CONFIG
import test_source_budget as budget_fixtures

OLD='Klasa 1x ma pasowanie 12 marca o godzinie 10:00.'
NEW='Klasa 1x ma ślubowanie 12 marca o godzinie 10:00.'
NEIGHBOR='Po południu dzieci wykonają papierowe dekoracje.'


def attendance():
    return action(title='Uroczystość uczniów',source_quote=NEW,start='2030-03-12T10:00:00+01:00',
        temporal_kind='event',all_day=False,optional=False,
        action_identity={'action_type':'attend','object':'ślubowanie','occasion':'uroczystość uczniów','evidence':NEW})


def confirmation(src,item,candidates):
    chosen=candidates[0];proof=chosen['original_evidence'][0]
    return {'decision':'match','action_id':chosen['id'],'evidence_id':proof['id'],
            'source_quote':item['source_quote'],'candidate_quote':proof['text'],
            'same_purpose':True,'same_occasion':True,'recipient_compatible':True,'unique_target':True,
            'source_purpose_field':'text','source_purpose_quote':item['source_quote'],
            'reason':'Fictional independently verified purpose.'}


class SemanticRuntime(Runtime):
    def __init__(self,actions=None):
        super().__init__(actions or[attendance()],decision='uncertain')
        self.last_verification_metadata=None;self.verification_inputs=[]
    def verify_source_action(self,src,item,candidates):
        self.calls.append(('verify',len(candidates)))
        self.verification_inputs.append(deepcopy(candidates))
        self.last_verification_metadata={'version':SOURCE_VERIFY_VERSION,'input_sha256':'f'*64,'result_sha256':'e'*64,'decision':'match'}
        return confirmation(src,item,candidates)


class SemanticProofTests(unittest.TestCase):
    setUp=fixtures.SourceEvidenceTests.setUp
    tearDown=fixtures.SourceEvidenceTests.tearDown
    ingest=fixtures.SourceEvidenceTests.ingest
    seed=fixtures.SourceEvidenceTests.seed

    def setup_attendance(self):
        aid,old=self.seed(quote=OLD,body=OLD+' '+NEIGHBOR,title='Uroczystość uczniów',
            temporal_kind='event',start='2030-03-12T10:00:00+01:00',all_day=False,optional=False)
        src=self.ingest(source('new-occasion',text=NEW,published_at='2030-02-28T10:00:00+01:00'))
        return aid,old,src

    def test_independent_paraphrase_links_original_untyped_action(self):
        aid,old,src=self.setup_attendance();runtime=SemanticRuntime()
        result=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(runtime.calls.count(('verify',1)),1)
        self.assertEqual(len(self.store.list_actions()),1)
        metadata=json.loads(self.store.db.execute('SELECT metadata FROM source_analysis_audit ORDER BY analyzed_at DESC LIMIT 1').fetchone()[0])
        self.assertEqual(metadata['identity_verifications'][0]['version'],SOURCE_VERIFY_VERSION)
        self.assertTrue(metadata['identity_verifications'][0]['accepted'])

    def test_actual_human_hours_and_frozen_operation_survive_older_paraphrase(self):
        aid,old,src=self.setup_attendance()
        self.store.resolve_proposal(aid,'approve',{'start':'2030-03-12T09:45:00+01:00'})
        queued=self.store.queue_operations(NOW);self.assertEqual(len(queued),1)
        before=[tuple(row)for row in self.store.db.execute('SELECT * FROM operations')]
        result=SourcePlanner(self.store,SemanticRuntime()).analyze(src,NOW)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(self.store.get_action(aid)['payload']['start'],'2030-03-12T09:45:00+01:00')
        self.assertEqual(before,[tuple(row)for row in self.store.db.execute('SELECT * FROM operations')])
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_legacy_provenance_projection_is_unique_current_and_read_only(self):
        aid,old,src=self.setup_attendance()
        self.store.resolve_proposal(aid,'approve',{'start':'2030-03-12T09:45:00+01:00'})
        before=self.store.source_decision_snapshot();raw=dict(self.store.db.execute('SELECT * FROM canonical_actions WHERE id=?',(aid,)).fetchone())
        self.assertIsNone(json.loads(raw['payload']).get('source_version_at'))
        projected=self.store.get_action(aid)['payload']
        self.assertEqual(projected['source_version_at'],old['published_at'])
        self.assertEqual(before,self.store.source_decision_snapshot())
        self.assertEqual(raw,dict(self.store.db.execute('SELECT * FROM canonical_actions WHERE id=?',(aid,)).fetchone()))
        for state in ('stale','deleted','retained','clock','ambiguous'):
            with self.subTest(state=state):
                with self.store.db:
                    if state=='stale':self.store.db.execute("UPDATE sources SET content_hash='changed' WHERE id=?",(old['id'],))
                    elif state=='deleted':self.store.db.execute('UPDATE sources SET deleted=1 WHERE id=?',(old['id'],))
                    elif state=='retained':self.store.db.execute("UPDATE sources SET status='retained' WHERE id=?",(old['id'],))
                    elif state=='clock':self.store.db.execute('UPDATE sources SET published_at=NULL,updated_at=NULL WHERE id=?',(old['id'],))
                    else:self.store.db.execute('INSERT INTO action_sources VALUES(?,?,?,?,?,?)',(aid,src['id'],src['content_hash'],NEW,'origin',NOW.isoformat()))
                self.assertIsNone(self.store.get_action(aid)['payload'].get('source_version_at'))
                with self.store.db:
                    self.store.db.execute('UPDATE sources SET content_hash=?,deleted=0,status=?,published_at=?,updated_at=? WHERE id=?',
                        (old['content_hash'],old['status'],old['published_at'],old['updated_at'],old['id']))
                    self.store.db.execute('DELETE FROM action_sources WHERE action_id=? AND source_id=?',(aid,src['id']))

    def test_verifier_transport_excludes_neighbor_and_first_matcher_reason(self):
        aid,old,src=self.setup_attendance();item=prepare_source_proposals(src,[attendance()],LEARNER,NOW)[0]
        runtime=CodexRuntime(TEST_CONFIG,read_only=True);candidate=self.store.get_action(aid)
        response=confirmation(src,item,[candidate])
        with patch.object(runtime,'_invoke',return_value=response)as invoke:
            runtime.verify_source_action(src,item,[candidate])
            prompt,schema=invoke.call_args.args;document=json.loads(prompt.split('\n',1)[1])
            self.assertEqual(schema,'source_verify.json')
            self.assertEqual(document['candidates'][0]['original_evidence'][0]['text'],OLD)
            self.assertNotIn(NEIGHBOR,prompt)
            self.assertNotIn('evidence',document['candidates'][0])
            self.assertNotIn('source_quote',document['candidates'][0]['action'])
            self.assertNotIn('matcher_reason',document)
            self.assertNotIn('evidence',document['action']['action_identity'])
            self.assertEqual(runtime.last_verification_metadata['version'],SOURCE_VERIFY_VERSION)
            self.assertEqual(len(runtime.last_verification_metadata['input_sha256']),64)

    def test_verifier_schema_rejects_malformed_protocol_without_retry(self):
        aid,old,src=self.setup_attendance();item=prepare_source_proposals(src,[attendance()],LEARNER,NOW)[0]
        candidate=self.store.get_action(aid);calls=[]
        def malformed(*args):
            calls.append(1)
            return 0,json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'decision':'match'})}}),''
        runtime=CodexRuntime(TEST_CONFIG,read_only=True,run_process=malformed)
        with self.assertRaises(RuntimeFailure):runtime.verify_source_action(src,item,[candidate])
        self.assertEqual(calls,[1]);self.assertIsNone(runtime.last_verification_metadata)

    def test_forged_incomplete_or_ambiguous_proof_is_refused(self):
        aid,old,src=self.setup_attendance();item=prepare_source_proposals(src,[attendance()],LEARNER,NOW)[0]
        candidate=self.store.get_action(aid);result=confirmation(src,item,[candidate])
        invalid=[{key:False}for key in ('same_purpose','same_occasion','recipient_compatible','unique_target')]
        invalid += [{'evidence_id':'invented'},{'action_id':'unknown'},{'source_quote':'Invented fact'},
                    {'candidate_quote':NEIGHBOR},{'source_quote':'Klasa 1x'},{'candidate_quote':'godzinie'}]
        for change in invalid:
            with self.subTest(change=change):self.assertEqual(verified_semantic_match(src,item,[candidate],dict(result,**change)),(None,'uncertain'))
        duplicate=deepcopy(candidate);duplicate['id']='other-compatible-target'
        self.assertEqual(verified_semantic_match(src,item,[candidate,duplicate],result),(None,'uncertain'))

    def title_fact(self):
        old='Klasa 1x ma dzień wolny od lekcji 20 lutego.'
        body='Zakres: Klasa 1x. Data: 2030-02-20.'
        aid,_=self.seed(quote=old,body=old,temporal_kind='event',start='2030-02-20',end='2030-02-21')
        src=self.ingest(source('calendar-dayoff',kind='school_event',title='Dzień wolny od lekcji',text=body,
            published_at='2030-02-28T10:00:00+01:00',metadata={'event_start':'2030-02-20'}))
        raw=action(temporal_kind='event',start='2030-02-20',end='2030-02-21',source_quote=body,
            action_identity={'action_type':'inform','object':'dzień wolny','occasion':'brak lekcji','evidence':body})
        item=prepare_source_proposals(src,[raw],LEARNER,NOW)[0];candidate=self.store.get_action(aid)
        result=confirmation(src,item,[candidate]);result.update(source_purpose_field='title',source_purpose_quote=src['title'])
        return aid,src,raw,item,candidate,result

    def test_observed_school_heading_can_name_a_fact_with_separate_date_scope_body(self):
        aid,src,raw,item,candidate,response=self.title_fact()
        self.assertNotIn(src['title'],src['text'])
        self.assertEqual(verified_semantic_match(src,item,[candidate],response)[0]['id'],aid)
        runtime=SemanticRuntime([raw]);runtime.verify_source_action=lambda *args:deepcopy(response)
        applied=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertEqual(applied['outcomes'][0]['action_id'],aid)
        self.assertEqual(applied['outcomes'][0]['outcome'],'linked')
        self.assertEqual(self.store.queue_operations(NOW),[])
        with patch.object(CodexRuntime,'_invoke',return_value=response)as invoke:
            CodexRuntime(TEST_CONFIG,read_only=True).verify_source_action(src,item,[candidate])
            document=json.loads(invoke.call_args.args[0].split('\n',1)[1])
            self.assertEqual(document['source']['observed_title'],src['title'])
            self.assertEqual(document['source']['action_quote'],src['text'])
            self.assertEqual(document['source']['observed_event']['event_start'],'2030-02-20')

    def test_observed_heading_cannot_bypass_kind_date_scope_or_purpose_proof(self):
        aid,src,raw,item,candidate,response=self.title_fact()
        for changes in ({'kind':'announcement'},{'title':'Unobserved changed heading'},
                        {'metadata':{}},{'metadata':{'event_start':'invalid'}},
                        {'metadata':{'event_start':'2030-02-21'}}):
            with self.subTest(changes=changes):self.assertEqual(verified_semantic_match(dict(src,**changes),item,[candidate],response),(None,'uncertain'))
        for changes in ({'source_purpose_field':'unknown'},{'source_purpose_field':None},
                        {'source_purpose_field':'text'},{'source_purpose_quote':'Invented heading'},
                        {'source_quote':'Invented body'},{'same_occasion':False}):
            with self.subTest(changes=changes):self.assertEqual(verified_semantic_match(src,item,[candidate],dict(response,**changes)),(None,'uncertain'))
        missing=dict(response);missing.pop('source_purpose_field')
        self.assertEqual(verified_semantic_match(src,item,[candidate],missing),(None,'uncertain'))
        compatible=deepcopy(src);compatible['metadata']['event_start']='2030-02-19T23:30:00Z'
        self.assertEqual(verified_semantic_match(compatible,item,[candidate],response)[0]['id'],aid)
        wrong=deepcopy(raw);wrong['recipient']['class_id']='2y'
        runtime=SemanticRuntime([wrong]);result=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertNotEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(runtime.verification_inputs,[])

    def test_typed_recipient_temporal_purpose_and_offer_guards_still_apply(self):
        aid,old,src=self.setup_attendance();item=prepare_source_proposals(src,[attendance()],LEARNER,NOW)[0]
        candidate=self.store.get_action(aid);response=confirmation(src,item,[candidate])
        for changes in ({'temporal_kind':'reminder'},
                        {'recipient':{'scope':'class','class_id':'2y'}},
                        {'action_identity':{'action_type':'bring','object':'strój','occasion':'uroczystość'}},
                        {'alternative_group':'offer','start':'2030-03-13T10:00:00+01:00'}):
            other=deepcopy(candidate);other['payload'].update(changes)
            current=deepcopy(item)
            if 'alternative_group'in changes:current['alternative_group']='offer'
            with self.subTest(changes=changes):self.assertEqual(verified_semantic_match(src,current,[other],response),(None,'distinct'))

    def test_proofless_candidate_prevents_manufactured_uniqueness(self):
        aid,old,src=self.setup_attendance();runtime=SemanticRuntime()
        unknown=deepcopy(self.store.get_action(aid));unknown['id']='inventory-only';unknown['payload']['inventory_only']=True
        unknown['original_evidence']=[];unknown['evidence']=[]
        original=self.store.candidate_actions
        def complete_candidates(*args,**kwargs):return original(*args,**kwargs)+[unknown]
        with patch.object(self.store,'candidate_actions',side_effect=complete_candidates):
            result=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertNotEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertEqual(runtime.verification_inputs,[])

    def test_raw_bad_context_match_cannot_authorize_dayoff_as_ceremony(self):
        saved='Tego dnia nie ma lekcji dla uczniów.'
        aid,_=self.seed(quote=saved,body=saved+' '+NEW,temporal_kind='event',all_day=False,
                        start='2030-03-12T10:00:00+01:00')
        src=self.ingest(source('ceremony',text=NEW));runtime=SemanticRuntime()
        def first_match(src,item,candidates):
            selected=candidates[0];proof=selected['evidence'][0]
            return {'decision':'match','action_id':aid,'evidence_id':proof['id'],'source_quote':NEW,
                    'candidate_quote':proof['text'],'anchors':['ślubowanie','marca']}
        def independent(src,item,candidates):
            self.assertEqual(candidates[0]['original_evidence'][0]['text'],saved)
            return {'decision':'uncertain'}
        runtime.match_source_action=first_match;runtime.verify_source_action=independent
        result=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertNotEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')

    def test_semantic_call_errors_and_cas_changes_are_not_swallowed(self):
        aid,old,src=self.setup_attendance();runtime=SemanticRuntime()
        runtime.verify_source_action=lambda *args:(_ for _ in ()).throw(RuntimeFailure('codex_timeout'))
        with self.assertRaises(RuntimeFailure):SourcePlanner(self.store,runtime).analyze(src,NOW)
        for preview in (False,True):
            runtime=SemanticRuntime();verify=runtime.verify_source_action
            def race(*args):
                result=verify(*args)
                with self.store.db:self.store.db.execute("UPDATE action_sources SET source_hash=source_hash||'x' WHERE action_id=?",(aid,))
                return result
            runtime.verify_source_action=race
            with self.assertRaises(ValueError):SourcePlanner(self.store,runtime).analyze(src,NOW,preview=preview)

    def test_semantic_call_uses_shared_preview_call_and_time_limits(self):
        runtime=SemanticRuntime();budget=_PreviewBudget(60,2);wrapped=_PreviewRuntime(runtime,budget)
        wrapped.analyze_source({'id':'fictional'},LEARNER)
        wrapped.match_source_action({}, {},[{}])
        with self.assertRaises(_PreviewBudgetExceeded):wrapped.verify_source_action({}, {},[])
        self.assertEqual(budget.calls,2);self.assertEqual(runtime.verification_inputs,[])
        budget=_PreviewBudget(60,3);wrapped=_PreviewRuntime(runtime,budget)
        def slow(*args):budget.deadline=0;return {'decision':'uncertain'}
        runtime.verify_source_action=slow
        with self.assertRaises(_PreviewBudgetExceeded):wrapped.verify_source_action({}, {},[])
        self.assertEqual(budget.calls,1)


class PreviewSemanticProgressTests(unittest.TestCase):
    setUp=budget_fixtures.SourceBudgetTests.setUp
    tearDown=budget_fixtures.SourceBudgetTests.tearDown
    run_sync=budget_fixtures.SourceBudgetTests.run_sync

    def run_many(self,count):
        words=('accordion balloon camera drum engine flower guitar hammer island jacket kettle lantern mirror notebook orchard pencil '
               'puzzle racket saddle telescope umbrella violin window yellow zebra apron basket copper diamond emerald').split()[:count]
        self.store.ingest_sources({'sources':[],'learner':LEARNER,'complete_kinds':[],'status':'partial'},budget_fixtures.NOW)
        actions=[];quotes=[]
        for index,word in enumerate(words):
            quote='Klasa 1x '+word+' 2030-03-12.';quotes.append(quote)
            item=action(title=word,source_quote=quote,needs_review=True,
                action_identity={'action_type':'bring','object':word,'occasion':'','evidence':quote})
            mid='fictional-seed-'+str(index)
            self.store.save_message({'id':mid,'subject':word,'text':quote,'sent_at':budget_fixtures.NOW.isoformat()})
            self.store.save_proposals(mid,[item],budget_fixtures.NOW)
            actions.append(dict(item,needs_review=False))
        class ManyRuntime(budget_fixtures.SourceRuntime):
            def __init__(inner):super().__init__(actions=actions);inner.verifications=0
            def match_source_action(inner,*args):return {'decision':'uncertain'}
            def verify_source_action(inner,src,item,candidates):
                inner.verifications+=1
                return confirmation(src,item,candidates)
        client=budget_fixtures.SourceClient([source('fictional-many',text=' '.join(quotes))],count=0)
        first_runtime=ManyRuntime();first=self.run_sync(client,first_runtime)
        self.assertEqual(first['sources']['model_calls'],31)
        ready=self.store.list_source_previews(status='ready');self.assertEqual(len(ready),1)
        self.assertEqual(self.store.list_source_previews(status='failed'),[])
        metadata=ready[0]['analysis_metadata'];deferred=count-(30-count)
        self.assertEqual(metadata['verification_deferred'],deferred)
        self.assertIs(metadata['identity_verification_complete'],False)
        uncertain=[outcome for outcome in ready[0]['outcomes']if outcome['action']['payload'].get('verification_deferred')]
        self.assertEqual(len(uncertain),deferred)
        for outcome in uncertain:
            self.assertEqual(outcome['outcome'],'review')
            self.assertIn('dodatkowej weryfikacji',outcome['reason'])
            self.assertIsNone(self.store.get_action(outcome['action_id']))
        second=self.run_sync(client,ManyRuntime())
        self.assertEqual(second['sources']['model_calls'],0)
        self.assertEqual(self.store.source_status()['preview_pending'],0)
        # An active fresh analysis does not replay the deferred preview plan.
        self.store.config=replace(self.store.config,multi_source_mode='active')
        active=ManyRuntime();src=self.store.get_source(ready[0]['source_id'])
        applied=SourcePlanner(self.store,active).analyze(src,budget_fixtures.NOW)
        self.assertEqual(active.verifications,count)
        self.assertTrue(all(outcome['outcome']=='linked'for outcome in applied['outcomes']))
        self.assertEqual(len(self.store.list_actions()),count)

    def test_sixteen_action_preview_finishes_with_honest_review(self):self.run_many(16)
    def test_thirty_action_preview_finishes_with_honest_review(self):self.run_many(30)

    def test_small_source_at_end_of_budget_is_retried_fresh_not_cached_unverified(self):
        self.store.ingest_sources({'sources':[],'learner':LEARNER,'complete_kinds':[],'status':'partial'},budget_fixtures.NOW)
        self.store.save_message({'id':'small-seed','subject':'ceremony','text':OLD,'sent_at':budget_fixtures.NOW.isoformat()})
        seed=attendance();seed.update(source_quote=OLD,needs_review=True);seed.pop('action_identity')
        self.store.save_proposals('small-seed',[seed],budget_fixtures.NOW)
        src=source('small-tail',text=NEW)
        self.store.ingest_sources({'sources':[src],'learner':LEARNER,'complete_kinds':['announcement','school_event','substitution'],'status':'ok'},budget_fixtures.NOW)
        from app.source_store import source_identity
        normalized=self.store.get_source(source_identity(src['kind'],src['upstream_id'],LEARNER['id']))
        first=SemanticRuntime();spent=_PreviewBudget(60,31);spent.calls=29
        with self.assertRaises(_PreviewBudgetExceeded):SourcePlanner(self.store,_PreviewRuntime(first,spent)).analyze(normalized,budget_fixtures.NOW,preview=True)
        self.assertEqual(first.verification_inputs,[])
        self.assertEqual(self.store.list_source_previews(status='ready'),[])
        fresh=SemanticRuntime();budget=_PreviewBudget(60,31)
        result=SourcePlanner(self.store,_PreviewRuntime(fresh,budget)).analyze(normalized,budget_fixtures.NOW,preview=True)
        self.assertEqual(budget.calls,3)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(len(self.store.list_source_previews(status='ready')),1)


if __name__=='__main__':unittest.main()

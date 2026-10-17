"""Current-origin facts are separate from historical and recipient authority."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from app.codex_runtime import CodexRuntime,SOURCE_RECOVERY_VERSION,RuntimeFailure,event_fingerprint
from app.core import digest
from app.source_planner import SourcePlanner,prepare_source_proposals,recovered_action_evidence
from app.source_store import CORROBORATION_RELATION
from app.sync import _PreviewBudget,_PreviewRuntime,_PreviewBudgetExceeded
import test_source_evidence as evidence
import test_source_semantics as semantic
import test_source_planner as fixtures
from test_support import TEST_CONFIG

DAYOFF='Klasa 1x ma dzień wolny od lekcji 12 marca.'
CEREMONY='Klasa 1x ma ślubowanie 12 marca o godzinie 10:00.'


class RecoveryRuntime(semantic.SemanticRuntime):
    def __init__(self,actions,quote=DAYOFF,purpose='wolny',day='2030-03-12',date='12 marca'):
        super().__init__(actions);self.quote,self.purpose,self.day,self.date=quote,purpose,day,date
        self.recovery_inputs=[];self.last_recovery_metadata=None
    def recover_source_action_evidence(self,candidates,learner_context=None):
        self.calls.append(('recover',len(candidates)));self.recovery_inputs.append(deepcopy(candidates))
        self.last_recovery_metadata={'version':SOURCE_RECOVERY_VERSION,'input_sha256':'d'*64,'result_sha256':'c'*64}
        return {'facts':[{'action_id':a['id'],'source_id':a['recovery_origin']['source_id'],
            'source_hash':a['recovery_origin']['source_hash'],'decision':'fact','source_quote':self.quote,
            'purpose_quote':self.purpose,'date_quote':self.date,'day':self.day,
            'same_purpose':True,'same_day':True,'recipient_compatible':True}for a in candidates]}
    def verify_source_action(self,src,item,candidates):
        originals=[dict(a,original_evidence=a.get('original_evidence',[])+a.get('reconstructed_evidence',[]))for a in candidates]
        return super().verify_source_action(src,item,originals)


class OriginRecoveryTests(unittest.TestCase):
    setUp=evidence.SourceEvidenceTests.setUp
    tearDown=evidence.SourceEvidenceTests.tearDown
    ingest=evidence.SourceEvidenceTests.ingest
    seed=evidence.SourceEvidenceTests.seed

    def setup_dayoff(self):
        aid,old=self.seed(quote='',body=DAYOFF+' '+CEREMONY,title='Dzień wolny od lekcji',
            temporal_kind='event',start='2030-03-12',end='2030-03-13',optional=False)
        src=self.ingest(fixtures.source('new-dayoff',text=DAYOFF,published_at='2030-02-28T10:00:00+01:00'))
        item=fixtures.action(title='Dzień wolny od lekcji',source_quote=DAYOFF,temporal_kind='event',
            start='2030-03-12',end='2030-03-13',optional=False,
            action_identity={'action_type':'inform','object':'dzień wolny','occasion':'brak lekcji','evidence':DAYOFF})
        return aid,old,src,item

    def test_missing_quote_recovers_only_current_origin_and_does_not_repair_history(self):
        aid,old,src,item=self.setup_dayoff();runtime=RecoveryRuntime([item])
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        self.assertEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        origin=self.store.db.execute("SELECT source_quote FROM action_sources WHERE action_id=? AND relation='origin'",(aid,)).fetchone()
        self.assertEqual(origin[0],'')
        self.assertEqual(runtime.calls.count(('recover',1)),1)
        self.assertEqual(runtime.calls.count(('verify',1)),1)
        proof=runtime.verification_inputs[0][0]['original_evidence'][0]
        self.assertEqual(proof['kind'],'current_origin_reconstructed');self.assertIn(proof['text'],DAYOFF)
        self.assertIn(proof['purpose_quote'],proof['text']);self.assertIn(proof['date_quote'],proof['text'])
        self.assertNotIn(CEREMONY,proof['text'])

    def test_origin_requires_actual_row_hash_current_unique_body_and_clock(self):
        aid,old,src,item=self.setup_dayoff();before=self.store.get_action(aid)
        self.assertIsNotNone(before['recovery_origin'])
        for state in ('rowhash','linkhash','deleted','retained','clock','ambiguous'):
            with self.subTest(state=state):
                with self.store.db:
                    if state=='rowhash':self.store.db.execute("UPDATE proposals SET source_hash='wrong' WHERE id=?",(aid,))
                    elif state=='linkhash':self.store.db.execute("UPDATE action_sources SET source_hash='wrong' WHERE action_id=?",(aid,))
                    elif state=='deleted':self.store.db.execute('UPDATE sources SET deleted=1 WHERE id=?',(old['id'],))
                    elif state=='retained':self.store.db.execute("UPDATE sources SET status='retained' WHERE id=?",(old['id'],))
                    elif state=='clock':self.store.db.execute('UPDATE sources SET published_at=NULL,updated_at=NULL WHERE id=?',(old['id'],))
                    else:self.store.db.execute('INSERT INTO action_sources VALUES(?,?,?,?,?,?)',(aid,src['id'],src['content_hash'],'','origin',fixtures.NOW.isoformat()))
                self.assertIsNone(self.store.get_action(aid)['recovery_origin'])
                with self.store.db:
                    self.store.db.execute('UPDATE proposals SET source_hash=? WHERE id=?',(old['content_hash'],aid))
                    self.store.db.execute('UPDATE action_sources SET source_hash=? WHERE action_id=? AND source_id=?',(old['content_hash'],aid,old['id']))
                    self.store.db.execute('UPDATE sources SET deleted=0,status=?,published_at=? WHERE id=?',(old['status'],old['published_at'],old['id']))
                    self.store.db.execute('DELETE FROM action_sources WHERE action_id=? AND source_id=?',(aid,src['id']))

    def test_forged_neighbor_date_hash_and_generic_purpose_are_not_registered(self):
        aid,old,src,item=self.setup_dayoff();candidate=self.store.get_action(aid);runtime=RecoveryRuntime([item])
        good=runtime.recover_source_action_evidence([candidate]);self.assertIn(aid,recovered_action_evidence([candidate],good))
        invalid=[{'source_quote':CEREMONY,'purpose_quote':'ślubowanie'}, {'date_quote':'invented date'},
            {'day':'2030-03-13'},{'source_hash':'wrong'},{'recipient_compatible':False},{'same_purpose':False}]
        for changes in invalid:
            bad=deepcopy(good);bad['facts'][0].update(changes)
            with self.subTest(changes=changes):self.assertEqual(recovered_action_evidence([candidate],bad),{})
        self.assertEqual(recovered_action_evidence([candidate],{'facts':good['facts']*2}),{})
        generic=deepcopy(candidate);generic['payload']['title']='Klasa 1x 12 marca'
        generic['recovery_origin']['canonical_payload_hash']=digest(generic['payload'])
        self.assertEqual(recovered_action_evidence([generic],good),{})

    def test_recovered_dayoff_does_not_prove_ceremony_even_if_first_matcher_claims_it(self):
        aid,old,src,item=self.setup_dayoff();src=self.ingest(fixtures.source('ceremony',text=CEREMONY))
        item=semantic.attendance();runtime=RecoveryRuntime([item])
        runtime.verify_source_action=lambda *args:{'decision':'uncertain'}
        self.store._source_candidates_complete=True
        with patch.object(self.store,'candidate_actions',return_value=[self.store.get_action(aid)]):
            result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        self.assertNotEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')

    def test_broad_recovery_cannot_supply_neighbor_ceremony_to_verifier(self):
        aid,old,src,item=self.setup_dayoff();candidate=self.store.get_action(aid)
        response=RecoveryRuntime([item],quote=DAYOFF+' '+CEREMONY).recover_source_action_evidence([candidate])
        # A broad quote repeats the date, so it is ambiguous and rejected.
        self.assertEqual(recovered_action_evidence([candidate],response),{})
        response=RecoveryRuntime([item],quote=DAYOFF+' Osobno zaplanowano uroczystość.').recover_source_action_evidence([candidate])
        origin=candidate['recovery_origin'];origin['body']=DAYOFF+' Osobno zaplanowano uroczystość.'
        proofs=recovered_action_evidence([candidate],response)[aid]
        self.assertNotIn('uroczystość',proofs[0]['text'])
        self.assertIn(proofs[0]['purpose_quote'],proofs[0]['text'])

    def test_recovery_transport_has_only_canonical_and_own_origin_no_incoming_fact(self):
        aid,old,src,item=self.setup_dayoff();candidate=self.store.get_action(aid);runtime=CodexRuntime(TEST_CONFIG,read_only=True)
        expected=RecoveryRuntime([item]).recover_source_action_evidence([candidate])
        with patch.object(runtime,'_invoke',return_value=expected)as invoke:
            runtime.recover_source_action_evidence([candidate],fixtures.LEARNER)
            prompt,schema=invoke.call_args.args;document=json.loads(prompt.split('\n',1)[1])
            self.assertEqual(schema,'source_recovery.json');self.assertNotIn('source',document)
            self.assertNotIn('incoming',document);self.assertNotIn('matcher',document)
            self.assertEqual(document['candidates'][0]['origin']['body'],DAYOFF+' '+CEREMONY)
            self.assertEqual(runtime.last_recovery_metadata['version'],SOURCE_RECOVERY_VERSION)
            args=runtime._args('/tmp/fictional','source_recovery.json',[])
            self.assertIn('--disable',args);self.assertIn('apps',args)
            self.assertTrue(any('source_recovery.md'in arg for arg in args))

    def test_recovery_failure_and_row_hash_races_propagate_preview_and_active(self):
        aid,old,src,item=self.setup_dayoff();runtime=RecoveryRuntime([item])
        runtime.recover_source_action_evidence=lambda *a:(_ for _ in ()).throw(RuntimeFailure('codex_timeout'))
        with self.assertRaises(RuntimeFailure):SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        for preview in (False,True):
            runtime=RecoveryRuntime([item]);original=runtime.verify_source_action
            def race(*args):
                result=original(*args)
                with self.store.db:self.store.db.execute("UPDATE proposals SET source_hash=source_hash||'x' WHERE id=?",(aid,))
                return result
            runtime.verify_source_action=race
            with self.assertRaises(ValueError):SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=preview)
            with self.store.db:self.store.db.execute('UPDATE proposals SET source_hash=? WHERE id=?',(old['content_hash'],aid))

    def test_four_calls_share_preview_cap_and_small_tail_retries_fresh(self):
        aid,old,src,item=self.setup_dayoff();spent=_PreviewBudget(60,31);spent.calls=28
        with self.assertRaises(_PreviewBudgetExceeded):SourcePlanner(self.store,_PreviewRuntime(RecoveryRuntime([item]),spent)).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(self.store.list_source_previews(status='ready'),[])
        budget=_PreviewBudget(60,31);runtime=RecoveryRuntime([item])
        result=SourcePlanner(self.store,_PreviewRuntime(runtime,budget)).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(budget.calls,4);self.assertEqual(result['outcomes'][0]['outcome'],'linked')

    def many_recoveries(self,count):
        self.ingest()
        words=('accordion balloon camera drum engine flower guitar hammer island jacket kettle lantern mirror notebook orchard pencil '
               'puzzle racket saddle telescope umbrella violin window yellow zebra apron basket copper diamond emerald').split()[:count]
        actions=[];quotes=[]
        for index,word in enumerate(words):
            quote='Klasa 1x '+word+' 2030-03-12.';quotes.append(quote)
            raw=fixtures.action(title=word,source_quote=quote,temporal_kind='event',start='2030-03-12',end='2030-03-13',
                optional=False,action_identity={'action_type':'inform','object':word,'occasion':'','evidence':quote})
            legacy=dict(raw,source_quote='',needs_review=True);legacy.pop('action_identity');legacy.pop('recipient')
            mid='fictional-recovery-'+str(index)
            self.store.save_message({'id':mid,'subject':word,'text':quote,'sent_at':fixtures.NOW.isoformat()})
            self.store.save_proposals(mid,[legacy],fixtures.NOW);actions.append(raw)
        src=self.ingest(fixtures.source('large-recovery',text=' '.join(quotes)))
        class Many(RecoveryRuntime):
            def recover_source_action_evidence(inner,candidates,learner_context=None):
                inner.calls.append(('recover',len(candidates)));inner.recovery_inputs.append(deepcopy(candidates))
                return {'facts':[{'action_id':a['id'],'source_id':a['recovery_origin']['source_id'],
                    'source_hash':a['recovery_origin']['source_hash'],'decision':'fact','source_quote':a['recovery_origin']['body'],
                    'purpose_quote':a['payload']['title'].removeprefix('[Librus] '),'date_quote':'2030-03-12','day':'2030-03-12',
                    'same_purpose':True,'same_day':True,'recipient_compatible':True}for a in candidates]}
        runtime=Many(actions);budget=_PreviewBudget(60,31)
        result=SourcePlanner(self.store,_PreviewRuntime(runtime,budget)).analyze(src,fixtures.NOW,preview=True)
        self.assertLessEqual(budget.calls,31);self.assertEqual(result['count'],count)
        preview=self.store.list_source_previews(status='ready')[0]
        self.assertFalse(preview['analysis_metadata']['identity_verification_complete'])
        deferred=[x for x in preview['outcomes']if x['action']['payload'].get('verification_deferred')]
        self.assertTrue(deferred);self.assertTrue(all(x['outcome']=='review'for x in deferred))
        self.assertNotIn(src['id'],[x['id']for x in self.store.pending_sources(preview=True)])
        # Active execution must re-extract/recover/verify, never replay previews.
        active=Many(actions);applied=SourcePlanner(self.store,active).analyze(src,fixtures.NOW)
        self.assertEqual(len(active.calls),1+3*count)
        self.assertTrue(all(x['outcome']=='linked'for x in applied['outcomes']))

    def test_eleven_recovery_actions_finish_honest_preview_and_active_rechecks(self):self.many_recoveries(11)
    def test_thirty_recovery_actions_keep_all_primary_matchers_inside31(self):self.many_recoveries(30)


class CorroborationTests(unittest.TestCase):
    setUp=evidence.SourceEvidenceTests.setUp
    tearDown=evidence.SourceEvidenceTests.tearDown
    ingest=evidence.SourceEvidenceTests.ingest
    seed=evidence.SourceEvidenceTests.seed

    def setup_owned(self):
        aid,old=self.seed(quote=semantic.OLD,body=semantic.OLD,title='Uroczystość uczniów',
            temporal_kind='event',start='2030-03-12T10:00:00+01:00',all_day=False,optional=False)
        self.store.resolve_proposal(aid,'approve',{'start':'2030-03-12T09:45:00+01:00'})
        operation=self.store.queue_operations(fixtures.NOW)[0];snapshot=dict(operation['event'],id='fictional-owned-event')
        self.store.record_result(operation['operation_id'],{'status':'applied','event_id':snapshot['id'],'snapshot':snapshot,
            'fingerprint':event_fingerprint(snapshot,self.store.config.calendar_owner)})
        src=self.ingest(fixtures.source('older-ceremony',text=semantic.NEW,published_at='2030-02-28T10:00:00+01:00'))
        raw=semantic.attendance();raw['recipient']={'scope':'unknown','learner_id':None,'class_id':None,'evidence':'','condition':'unknown'}
        return aid,old,src,raw

    def protected(self):
        return {name:[tuple(row)for row in self.store.db.execute('SELECT * FROM '+name)]
            for name in ('canonical_actions','proposals','canonical_decisions','decision_intents','events','operations')}

    def test_older_proved_unknown_recipient_adds_only_labelled_link(self):
        aid,old,src,raw=self.setup_owned();before=self.protected();runtime=semantic.SemanticRuntime([raw])
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        self.assertEqual(result['outcomes'][0]['action_id'],aid);self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(before,self.protected());self.assertIn('niezweryfikowany',result['outcomes'][0]['reason'])
        link=self.store.db.execute('SELECT relation FROM action_sources WHERE action_id=? AND source_id=?',(aid,src['id'])).fetchone()
        self.assertEqual(link[0],CORROBORATION_RELATION);self.assertEqual(len(runtime.verification_inputs),1)

    def test_preview_isolated_and_labelled_link_cannot_become_matching_authority(self):
        aid,old,src,raw=self.setup_owned();before=self.protected();runtime=semantic.SemanticRuntime([raw])
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked');self.assertEqual(before,self.protected())
        self.assertFalse(self.store.db.execute('SELECT 1 FROM action_sources WHERE source_id=?',(src['id'],)).fetchone())
        SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW)
        with self.store.db:self.store.db.execute("DELETE FROM action_sources WHERE action_id=? AND relation='origin'",(aid,))
        candidate=self.store.get_action(aid)
        self.assertEqual(candidate['evidence'],[]);self.assertEqual(candidate['original_evidence'],[])
        self.assertIsNone(candidate['recovery_origin'])

    def test_two_previews_keep_real_target_proof_instead_of_virtual_overlay(self):
        aid,old,src,raw=self.setup_owned();before=self.protected()
        first=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW,preview=True)
        other=self.ingest(fixtures.source('another-older-source',text=semantic.NEW,published_at='2030-02-27T10:00:00+01:00'))
        second=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(other,fixtures.NOW,preview=True)
        self.assertEqual(first['outcomes'][0]['action_id'],aid);self.assertEqual(second['outcomes'][0]['action_id'],aid)
        self.assertEqual(first['outcomes'][0]['outcome'],'linked');self.assertEqual(second['outcomes'][0]['outcome'],'linked')
        self.assertEqual(before,self.protected());self.assertEqual(len(self.store.list_source_previews(status='ready')),2)

    def test_corroboration_row_or_decision_race_invalidates_preview_and_active(self):
        aid,old,src,raw=self.setup_owned()
        for preview in (False,True):
            runtime=semantic.SemanticRuntime([raw]);verify=runtime.verify_source_action
            def race(*args):
                result=verify(*args)
                with self.store.db:self.store.db.execute("UPDATE proposals SET source_hash=source_hash||'x' WHERE id=?",(aid,))
                return result
            runtime.verify_source_action=race
            with self.assertRaises(ValueError):SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=preview)
            with self.store.db:self.store.db.execute('UPDATE proposals SET source_hash=? WHERE id=?',(old['content_hash'],aid))

    def test_removing_low_authority_source_does_not_reopen_human_intent(self):
        aid,old,src,raw=self.setup_owned()
        SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW)
        before=self.protected()
        self.ingest(fixtures.source('older-ceremony',text=semantic.NEW,deleted=True))
        self.assertEqual(before,self.protected())

    def test_newer_unknown_clock_crossday_wrongclass_role_offer_never_corroborate(self):
        aid,old,src,raw=self.setup_owned()
        cases=[({'published_at':'2030-03-02T10:00:00+01:00'},{}),({'published_at':None},{}),
            ({},{'start':'2030-03-13T10:00:00+01:00'}),
            ({},{'recipient':{'scope':'class','class_id':'2y','evidence':'Klasa 1x'}}),
            ({},{'recipient':{'scope':'conditional','condition':'role','evidence':'Klasa 1x'}}),
            ({},{'activity_scope':'extracurricular','alternative_group':'choice'})]
        for i,(source_changes,changes)in enumerate(cases):
            with self.subTest(index=i):
                candidate=self.ingest(fixtures.source('negative-'+str(i),text=semantic.NEW,published_at=source_changes.get('published_at','2030-02-28T10:00:00+01:00')))
                current=deepcopy(raw);current.update(changes)
                result=SourcePlanner(self.store,semantic.SemanticRuntime([current])).analyze(candidate,fixtures.NOW)
                self.assertFalse(self.store.db.execute('SELECT 1 FROM action_sources WHERE action_id=? AND source_id=?',(aid,candidate['id'])).fetchone())

    def test_expired_unknown_school_fact_ignored_but_future_uncertain_update_cancel_review(self):
        src=self.ingest(fixtures.source(text=semantic.NEW))
        raw=semantic.attendance();raw['recipient']={'scope':'unknown','learner_id':None,'class_id':None,'evidence':''}
        past=fixtures.NOW+timedelta(days=20)
        prepared=prepare_source_proposals(src,[raw],fixtures.LEARNER,past)[0]
        self.assertEqual(prepared['status'],'ignored');self.assertTrue(prepared['expired_without_authority'])
        result=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,past)
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored');self.assertEqual(self.store.queue_operations(past),[])
        diagnostic=self.store.get_action(result['outcomes'][0]['action_id'])
        self.assertEqual(diagnostic['evidence'],[]);self.assertEqual(diagnostic['original_evidence'],[])
        self.assertIsNone(diagnostic['recovery_origin'])
        self.assertNotIn(diagnostic['id'],[x['id']for x in self.store.candidate_actions(fixtures.action())])
        for changes in ({},{'date_certainty':'uncertain'},{'source_quote':'invented'}, {'kind':'update'},{'kind':'cancel'}):
            current=dict(raw,**changes);clock=fixtures.NOW if not changes else past
            self.assertEqual(prepare_source_proposals(src,[current],fixtures.LEARNER,clock)[0]['status'],'review')

    def test_full_retention_prunes_owned_expired_diagnostic_without_creating_authority(self):
        from app.data_admin import prune_state
        phrase='Fikcyjny poufny tekst o pieczeniu chleba.'
        src=self.ingest(fixtures.source(text=semantic.NEW+' '+phrase))
        raw=semantic.attendance();raw.update(description=phrase,source_quote=src['text'])
        raw['recipient']={'scope':'unknown','learner_id':None,'class_id':None,'evidence':''}
        past=fixtures.NOW+timedelta(days=20)
        result=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,past)
        aid=result['outcomes'][0]['action_id']
        with self.store.db:self.store.db.execute('UPDATE sources SET content_since=?',(past.isoformat(),))
        prune_state(self.store,apply=True,current=past+timedelta(days=31))
        saved=self.store.db.execute('SELECT payload FROM canonical_actions WHERE id=?',(aid,)).fetchone()
        self.assertNotIn(phrase,saved[0]);diagnostic=self.store.get_action(aid)
        self.assertTrue(diagnostic['payload']['expired_without_authority'])
        self.assertEqual(diagnostic['payload']['description'],'');self.assertEqual(diagnostic['payload']['source_quote'],'')
        self.assertEqual(diagnostic['original_evidence'],[]);self.assertEqual(diagnostic['evidence'],[])
        self.assertNotIn(aid,[x['id']for x in self.store.candidate_actions(fixtures.action())])
        self.assertEqual(self.store.get_source(src['id'])['text'],'')

    def test_pruning_only_corroborating_source_cannot_minimize_the_human_target(self):
        aid,old,src,raw=self.setup_owned()
        SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW)
        before=self.protected();clock=fixtures.NOW+timedelta(days=31)
        with self.store.db:
            self.store.db.execute('UPDATE sources SET content_since=?',(clock.isoformat(),))
            self.store.db.execute('UPDATE sources SET content_since=? WHERE id=?',(fixtures.NOW.isoformat(),src['id']))
        self.store.prune_source_content(clock,apply=True)
        self.assertEqual(before,self.protected())


if __name__=='__main__':unittest.main()

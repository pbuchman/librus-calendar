"""Unconfirmed participation cannot reopen or approve an existing closed offer."""
from copy import deepcopy
import json
import unittest

from app.codex_runtime import event_fingerprint
from app.source_planner import SourcePlanner
import test_source_planner as fixtures

TEXT='Klasa 1x: trening piłkarski, grupa szkolna. Do wyboru jeden termin.'


def offers(count,*,conditional=False,markers=True,text=TEXT,object='trening piłkarski'):
    items=[fixtures.action(title=object,start='2030-03-%02d'%(10+i),temporal_kind='event',activity_scope='extracurricular',optional=False,
        source_quote=text,alternative_group='offer'if markers else None,alternative_group_evidence=text if markers else None,
        action_identity={'action_type':'attend','object':object,'occasion':'grupa szkolna','evidence':text},
        offer_identity={'label':object,'evidence':text})for i in range(count)]
    if conditional:
        for item in items:item['recipient']={'scope':'conditional','condition':'participation','learner_id':None,'class_id':None,'evidence':'Klasa 1x'}
    return items


class OfferRuntime(fixtures.Runtime):
    def match_source_action(self,src,item,candidates):
        chosen=next((x for x in candidates if x['payload'].get('start')==item.get('start')),None)
        return {'decision':'distinct'}if chosen is None else{'decision':'match','action_id':chosen['id'],'source_quote':TEXT,'candidate_quote':TEXT,'anchors':['trening piłkarski','grupa szkolna']}


class ClosedParticipationTests(unittest.TestCase):
    setUp=fixtures.SourcesTests.setUp
    tearDown=fixtures.SourcesTests.tearDown
    ingest=fixtures.SourcesTests.ingest

    def initial(self,selected=False,applied=False,legacy=False):
        src=self.ingest(fixtures.source(text=TEXT))
        if legacy:
            self.store.save_message({'id':'fictional-legacy-offer','subject':'Trening piłkarski','text':TEXT,'sent_at':fixtures.NOW.isoformat()})
            items=offers(5)
            for item in items:
                for key in ('action_identity','recipient','offer_identity','date_certainty','informational'):item.pop(key,None)
            self.store.save_proposals('fictional-legacy-offer',items,fixtures.NOW)
        else:SourcePlanner(self.store,OfferRuntime(offers(5))).analyze(src,fixtures.NOW)
        group=self.store.list_proposal_groups()[0]
        self.store.resolve_proposal_group(group['id'],'select'if selected else'reject',group['members'][0]['id']if selected else None,
            expected_revision=group['revision'],current=fixtures.NOW)
        if selected:
            operation=self.store.queue_operations(fixtures.NOW)[0]
            if applied:
                snapshot=dict(operation['event'],id='fictional-chosen-event')
                self.store.record_result(operation['operation_id'],{'status':'applied','event_id':snapshot['id'],'snapshot':snapshot,
                    'fingerprint':event_fingerprint(snapshot,self.store.config.calendar_owner)})
        return group

    def repeat(self,count=5,markers=True,**changes):
        src=self.ingest(fixtures.source('new-offer-channel',kind='school_event',text=TEXT))
        items=offers(count,conditional=True,markers=markers)
        for item in items:item.update(deepcopy(changes))
        return SourcePlanner(self.store,OfferRuntime(items)).analyze(src,fixtures.NOW)

    def protected(self):
        return {name:[tuple(row)for row in self.store.db.execute('SELECT * FROM '+name)]
            for name in ('canonical_actions','proposals','decision_intents','canonical_decisions','events','operations','proposal_groups','proposal_group_members')}

    def test_rejected_five_repeat_retains_all_decisions_without_new_group(self):
        self.initial();before=self.protected();result=self.repeat()
        self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(before,self.protected());self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_missing_markers_still_preserve_each_exact_rejected_member(self):
        self.initial();before=self.protected();result=self.repeat(markers=False)
        self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(before,self.protected());self.assertEqual(len(self.store.list_proposal_groups()),1)

    def test_proved_same_closed_offer_holds_new_sixth_option(self):
        self.initial();result=self.repeat(6)
        self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(len(self.store.list_proposal_groups()),1);self.assertEqual(self.store.list_proposals('review'),[])
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_missing_membership_marker_blocks_sixth_for_reconciliation(self):
        group=self.initial();result=self.repeat(6,markers=False)
        self.assertEqual([x['outcome']for x in result['outcomes']],['ignored']*5+['review'])
        sixth=self.store.list_proposals('review')[0];self.assertEqual(sixth['blocked_by_offer_group'],group['id'])
        with self.assertRaises(ValueError):self.store.resolve_proposal(sixth['id'],'approve')
        self.assertEqual(len(self.store.list_proposal_groups()),1);self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_selected_pending_offer_is_byte_preserved_even_without_markers(self):
        self.initial(selected=True);before=self.protected();result=self.repeat(markers=False)
        self.assertEqual([x['outcome']for x in result['outcomes']].count('linked'),1)
        self.assertEqual([x['outcome']for x in result['outcomes']].count('ignored'),4)
        self.assertEqual(before,self.protected());self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(len(self.store.pending_operations()),1)

    def test_selected_applied_offer_does_not_create_another_operation(self):
        self.initial(selected=True,applied=True);before=self.protected();self.repeat()
        self.assertEqual(before,self.protected());self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_genuinely_new_unconfirmed_offer_remains_review(self):
        self.initial();text=TEXT.replace('trening piłkarski','warsztaty plastyczne')
        src=self.ingest(fixtures.source('different-offer',text=text))
        result=SourcePlanner(self.store,OfferRuntime(offers(5,conditional=True,text=text,object='warsztaty plastyczne'))).analyze(src,fixtures.NOW)
        self.assertTrue(all(x['outcome']=='review'for x in result['outcomes']))
        self.assertEqual(len(self.store.list_proposals('review')),5);self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_genuine_legacy_rejected_offer_keeps_original_untyped_decisions(self):
        self.initial(legacy=True);before=self.protected();result=self.repeat(markers=False)
        self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(before,self.protected());self.assertEqual(len(self.store.list_proposal_groups()),1)
        for row in self.store.db.execute('SELECT payload FROM canonical_actions'):
            self.assertNotIn('action_identity',json.loads(row[0]))
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_genuine_legacy_selected_member_and_original_operation_are_unchanged(self):
        self.initial(legacy=True,selected=True);before=self.protected();result=self.repeat()
        self.assertEqual([x['outcome']for x in result['outcomes']].count('linked'),1)
        self.assertEqual(before,self.protected());self.assertEqual(self.store.queue_operations(fixtures.NOW),[])
        self.assertEqual(len(self.store.pending_operations()),1)

    def test_genuine_legacy_closed_offer_suppresses_proven_sixth_choice(self):
        self.initial(legacy=True);result=self.repeat(6)
        self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(len(self.store.list_proposal_groups()),1);self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_genuine_legacy_sixth_without_complete_membership_stays_blocked(self):
        group=self.initial(legacy=True);result=self.repeat(6,markers=False)
        self.assertEqual([x['outcome']for x in result['outcomes']],['ignored']*5+['review'])
        sixth=self.store.list_proposals('review')[0]
        self.assertEqual(sixth['blocked_by_offer_group'],group['id'])
        with self.assertRaises(ValueError):self.store.resolve_proposal(sixth['id'],'approve')
        self.assertEqual(len(self.store.list_proposal_groups()),1);self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_competing_same_slot_legacy_action_is_not_hidden_by_closed_group(self):
        self.initial(legacy=True)
        self.store.save_message({'id':'fictional-competing-offer','subject':'Trening piłkarski','text':TEXT,'sent_at':fixtures.NOW.isoformat()})
        item=offers(1,markers=False)[0];item['needs_review']=True
        for key in ('action_identity','recipient','offer_identity','date_certainty','informational'):item.pop(key,None)
        self.store.save_proposals('fictional-competing-offer',[item],fixtures.NOW)
        result=self.repeat()
        self.assertEqual([x['outcome']for x in result['outcomes']],['review']+['ignored']*4)
        fresh=next(x for x in self.store.list_proposals('review')if x.get('closed_offer_identity_uncertain'))
        self.assertTrue(fresh['blocked_by_offer_group'])
        with self.assertRaises(ValueError):self.store.resolve_proposal(fresh['id'],'approve')
        self.assertEqual(len(self.store.list_proposal_groups()),1);self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_role_or_participation_condition_is_guarded_for_every_broad_scope(self):
        for scope in ('all','class','learner'):
            for condition in ('role','participation'):
                with self.subTest(scope=scope,condition=condition):
                    recipient={'scope':scope,'class_id':'1x','learner_id':fixtures.LEARNER['id'],
                               'condition':condition,'evidence':'Klasa 1x'}
                    src=self.ingest(fixtures.source('contradictory-'+scope+'-'+condition,text=TEXT))
                    item=offers(1,markers=False)[0];item['recipient']=recipient
                    result=SourcePlanner(self.store,OfferRuntime([item])).analyze(src,fixtures.NOW)
                    self.assertEqual(result['outcomes'][0]['outcome'],'ignored'if condition=='role'else'review')
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_legacy_paraphrase_uses_independent_original_quote_verification_only(self):
        self.initial(legacy=True);before=self.protected();text=TEXT+' Uczestnicy trenują wspólnie.'
        src=self.ingest(fixtures.source('legacy-paraphrase',text=text))
        items=offers(5,conditional=True,text=text)
        class Independent(OfferRuntime):
            def __init__(inner):super().__init__(items);inner.verifications=0
            def verify_source_action(inner,source,item,candidates):
                inner.verifications+=1
                chosen=next(a for a in candidates if a['payload']['start']==item['start'])
                proof=chosen['original_evidence'][0]
                return {'decision':'match','action_id':chosen['id'],'evidence_id':proof['id'],
                    'source_quote':text,'candidate_quote':proof['text'],
                    'source_purpose_field':'text','source_purpose_quote':'trening piłkarski',
                    'same_purpose':True,'same_occasion':True,'recipient_compatible':True,'unique_target':True}
        runtime=Independent();result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW)
        self.assertEqual(runtime.verifications,5);self.assertTrue(all(x['outcome']=='ignored'for x in result['outcomes']))
        self.assertEqual(before,self.protected());self.assertEqual(self.store.queue_operations(fixtures.NOW),[])

    def test_legacy_closed_verification_rechecks_decision_cas_for_preview_and_active(self):
        group=self.initial(legacy=True);text=TEXT+' Uczestnicy trenują wspólnie.'
        src=self.ingest(fixtures.source('legacy-race',text=text));items=offers(5,conditional=True,text=text)
        case=self
        class Racing(OfferRuntime):
            def verify_source_action(inner,source,item,candidates):
                chosen=next(a for a in candidates if a['payload']['start']==item['start']);proof=chosen['original_evidence'][0]
                with case.store.db:case.store.db.execute('UPDATE proposal_groups SET revision=revision+1 WHERE id=?',(group['id'],))
                return {'decision':'match','action_id':chosen['id'],'evidence_id':proof['id'],
                    'source_quote':text,'candidate_quote':proof['text'],'source_purpose_field':'text','source_purpose_quote':'trening piłkarski',
                    'same_purpose':True,'same_occasion':True,'recipient_compatible':True,'unique_target':True}
        for preview in (True,False):
            with self.assertRaises(ValueError):SourcePlanner(self.store,Racing(items)).analyze(src,fixtures.NOW,preview=preview)
            self.assertFalse(self.store.db.execute('SELECT 1 FROM action_sources WHERE source_id=?',(src['id'],)).fetchone())

    def test_closed_decision_research_refuses_role_wrong_class_and_uncertain_date(self):
        self.initial(legacy=True)
        invalid=[{'recipient':{'scope':'conditional','condition':'role','class_id':None,'learner_id':None,'evidence':'Klasa 1x'}},
                 {'recipient':{'scope':'conditional','condition':'participation','class_id':'2z','learner_id':None,'evidence':'Klasa 1x'}},
                 {'date_certainty':'uncertain'}]
        for index,changes in enumerate(invalid):
            src=self.ingest(fixtures.source('invalid-participation-'+str(index),text=TEXT))
            item=offers(1,conditional=True,markers=False)[0];item.update(changes)
            SourcePlanner(self.store,OfferRuntime([item])).analyze(src,fixtures.NOW)
            self.assertFalse(self.store.db.execute("SELECT 1 FROM action_sources WHERE source_id=? AND relation='retains_closed_offer_decision'",(src['id'],)).fetchone())
        src=self.ingest(fixtures.source('unanchored-inferred-offer',text=TEXT,published_at=None,updated_at=None))
        item=offers(1,conditional=True,markers=False)[0];item['date_certainty']='inferred'
        SourcePlanner(self.store,OfferRuntime([item])).analyze(src,fixtures.NOW)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM action_sources WHERE source_id=? AND relation='retains_closed_offer_decision'",(src['id'],)).fetchone())
        self.assertEqual(self.store.queue_operations(fixtures.NOW),[])


if __name__=='__main__':unittest.main()

"""Fictional current previews supply bound quotes, never model proof authority."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock, patch

from app.core import digest
from app.codex_runtime import event_fingerprint
from app.source_planner import SourcePlanner, prepare_source_proposals
from app.source_preview import SOURCE_PREVIEW_PLANNER_VERSION
from app.sync import _PreviewBudget, _PreviewRuntime
import test_source_evidence as evidence
import test_source_planner as fixtures
import test_source_recovery as recovery
import test_source_semantics as semantic
import test_source_offers as offers

VIRTUAL = 'Klasa 1x ma zmianę godzin lekcji 12 marca.'


class TargetRecovery(recovery.RecoveryRuntime):
    def __init__(self, item, target):
        super().__init__([item]); self.target = target
        self.execute = Mock(side_effect=AssertionError('Preview cannot write Calendar'))

    def verify_source_action(self, src, item, candidates):
        self.calls.append(('verify', len(candidates)))
        self.verification_inputs.append(deepcopy(candidates))
        selected = next(action for action in candidates if action['id'] == self.target)
        selected = dict(selected, original_evidence=selected.get('original_evidence', []) + selected.get('reconstructed_evidence', []))
        return semantic.confirmation(src, item, [selected])


class VirtualEvidenceTests(unittest.TestCase):
    setUp = evidence.SourceEvidenceTests.setUp
    tearDown = evidence.SourceEvidenceTests.tearDown
    ingest = evidence.SourceEvidenceTests.ingest
    seed = evidence.SourceEvidenceTests.seed
    setup_dayoff = recovery.OriginRecoveryTests.setup_dayoff

    def virtual(self, same=False):
        text = recovery.DAYOFF if same else VIRTUAL
        src = self.ingest(fixtures.source('virtual-origin', text=text), mode='preview')
        item = fixtures.action(title='Dzień wolny od lekcji' if same else 'Zmiana godzin lekcji',
            source_quote=text, temporal_kind='event', start='2030-03-12', end='2030-03-13', optional=False,
            action_identity={'action_type':'inform','object':'dzień wolny' if same else 'zmiana godzin lekcji',
                             'occasion':'brak lekcji' if same else 'lekcje','evidence':text})
        result = SourcePlanner(self.store, fixtures.Runtime([item])).analyze(src, fixtures.NOW, preview=True)
        self.virtual_source_id=src['id']
        return src, result['outcomes'][0]['action_id']

    def protected(self):
        return {table:[tuple(row) for row in self.store.db.execute('SELECT * FROM '+table+' ORDER BY 1')]
                for table in ('proposals','operations','events','decision_intents','proposal_groups',
                              'proposal_group_members','canonical_actions','canonical_decisions','action_sources')}

    def candidates(self, src, item):
        planner = SourcePlanner(self.store, fixtures.Runtime())
        virtual = planner._virtual_candidates(src,fixtures.NOW)
        prepared = prepare_source_proposals(src, [item], fixtures.LEARNER, fixtures.NOW)[0]
        return self.store.candidate_actions(prepared, additional=virtual)

    def corrupt(self, change):
        row = self.store.db.execute('SELECT * FROM source_previews WHERE source_id=?',(self.virtual_source_id,)).fetchone()
        payload = json.loads(row['payload']); change(payload)
        with self.store.db:
            self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?', (json.dumps(payload), row['id']))

    def regenerate_id(self,payload):
        action=payload['outcomes'][0]['action'];item=action['payload']
        aid=digest(['source-action',self.virtual_source_id,item['source_hash'],0,item])[:32]
        action.update(id=aid,proposal_id=aid);payload['outcomes'][0].update(action_id=aid,proposal_id=aid)
        return aid

    def test_current_virtual_quote_unblocks_legacy_recovery_and_full_independent_verification(self):
        origin, virtual_id = self.virtual()
        target, old, src, item = self.setup_dayoff()
        self.store.resolve_proposal(target, 'approve')
        event={'id':'fictional-owned-dayoff','summary':'Dzień wolny od lekcji',
               'start':{'date':'2030-03-12'},'end':{'date':'2030-03-13'},'description':'librus-calendar:fictional-dayoff'}
        with self.store.db:self.store.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',
            (event['id'],self.store.calendar_id,old['upstream_id'],target,json.dumps(event),
             event_fingerprint(event,self.store.config.calendar_owner),'librus-calendar:fictional-dayoff',fixtures.NOW.isoformat(),'active'))
        before = self.protected()
        runtime = TargetRecovery(item, target); budget = _PreviewBudget(60, 31)
        result = SourcePlanner(self.store, _PreviewRuntime(runtime, budget)).analyze(src, fixtures.NOW, preview=True)
        self.assertEqual((result['outcomes'][0]['action_id'],result['outcomes'][0]['outcome']), (target,'linked'))
        self.assertEqual(result['outcomes'][0]['action']['event_ids'],['fictional-owned-dayoff'])
        self.assertEqual(budget.calls, 4)
        self.assertIn(('recover',1),runtime.calls); self.assertIn(('verify',2),runtime.calls)
        virtual = next(action for action in runtime.verification_inputs[0] if action['id']==virtual_id)
        self.assertEqual(virtual['original_evidence'],self.store._original_action_evidence(virtual_id,virtual['source_links']))
        self.assertEqual(virtual['original_evidence'][0]['text'],VIRTUAL)
        self.assertEqual(virtual['original_evidence'][0]['source_hash'],origin['content_hash'])
        self.assertEqual(before,self.protected()); runtime.execute.assert_not_called()
        diagnostic = self.store.list_source_previews()[0]['analysis_metadata']['matching_diagnostics'][0]
        self.assertEqual(diagnostic['primary_validation'],'model_uncertain')
        self.assertEqual((diagnostic['missing_original_count'],diagnostic['unrecoverable_count']), (1,0))
        self.assertEqual((diagnostic['recovery'],diagnostic['verification']), ('completed','accepted'))

    def test_serialized_proof_fields_are_ignored_and_rehydrated_from_own_source(self):
        origin, virtual_id = self.virtual(); target, old, src, item = self.setup_dayoff()
        poison = 'fictional unregistered proof'
        def forged(payload):
            action=payload['outcomes'][0]['action']
            action.update(evidence=[{'text':poison}],original_evidence=[{'text':poison}],
                          reconstructed_evidence=[{'text':poison}],recovery_origin={'body':poison})
        self.corrupt(forged)
        action=next(value for value in self.candidates(src,item)if value['id']==virtual_id)
        self.assertEqual(action['original_evidence'][0]['text'],VIRTUAL)
        self.assertNotIn(poison,json.dumps(action))
        self.assertFalse(action.get('recovery_origin')); self.assertFalse(action.get('reconstructed_evidence'))

    def test_forged_or_ambiguous_virtual_binding_stays_candidate_without_proof(self):
        origin, virtual_id = self.virtual(); target, old, src, item = self.setup_dayoff()
        row=self.store.db.execute('SELECT * FROM source_previews').fetchone(); original=json.loads(row['payload'])
        def quote(payload):payload['outcomes'][0]['action']['source_links'][0]['source_quote']='fictional invented quotation'
        def hashed(payload):payload['outcomes'][0]['action']['source_links'][0]['source_hash']='wrong'
        def identity(payload):payload['outcomes'][0]['action']['payload']['source_id']='fictional-other-source'
        def ambiguous(payload):payload['outcomes'][0]['action']['source_links']*=2
        def action_binding(payload):payload['outcomes'][0]['action']['payload']['title']='fictional replacement title'
        for change in (quote,hashed,identity,ambiguous,action_binding):
            with self.subTest(change=change.__name__):
                with self.store.db:self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?',(json.dumps(original),row['id']))
                self.corrupt(change)
                action=next(value for value in self.candidates(src,item)if value['id']!=target)
                self.assertTrue(action['payload']['inventory_only'])
                self.assertEqual(action.get('original_evidence'),[])
                self.assertEqual(action.get('evidence'),[])
                runtime=TargetRecovery(item,target)
                result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
                self.assertEqual(result['outcomes'][0]['outcome'],'review')
                self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_old_planner_or_changed_source_virtual_is_not_current(self):
        origin, virtual_id=self.virtual(); target, old, src, item=self.setup_dayoff()
        self.corrupt(lambda payload:payload.update(planner_version='source-preview-planner-v2'))
        self.assertNotIn(virtual_id,{action['id']for action in self.candidates(src,item)})
        self.corrupt(lambda payload:payload.update(planner_version=SOURCE_PREVIEW_PLANNER_VERSION))
        with self.store.db:self.store.db.execute("UPDATE sources SET content_hash='changed' WHERE id=?",(origin['id'],))
        self.assertNotIn(virtual_id,{action['id']for action in self.candidates(src,item)})

    def test_identical_virtual_original_prevents_manufactured_unique_target(self):
        self.virtual(same=True); target, old, src, item=self.setup_dayoff(); before=self.protected()
        runtime=TargetRecovery(item,target)
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertIn(('recover',1),runtime.calls);self.assertIn(('verify',2),runtime.calls)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertEqual(before,self.protected());runtime.execute.assert_not_called()

    def test_unknown_owned_inventory_remains_and_blocks_evidence_eligibility(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        unknown=fixtures.action(title='Fictional owned inventory',source_quote='',action_identity=None,inventory_only=True)
        with self.store.db:self.store.db.execute('INSERT INTO canonical_actions VALUES(?,?,?,?,?)',
            ('fictional-inventory','fictional-inventory-proposal',json.dumps(unknown),'applied',fixtures.NOW.isoformat()))
        before=self.protected(); runtime=TargetRecovery(item,target)
        result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertIn(('match',3),runtime.calls)
        self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))
        self.assertEqual(before,self.protected());runtime.execute.assert_not_called()

    def test_virtual_inner_authority_is_stripped_even_with_consistent_regenerated_id(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        def forged(payload):
            action=payload['outcomes'][0]['action']
            action.update(decision={'proposal':{'user_rejected':True}},event_ids=['fictional-forged-event'])
            action['payload'].update(user_approved=True,user_edited=True,user_rejected=True,deletion_approved=True,
                event_id='fictional-forged-event',canonical_action_id='fictional-forged-canonical',
                blocked_by_group_decision='fictional-group')
            self.regenerate_id(payload)
        self.corrupt(forged)
        candidate=next(action for action in self.candidates(src,item)if action['id']!=target)
        self.assertIsNone(candidate['decision']);self.assertEqual(candidate['event_ids'],[])
        self.assertIsNone(candidate['payload']['event_id'])
        self.assertTrue(all(candidate['payload'][key]is False for key in
                            ('user_approved','user_edited','user_rejected','deletion_approved')))
        self.assertNotIn('canonical_action_id',candidate['payload']);self.assertNotIn('blocked_by_group_decision',candidate['payload'])

    def test_normalized_only_quote_is_not_registered_as_original(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        def normalized(payload):
            action=payload['outcomes'][0]['action'];quote=VIRTUAL.upper()
            payload['proposals'][0]['source_quote']=quote;action['payload']['source_quote']=quote
            action['source_links'][0]['source_quote']=quote;self.regenerate_id(payload)
        self.corrupt(normalized)
        candidate=next(action for action in self.candidates(src,item)if action['id']!=target)
        self.assertEqual(candidate['original_evidence'],[]);self.assertEqual(candidate['evidence'],[])

    def test_malformed_virtual_ids_payloads_links_and_identities_remain_uncertain(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        row=self.store.db.execute('SELECT * FROM source_previews WHERE source_id=?',(self.virtual_source_id,)).fetchone()
        original=json.loads(row['payload'])
        def missing_id(payload):payload['outcomes'][0]['action'].pop('id')
        def malformed_id(payload):payload['outcomes'][0]['action']['id']=[]
        def missing_payload(payload):payload['outcomes'][0]['action'].pop('payload')
        def malformed_payload(payload):payload['outcomes'][0]['action']['payload']=[]
        def links(payload):payload['outcomes'][0]['action']['source_links']=[None]
        def identity(payload):payload['outcomes'][0]['action']['payload']['action_identity']=[]
        for change in (missing_id,malformed_id,missing_payload,malformed_payload,links,identity):
            with self.subTest(change=change.__name__):
                with self.store.db:self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?',(json.dumps(original),row['id']))
                self.corrupt(change)
                candidates=self.candidates(src,item)
                self.assertEqual(len(candidates),2)
                candidate=next(action for action in candidates if action['id']!=target)
                self.assertEqual(candidate['original_evidence'],[])
                runtime=TargetRecovery(item,target);result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
                self.assertEqual(result['outcomes'][0]['outcome'],'review')
                self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_duplicate_claimed_id_with_different_payloads_does_not_erase_ambiguity(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        def duplicates(payload):
            duplicate=deepcopy(payload['outcomes'][0]);duplicate['action']['payload']['title']='Fictional conflicting lekcji'
            payload['outcomes'].append(duplicate);payload['proposals'].append(deepcopy(payload['proposals'][0]))
        self.corrupt(duplicates)
        candidates=self.candidates(src,item)
        self.assertEqual(len(candidates),3);self.assertEqual(len({value['id']for value in candidates}),3)
        self.assertTrue(all(not value['original_evidence']for value in candidates))
        runtime=TargetRecovery(item,target);result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_origin_proof_wins_after_refresh_reorders_linked_alias(self):
        origin,virtual_id=self.virtual()
        alias=self.ingest(fixtures.source('virtual-alias',text=VIRTUAL))
        original_payload=json.loads(self.store.db.execute('SELECT payload FROM source_previews').fetchone()[0])
        raw=deepcopy(original_payload['proposals'][0]);raw.update(source_id=alias['id'],source_hash=alias['content_hash'])
        action=deepcopy(original_payload['outcomes'][0]['action'])
        action['source_links'].append({'source_id':alias['id'],'source_hash':alias['content_hash'],'source_quote':VIRTUAL,'relation':'linked'})
        action.update(decision={'proposal':{'user_approved':True}},event_ids=['fictional-forged-event'])
        self.store.save_source_preview(alias['id'],[raw],outcomes=[{'action_id':virtual_id,'proposal_id':virtual_id,'outcome':'linked','action':action}])
        with self.store.db:self.store.db.execute("UPDATE source_previews SET created_at='2099-01-01' WHERE source_id=?",(origin['id'],))
        target,old,src,item=self.setup_dayoff()
        candidates=self.candidates(src,item);self.assertEqual(len(candidates),2)
        candidate=next(value for value in candidates if value['id']==virtual_id)
        self.assertEqual(candidate['original_evidence'][0]['source_id'],origin['id'])
        self.assertIsNone(candidate['decision']);self.assertEqual(candidate['event_ids'],[])

    def test_diagnostics_distinguish_rejected_primary_proof_from_model_uncertainty(self):
        self.virtual();target,old,src,item=self.setup_dayoff();runtime=TargetRecovery(item,target)
        runtime.match_source_action=lambda *_:{'decision':'match','action_id':target,'source_quote':recovery.DAYOFF,
            'candidate_quote':'fictional unmatched private proof','anchors':['fictional','proof'],
            'reason':'fictional response must not be retained'}
        SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        diagnostic=self.store.list_source_previews()[0]['analysis_metadata']['matching_diagnostics'][0]
        self.assertEqual((diagnostic['primary_response'],diagnostic['primary_validation']),('match','candidate_evidence_not_unique'))
        self.assertEqual((diagnostic['recovery'],diagnostic['verification']),('completed','accepted'))
        serialized=json.dumps(diagnostic)
        self.assertNotIn(target,serialized);self.assertNotIn('fictional',serialized);self.assertNotIn(recovery.DAYOFF,serialized)
        self.assertTrue(all(isinstance(value,(str,int,bool))for value in diagnostic.values()))

    def test_persisted_authority_always_wins_over_serialized_virtual_authority(self):
        origin,virtual_id=self.virtual();target,old,src,item=self.setup_dayoff()
        payload=fixtures.action(title='Actual persisted lekcji',inventory_only=True,event_id='fictional-real-event',user_edited=True)
        decision={'proposal':{'user_edited':True,'title':'Actual persisted lekcji'}}
        with self.store.db:
            self.store.db.execute('INSERT INTO canonical_actions VALUES(?,?,?,?,?)',
                (virtual_id,'fictional-persisted-proposal',json.dumps(payload),'applied',fixtures.NOW.isoformat()))
            self.store.db.execute('INSERT INTO canonical_decisions VALUES(?,?,?)',
                (virtual_id,json.dumps(decision),fixtures.NOW.isoformat()))
        before=self.protected()
        expected=self.store.get_action(virtual_id)
        candidate=next(value for value in self.candidates(src,item)if value['id']==virtual_id)
        self.assertEqual(candidate,expected);self.assertEqual(candidate['event_ids'],['fictional-real-event'])
        self.assertEqual(candidate['decision'],decision);self.assertEqual(before,self.protected())

    def test_conflicting_id_from_another_origin_is_a_separate_proofless_candidate(self):
        origin,virtual_id=self.virtual()
        alias=self.ingest(fixtures.source('conflicting-origin',text=VIRTUAL))
        original=json.loads(self.store.db.execute('SELECT payload FROM source_previews').fetchone()[0])
        raw=deepcopy(original['proposals'][0]);raw.update(source_id=alias['id'],source_hash=alias['content_hash'])
        action=deepcopy(original['outcomes'][0]['action']);action['payload']=raw
        action['source_links']=[{'source_id':alias['id'],'source_hash':alias['content_hash'],'source_quote':VIRTUAL,'relation':'added'}]
        self.store.save_source_preview(alias['id'],[raw],outcomes=[{'action_id':virtual_id,'proposal_id':virtual_id,'outcome':'added','action':action}])
        target,old,src,item=self.setup_dayoff()
        candidates=self.candidates(src,item)
        self.assertEqual(len(candidates),3)
        unknown=next(value for value in candidates if value['id']not in {virtual_id,target})
        self.assertTrue(unknown['payload']['inventory_only']);self.assertEqual(unknown['original_evidence'],[])
        runtime=TargetRecovery(item,target);result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_corruption_cannot_make_identical_virtual_proof_disappear_or_prove_distinct(self):
        self.virtual(same=True);target,old,src,item=self.setup_dayoff()
        row=self.store.db.execute('SELECT * FROM source_previews WHERE source_id=?',(self.virtual_source_id,)).fetchone()
        original=json.loads(row['payload'])
        baseline=SourcePlanner(self.store,TargetRecovery(item,target)).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(baseline['outcomes'][0]['outcome'],'review')
        def absent(payload):payload.pop('outcomes')
        def empty(payload):payload['outcomes']=[]
        def malformed(payload):payload['outcomes']=None
        def collision(payload):
            action=payload['outcomes'][0]['action'];action.update(id=target,proposal_id=target)
            payload['outcomes'][0].update(action_id=target,proposal_id=target)
        def temporal(payload):payload['outcomes'][0]['action']['payload']['temporal_kind']='deadline'
        def identity(payload):payload['outcomes'][0]['action']['payload']['action_identity']['action_type']='bring'
        def expiry(payload):payload['outcomes'][0]['action']['payload'].update(expired_without_authority=True,temporal_kind='deadline')
        def ignored(payload):payload['outcomes'][0]['action']['payload']['kind']='ignore'
        for change in (absent,empty,malformed,collision,temporal,identity,expiry,ignored):
            with self.subTest(change=change.__name__):
                with self.store.db:self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?',(json.dumps(original),row['id']))
                self.corrupt(change);candidates=self.candidates(src,item)
                self.assertEqual(len(candidates),2)
                unknown=next(value for value in candidates if value['id']!=target)
                self.assertTrue(unknown['payload']['inventory_only']);self.assertNotIn('expired_without_authority',unknown['payload'])
                runtime=TargetRecovery(item,target);result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
                self.assertEqual(result['outcomes'][0]['outcome'],'review')
                self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_short_outcomes_conserve_remaining_nonignore_prepared_actions_only(self):
        self.virtual();target,old,src,item=self.setup_dayoff()
        def shorter(payload):
            payload['proposals'].append(deepcopy(payload['proposals'][0]))
            payload['proposals'].append(dict(payload['proposals'][0],kind='ignore'))
        self.corrupt(shorter);candidates=self.candidates(src,item)
        self.assertEqual(len(candidates),3)
        self.assertEqual(sum(bool(value['payload'].get('inventory_only'))for value in candidates),1)
        self.corrupt(lambda payload:payload.update(proposals=[],outcomes=[]))
        self.assertEqual({value['id']for value in self.candidates(src,item)},{target})
        self.corrupt(lambda payload:payload.update(proposals=[dict(item,kind='ignore')],outcomes=[]))
        self.assertEqual({value['id']for value in self.candidates(src,item)},{target})

    def test_alias_without_current_own_quote_link_cannot_hide_its_prepared_obligation(self):
        origin,virtual_id=self.virtual(same=True)
        alias=self.ingest(fixtures.source('unbound-alias',text=recovery.DAYOFF))
        original=json.loads(self.store.db.execute('SELECT payload FROM source_previews').fetchone()[0])
        raw=deepcopy(original['proposals'][0]);raw.update(source_id=alias['id'],source_hash=alias['content_hash'])
        raw['action_identity']['object']='Fictional distinct obligation'
        action=deepcopy(original['outcomes'][0]['action'])
        self.store.save_source_preview(alias['id'],[raw],outcomes=[{'action_id':virtual_id,'proposal_id':virtual_id,'outcome':'linked','action':action}])
        target,old,src,item=self.setup_dayoff();candidates=self.candidates(src,item)
        self.assertEqual(len(candidates),3)
        self.assertEqual(sum(bool(value['payload'].get('inventory_only'))for value in candidates),1)
        runtime=TargetRecovery(item,target);result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertFalse(any(call[0]in ('recover','verify')for call in runtime.calls))

    def test_genuine_persisted_alias_collapses_only_to_exact_current_authority(self):
        origin,virtual_id=self.virtual()
        self.store.config=replace(self.store.config,multi_source_mode='active')
        raw=json.loads(self.store.db.execute('SELECT payload FROM source_previews').fetchone()[0])['proposals'][0]
        SourcePlanner(self.store,fixtures.Runtime([raw])).analyze(origin,fixtures.NOW)
        actual=self.store.get_action(virtual_id);self.assertIsNotNone(actual)
        alias=self.ingest(fixtures.source('canonical-alias',text=VIRTUAL),mode='preview')
        prepared=dict(raw,source_id=alias['id'],source_hash=alias['content_hash'])
        action=deepcopy(actual);action['source_links'].append({'source_id':alias['id'],'source_hash':alias['content_hash'],
            'source_quote':VIRTUAL,'relation':'linked'})
        self.store.save_source_preview(alias['id'],[prepared],outcomes=[{'action_id':virtual_id,'proposal_id':actual['proposal_id'],
            'outcome':'linked','action':action}])
        src=self.ingest(fixtures.source('later-query',text=VIRTUAL))
        candidates=self.candidates(src,raw)
        self.assertEqual(candidates,[self.store.get_action(virtual_id)])

    def test_unbound_virtual_cannot_supply_exclusions_to_closed_offer_lookup(self):
        self.virtual();self.corrupt(lambda payload:payload.update(outcomes=[]))
        src=self.ingest(fixtures.source('closed-query',text=offers.TEXT))
        raw=offers.offers(1,conditional=True,markers=False)[0]
        runtime=offers.OfferRuntime([raw]);runtime.verify_source_action=Mock(side_effect=AssertionError('Unbound cache'))
        with patch.object(SourcePlanner,'_closed_offer_target',side_effect=AssertionError('Unbound type is not exclusion proof')):
            result=SourcePlanner(self.store,runtime).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'review');runtime.verify_source_action.assert_not_called()
        diagnostic=self.store.list_source_previews()[0]['analysis_metadata']['matching_diagnostics'][0]
        self.assertEqual(diagnostic['primary_validation'],'unbound_preview_candidate')
        self.assertEqual(diagnostic['verification'],'ineligible_unbound_preview')

    def test_generated_corroboration_alias_keeps_only_real_target_authority(self):
        target,old,src,raw=recovery.CorroborationTests.setup_owned(self)
        before=self.protected()
        first=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW,preview=True)
        later=self.ingest(fixtures.source('later-corroboration',text=semantic.NEW,published_at='2030-02-27T10:00:00+01:00'))
        virtual=SourcePlanner(self.store,fixtures.Runtime())._virtual_candidates(later,fixtures.NOW)
        self.assertEqual(virtual,[self.store.get_action(target)])
        self.assertEqual(virtual[0]['original_evidence'][0]['source_id'],old['id'])
        second=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(later,fixtures.NOW,preview=True)
        self.assertEqual(first['outcomes'][0]['action_id'],target);self.assertEqual(second['outcomes'][0]['action_id'],target)
        self.assertEqual(second['outcomes'][0]['outcome'],'linked');self.assertEqual(before,self.protected())

    def test_corroboration_alias_cannot_override_the_existing_typed_distinct_guard(self):
        target,old,src,raw=recovery.CorroborationTests.setup_owned(self)
        identity={'action_type':'attend','object':'pasowanie','occasion':'uczniów','evidence':semantic.OLD}
        with self.store.db:
            for table in ('proposals','canonical_actions'):
                row=self.store.db.execute('SELECT payload FROM '+table+' WHERE id=?',(target,)).fetchone()
                value=json.loads(row[0]);value['action_identity']=identity
                self.store.db.execute('UPDATE '+table+' SET payload=? WHERE id=?',(json.dumps(value),target))
        first=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual(first['outcomes'][0]['action_id'],target)
        row=self.store.db.execute('SELECT * FROM source_previews WHERE source_id=?',(src['id'],)).fetchone()
        value=json.loads(row['payload']);value['proposals'][0]['action_identity']['action_type']='bring'
        with self.store.db:self.store.db.execute('UPDATE source_previews SET payload=? WHERE id=?',(json.dumps(value),row['id']))
        later=self.ingest(fixtures.source('typed-corroboration-query',text=semantic.NEW,published_at='2030-02-27T10:00:00+01:00'))
        virtual=SourcePlanner(self.store,fixtures.Runtime())._virtual_candidates(later,fixtures.NOW)
        self.assertEqual(len(virtual),1);self.assertTrue(virtual[0]['_preview_unbound'])
        self.assertNotEqual(virtual[0]['id'],target)
        before=self.protected()
        second=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(later,fixtures.NOW,preview=True)
        self.assertEqual(second['outcomes'][0]['outcome'],'review');self.assertEqual(before,self.protected())

    def test_generated_closed_offer_aliases_keep_actual_closed_decisions(self):
        offers.ClosedParticipationTests.initial(self)
        src=self.ingest(fixtures.source('closed-preview',text=offers.TEXT))
        raws=offers.offers(5,conditional=True);before=self.protected()
        first=SourcePlanner(self.store,offers.OfferRuntime(raws)).analyze(src,fixtures.NOW,preview=True)
        self.assertEqual([entry['outcome']for entry in first['outcomes']],['ignored']*5)
        later=self.ingest(fixtures.source('later-closed-preview',text=offers.TEXT))
        virtual=SourcePlanner(self.store,fixtures.Runtime())._virtual_candidates(later,fixtures.NOW)
        self.assertEqual(len(virtual),5);self.assertTrue(all(not entry.get('_preview_unbound')for entry in virtual))
        second=SourcePlanner(self.store,offers.OfferRuntime(raws)).analyze(later,fixtures.NOW,preview=True)
        self.assertEqual([entry['outcome']for entry in second['outcomes']],['ignored']*5)
        self.assertEqual({entry['action_id']for entry in first['outcomes']},{entry['action_id']for entry in second['outcomes']})
        self.assertEqual(before,self.protected())

    def test_generated_expired_fact_exclusion_is_rederived_not_trusted_as_a_flag(self):
        src=self.ingest(fixtures.source(text=semantic.NEW))
        raw=semantic.attendance();raw['recipient']={'scope':'unknown','learner_id':None,'class_id':None,'evidence':''}
        past=fixtures.NOW+timedelta(days=20)
        result=SourcePlanner(self.store,semantic.SemanticRuntime([raw])).analyze(src,past,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored')
        later=self.ingest(fixtures.source('after-expiry',text=semantic.NEW),current=past)
        before=self.protected()
        self.assertEqual(SourcePlanner(self.store,fixtures.Runtime())._virtual_candidates(later,past),[])
        self.assertEqual(before,self.protected())
        with self.store.db:self.store.db.execute('DELETE FROM source_previews')
        origin,virtual_id=self.virtual();target,old,query,item=self.setup_dayoff()
        def forged(payload):
            action=payload['outcomes'][0]['action'];action['source_links'][0]['relation']='expired_unverified_fact'
            payload['outcomes'][0]['outcome']='ignored';action['payload']['expired_without_authority']=True
            payload['proposals'][0]['expired_without_authority']=True;self.regenerate_id(payload)
        self.corrupt(forged)
        candidate=next(entry for entry in self.candidates(query,item)if entry['id']!=target)
        self.assertTrue(candidate['_preview_unbound']);self.assertTrue(candidate['payload']['inventory_only'])


if __name__=='__main__':unittest.main()

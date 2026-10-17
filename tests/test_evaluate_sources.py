"""Synthetic evaluator contracts; no provider, Calendar or private school data."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core import AppStore,WARSAW,digest
from app.source_planner import SourcePlanner
from app.source_store import source_identity
from scripts import evaluate_sources as evaluator
from test_support import TEST_CONFIG

NOW=datetime(2030,3,1,12,tzinfo=WARSAW)
LEARNER={'id':'fictional-learner','class_id':'1x'}
TEXT='Klasa 1x przygotuje papierowe serce na kiermasz wiosenny do 12 marca.'


def source(upstream='fictional-source',kind='announcement',**changes):
    result={'kind':kind,'upstream_id':upstream,'learner_id':LEARNER['id'],'title':'Fictional school notice',
            'sender':'Fictional teacher','published_at':'2030-03-01T10:00:00+01:00','updated_at':None,
            'text':TEXT,'metadata':{},'deleted':False}
    result.update(changes);return result


def action(**changes):
    result={'kind':'create','title':'Przygotować papierowe serce','description':'Fictional school action.',
            'start':'2030-03-12','end':None,'all_day':True,'temporal_kind':'deadline','due_at':None,
            'activity_scope':'school','confidence':'high','needs_review':False,'review_reason':'',
            'source_quote':TEXT,'event_id':None,'alternative_group':None,'alternative_group_evidence':None,
            'optional':False,'date_certainty':'explicit','informational':False,'offer_identity':None,
            'recipient':{'scope':'class','class_id':'1x','learner_id':None,'evidence':'Klasa 1x'},
            'action_identity':{'action_type':'submit','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':TEXT}}
    result.update(changes);return result


def matched_candidate(source,item,candidates):
    return {'decision':'match','action_id':candidates[0]['id'],'source_quote':item['source_quote'],
            'candidate_quote':candidates[0]['payload']['source_quote'],
            'anchors':['papierowe serce','kiermasz wiosenny']}


class SyntheticRuntime:
    def __init__(self,responses=None,matcher=None,on_analysis=None):
        self.responses=responses or {};self.matcher=matcher;self.on_analysis=on_analysis
        self.read_only=True;self.last_analysis_metadata=None;self.calls=[];self.context=None
    def _invoke(self,prompt,schema,tools=()):
        self.calls.append(schema)
        if schema=='source_analysis.json':return deepcopy(self.responses.get(self.context['upstream_id'],[action()]))
        return self.matcher(*self.match_context)if self.matcher else {'decision':'distinct'}
    def analyze_source(self,source,context):
        self.context=source
        if self.on_analysis:self.on_analysis()
        raw=self._invoke('fictional','source_analysis.json')
        self.last_analysis_metadata={'model':'synthetic','effort':'high','prompt_version':'synthetic-v1'}
        return raw
    def match_source_action(self,source,item,candidates):
        self.match_context=(source,item,candidates)
        return self._invoke('fictional','source_match.json')


class SyntheticVerifyingRuntime(SyntheticRuntime):
    def __init__(self,*args,verifier=None,**kwargs):
        super().__init__(*args,**kwargs);self.verifier=verifier;self.last_verification_metadata=None
    def _invoke(self,prompt,schema,tools=()):
        if schema!='source_verify.json':return super()._invoke(prompt,schema,tools)
        self.calls.append(schema)
        return self.verifier(*self.verification_context)
    def verify_source_action(self,source,item,candidates):
        self.verification_context=(source,item,candidates)
        result=self._invoke('fictional','source_verify.json')
        self.last_verification_metadata={'version':'synthetic-verification-v1',
            'input_sha256':evaluator._json_digest([source,item,candidates]),
            'result_sha256':evaluator._json_digest(result),'decision':result['decision']}
        return result


class SyntheticRecoveringRuntime(SyntheticRuntime):
    def __init__(self,*args,recovery=None,**kwargs):
        super().__init__(*args,**kwargs);self.recovery=recovery;self.last_recovery_metadata=None
    def _invoke(self,prompt,schema,tools=()):
        if schema!='source_recovery.json':return super()._invoke(prompt,schema,tools)
        self.calls.append(schema);return self.recovery(*self.recovery_context)
    def recover_source_action_evidence(self,candidates,learner_context=None):
        self.recovery_context=(candidates,learner_context)
        result=self._invoke('fictional','source_recovery.json')
        self.last_recovery_metadata={'version':'synthetic-recovery-v1',
            'input_sha256':evaluator._json_digest(self.recovery_context),'result_sha256':evaluator._json_digest(result)}
        return result


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config_path=self.root/'config.json';self.cases_path=self.root/'cases.json'
        self.write_json(self.config_path,asdict(TEST_CONFIG))
    def tearDown(self):self.temp.cleanup()
    def write_json(self,path,value):
        path.write_text(json.dumps(value,ensure_ascii=False,default=str));path.chmod(0o600)
    def document(self,case=None,**changes):
        case=case or {'id':'fictional-case','source':source(),'expected':[{'start':'2030-03-12','source_quote':TEXT}],
                      'current':NOW.isoformat(),'expected_new_operations':1,'repeat':True}
        return {'learner':LEARNER,'cases':[case],**changes}
    def run_evaluation(self,document=None,runtime=None,details=False,progress=False):
        self.write_json(self.cases_path,document or self.document())
        runtime=runtime or SyntheticRuntime()
        arguments=['--config',str(self.config_path),'--cases',str(self.cases_path)]
        if details:arguments+=['--details',str(self.root/'details.json')]
        if progress:arguments+=['--progress']
        output=io.StringIO()
        with patch('app.codex_runtime.CodexRuntime',return_value=runtime)as factory,redirect_stdout(output):
            status=evaluator.main(arguments)
        return status,[json.loads(line)for line in output.getvalue().splitlines()],factory

    def test_isolated_class_receipt_supports_observed_audience_without_fabricated_quote(self):
        from app.source_audience import AUDIENCE_KEY,mint_calendar_audience
        body=TEXT.removeprefix('Klasa 1x ');row=source(kind='school_event',text=body)
        row['metadata'][AUDIENCE_KEY]=mint_calendar_audience(row,LEARNER)
        raw=action(source_quote=body,recipient={'scope':'class','class_id':'1x','learner_id':None,'condition':'none','evidence':''},
            action_identity={'action_type':'submit','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':body})
        case={'id':'fictional-served-audience','source':row,'expected':[{'start':'2030-03-12','source_quote':body}],
              'current':NOW.isoformat(),'expected_new_operations':1,'repeat':True}
        status,outputs,_=self.run_evaluation(self.document(case),SyntheticRuntime({row['upstream_id']:[raw]}),details=True)
        self.assertEqual(status,0);self.assertEqual(outputs[-1]['results'][0]['new_local_operations'],1)
        self.assertEqual(outputs[-1]['results'][0]['repeat_model_calls'],0)

    def target_document(self,legacy=False):
        seed={'source':source('seed',kind='message'if legacy else'announcement'),
              'proposals':[action(action_identity=None)if legacy else action()],
              'current':NOW.isoformat(),'decisions':[{'index':0,'decision':'approve'}],
              'events':[{'index':0,'id':'fictional-approved-event'}]}
        if legacy:seed['legacy']=True
        return self.document({'id':'canonical-target','source':source(),
            'expected':[{'start':'2030-03-12','source_quote':TEXT}],
            'expected_outcomes':['linked'],'expected_targets':[{'seed_index':0,'proposal_index':0}],
            'expected_new_actions':0,'expected_new_proposals':0,'expected_new_operations':0,
            'expected_preserved_events':['fictional-approved-event'],'current':NOW.isoformat(),'repeat':True},
            seeds=[seed])

    def offer_document(self,grouped=True,pending=False):
        text='Klasa 1x otrzymuje ofertę szkolenia rodziców. Wybierz jeden termin: 5 lub 8 marca o 16:00.'
        items=[action(title='Szkolenie rodziców',start=f'2030-03-{day:02d}T16:00:00+01:00',
            all_day=False,temporal_kind='event',activity_scope='school'if pending else'extracurricular',
            optional=not pending,source_quote=text,
            action_identity={'action_type':'attend','object':'szkolenie rodziców','occasion':'ofertę szkolenia','evidence':text},
            offer_identity={'label':'szkolenie rodziców','evidence':text},
            alternative_group='fictional-offer'if grouped else None,
            alternative_group_evidence=text if grouped else None)for day in (5,8)]
        case={'id':'offer-case','source':source('offer',text=text),
            'expected':[{'start':item['start'],'source_quote':text}for item in items],
            'expected_outcomes':['added'if pending else'review']*2,'expected_group_sizes':[2],
            'select_offer_index':0,'expected_new_operations':1,'current':NOW.isoformat(),'repeat':True}
        tail={'id':'tail-case','source':source('tail'),'expected':[{'kind':'ignore','source_quote':TEXT}],
              'expected_outcomes':['ignored'],'expected_new_operations':0,'current':NOW.isoformat(),'repeat':True}
        return {'learner':LEARNER,'cases':[case,tail]},SyntheticRuntime({'offer':items,'tail':[action(informational=True)]})

    def legacy_offer_document(self,attachment_count=0):
        document,runtime=self.offer_document();offer=document['cases'][0]
        message_source=source('legacy-offer',kind='message',text=offer['source']['text'],metadata={'attachment_count':attachment_count})
        store=AppStore(self.root/'raw-legacy.sqlite3',config=TEST_CONFIG)
        try:
            store.save_message({'id':message_source['upstream_id'],'subject':message_source['title'],
                'sender':message_source['sender'],'sent_at':message_source['published_at'],'text':message_source['text'],
                'attachment_count':attachment_count})
            store.save_proposals(message_source['upstream_id'],runtime.responses['offer'],NOW)
            # Real legacy tables can carry a group key different from the old
            # payload marker. Those authoritative rows must not be regenerated.
            with store.db:store.db.execute("UPDATE proposal_groups SET group_key='authoritative-table-key'")
            rows={table:[dict(row)for row in store.db.execute('SELECT * FROM '+table+(
                ' ORDER BY group_id,position'if table=='proposal_group_members'else' ORDER BY id'))]
                for table in evaluator.LEGACY_ROW_LIMITS}
        finally:store.close()
        case={'id':'legacy-offer-state','source':message_source,'expected':[],
              'legacy_offer_group_index':0,'expected_group_sizes':[2],'select_offer_index':0,
              'expected_new_actions':0,'expected_new_proposals':0,'expected_new_groups':0,
              'expected_new_operations':1,'current':NOW.isoformat(),'repeat':True}
        return {'learner':LEARNER,'legacy_rows':rows,'cases':[case]}

    def test_production_analyze_records_one_call_and_repeat_is_inert(self):
        original=SourcePlanner.analyze;seen=[]
        def counted(planner,*args,**kwargs):
            seen.append(args[0]['kind']);return original(planner,*args,**kwargs)
        with patch.object(SourcePlanner,'analyze',counted):
            status,lines,factory=self.run_evaluation(details=True,progress=True)
        self.assertEqual(status,0);self.assertEqual(seen,['announcement'])
        result=lines[-1];self.assertEqual(result['model_calls'],1)
        self.assertEqual(result['calendar_writes'],0);self.assertEqual(result['calendar_reads'],0)
        self.assertEqual(result['discovery_calls'],0);self.assertTrue(factory.call_args.kwargs['read_only'])
        repeated=result['results'][0]
        self.assertEqual(repeated['repeat_new_local_operations'],0)
        self.assertEqual(repeated['repeat_model_calls'],0);self.assertTrue(repeated['repeat_state_unchanged'])
        self.assertEqual(result['provenance']['stability'],'verified')
        self.assertEqual(result['provenance']['verification_versions'],[])
        self.assertEqual(result['provenance']['recovery_versions'],[])
        for line in lines:self.assertIn('input_sha256',line['provenance'])
        details=json.loads((self.root/'details.json').read_text())
        recording=details['transcript'][0]['recording']
        self.assertEqual(recording['raw'],[action()]);self.assertEqual(len(recording['prepared']),1)
        self.assertEqual(recording['applied']['status'],'applied')
        self.assertEqual(recording['runtime_calls'][0]['method'],'analyze_source')
        self.assertNotIn(TEXT,json.dumps(lines));self.assertNotIn('fictional-case',json.dumps(lines))
        for path in self.root.glob('*.json'):self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_recording_runtime_preserves_absent_verifier_capability(self):
        runtime=SyntheticRuntime();recording=evaluator.RecordingRuntime(runtime)
        self.assertFalse(hasattr(runtime,'verify_source_action'));self.assertFalse(hasattr(recording,'verify_source_action'))
        self.assertIsNone(recording.last_verification_metadata);self.assertEqual(recording.model_calls,0)
        self.assertFalse(hasattr(recording,'recover_source_action_evidence'));self.assertIsNone(recording.last_recovery_metadata)
        self.assertEqual(recording.records,[])

    def test_recording_runtime_forwards_verifier_once_and_freezes_metadata(self):
        result={'decision':'uncertain','reason':'Fictional proof.'}
        runtime=SyntheticVerifyingRuntime(verifier=lambda *args:result)
        recording=evaluator.RecordingRuntime(runtime)
        supplied={'version':'pre-call'};recording.last_verification_metadata=supplied
        self.assertIs(runtime.last_verification_metadata,supplied)
        self.assertTrue(hasattr(recording,'verify_source_action'))
        self.assertIs(recording.verify_source_action(source(),action(),[]),result)
        self.assertEqual(runtime.calls,['source_verify.json']);self.assertEqual(recording.model_calls,1)
        recorded=recording.records[0]
        self.assertEqual(recorded['method'],'verify_source_action');self.assertEqual(recorded['result'],result)
        self.assertEqual(recorded['metadata'],runtime.last_verification_metadata)
        self.assertEqual(recorded['metadata']['result_sha256'],evaluator._json_digest(result))
        result['reason']='Changed fictional proof.';runtime.last_verification_metadata['decision']='changed'
        self.assertEqual(recorded['result']['reason'],'Fictional proof.')
        self.assertEqual(recorded['metadata']['decision'],'uncertain')

    def test_recording_runtime_forwards_recovery_once_with_optional_context_and_frozen_evidence(self):
        for include_context in (False,True):
            with self.subTest(include_context=include_context):
                result={'facts':[{'source_quote':'Fictional historical source proof.'}]}
                runtime=SyntheticRecoveringRuntime(recovery=lambda *args:result)
                recording=evaluator.RecordingRuntime(runtime)
                supplied={'version':'pre-call'};recording.last_recovery_metadata=supplied
                self.assertIs(runtime.last_recovery_metadata,supplied)
                candidates=[{'id':'fictional-canonical','payload':{'source_quote':''}}]
                options={'learner_context':LEARNER}if include_context else{}
                returned=recording.recover_source_action_evidence(candidates,**options)
                self.assertIs(returned,result);self.assertIs(runtime.recovery_context[0],candidates)
                self.assertIs(runtime.recovery_context[1],LEARNER if include_context else None)
                self.assertEqual(runtime.calls,['source_recovery.json']);self.assertEqual(recording.model_calls,1)
                recorded=recording.records[0]
                self.assertEqual(recorded['method'],'recover_source_action_evidence');self.assertEqual(recorded['result'],result)
                self.assertEqual(recorded['metadata'],runtime.last_recovery_metadata)
                self.assertEqual(recorded['metadata']['result_sha256'],evaluator._json_digest(result))
                result['facts'][0]['source_quote']='Changed fictional proof.'
                runtime.last_recovery_metadata['version']='changed-version'
                self.assertEqual(recorded['result']['facts'][0]['source_quote'],'Fictional historical source proof.')
                self.assertEqual(recorded['metadata']['version'],'synthetic-recovery-v1')

    def test_production_analyze_records_verifier_and_repeat_is_inert(self):
        def verified(src,item,candidates):
            candidate=candidates[0];proof=candidate['original_evidence'][0]
            return {'decision':'match','action_id':candidate['id'],'evidence_id':proof['id'],
                'source_quote':item['source_quote'],'candidate_quote':proof['text'],
                'source_purpose_field':'text','source_purpose_quote':item['source_quote'],
                'same_purpose':True,'same_occasion':True,'recipient_compatible':True,'unique_target':True,
                'reason':'The original fictional quotations identify the same school obligation.'}
        runtime=SyntheticVerifyingRuntime(matcher=lambda *args:{'decision':'uncertain'},verifier=verified)
        status,lines,_=self.run_evaluation(self.target_document(True),runtime,details=True)
        self.assertEqual(status,0);result=lines[-1]
        self.assertEqual(runtime.calls,['source_analysis.json','source_match.json','source_verify.json'])
        self.assertEqual(result['model_calls'],3);self.assertEqual(result['results'][0]['case_model_calls'],3)
        self.assertTrue(result['results'][0]['targets_passed'])
        self.assertEqual(result['results'][0]['repeat_model_calls'],0)
        self.assertEqual(result['results'][0]['repeat_new_local_operations'],0)
        self.assertTrue(result['results'][0]['repeat_state_unchanged'])
        self.assertEqual(result['provenance']['verification_versions'],['synthetic-verification-v1'])
        details=json.loads((self.root/'details.json').read_text())
        calls=details['transcript'][0]['recording']['runtime_calls']
        self.assertEqual([call['method']for call in calls],['analyze_source','match_source_action','verify_source_action'])
        self.assertEqual(calls[-1]['result']['decision'],'match')
        self.assertEqual(calls[-1]['metadata']['result_sha256'],evaluator._json_digest(calls[-1]['result']))
        self.assertNotIn(TEXT,json.dumps(lines));self.assertNotIn('action_id',json.dumps(lines))

    def test_production_valid_literal_match_does_not_invoke_optional_verifier(self):
        def unexpected(*args):raise AssertionError('Independent verification was not required.')
        runtime=SyntheticVerifyingRuntime(matcher=matched_candidate,verifier=unexpected)
        status,lines,_=self.run_evaluation(self.target_document(True),runtime)
        self.assertEqual(status,0);self.assertEqual(runtime.calls,['source_analysis.json','source_match.json'])
        self.assertEqual(lines[-1]['model_calls'],2)
        self.assertEqual(lines[-1]['provenance']['verification_versions'],[])

    def test_substitution_uses_production_zero_model_branch_and_metadata(self):
        document=self.document({'id':'substitution','source':source(kind='substitution'),'expected':[],
            'current':NOW.isoformat(),'expected_new_operations':0,'repeat':True})
        status,lines,_=self.run_evaluation(document,details=True)
        self.assertEqual(status,0);self.assertEqual(lines[-1]['model_calls'],0)
        details=json.loads((self.root/'details.json').read_text())
        self.assertEqual(details['transcript'][0]['recording']['raw'],[])
        self.assertEqual(details['transcript'][0]['metadata']['prompt_version'],'substitution-policy-v1')

    def test_missing_offer_group_fails_and_continues_without_preparing_operations(self):
        document,runtime=self.offer_document(grouped=False)
        status,lines,_=self.run_evaluation(document,runtime,details=True)
        self.assertEqual(status,1);result=lines[-1];self.assertEqual(result['cases'],2)
        first,tail=result['results'];self.assertFalse(first['passed']);self.assertTrue(tail['passed'])
        self.assertTrue(first['semantic_passed']);self.assertTrue(first['quotes_passed']);self.assertTrue(first['outcomes_passed'])
        self.assertFalse(first['offer_group_sizes_passed']);self.assertFalse(first['offer_selection_passed'])
        self.assertEqual(first['offer_selection_status'],'not_possible')
        self.assertFalse(first['operation_checks_performed']);self.assertIsNone(first['new_local_operations'])
        for key in ('offer_max_one_operation_passed','replay_checks_passed','repeat_model_calls','repeat_state_unchanged'):
            self.assertIsNone(first[key])
        self.assertTrue(tail['operation_checks_performed']);self.assertTrue(tail['replay_checks_passed'])
        self.assertEqual(runtime.calls,['source_analysis.json','source_analysis.json'])
        details=json.loads((self.root/'details.json').read_text())
        self.assertTrue(all(entry['state']['operations']==[]for entry in details['transcript']))
        self.assertEqual(result['calendar_writes'],0)

    def test_pending_work_from_invalid_offer_cannot_leak_into_tail_queue_or_replay(self):
        document,runtime=self.offer_document(grouped=False,pending=True)
        status,lines,_=self.run_evaluation(document,runtime,details=True)
        self.assertEqual(status,1);first,tail=lines[-1]['results']
        self.assertFalse(first['passed']);self.assertFalse(tail['passed'])
        self.assertTrue(tail['semantic_passed']);self.assertTrue(tail['outcomes_passed'])
        self.assertFalse(tail['operation_checks_performed']);self.assertIsNone(tail['new_local_operations'])
        self.assertEqual(tail['operation_check_status'],'not_run_prior_offer_pending')
        self.assertIn('prior_invalid_offer_pending',tail['case_errors']);self.assertIsNone(tail['replay_checks_passed'])
        self.assertEqual(runtime.calls,['source_analysis.json','source_analysis.json'])
        details=json.loads((self.root/'details.json').read_text())
        self.assertTrue(all(entry['state']['operations']==[]for entry in details['transcript']))

    def test_valid_offer_still_selects_one_operation_and_repeats_without_calls(self):
        document,runtime=self.offer_document()
        status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,0);first,tail=lines[-1]['results']
        self.assertTrue(first['offer_group_sizes_passed']);self.assertTrue(first['offer_selection_passed'])
        self.assertEqual(first['offer_selection_status'],'selected');self.assertTrue(first['offer_max_one_operation_passed'])
        self.assertEqual(first['new_local_operations'],1);self.assertEqual(first['repeat_new_local_operations'],0)
        self.assertEqual(first['repeat_model_calls'],0);self.assertTrue(first['replay_checks_passed'])
        self.assertEqual(first['case_errors'],[]);self.assertTrue(tail['passed'])

    def test_offer_size_or_actual_selection_index_mismatch_fails_without_abort(self):
        for wrong_size in (True,False):
            with self.subTest(wrong_size=wrong_size):
                document,runtime=self.offer_document()
                if wrong_size:document['cases'][0]['expected_group_sizes']=[3]
                else:document['cases'][0].pop('expected_group_sizes');document['cases'][0]['select_offer_index']=5
                status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);first,tail=lines[-1]['results']
                self.assertFalse(first['passed']);self.assertTrue(tail['passed'])
                self.assertEqual(first['offer_selection_status'],'not_possible')
                self.assertIsNone(first['offer_max_one_operation_passed']);self.assertIsNone(first['replay_checks_passed'])

    def test_unrelated_seed_group_is_never_chosen_for_current_failed_offer(self):
        document,runtime=self.offer_document()
        offer=document['cases'][0]
        document['seeds']=[{'source':source('seed-offer',text=offer['source']['text']),
                            'proposals':runtime.responses['offer'],'current':NOW.isoformat()}]
        document['cases']=[dict(document['cases'][1],expected_group_sizes=[2],select_offer_index=0)]
        with patch.object(AppStore,'resolve_proposal_group',side_effect=AssertionError('An unrelated group must not be selected.'))as resolve:
            status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,1);self.assertFalse(resolve.called)
        self.assertEqual(lines[-1]['results'][0]['offer_selection_status'],'not_possible')

    def test_offer_guard_rejection_is_failed_evidence_and_tail_still_runs(self):
        document,runtime=self.offer_document()
        with patch.object(AppStore,'resolve_proposal_group',side_effect=ValueError('DO_NOT_LEAK_FICTIONAL_GUARD')):
            status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,1);first,tail=lines[-1]['results']
        self.assertEqual(first['offer_selection_status'],'rejected');self.assertFalse(first['passed']);self.assertTrue(tail['passed'])
        self.assertIn('offer_selection_rejected',first['case_errors']);self.assertNotIn('DO_NOT_LEAK_FICTIONAL_GUARD',json.dumps(lines))

    def test_malformed_offer_fixture_remains_fatal_before_inference(self):
        for sizes,selection in ((None,0),('2',0),([True],0),([1],0),([2],True),([2],2),([],0),([2,2],0)):
            with self.subTest(sizes=sizes,selection=selection):
                document,runtime=self.offer_document()
                document['cases'][0].update(expected_group_sizes=sizes,select_offer_index=selection)
                status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError')
                self.assertEqual(runtime.calls,[]);self.assertEqual(lines[-1]['completed_cases'],0)

    def test_exact_legacy_offer_migrates_reingests_preserves_and_selects_once(self):
        document=self.legacy_offer_document();runtime=SyntheticRuntime()
        status,lines,_=self.run_evaluation(document,runtime,details=True)
        self.assertEqual(status,0);result=lines[-1]['results'][0]
        self.assertEqual(result['acceptance_kind'],'existing_legacy_offer_state')
        self.assertEqual(result['analysis_status'],'skipped');self.assertTrue(result['legacy_offer_state_passed'])
        self.assertEqual(result['legacy_offer_member_count'],2);self.assertEqual(runtime.calls,[])
        self.assertEqual(result['case_model_calls'],0);self.assertEqual(result['actual_count'],0)
        self.assertEqual(result['new_local_proposals'],0);self.assertEqual(result['new_local_groups'],0)
        self.assertTrue(result['offer_selection_passed']);self.assertTrue(result['offer_max_one_operation_passed'])
        self.assertEqual(result['new_local_operations'],1);self.assertEqual(result['repeat_model_calls'],0)
        self.assertEqual(result['repeat_new_local_operations'],0);self.assertTrue(result['repeat_state_unchanged'])
        details=json.loads((self.root/'details.json').read_text());state=details['transcript'][0]['state']
        expected_ids={row['id']for row in document['legacy_rows']['proposals']}
        self.assertEqual({row[0]for row in state['proposals']},expected_ids)
        self.assertEqual(state['proposal_groups'][0][0],document['legacy_rows']['proposal_groups'][0]['id'])
        for value in expected_ids:self.assertNotIn(value,json.dumps(lines))
        self.assertEqual(lines[-1]['provenance']['legacy_rows_sha256'],evaluator._json_digest(document['legacy_rows']))

    def test_legacy_offer_changed_reingestion_fails_without_selection_or_operations(self):
        document=self.legacy_offer_document();document['cases'][0]['source']['text']+=' Revised fictional source.'
        runtime=SyntheticRuntime({'legacy-offer':[]})
        status,lines,_=self.run_evaluation(document,runtime,details=True)
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertFalse(result['legacy_offer_state_passed']);self.assertFalse(result['offer_selection_passed'])
        self.assertFalse(result['operation_checks_performed']);self.assertIsNone(result['replay_checks_passed'])
        self.assertIn('legacy_offer_state_changed',result['case_errors'])
        details=json.loads((self.root/'details.json').read_text())
        self.assertEqual(details['transcript'][0]['state']['operations'],[])

    def test_legacy_offer_preservation_does_not_ignore_proposal_timestamps(self):
        document=self.legacy_offer_document();original=AppStore.save_message
        def changed(store,data):
            result=original(store,data)
            if data['id']=='legacy-offer':
                with store.db:store.db.execute("UPDATE proposals SET updated_at='changed-fixture-time' WHERE message_id=?",(data['id'],))
            return result
        with patch.object(AppStore,'save_message',changed):
            status,lines,_=self.run_evaluation(document)
        self.assertEqual(status,1);self.assertFalse(lines[-1]['results'][0]['legacy_offer_state_passed'])
        self.assertIsNone(lines[-1]['results'][0]['new_local_operations'])

    def test_legacy_offer_preservation_checks_all_groups_even_without_selection_or_group_oracle(self):
        document=self.legacy_offer_document();case=document['cases'][0]
        case.pop('select_offer_index');case.pop('expected_group_sizes');case['expected_new_operations']=0
        extra=deepcopy(document['legacy_rows']['proposal_groups'][0]);extra.update(id='additional-fictional-group',group_key='additional-key')
        original=AppStore.save_message
        def insert_extra_group(store,data):
            result=original(store,data)
            if data['id']=='legacy-offer':
                columns=list(extra)
                with store.db:store.db.execute('INSERT INTO proposal_groups ('+','.join(columns)+') VALUES ('+
                    ','.join('?'for _ in columns)+')',tuple(extra[key]for key in columns))
            return result
        with patch.object(AppStore,'save_message',insert_extra_group):
            status,lines,_=self.run_evaluation(document,details=True)
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertFalse(result['legacy_offer_state_passed']);self.assertFalse(result['passed'])
        self.assertEqual(result['new_local_groups'],1);self.assertIn('legacy_offer_state_changed',result['case_errors'])
        self.assertFalse(result['operation_checks_performed']);self.assertIsNone(result['replay_checks_passed'])
        self.assertEqual(result['offer_group_sizes'],[0,2])

    def test_legacy_offer_requires_empty_oracle_and_explicit_matching_group_source(self):
        for change in ('oracle','index','source','bool'):
            with self.subTest(change=change):
                document=self.legacy_offer_document();case=document['cases'][0]
                if change=='oracle':case['expected']=[{'source_quote':case['source']['text']}]
                elif change=='index':case['legacy_offer_group_index']=1
                elif change=='source':case['source']['upstream_id']='other-source'
                else:case['legacy_offer_group_index']=True
                runtime=SyntheticRuntime();status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError');self.assertEqual(runtime.calls,[])

    def test_legacy_raw_import_rejects_unapproved_tables_bad_hashes_and_unsafe_rows(self):
        for change in ('table','column','duplicate','hash','association','position','selected','status','approval','event','evidence','payload'):
            with self.subTest(change=change):
                document=self.legacy_offer_document();rows=document['legacy_rows']
                if change=='table':rows['operations']=[]
                elif change=='column':rows['proposals'][0]['unexpected']='untrusted'
                elif change=='duplicate':rows['proposals'].append(deepcopy(rows['proposals'][0]))
                elif change=='hash':rows['proposals'][0]['source_hash']='0'*64
                elif change=='association':rows['proposal_group_members'][0]['proposal_id']='missing'
                elif change=='position':rows['proposal_group_members'][0]['position']=2
                elif change=='selected':rows['proposal_groups'][0]['selected_proposal_id']=rows['proposals'][0]['id']
                elif change=='status':rows['proposals'][0]['status']='pending'
                elif change=='evidence':rows['proposal_groups'][0]['source_evidence']='Unobserved fictional alternative evidence.'
                else:
                    payload=json.loads(rows['proposals'][0]['payload'])
                    if change=='approval':payload['user_approved']=True
                    elif change=='event':payload['event_id']='fictional-event'
                    else:payload['canonical_action_id']='foreign-canonical'
                    rows['proposals'][0]['payload']=json.dumps(payload,ensure_ascii=False)
                runtime=SyntheticRuntime();status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError');self.assertEqual(runtime.calls,[])

    def test_actual_legacy_proposal_source_hash_is_asserted_without_override(self):
        document=self.target_document(True);seed=document['seeds'][0];src=seed['source']
        message={'id':src['upstream_id'],'subject':src['title'],'sender':src['sender'],
                 'sent_at':src['published_at'],'text':src['text'],'attachment_count':0}
        seed['legacy_proposal_source_hashes']=[digest(message)]
        status,lines,_=self.run_evaluation(document,SyntheticRuntime(matcher=matched_candidate))
        self.assertEqual(status,0);self.assertTrue(lines[-1]['results'][0]['targets_passed'])
        for value in (None,[],[None],['0'*64],[digest(message),digest(message)]):
            with self.subTest(value=value):
                changed=deepcopy(document);changed['seeds'][0]['legacy_proposal_source_hashes']=value
                runtime=SyntheticRuntime(matcher=matched_candidate);status,lines,_=self.run_evaluation(changed,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError');self.assertEqual(runtime.calls,[])

    def test_legacy_attachment_counter_preserves_captured_row_hash_and_inert_replay(self):
        src=source(kind='message',metadata={'attachment_count':1})
        message={'id':src['upstream_id'],'subject':src['title'],'sender':src['sender'],
                 'sent_at':src['published_at'],'text':src['text'],'attachment_count':1}
        row_hash=digest(message)
        document=self.document({'id':'attachment-replay','source':deepcopy(src),'expected':[],
            'expected_new_actions':0,'expected_new_proposals':0,'expected_new_operations':0,
            'current':NOW.isoformat(),'repeat':True},seeds=[{'source':src,'legacy':True,
            'proposals':[action(action_identity=None)],'current':NOW.isoformat(),
            'legacy_proposal_source_hashes':[row_hash],'decisions':[{'index':0,'decision':'ignore'}]}])
        runtime=SyntheticRuntime();status,lines,_=self.run_evaluation(document,runtime,details=True)
        self.assertEqual(status,0);result=lines[-1]['results'][0]
        self.assertEqual(runtime.calls,[]);self.assertEqual(result['analysis_status'],'skipped')
        self.assertEqual(result['repeat_model_calls'],0);self.assertEqual(result['repeat_new_local_operations'],0)
        self.assertTrue(result['repeat_state_unchanged'])
        details=json.loads((self.root/'details.json').read_text())
        self.assertEqual(details['transcript'][0]['state']['proposals'][0][2],row_hash)
        self.assertNotEqual(row_hash,digest(dict(message,attachment_count=0)))
        mismatched=deepcopy(document);mismatched['seeds'][0]['source']['metadata']['attachment_count']=0
        runtime=SyntheticRuntime();status,lines,_=self.run_evaluation(mismatched,runtime)
        self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError');self.assertEqual(runtime.calls,[])

    def test_exact_legacy_offer_reingestion_respects_attachment_counter(self):
        document=self.legacy_offer_document(attachment_count=1)
        status,lines,_=self.run_evaluation(document,SyntheticRuntime())
        self.assertEqual(status,0);result=lines[-1]['results'][0]
        self.assertTrue(result['legacy_offer_state_passed']);self.assertEqual(result['case_model_calls'],0)
        self.assertEqual(result['repeat_model_calls'],0);self.assertTrue(result['repeat_state_unchanged'])
        document['cases'][0]['source']['metadata']['attachment_count']=0
        status,lines,_=self.run_evaluation(document,SyntheticRuntime({'legacy-offer':[]}))
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertFalse(result['legacy_offer_state_passed']);self.assertFalse(result['operation_checks_performed'])
        self.assertIsNone(result['new_local_operations'])

    def test_message_fixture_attachment_counter_requires_bounded_nonnegative_integer(self):
        for counter in (-1,True,1.5,'1',1001,None):
            with self.subTest(counter=counter):
                document=self.document();document['cases'][0]['source']=source(kind='message',metadata={'attachment_count':counter})
                runtime=SyntheticRuntime();status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError');self.assertEqual(runtime.calls,[])

    def test_ignore_does_not_call_matcher_and_actionable_scope_remains_explicit(self):
        document=self.document({'id':'information','source':source(),'expected':[],
            'expected_scope':'actionable','current':NOW.isoformat(),'expected_new_operations':0,'repeat':True},
            seeds=[{'source':source('seed'),'proposals':[action()],'current':NOW.isoformat(),
                    'decisions':[{'index':0,'decision':'ignore'}]}])
        runtime=SyntheticRuntime({'fictional-source':[action(informational=True)]})
        status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,0);self.assertEqual(runtime.calls,['source_analysis.json'])
        result=lines[-1]['results'][0]
        self.assertEqual(result['actual_count'],1);self.assertEqual(result['actionable_count'],0)
        self.assertEqual(result['expected_scope'],'actionable');self.assertTrue(result['ignored_operations_safe'])
        document['cases'][0].pop('expected_scope')
        status,lines,_=self.run_evaluation(document,SyntheticRuntime({'fictional-source':[action(informational=True)]}))
        self.assertEqual(status,1);self.assertFalse(lines[-1]['results'][0]['semantic_passed'])

    def test_retained_rejection_uses_production_deterministic_path(self):
        document=self.document({'id':'retained-rejection','source':source(),'expected':[{'source_quote':TEXT}],
            'expected_outcomes':['ignored'],'expected_new_actions':0,'expected_new_operations':0,
            'current':NOW.isoformat(),'repeat':True},seeds=[{'source':source('seed'),'proposals':[action()],
            'current':NOW.isoformat(),'decisions':[{'index':0,'decision':'ignore'}]}])
        runtime=SyntheticRuntime()
        status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,0);self.assertEqual(runtime.calls,['source_analysis.json'])
        self.assertEqual(lines[-1]['results'][0]['outcomes'],['ignored'])

    def test_unordered_subset_matching_and_outcomes_share_one_assignment(self):
        actual=[{'title':'narrow','start':'2030-03-12','source_quote':TEXT},
                {'title':'broad','start':'2030-03-12','source_quote':TEXT}]
        case={'expected':[{'start':'2030-03-12','source_quote':TEXT},{'title':'narrow','source_quote':TEXT}],
              'expected_outcomes':['linked','added']}
        result=evaluator.compare_oracle(case,actual,['added','linked'],TEXT)
        self.assertTrue(all(result[key]for key in ('semantic_passed','quotes_passed','outcomes_passed')))
        self.assertEqual(result['matched_actual_indices'],[1,0])
        case['expected_outcomes']=['added','added']
        self.assertFalse(evaluator.compare_oracle(case,actual,['added','linked'],TEXT)['outcomes_passed'])

    def test_canonical_target_requires_real_source_link_for_legacy_and_source_seeds(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                status,lines,_=self.run_evaluation(self.target_document(legacy),SyntheticRuntime(matcher=matched_candidate),progress=True)
                self.assertEqual(status,0)
                result=lines[-1]['results'][0]
                self.assertTrue(result['targets_passed']);self.assertEqual(result['target_check_count'],1)
                self.assertTrue(result['preserved_seed_events']);self.assertEqual(result['new_local_actions'],0)
                self.assertEqual(result['new_local_operations'],0);self.assertEqual(result['outcomes'],['linked'])
                self.assertNotIn('action_id',json.dumps(lines));self.assertNotIn('fictional-approved-event',json.dumps(lines))
                self.assertNotIn('seed_index',json.dumps(lines));self.assertNotIn('proposal_index',json.dumps(lines))

    def test_preserved_event_and_extracted_fact_cannot_replace_canonical_target(self):
        status,lines,_=self.run_evaluation(self.target_document(True),SyntheticRuntime())
        self.assertEqual(status,1)
        result=lines[-1]['results'][0]
        self.assertTrue(result['semantic_passed']);self.assertTrue(result['quotes_passed'])
        self.assertTrue(result['preserved_seed_events']);self.assertFalse(result['targets_passed'])
        self.assertEqual(result['new_local_actions'],1)

    def test_wrong_existing_target_fails_even_when_counts_and_outcomes_match(self):
        document=self.target_document()
        document['seeds']=[{'source':source('seed-'+str(index)),
                           'proposals':[action(title='Candidate '+str(index),needs_review=True)],
                           'current':NOW.isoformat()}for index in range(2)]
        document['cases'][0]['expected_preserved_events']=[]
        def wrong_target(source,item,candidates):
            selected=next(candidate for candidate in candidates if candidate['payload']['title'].endswith('Candidate 1'))
            return matched_candidate(source,item,[selected])
        status,lines,_=self.run_evaluation(document,SyntheticRuntime(matcher=wrong_target))
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertTrue(result['semantic_passed']);self.assertTrue(result['quotes_passed']);self.assertTrue(result['outcomes_passed'])
        self.assertFalse(result['targets_passed']);self.assertEqual(result['outcomes'],['linked'])
        self.assertEqual(result['new_local_actions'],0);self.assertEqual(result['new_local_proposals'],0)
        self.assertEqual(result['new_local_operations'],0)

    def test_reported_target_id_requires_current_source_hash_link(self):
        original=SourcePlanner.apply
        for change in ('missing','stale'):
            with self.subTest(change=change):
                def mutate_link(planner,src,*args,**kwargs):
                    applied=original(planner,src,*args,**kwargs)
                    if src['upstream_id']=='fictional-source':
                        with planner.store.db:
                            if change=='missing':planner.store.db.execute('DELETE FROM action_sources WHERE source_id=?',(src['id'],))
                            else:planner.store.db.execute("UPDATE action_sources SET source_hash='stale-fixture-hash' WHERE source_id=?",(src['id'],))
                    return applied
                document=self.target_document(True)
                with patch.object(SourcePlanner,'apply',mutate_link):
                    status,lines,_=self.run_evaluation(document,SyntheticRuntime(matcher=matched_candidate))
                self.assertEqual(status,1);result=lines[-1]['results'][0]
                self.assertTrue(result['outcomes_passed']);self.assertEqual(result['outcomes'],['linked'])
                self.assertTrue(result['preserved_seed_events']);self.assertFalse(result['targets_passed'])

    def run_target_after_current_source_change(self,statement=None):
        original=SourcePlanner.apply
        def mutate_source(planner,src,*args,**kwargs):
            applied=original(planner,src,*args,**kwargs)
            if statement and src['upstream_id']=='fictional-source':
                with planner.store.db:planner.store.db.execute(statement,(src['id'],))
            return applied
        document=self.target_document(True);document['cases'][0]['repeat']=False
        with patch.object(SourcePlanner,'apply',mutate_source):
            return self.run_evaluation(document,SyntheticRuntime(matcher=matched_candidate))

    def test_target_rejects_source_version_changed_after_apply(self):
        status,lines,_=self.run_target_after_current_source_change(
            "UPDATE sources SET content_hash='new-observed-version' WHERE id=?")
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertTrue(result['outcomes_passed']);self.assertFalse(result['targets_passed'])
        self.assertEqual(result['outcomes'],['linked']);self.assertTrue(result['preserved_seed_events'])
        self.assertEqual(result['new_local_actions'],0);self.assertEqual(result['new_local_operations'],0)

    def test_target_rejects_source_deleted_after_apply(self):
        status,lines,_=self.run_target_after_current_source_change('UPDATE sources SET deleted=1 WHERE id=?')
        self.assertEqual(status,1);result=lines[-1]['results'][0]
        self.assertTrue(result['outcomes_passed']);self.assertFalse(result['targets_passed'])
        self.assertEqual(result['outcomes'],['linked']);self.assertTrue(result['preserved_seed_events'])
        self.assertEqual(result['new_local_actions'],0);self.assertEqual(result['new_local_operations'],0)

    def test_target_accepts_unchanged_current_source_after_apply_without_repeat(self):
        status,lines,_=self.run_target_after_current_source_change()
        self.assertEqual(status,0);result=lines[-1]['results'][0]
        self.assertTrue(result['targets_passed']);self.assertEqual(result['target_check_count'],1)
        self.assertEqual(result['outcomes'],['linked']);self.assertTrue(result['preserved_seed_events'])

    def test_targets_outcomes_and_semantics_use_one_assignment_after_reordering(self):
        actual=[{'title':'narrow','source_quote':TEXT},{'title':'broad','source_quote':TEXT}]
        case={'expected':[{'source_quote':TEXT},{'title':'narrow','source_quote':TEXT}],
              'expected_outcomes':['linked','added'],
              'expected_targets':[{'seed_index':0,'proposal_index':0},{'seed_index':0,'proposal_index':1}]}
        result=evaluator.compare_oracle(case,actual,['added','linked'],TEXT,
            expected_target_ids=['broad-id','narrow-id'],actual_target_ids=['narrow-id','broad-id'])
        self.assertTrue(result['targets_passed']);self.assertEqual(result['matched_actual_indices'],[1,0])
        result=evaluator.compare_oracle(case,list(reversed(actual)),['linked','added'],TEXT,
            expected_target_ids=['broad-id','narrow-id'],actual_target_ids=['broad-id','narrow-id'])
        self.assertTrue(result['targets_passed']);self.assertEqual(result['matched_actual_indices'],[0,1])
        # Each constraint separately has a perfect match, but there is no single
        # assignment satisfying outcomes and target identity together.
        result=evaluator.compare_oracle(case,actual,['added','linked'],TEXT,
            expected_target_ids=['narrow-id','broad-id'],actual_target_ids=['narrow-id','broad-id'])
        self.assertTrue(result['outcomes_passed']);self.assertFalse(result['targets_passed'])
        self.assertIsNone(result['matched_actual_indices'])

    def test_targets_disambiguate_overlapping_expected_rows_in_both_orders(self):
        case={'expected':[{'source_quote':TEXT},{'source_quote':TEXT}],
              'expected_outcomes':['linked','linked'],
              'expected_targets':[{'seed_index':0,'proposal_index':0},{'seed_index':1,'proposal_index':0}]}
        actual=[{'title':'first','source_quote':TEXT},{'title':'second','source_quote':TEXT}]
        for ids,assignment in ((['first-id','second-id'],[1,0]),(['second-id','first-id'],[0,1])):
            with self.subTest(ids=ids):
                result=evaluator.compare_oracle(case,actual,['linked','linked'],TEXT,
                    expected_target_ids=['second-id','first-id'],actual_target_ids=ids)
                self.assertTrue(result['targets_passed']);self.assertEqual(result['matched_actual_indices'],assignment)

    def test_target_proposal_index_selects_second_seed_input(self):
        document=self.target_document()
        seed=document['seeds'][0]
        seed['proposals'].append(action(start='2030-03-13'))
        seed['decisions'].append({'index':1,'decision':'approve'})
        seed['events'].append({'index':1,'id':'fictional-second-event'})
        case=document['cases'][0]
        case['expected'][0]['start']='2030-03-13';case['expected_targets'][0]['proposal_index']=1
        case['expected_preserved_events'].append('fictional-second-event')
        def select_second(src,item,candidates):
            selected=next(candidate for candidate in candidates if candidate['payload']['start']=='2030-03-13')
            return matched_candidate(src,item,[selected])
        status,lines,_=self.run_evaluation(document,
            SyntheticRuntime({'fictional-source':[action(start='2030-03-13')]},matcher=select_second))
        self.assertEqual(status,0);self.assertTrue(lines[-1]['results'][0]['targets_passed'])

    def test_null_target_is_unconstrained_and_cannot_bypass_strict_oracles(self):
        document=self.document();document['cases'][0]['expected_targets']=[None]
        status,lines,_=self.run_evaluation(document)
        self.assertEqual(status,0);self.assertTrue(lines[-1]['results'][0]['targets_passed'])
        self.assertEqual(lines[-1]['results'][0]['target_check_count'],0)
        document['cases'][0]['expected'][0]['start']='2030-03-13'
        status,lines,_=self.run_evaluation(document)
        self.assertEqual(status,1);self.assertFalse(lines[-1]['results'][0]['semantic_passed'])
        for change in ('quote','operation','date','count'):
            with self.subTest(change=change):
                document=self.target_document(True)
                case=document['cases'][0]
                if change=='quote':case['expected'][0]['source_quote']='Fabricated fictional quotation.'
                elif change=='operation':case['expected_new_operations']=1
                elif change=='date':case['expected'][0]['start']='2030-03-13'
                else:case['expected'].append({'source_quote':TEXT});case['expected_outcomes'].append('linked');case['expected_targets'].append(None)
                status,lines,_=self.run_evaluation(document,SyntheticRuntime(matcher=matched_candidate))
                self.assertEqual(status,1)

    def test_invalid_target_shapes_and_unknown_indices_fail_before_model(self):
        invalid=[None,{},'seed',[],[None,None],[{}],[{'seed_index':0}],
                 [{'seed_index':0,'proposal_index':0,'action_id':'untrusted'}],
                 [{'seed_index':True,'proposal_index':0}],[{'seed_index':0,'proposal_index':False}],
                 [{'seed_index':-1,'proposal_index':0}],[{'seed_index':1,'proposal_index':0}],
                 [{'seed_index':0,'proposal_index':-1}],[{'seed_index':0,'proposal_index':1}]]
        for targets in invalid:
            with self.subTest(targets=targets):
                document=self.target_document();document['cases'][0]['expected_targets']=targets
                runtime=SyntheticRuntime(matcher=matched_candidate)
                status,lines,_=self.run_evaluation(document,runtime)
                self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError')
                self.assertEqual(runtime.calls,[])

    def test_deduplicated_seed_input_mapping_fails_closed(self):
        document=self.target_document()
        document['seeds'][0]['proposals'].append(action())
        runtime=SyntheticRuntime(matcher=matched_candidate)
        status,lines,_=self.run_evaluation(document,runtime)
        self.assertEqual(status,1);self.assertEqual(lines[-1]['error_type'],'ValueError')
        self.assertEqual(runtime.calls,[])

    def test_target_constraint_cannot_be_dropped_from_resolved_comparison(self):
        case={'expected':[{'source_quote':TEXT}],
              'expected_targets':[{'seed_index':0,'proposal_index':0}]}
        for values in ({},{'expected_target_ids':[None],'actual_target_ids':[None]},
                       {'expected_target_ids':['canonical'],'actual_target_ids':[]}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                evaluator.compare_oracle(case,[{'source_quote':TEXT}],['linked'],TEXT,**values)

    def test_seed_target_ids_require_verified_canonical_proposal_mapping(self):
        with tempfile.TemporaryDirectory()as temporary:
            store=AppStore(Path(temporary)/'seed.sqlite3',config=TEST_CONFIG)
            try:
                with store.db:
                    store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',('canonical','proposal','{}','review','fixture'))
                    store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',('legacy','legacy','{}','review','fixture'))
                self.assertEqual(evaluator.seed_target_ids(store,{'proposals':[{}]},
                    {'outcomes':[{'action_id':'canonical','proposal_id':'proposal'}]}),['canonical'])
                self.assertEqual(evaluator.seed_target_ids(store,{'proposals':[{}],'legacy':True},
                    {'outcomes':[{'proposal_id':'legacy'}]}),['legacy'])
                for outcome in ({'proposal_id':'proposal'}, {'action_id':'canonical','proposal_id':'other'},
                                {'action_id':'missing','proposal_id':'proposal'}):
                    self.assertIsNone(evaluator.seed_target_ids(store,{'proposals':[{}]},{'outcomes':[outcome]}))
                self.assertIsNone(evaluator.seed_target_ids(store,{'proposals':[{}],'legacy':True},
                    {'outcomes':[{'proposal_id':'proposal'}]}))
                self.assertIsNone(evaluator.seed_target_ids(store,{'proposals':[{},{}]},
                    {'outcomes':[{'action_id':'canonical','proposal_id':'proposal'}]*2}))
            finally:store.close()

    def test_actionable_scope_still_rejects_bad_ignored_quote_and_counts_past_actions(self):
        case={'expected':[],'expected_scope':'actionable'}
        result=evaluator.compare_oracle(case,[{'kind':'ignore','source_quote':'Fabricated quote'}],['ignored'],TEXT)
        self.assertFalse(result['quotes_passed'])
        result=evaluator.compare_oracle(case,[{'kind':'create','status':'ignored','source_quote':TEXT}],['ignored'],TEXT)
        self.assertFalse(result['semantic_passed']);self.assertEqual(result['oracle_actual_count'],1)
        case={'expected':[{'source_quote':'Quote from another fictional source'}]}
        result=evaluator.compare_oracle(case,[{'source_quote':TEXT}],['added'],TEXT,'literal')
        self.assertFalse(result['quotes_passed'])

    def test_legacy_seed_unchanged_message_follows_incremental_queue_without_model(self):
        document=self.document({'id':'legacy-replay','source':source(kind='message'),'expected':[],
            'current':NOW.isoformat(),'expected_new_actions':0,'expected_new_proposals':0,
            'expected_new_operations':0,'repeat':True},seeds=[{'source':source(kind='message'),
            'proposals':[action(action_identity=None)],'legacy':True,'current':NOW.isoformat(),
            'decisions':[{'index':0,'decision':'approve'}],
            'events':[{'index':0,'id':'fictional-approved-event'}]}])
        document['cases'][0]['expected_preserved_events']=['fictional-approved-event']
        status,lines,_=self.run_evaluation(document,details=True)
        self.assertEqual(status,0);self.assertEqual(lines[-1]['model_calls'],0)
        result=lines[-1]['results'][0];self.assertTrue(result['preserved_seed_events'])
        details=json.loads((self.root/'details.json').read_text())
        self.assertEqual(details['transcript'][0]['recording']['applied']['status'],'skipped')

    def test_forbidden_new_action_date_is_additive_to_strict_semantic_oracle(self):
        document=self.document();document['cases'][0]['forbidden_new_action_dates']=['2030-03-12']
        status,lines,_=self.run_evaluation(document)
        self.assertEqual(status,1)
        result=lines[-1]['results'][0]
        self.assertTrue(result['semantic_passed']);self.assertFalse(result['forbidden_new_action_dates_safe'])

    def test_unknown_quote_policy_rejected_without_model(self):
        runtime=SyntheticRuntime()
        status,lines,_=self.run_evaluation(self.document(quote_policy='ignore'),runtime)
        self.assertEqual(status,1);self.assertEqual(runtime.calls,[])

    def test_exact_input_bytes_cover_oracle_and_seed_changes(self):
        original=self.document(seeds=[]);self.write_json(self.cases_path,original)
        parsed,first=evaluator.read_evaluation_input(self.cases_path)
        self.assertEqual(first,hashlib.sha256(self.cases_path.read_bytes()).hexdigest())
        self.cases_path.write_bytes(self.cases_path.read_bytes()+b'\n')
        self.assertEqual(evaluator.read_evaluation_input(self.cases_path)[0],parsed)
        self.assertNotEqual(evaluator.read_evaluation_input(self.cases_path)[1],first)
        changed=deepcopy(original);changed['cases'][0]['expected'][0]['start']='2030-03-13'
        self.write_json(self.cases_path,changed)
        self.assertNotEqual(evaluator.read_evaluation_input(self.cases_path)[1],first)
        changed=deepcopy(original);changed['seeds']=[{'proposals':[]}]
        self.write_json(self.cases_path,changed)
        self.assertNotEqual(evaluator.read_evaluation_input(self.cases_path)[1],first)

    def test_code_snapshot_includes_all_app_code_prompts_and_script(self):
        root=self.root/'code'
        for relative in ('app/core.py','app/offer_groups.py','app/data_admin.py','app/config.py',
                         'app/schemas/analysis.md','app/schemas/source_verify.json','app/schemas/source_verify.md',
                         'app/schemas/source_recovery.json','app/schemas/source_recovery.md',
                         'app/schemas/nested/prompt.txt','scripts/evaluate_sources.py'):
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('synthetic')
        manifest,before=evaluator.evaluation_code_snapshot(root)
        self.assertEqual(len(manifest),11)
        for relative in manifest:
            path=root/relative;path.write_text('changed')
            self.assertNotEqual(evaluator.evaluation_code_snapshot(root)[1],before)
            path.write_text('synthetic')

    def test_oracle_mutation_during_analysis_invalidates_run(self):
        def mutate():self.cases_path.write_bytes(self.cases_path.read_bytes()+b'\n')
        status,lines,_=self.run_evaluation(runtime=SyntheticRuntime(on_analysis=mutate))
        self.assertEqual(status,1);self.assertEqual(lines[-1]['status'],'failed')
        self.assertFalse(lines[-1]['provenance']['input_stable'])
        self.assertEqual(lines[-1]['provenance']['stability'],'changed')

    def test_config_mutation_during_analysis_invalidates_run(self):
        def mutate():self.config_path.write_bytes(self.config_path.read_bytes()+b'\n')
        status,lines,_=self.run_evaluation(runtime=SyntheticRuntime(on_analysis=mutate))
        self.assertEqual(status,1);self.assertFalse(lines[-1]['provenance']['config_stable'])

    def test_code_mutation_during_analysis_invalidates_run(self):
        mutated=[False];snapshot=evaluator.evaluation_code_snapshot
        def changed_snapshot():
            manifest,sha=snapshot()
            if mutated[0]:manifest['app/core.py']='0'*64;sha=evaluator._json_digest(manifest)
            return manifest,sha
        with patch.object(evaluator,'evaluation_code_snapshot',side_effect=changed_snapshot):
            status,lines,_=self.run_evaluation(runtime=SyntheticRuntime(on_analysis=lambda:mutated.__setitem__(0,True)))
        self.assertEqual(status,1);self.assertFalse(lines[-1]['provenance']['code_stable'])

    def test_input_permissions_symlink_and_error_text_are_safe(self):
        self.write_json(self.cases_path,self.document());self.cases_path.chmod(0o644)
        with self.assertRaises(ValueError):evaluator.read_evaluation_input(self.cases_path)
        self.cases_path.chmod(0o600);link=self.root/'link.json';link.symlink_to(self.cases_path)
        with self.assertRaises(OSError):evaluator.read_evaluation_input(link)
        def fail():raise RuntimeError('DO_NOT_LEAK_FICTIONAL_CONTENT')
        status,lines,_=self.run_evaluation(runtime=SyntheticRuntime(on_analysis=fail))
        self.assertEqual(status,1);self.assertNotIn('DO_NOT_LEAK_FICTIONAL_CONTENT',json.dumps(lines))
        self.assertEqual(lines[-1]['error_type'],'RuntimeError')

    def test_explicit_config_ignores_environment_and_existing_evidence_is_not_overwritten(self):
        runtime=SyntheticRuntime()
        with patch.dict(os.environ,{'LIBRUS_ANALYSIS_MODEL':'should-not-be-used','LIBRUS_STATE_PATH':'/does/not/exist'}):
            status,lines,factory=self.run_evaluation(runtime=runtime,details=True)
        self.assertEqual(status,0);self.assertEqual(factory.call_args.args[0].analysis_model,TEST_CONFIG.analysis_model)
        before=(self.root/'details.json').read_bytes()
        status,lines,_=self.run_evaluation(runtime=SyntheticRuntime(),details=True)
        self.assertEqual(status,1);self.assertEqual((self.root/'details.json').read_bytes(),before)

    def test_false_oracle_is_preserved_as_failed_evidence(self):
        document=self.document();document['cases'][0]['expected'][0]['start']='2030-03-13'
        status,lines,_=self.run_evaluation(document,details=True)
        self.assertEqual(status,1);self.assertEqual(lines[-1]['status'],'failed')
        details=json.loads((self.root/'details.json').read_text())
        self.assertFalse(details['results'][0]['passed'])
        self.assertEqual(details['transcript'][0]['expected'][0]['start'],'2030-03-13')


class RepeatStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=AppStore(Path(self.temp.name)/'state.sqlite3',config=replace(TEST_CONFIG,multi_source_mode='active'))
        self.store.ingest_sources({'sources':[source()],'learner':LEARNER,'complete_kinds':['announcement','school_event','substitution'],'status':'ok'},NOW)
        self.store.set_setting('source_activation_at','synthetic')
        self.store.set_setting('source_activation_learner',LEARNER['id'])
        self.store.set_setting('source_activation_binding',digest([TEST_CONFIG.calendar_id,TEST_CONFIG.calendar_owner]))
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',('message',NOW.isoformat(),NOW.isoformat(),'ok',None,1))
        self.runtime=evaluator.RecordingRuntime(SyntheticRuntime());self.planner=SourcePlanner(self.store,self.runtime)
        self.source=self.store.get_source(source_identity('announcement','fictional-source',LEARNER['id']))
        evaluator.analyze_and_record(self.planner,self.source,NOW);self.store.queue_operations(NOW)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def ingest(self,data,current):
        self.store.ingest_sources({'sources':[data],'learner':LEARNER,'complete_kinds':['announcement','school_event','substitution'],'status':'ok'},current)
        return self.store.get_source(self.source['id'])
    def test_payload_and_frozen_operation_changes_fail_even_without_new_ids_or_calls(self):
        for table in ('proposals','canonical_actions','operations'):
            with self.subTest(table=table):
                if table=='operations':
                    with self.store.db:self.store.db.execute("UPDATE operations SET attempts=1,status='unknown'")
                def mutate(data,current):
                    repeated=self.ingest(data,current)
                    row=self.store.db.execute('SELECT id,payload FROM '+table+' LIMIT 1').fetchone()
                    payload=json.loads(row['payload']);payload['description']='Mutated fictional intent.'
                    with self.store.db:self.store.db.execute('UPDATE '+table+' SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
                    return repeated
                result=evaluator.repeat_fixture(self.store,self.planner,mutate,source(),NOW)
                self.assertFalse(result['state_unchanged']);self.assertIn(table,result['changed_tables'])
                self.assertEqual(result['new_local_operations'],0);self.assertEqual(result['model_calls'],0)
    def test_only_proposal_updated_at_is_excluded(self):
        before=evaluator.durable_state_snapshot(self.store)
        with self.store.db:self.store.db.execute("UPDATE proposals SET updated_at='bookkeeping'")
        self.assertEqual(evaluator.durable_state_snapshot(self.store),before)
        with self.store.db:self.store.db.execute("UPDATE operations SET updated_at='frozen-operation-changed'")
        self.assertNotEqual(evaluator.durable_state_snapshot(self.store),before)
    def test_repeat_pending_queue_catches_reanalysis_even_when_no_operation_created(self):
        def changed(data,current):
            repeated=self.ingest(data,current)
            with self.store.db:self.store.db.execute("UPDATE sources SET status='pending' WHERE id=?",(repeated['id'],))
            return self.store.get_source(repeated['id'])
        result=evaluator.repeat_fixture(self.store,self.planner,changed,source(),NOW)
        self.assertGreater(result['model_calls'],0);self.assertEqual(len(result['recordings']),1)


if __name__=='__main__':unittest.main()

"""Synthetic multi-source contracts; no real pupil, IDs, dates or provider state."""
from dataclasses import replace
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_support import TEST_CONFIG
from app.core import AppStore,WARSAW,digest,normalize_proposal
from app.source_store import source_identity
from app.source_planner import SourcePlanner,prepare_source_proposals,verified_match
from app.source_admin import activate_sources,cleanup_existing

NOW=datetime(2030,3,1,12,tzinfo=WARSAW)
LEARNER={'id':'fictional-learner','class_id':'1x'}
KINDS=['announcement','school_event','substitution']
TEXT='Klasa 1x przygotuje papierowe serce na kiermasz wiosenny do 12 marca.'


def source(upstream='a',kind='announcement',text=TEXT,**changes):
    result={'kind':kind,'upstream_id':upstream,'learner_id':LEARNER['id'],'title':'Informacja szkolna','sender':'Fictional teacher',
            'published_at':'2030-03-01T10:00:00+01:00','updated_at':None,'text':text,'metadata':{},'deleted':False}
    result.update(changes);return result


def action(**changes):
    result={'kind':'create','title':'Przygotować papierowe serce','description':'Zadanie dla chętnych.','start':'2030-03-12','end':None,'all_day':True,
            'temporal_kind':'deadline','due_at':None,'activity_scope':'school','confidence':'high','needs_review':False,'review_reason':'',
            'source_quote':TEXT,'event_id':None,'alternative_group':None,'alternative_group_evidence':None,
            'optional':True,'date_certainty':'explicit','informational':False,'offer_identity':None,
            'recipient':{'scope':'class','class_id':'1x','learner_id':None,'evidence':'Klasa 1x'},
            'action_identity':{'action_type':'submit','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':TEXT}}
    result.update(changes);return result


class Runtime:
    read_only=True
    def __init__(self,actions=None,decision='match'):
        self.actions=actions if actions is not None else [action()]
        self.decision=decision;self.calls=[];self.last_analysis_metadata={}
    def analyze_source(self,source,context):
        self.calls.append(('analyze',source['id']))
        self.last_analysis_metadata={'model':'synthetic','effort':'high','prompt_version':'synthetic-v1','decision_reason':'Synthetic school action.'}
        return json.loads(json.dumps(self.actions))
    def match_source_action(self,source,item,candidates):
        self.calls.append(('match',len(candidates)))
        if self.decision=='distinct':return {'decision':'distinct'}
        if self.decision=='uncertain':return {'decision':'uncertain'}
        # Different offered times identify different choices.
        chosen=next((c for c in candidates if c['payload'].get('start')==item.get('start')),candidates[0])
        return {'decision':'match','action_id':chosen['id'],'source_quote':item['source_quote'],
                'candidate_quote':chosen['payload'].get('source_quote',''),'anchors':['papierowe serce','kiermasz wiosenny']}
    def probe(self):return {'calendar_id':TEST_CONFIG.calendar_id,'access_role':'owner'}
    def read_event(self,*args):raise AssertionError('Unexpected Calendar read')


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'private/state.sqlite3'
        self.store=AppStore(self.path,config=replace(TEST_CONFIG,multi_source_mode='active'))
        self.store.set_setting('source_activation_at','synthetic')
        self.store.set_setting('source_activation_learner',LEARNER['id'])
        self.store.set_setting('source_activation_binding',digest([TEST_CONFIG.calendar_id,TEST_CONFIG.calendar_owner]))
        with self.store.db:self.store.db.execute('INSERT INTO source_health VALUES (?,?,?,?,?,?)',('message',NOW.isoformat(),NOW.isoformat(),'ok',None,1))
    def tearDown(self):self.store.close();self.temp.cleanup()
    def ingest(self,data=None,mode=None,current=NOW):
        if mode:self.store.config=replace(self.store.config,multi_source_mode=mode)
        data=data or source()
        all_sources=[{k:v for k,v in old.items() if k not in ('content_hash','status','fetched_at','analyzed_at','last_error','outcome','reason','action_ids')} for old in self.store.list_sources(limit=500) if old['kind']!='message' and old['upstream_id']!=data['upstream_id'] and not old['deleted']]
        self.store.ingest_sources({'sources':all_sources+[data],'complete_kinds':KINDS,'learner':LEARNER,'status':'ok'},current)
        return self.store.get_source(source_identity(data['kind'],data['upstream_id'],data['learner_id']))
    def analyze(self,data=None,actions=None,runtime=None):
        src=self.ingest(data);rt=runtime or Runtime(actions)
        return SourcePlanner(self.store,rt).analyze(src,NOW)
    def test_preview_never_changes_legacy_proposals_decisions_links_or_operations(self):
        src=self.ingest(mode='preview')
        before={t:list(self.store.db.execute('SELECT * FROM '+t)) for t in ('proposals','operations','canonical_actions','action_sources','decision_intents')}
        SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        for table,rows in before.items():self.assertEqual(rows,list(self.store.db.execute('SELECT * FROM '+table)))
        self.assertEqual(self.store.list_source_previews()[0]['status'],'ready')
        self.assertEqual(self.store.source_status()['preview_pending'],0)
        self.assertEqual(self.store.pending_sources(preview=True),[])
    def test_active_optional_school_action_and_same_day_date_deadline(self):
        result=self.analyze(actions=[action(start='2030-03-01')])
        self.assertEqual(result['outcomes'][0]['outcome'],'added')
        p=self.store.list_proposals()[0];self.assertTrue(p['optional']);self.assertEqual(p['status'],'pending')
        self.assertIn('Opcjonalne.',p['description'])
        self.assertEqual(len(self.store.queue_operations(NOW)),1)
    def test_repeat_and_cross_source_link_do_not_create_more_operations(self):
        self.analyze();self.assertEqual(len(self.store.queue_operations(NOW)),1)
        result=self.analyze(source('b',kind='school_event'))
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(len(self.store.list_actions()[0]['source_links']),2)
        self.assertEqual(self.store.queue_operations(NOW),[])
    def test_rejected_cross_source_action_is_never_resurrected(self):
        result=self.analyze();pid=result['outcomes'][0]['proposal_id']
        self.store.resolve_proposal(pid,'ignore')
        result=self.analyze(source('b',published_at='2030-03-02T10:00:00+01:00'),actions=[action(title='Oddać serce')])
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored')
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertTrue(self.store.get_action(pid)['payload']['user_rejected'])
    def test_newer_source_restores_lost_exact_deadline_without_new_action(self):
        first=self.analyze();pid=first['outcomes'][0]['proposal_id']
        self.analyze(source('b',published_at='2030-03-02T10:00:00+01:00'),[action(due_at='2030-03-12T13:00:00+01:00')])
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(self.store.get_action(pid)['payload']['due_at'],'2030-03-12T13:00:00+01:00')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)
    def test_older_source_cannot_regress_newer_automatic_deadline(self):
        first=self.analyze(source(published_at='2030-03-03T10:00:00+01:00'),[action(due_at='2030-03-12T13:00:00+01:00')])
        self.analyze(source('b',published_at='2030-03-02T10:00:00+01:00'),[action()])
        self.assertEqual(self.store.get_action(first['outcomes'][0]['action_id'])['payload']['due_at'],'2030-03-12T13:00:00+01:00')
    def test_two_different_occasions_never_merge_from_title_or_date(self):
        self.analyze()
        self.analyze(source('b'),[action(title='Przygotować papierowe serce',start='2030-03-28',action_identity={'action_type':'submit','object':'papierowe serce','occasion':'występ klasy','evidence':TEXT})],runtime=Runtime([action(start='2030-03-28')],decision='distinct'))
        self.assertEqual(len(self.store.list_actions()),2)
    def test_uncertain_candidate_never_auto_creates_duplicate(self):
        self.analyze();self.analyze(source('b'),runtime=Runtime(decision='uncertain'))
        self.assertEqual(len(self.store.list_proposals('pending')),1)
        self.assertEqual(len(self.store.list_proposals('review')),1)
    def test_other_class_conditional_and_unknown_recipient_are_guarded(self):
        src=self.ingest()
        for recipient in ({'scope':'class','class_id':'2y','evidence':'Klasa 1x'},{'scope':'conditional','evidence':'Klasa 1x'},{'scope':'unknown','evidence':'Klasa 1x'}):
            with self.subTest(recipient=recipient):self.assertEqual(prepare_source_proposals(src,[action(recipient=recipient)],LEARNER,NOW)[0]['status'],'review')
        self.assertEqual(prepare_source_proposals(src,[action(recipient={'scope':'other'})],LEARNER,NOW)[0]['status'],'ignored')

    def test_class_recipient_evidence_in_title_allows_body_task(self):
        body='Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.'
        src=self.ingest(source(title='Klasa 1x — przygotowanie do kiermaszu',text=body))
        item=action(source_quote=body,action_identity={**action()['action_identity'],'evidence':body})
        self.assertNotIn(item['recipient']['evidence'],body)
        result=SourcePlanner(self.store,Runtime([item])).analyze(src,NOW)
        self.assertEqual(result['outcomes'][0]['outcome'],'added')
        self.assertEqual(self.store.list_proposals()[0]['status'],'pending')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_title_recipient_evidence_preserves_identity_and_scope_guards(self):
        body='Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.'
        src=self.ingest(source(title='Klasa 1x — przygotowanie do kiermaszu',text=body))
        item=action(source_quote=body,action_identity={**action()['action_identity'],'evidence':body})
        cases=[({'scope':'class','class_id':'2y','evidence':'Klasa 1x'},'Recipient class'),
               ({'scope':'class','class_id':None,'evidence':'Klasa 1x'},'Recipient class'),
               ({'scope':'learner','learner_id':'another-learner','evidence':'Klasa 1x'},'Recipient learner'),
               ({'scope':'unknown','class_id':'1x','evidence':'Klasa 1x'},'Conditional or unknown'),
               ({'scope':'conditional','class_id':'1x','evidence':'Klasa 1x'},'Conditional or unknown'),
               ({'scope':'class','class_id':'1x','evidence':''},'Recipient applicability'),
               ({'scope':'class','class_id':'1x','evidence':None},'Recipient applicability'),
               ({'scope':'class','class_id':'1x'},'Recipient applicability'),
               ({'scope':'class','class_id':'1x','evidence':'Klasa 2y'},'Recipient applicability')]
        for recipient,reason in cases:
            with self.subTest(recipient=recipient):
                prepared=prepare_source_proposals(src,[dict(item,recipient=recipient)],LEARNER,NOW)[0]
                self.assertEqual(prepared['status'],'review')
                self.assertIn(reason,prepared['review_reason'])

    def test_recipient_evidence_cannot_cross_title_body_or_use_other_fields(self):
        for changes in ({'title':'Ogłoszenie: Klasa','text':'1x przygotuje papierowe serce na kiermasz wiosenny do 12 marca.'},
                        {'title':'Przygotowanie do kiermaszu','text':'Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.',
                         'sender':'Klasa 1x','metadata':{'recipient':'Klasa 1x'}}):
            with self.subTest(changes=changes):
                src=self.ingest(source(**changes))
                item=action(source_quote=src['text'],action_identity={**action()['action_identity'],'evidence':src['text']})
                prepared=prepare_source_proposals(src,[item],LEARNER,NOW)[0]
                self.assertEqual(prepared['status'],'review')
                self.assertEqual(prepared['review_reason'],'Recipient applicability lacks literal evidence.')

    def test_title_recipient_support_does_not_widen_action_or_date_evidence(self):
        body='Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.'
        src=self.ingest(source(title='Klasa 1x — materiały na kiermasz',text=body))
        item=action(source_quote=body,action_identity={**action()['action_identity'],'evidence':body})
        for changed,reason in ((dict(item,source_quote='materiały na kiermasz'),'Literal source evidence'),
                               (dict(item,action_identity={**item['action_identity'],'evidence':'materiały na kiermasz'}),'Typed action identity'),
                               (dict(item,date_certainty='unknown'),'Date certainty')):
            with self.subTest(reason=reason):
                prepared=prepare_source_proposals(src,[changed],LEARNER,NOW)[0]
                self.assertEqual(prepared['status'],'review')
                self.assertIn(reason,prepared['review_reason'])

    def test_title_change_invalidates_recipient_evidence_before_preview_or_apply(self):
        body='Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.'
        item=action(source_quote=body,action_identity={**action()['action_identity'],'evidence':body})
        for preview in (True,False):
            with self.subTest(preview=preview):
                data=source('title-race-'+str(preview),title='Klasa 1x — kiermasz',text=body)
                src=self.ingest(data,mode='preview' if preview else 'active')
                runtime=Runtime([item]);analyze=runtime.analyze_source
                def changed_during_analysis(*args):
                    result=analyze(*args)
                    self.ingest(dict(data,title='Klasa 2y — kiermasz'))
                    return result
                runtime.analyze_source=changed_during_analysis
                with self.assertRaisesRegex(ValueError,'Source.*changed'):
                    SourcePlanner(self.store,runtime).analyze(src,NOW,preview=preview)
                current=self.store.get_source(src['id'])
                self.assertNotEqual(src['content_hash'],current['content_hash'])
                self.assertEqual(self.store.list_source_previews(),[])
                self.assertEqual(self.store.list_proposals(),[])
                self.assertEqual(self.store.list_actions(),[])
                self.assertEqual(self.store.pending_operations(),[])

    def test_message_subject_change_versions_mirrored_recipient_title(self):
        data={'id':'title-recipient-message','subject':'Klasa 1x — kiermasz','sender':'Fictional teacher',
              'sent_at':NOW.isoformat(),'text':'Przygotujcie papierowe serce na kiermasz wiosenny do 12 marca.'}
        self.store.save_message(data)
        original=self.store.get_source('message:'+data['id'])
        self.store.save_message(dict(data,subject='Klasa 2y — kiermasz'))
        changed=self.store.get_source(original['id'])
        self.assertEqual((original['title'],changed['title']),('Klasa 1x — kiermasz','Klasa 2y — kiermasz'))
        self.assertEqual(original['text'],changed['text'])
        self.assertNotEqual(original['content_hash'],changed['content_hash'])
    def test_undated_roles_and_substitutions_never_create_events(self):
        src=self.ingest()
        p=prepare_source_proposals(src,[action(start='',date_certainty='undated',informational=True)],LEARNER,NOW)[0]
        self.assertEqual(p['kind'],'ignore')
        rt=Runtime();src=self.ingest(source('s',kind='substitution'))
        SourcePlanner(self.store,rt).analyze(src,NOW)
        self.assertEqual(rt.calls,[]);self.assertEqual(self.store.list_proposals(),[])
    def test_source_edit_keeps_action_identity_and_decisions(self):
        first=self.analyze();aid=first['outcomes'][0]['action_id']
        self.store.resolve_proposal(aid,'ignore')
        changed=TEXT+' Uzupełnienie.'
        self.analyze(source(text=changed),[action(source_quote=changed)])
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(self.store.get_action(aid)['status'],'ignored')
        self.assertEqual(self.store.queue_operations(NOW),[])
    def test_source_delete_never_cancels_calendar_and_blocks_pending(self):
        self.analyze();self.store.queue_operations(NOW)
        self.ingest(source(deleted=True))
        self.assertEqual(len(self.store.list_proposals('review')),1)
        self.assertEqual(self.store.pending_operations(),[])
        self.assertEqual(self.store.list_events(),[])
    def test_content_hash_ignores_fetch_and_window_metadata(self):
        first=self.ingest();second=self.ingest(source(metadata={'fetched_at':'later','window_start':'different'}))
        self.assertEqual(first['content_hash'],second['content_hash'])
    def test_learner_pin_cannot_switch(self):
        self.ingest()
        with self.assertRaises(ValueError):self.store.ingest_sources({'sources':[],'learner':{'id':'other'},'complete_kinds':KINDS,'status':'ok'})
    def test_pagination_and_truncated_candidates_are_never_claimed_unique(self):
        # More than a page of actions must be visible to candidate selection.
        self.analyze()
        original=self.store.list_actions()[0]
        with self.store.db:
            for i in range(130):
                p=dict(original['payload'],id='fictional-%03d'%i,canonical_action_id='fictional-%03d'%i)
                self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',(p['id'],p['id'],json.dumps(p),'review','2030-03-01'))
        self.assertEqual(len(self.store.list_actions(limit=100,offset=100)),31)
        result=self.analyze(source('b'))
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
    def test_outside_candidate_or_forged_quote_is_not_a_match(self):
        self.analyze();src=self.ingest(source('b'));item=prepare_source_proposals(src,[action()],LEARNER,NOW)[0]
        candidates=self.store.candidate_actions(item)
        result={'decision':'match','action_id':'outside','source_quote':TEXT,'candidate_quote':TEXT,'anchors':['papierowe serce','kiermasz wiosenny']}
        self.assertEqual(verified_match(src,item,candidates,result),(None,'uncertain'))
        result.update(action_id=candidates[0]['id'],candidate_quote='Invented text')
        self.assertEqual(verified_match(src,item,candidates,result),(None,'uncertain'))
    def test_legacy_migration_preserves_all_identifiers_and_verified_context(self):
        message={'id':'legacy','subject':'A','sender':'Teacher','sent_at':'2030-03-01T10:00:00+01:00','text':TEXT}
        self.store.save_message(message);legacy=self.store.save_proposals('legacy',[action()],NOW)[0]
        before=self.store.list_proposals()[0]['id'];self.ingest()
        self.store.close();self.store=AppStore(self.path,config=replace(TEST_CONFIG,multi_source_mode='active'))
        self.assertEqual(self.store.get_source('message:legacy')['learner_id'],LEARNER['id'])
        self.assertEqual(self.store.get_source('message:legacy')['status'],'analyzed')
        self.assertIsNotNone(self.store.get_action(before));self.assertEqual(self.store.list_proposals()[0]['id'],before)
        self.analyze(source('b'));self.assertEqual(len(self.store.list_actions()),1)
    def test_preview_retention_removes_text_without_rehydrating_unchanged_source(self):
        src=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        with self.store.db:counts=self.store.prune_source_content(NOW+timedelta(days=31),apply=True)
        self.assertEqual(counts['sources_pruned'],1);self.assertEqual(self.store.list_source_previews(),[])
        src=self.ingest(current=NOW+timedelta(days=32));self.assertEqual(src['text'],'');self.assertEqual(src['status'],'retained')
    def test_activation_refuses_changed_preview_and_never_executes_saved_payload(self):
        src=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        plan=self.path.parent/'activation.json';activate_sources(self.store,Runtime(),plan_path=plan)
        self.store.config=replace(self.store.config,multi_source_mode='active')
        self.ingest(source(text=TEXT+' Changed.'))
        with self.assertRaises(ValueError):activate_sources(self.store,Runtime(),plan_path=plan,apply=True,current=NOW)
        self.assertEqual(self.store.list_proposals(),[])
    def test_activation_fresh_analysis_ignores_poisoned_preview(self):
        src=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        with self.store.db:self.store.db.execute("UPDATE source_previews SET payload=?",(json.dumps({'proposals':[{'title':'poisoned preview'}]}),))
        plan=self.path.parent/'activation.json';activate_sources(self.store,Runtime(),plan_path=plan)
        self.store.config=replace(self.store.config,multi_source_mode='active');rt=Runtime()
        result=activate_sources(self.store,rt,plan_path=plan,apply=True,current=NOW,client_factory=lambda:type('Client',(),{'collect_sources':lambda *a,**kw:{'sources':[source()],'learner':LEARNER,'complete_kinds':KINDS,'status':'ok'}})())
        self.assertEqual(result['calendar_writes'],0);self.assertEqual(rt.calls[0][0],'analyze')
        self.assertNotIn('poisoned',self.store.list_proposals()[0]['title'])
    def test_cleanup_refuses_stale_snapshot_and_never_changes_calendar(self):
        self.analyze();pid=self.store.list_proposals()[0]['id'];plan=self.path.parent/'cleanup.json'
        cleanup_existing(self.store,plan_path=plan,request={'changes':[{'proposal_id':pid,'reason':'Synthetic obsolete duplicate.'}]})
        self.store.resolve_proposal(pid,'ignore')
        with self.assertRaises(ValueError):cleanup_existing(self.store,plan_path=plan,apply=True)
        self.assertEqual(self.store.list_events(),[])
    def test_cas_rejects_human_decision_during_matching(self):
        self.analyze();src=self.ingest(source('b'));rt=Runtime();original=rt.match_source_action
        def racing(*args):
            result=original(*args);self.store.resolve_proposal(self.store.list_proposals()[0]['id'],'ignore');return result
        rt.match_source_action=racing
        with self.assertRaises(ValueError):SourcePlanner(self.store,rt).analyze(src,NOW)
        self.assertEqual(len(self.store.list_proposals()),1)

    def test_same_date_cancellation_replaces_pending_creation_with_review(self):
        first=self.analyze();aid=first['outcomes'][0]['action_id'];self.store.queue_operations(NOW)
        result=self.analyze(source(text=TEXT+' Odwołane.'),[action(kind='cancel',source_quote=TEXT+' Odwołane.')])
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertEqual(self.store.get_action(aid)['payload']['kind'],'cancel')
        self.assertEqual(self.store.get_action(aid)['status'],'review')
        self.assertEqual(self.store.pending_operations(),[])
    def test_more_than_twelve_unrelated_actions_do_not_pollute_candidate_search(self):
        with self.store.db:
            for i in range(25):
                p=action(title='Unrelated',source_quote='Unrelated content',action_identity={'action_type':'collect','object':'UniqueObject'+str(i),'occasion':'UniqueOccasion'+str(i),'evidence':'Unrelated content'})
                self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',(str(i),str(i),json.dumps(p),'review','2030-03-01'))
        src=self.ingest();item=prepare_source_proposals(src,[action()],LEARNER,NOW)[0]
        self.assertEqual(self.store.candidate_actions(item),[])
        self.assertTrue(self.store._source_candidates_complete)
        self.assertEqual(SourcePlanner(self.store,Runtime()).analyze(src,NOW)['outcomes'][0]['outcome'],'added')
    def test_activation_refuses_partial_channels_and_missing_previews(self):
        src=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        with self.store.db:self.store.db.execute("UPDATE source_health SET last_result='partial' WHERE kind='school_event'")
        with self.assertRaises(ValueError):activate_sources(self.store,Runtime(),plan_path=self.path.parent/'partial.json')
        self.ingest(source('b'))
        with self.assertRaises(ValueError):activate_sources(self.store,Runtime(),plan_path=self.path.parent/'missing.json')
    def test_active_mode_without_receipt_cannot_queue_source_operations(self):
        self.analyze();self.store.set_setting('source_activation_at','')
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertEqual(len(self.store.list_proposals('review')),1)

    def test_unknown_publication_never_automatically_regresses_known_facts(self):
        first=self.analyze();aid=first['outcomes'][0]['action_id']
        result=self.analyze(source('unknown',published_at=None),[action(start='2030-03-11')])
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertEqual(self.store.get_action(aid)['status'],'review')
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_preview_and_off_never_execute_or_modify_preexisting_source_operations(self):
        self.analyze();operation=self.store.queue_operations(NOW)[0];self.store.set_writes_paused(False)
        before=self.store.list_proposals()
        for mode in ('preview','off'):
            self.store.config=replace(self.store.config,multi_source_mode=mode)
            self.assertEqual(self.store.queue_operations(NOW),[])
            self.assertFalse(self.store.claim_operation(operation['operation_id']))
            self.assertEqual(self.store.list_proposals(),before)
    def test_complete_missing_announcement_blocks_unwritten_without_calendar_cancel(self):
        self.analyze();self.store.queue_operations(NOW)
        self.store.ingest_sources({'sources':[],'learner':LEARNER,'complete_kinds':KINDS,'status':'ok'},NOW)
        self.assertTrue(self.store.list_sources()[0]['deleted'])
        self.assertEqual(self.store.pending_operations(),[])
        self.assertEqual(self.store.list_events(),[])
    def test_incomplete_or_outside_window_absence_does_not_delete(self):
        src=self.ingest(source(kind='school_event',metadata={'event_start':'2030-07-01'}))
        self.store.ingest_sources({'sources':[],'learner':LEARNER,'complete_kinds':[],'status':'partial'},NOW)
        self.assertFalse(self.store.get_source(src['id'])['deleted'])
        self.store.ingest_sources({'sources':[],'learner':LEARNER,'complete_kinds':KINDS,'status':'ok','window_start':'2030-02-01','window_end':'2030-05-01'},NOW)
        self.assertFalse(self.store.get_source(src['id'])['deleted'])
    def test_unknown_publication_projection_content_expires(self):
        self.analyze(source(published_at=None))
        from app.data_admin import prune_state
        prune_state(self.store,current=NOW+timedelta(days=31),apply=True)
        for row in self.store.db.execute('SELECT payload FROM messages'):
            self.assertEqual(json.loads(row[0]).get('text',''),'')
        self.assertEqual(self.store.list_sources()[0]['text'],'')

    def test_preview_matches_other_previews_without_creating_active_actions(self):
        src=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        second=self.ingest(source('b',kind='school_event'))
        result=SourcePlanner(self.store,Runtime()).analyze(second,NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        previews=self.store.list_source_previews()
        targets={x['outcomes'][0]['action_id'] for x in previews}
        self.assertEqual(len(targets),1)
        self.assertEqual(self.store.list_actions(),[])
        self.assertEqual(self.store.list_proposals(),[])
    def test_preview_displays_rejection_and_preserves_active_decision(self):
        first=self.analyze();self.store.resolve_proposal(first['outcomes'][0]['proposal_id'],'ignore')
        src=self.ingest(source('b'),mode='preview');before=self.store.source_decision_snapshot()
        result=SourcePlanner(self.store,Runtime()).analyze(src,NOW,preview=True)
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored')
        self.assertEqual(before,self.store.source_decision_snapshot())
    def test_rejected_action_after_text_retention_still_does_not_resurface(self):
        first=self.analyze();self.store.resolve_proposal(first['outcomes'][0]['proposal_id'],'ignore')
        from app.data_admin import prune_state
        prune_state(self.store,current=NOW+timedelta(days=31),apply=True)
        result=self.analyze(source('b',published_at='2030-04-02T10:00:00+02:00'))
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored')
        self.assertEqual(len(self.store.list_actions()),1)
    def test_conditional_parent_role_is_informational_even_with_date(self):
        src=self.ingest();p=action(recipient={'scope':'conditional','condition':'role','evidence':'Klasa 1x'})
        self.assertEqual(prepare_source_proposals(src,[p],LEARNER,NOW)[0]['status'],'ignored')
    def test_closed_five_choice_offer_holds_new_sixth_option_without_singleton_group(self):
        text='Klasa 1x: trening piłkarski, grupa szkolna. Do wyboru jeden termin.'
        def offers(count):
            return [action(title='Trening piłkarski',start='2030-03-%02d'%(10+i),temporal_kind='event',activity_scope='extracurricular',optional=False,
                source_quote=text,alternative_group='offer',alternative_group_evidence=text,
                action_identity={'action_type':'attend','object':'trening piłkarski','occasion':'grupa szkolna','evidence':text},
                offer_identity={'label':'trening piłkarski','evidence':text}) for i in range(count)]
        class OfferRuntime(Runtime):
            def match_source_action(self,src,item,candidates):
                chosen=next((x for x in candidates if x['payload'].get('start')==item.get('start')),None)
                return {'decision':'distinct'} if chosen is None else {'decision':'match','action_id':chosen['id'],'source_quote':text,'candidate_quote':text,'anchors':['trening piłkarski','grupa szkolna']}
        self.analyze(source(text=text),runtime=OfferRuntime(offers(5)))
        group=self.store.list_proposal_groups()[0]
        self.store.resolve_proposal_group(group['id'],'reject',expected_revision=group['revision'],current=NOW)
        result=self.analyze(source('b',kind='school_event',text=text),runtime=OfferRuntime(offers(6)))
        self.assertTrue(all(x['outcome']=='ignored' for x in result['outcomes']))
        groups=self.store.list_proposal_groups();self.assertEqual(len(groups),1);self.assertEqual(groups[0]['status'],'rejected')
        self.assertEqual(self.store.list_proposals('review'),[])
        self.assertEqual(self.store.queue_operations(NOW),[])
    def test_owned_event_without_proposal_is_indexed_and_cannot_auto_duplicate(self):
        event={'id':'fictional-owned','summary':'Przygotować papierowe serce','start':{'date':'2030-03-12'},'end':{'date':'2030-03-13'},'description':'librus-calendar:fictional-owner'}
        with self.store.db:self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',('fictional-owned',TEST_CONFIG.calendar_id,'legacy-source','missing-proposal',json.dumps(event),digest(event),'librus-calendar:fictional-owner','2030-03-01','active'))
        self.store.close();self.store=AppStore(self.path,config=replace(TEST_CONFIG,multi_source_mode='active'))
        self.assertIsNotNone(self.store.get_action('missing-proposal'))
        result=self.analyze()
        self.assertEqual(result['outcomes'][0]['outcome'],'review')
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_partial_source_channel_defers_new_canonical_writes_without_discarding_intent(self):
        self.analyze()
        with self.store.db:self.store.db.execute("UPDATE source_health SET last_result='partial' WHERE kind='school_event'")
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertEqual(len(self.store.list_proposals('pending')),1)
        with self.store.db:self.store.db.execute("UPDATE source_health SET last_result='ok' WHERE kind='school_event'")
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_nonmessage_projection_is_hidden_from_legacy_messages(self):
        self.analyze()
        self.assertEqual(self.store.list_messages(),[])
        self.assertEqual(self.store.status()['messages'],0)
        self.assertEqual(len(self.store.list_sources()),1)
        proposal=self.store.list_proposals()[0]
        self.assertIsNotNone(self.store.get_message(proposal['message_id']))
        with self.assertRaises(ValueError):self.store.cached_reanalysis_scope(proposal['message_id'])

    def test_duplicate_extraction_in_one_response_yields_one_action_and_operation(self):
        self.analyze(actions=[action(),action(),action(title='Oddać papierowe serce')])
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(len(self.store.queue_operations(NOW)),1)
    def test_changed_source_preserves_unknown_journal_and_visible_correction(self):
        first=self.analyze();aid=first['outcomes'][0]['action_id'];operation=self.store.queue_operations(NOW)[0]
        self.store.set_writes_paused(False);self.assertTrue(self.store.claim_operation(operation['operation_id']))
        self.store.recover_interrupted()
        before=dict(self.store.db.execute('SELECT * FROM operations').fetchone())
        self.analyze(source(text=TEXT+' Korekta.'),[action(start='2030-03-11',source_quote=TEXT+' Korekta.')])
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM operations').fetchone()),before)
        correction=self.store.get_action(aid)['payload'];self.assertEqual(correction['status'],'review');self.assertEqual(correction['start'],'2030-03-11')
        self.assertEqual(correction['deferred_unknown_operation'],operation['operation_id'])
        event={**operation['event'],'id':'fictional-reconciled'}
        self.store.record_result(operation['operation_id'],{'status':'applied','event_id':event['id'],'snapshot':event,'fingerprint':digest(event),'reconciled':True})
        correction=self.store.get_action(aid)['payload'];self.assertEqual(correction['event_id'],event['id']);self.assertEqual(correction['status'],'review')
        self.assertNotIn('deferred_unknown_operation',correction)

    def test_unknown_create_then_cancel_waits_for_reconciled_exact_target(self):
        first=self.analyze();aid=first['outcomes'][0]['action_id'];operation=self.store.queue_operations(NOW)[0]
        self.store.set_writes_paused(False);self.store.claim_operation(operation['operation_id']);self.store.recover_interrupted()
        self.analyze(source(text=TEXT+' Odwołane.'),[action(kind='cancel',source_quote=TEXT+' Odwołane.')])
        correction=self.store.get_action(aid)['payload']
        self.assertEqual(correction['kind'],'cancel');self.assertIn('deferred_unknown_operation',correction)
        event={**operation['event'],'id':'fictional-reconciled-cancel-target'}
        self.store.record_result(operation['operation_id'],{'status':'applied','event_id':event['id'],'snapshot':event,'fingerprint':digest(event),'reconciled':True})
        correction=self.store.get_action(aid)['payload'];self.assertEqual(correction['kind'],'cancel');self.assertEqual(correction['event_id'],event['id'])
        approved=self.store.resolve_proposal(correction['id'],'confirm_cancel')
        self.assertEqual(approved['event_id'],event['id']);self.assertEqual(approved['kind'],'cancel')

    def test_active_reanalysis_refuses_before_runtime_initialization(self):
        from app.sync import SyncController
        self.store.save_message({'id':'unlinked-message','subject':'Synthetic','sender':'Teacher','sent_at':NOW.isoformat(),'text':TEXT})
        def forbidden_runtime():raise AssertionError('Runtime must not initialize')
        result=SyncController(self.store,runtime_factory=forbidden_runtime).reanalyze(['unlinked-message'],apply=True,current=NOW)
        self.assertEqual(result['status'],'refused');self.assertEqual(result['calendar_writes'],0)
    def test_linked_message_without_owned_proposals_cannot_bypass_reanalysis_after_mode_switch(self):
        from app.sync import SyncController
        self.analyze()
        self.store.save_message({'id':'linked-message','subject':'Synthetic','sender':'Teacher','sent_at':NOW.isoformat(),'text':TEXT})
        SourcePlanner(self.store,Runtime()).analyze(self.store.get_source('message:linked-message'),NOW)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM proposals WHERE message_id=?',('linked-message',)).fetchone()[0],0)
        before=self.store.source_decision_snapshot()
        for mode in ('active','preview','off'):
            self.store.config=replace(self.store.config,multi_source_mode=mode)
            with self.assertRaises(ValueError):self.store.cached_reanalysis_scope('linked-message')
            def forbidden_runtime():raise AssertionError('Linked source must be refused before runtime initialization')
            result=SyncController(self.store,runtime_factory=forbidden_runtime).reanalyze(['linked-message'],apply=True,current=NOW)
            self.assertEqual(result['status'],'refused')
        self.assertEqual(self.store.source_decision_snapshot(),before)
        self.assertEqual(len(self.store.list_actions()),1)

    def test_open_five_choice_offer_new_sixth_cannot_be_approved_independently(self):
        text='Klasa 1x: trening piłkarski, grupa szkolna. Do wyboru jeden termin.'
        def offers(count):
            return [action(title='Trening piłkarski',start='2030-03-%02d'%(10+i),temporal_kind='event',activity_scope='extracurricular',optional=False,
                source_quote=text,alternative_group='offer',alternative_group_evidence=text,
                action_identity={'action_type':'attend','object':'trening piłkarski','occasion':'grupa szkolna','evidence':text},
                offer_identity={'label':'trening piłkarski','evidence':text}) for i in range(count)]
        class OfferRuntime(Runtime):
            def match_source_action(self,src,item,candidates):
                chosen=next((x for x in candidates if x['payload'].get('start')==item.get('start')),None)
                return {'decision':'distinct'} if chosen is None else {'decision':'match','action_id':chosen['id'],'source_quote':text,'candidate_quote':text,'anchors':['trening piłkarski','grupa szkolna']}
        self.analyze(source(text=text),runtime=OfferRuntime(offers(5)))
        original=self.store.list_proposal_groups()[0]
        self.analyze(source('b',kind='school_event',text=text),runtime=OfferRuntime(offers(6)))
        sixth=next(p for p in self.store.list_proposals() if p['start']=='2030-03-15')
        with self.assertRaises(ValueError):self.store.resolve_proposal(sixth['id'],'approve')
        self.assertEqual(sixth.get('blocked_by_offer_group'),original['id'])
        current=self.store.get_proposal_group(original['id'],NOW)
        selected=current['members'][0]['id']
        self.store.resolve_proposal_group(original['id'],'select',selected,expected_revision=current['revision'],current=NOW)
        queued=self.store.queue_operations(NOW)
        self.assertEqual(len(queued),1)
        self.assertEqual(queued[0]['proposal_id'],selected)
        with self.assertRaises(ValueError):self.store.resolve_proposal(sixth['id'],'approve')
        # Even a malformed injected local pending status cannot bypass claim.
        with self.store.db:self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?",(sixth['id'],))
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertNotIn(sixth['id'],[o['proposal_id'] for o in self.store.pending_operations()])

    def test_preview_backlog_distinguishes_ready_failed_and_unanalyzed_versions(self):
        ready=self.ingest(mode='preview');SourcePlanner(self.store,Runtime()).analyze(ready,NOW,preview=True)
        failed=self.ingest(source('failed'));self.store.fail_source(failed['id'],preview=True)
        self.ingest(source('pending'))
        status=self.store.source_status();counts=status['counts']['announcement']
        self.assertEqual((counts['preview_ready'],counts['preview_failed'],counts['preview_pending']),(1,1,1))
        self.assertEqual((status['preview_count'],status['preview_failed'],status['preview_pending']),(1,1,1))


if __name__=='__main__':unittest.main()

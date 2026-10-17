"""Fictional source proof, instruction isolation and commit-time authority tests."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.codex_runtime import CodexRuntime,SOURCE_PROMPT_VERSION
from app.source_planner import SourcePlanner,prepare_source_proposals,verified_match
import test_source_planner as fixtures
from test_source_planner import Runtime,source,action,NOW,LEARNER,TEXT
from test_support import TEST_CONFIG


SHORT='Klasa 1x przyniesie serce.'
BODY=SHORT+' '+TEXT


class ContextRuntime(Runtime):
    def match_source_action(self,src,item,candidates):
        chosen=candidates[0];proof=chosen['evidence'][0]
        return {'decision':'match','action_id':chosen['id'],'evidence_id':proof['id'],
                'source_quote':item['source_quote'],'candidate_quote':proof['text'],
                'anchors':['papierowe serce','kiermasz wiosenny'],'reason':'Fictional grounded match.'}


class SourceEvidenceTests(unittest.TestCase):
    setUp=fixtures.SourcesTests.setUp
    tearDown=fixtures.SourcesTests.tearDown
    ingest=fixtures.SourcesTests.ingest

    def seed(self,quote=SHORT,body=BODY,**changes):
        self.ingest()
        self.store.save_message({'id':'fictional-legacy','subject':'Kiermasz','sender':'Fictional teacher',
                                 'sent_at':'2030-03-01T10:00:00+01:00','text':body})
        item=action(source_quote=quote,**changes)
        if 'action_identity'not in changes:item.pop('action_identity')
        if 'recipient'not in changes:item.pop('recipient')
        proposal=self.store.save_proposals('fictional-legacy',[item],NOW)[0]
        return proposal['id'],self.store.get_source('message:fictional-legacy')

    def matching(self,aid,src=None):
        src=src or self.ingest(source('fresh'))
        item=prepare_source_proposals(src,[action()],LEARNER,NOW)[0]
        candidate=self.store.get_action(aid)
        result=ContextRuntime().match_source_action(src,item,[candidate])
        return verified_match(src,item,[candidate],result),candidate,result

    def test_current_linked_context_restores_short_legacy_quote_match(self):
        aid,_=self.seed();src=self.ingest(source('fresh'))
        proof=self.store.get_action(aid)['evidence'][0]
        self.assertEqual(proof['kind'],'current_context')
        self.assertEqual(proof['source_quote'],SHORT)
        self.assertLessEqual(len(proof['text']),2000)
        result=SourcePlanner(self.store,ContextRuntime()).analyze(src,NOW)
        self.assertEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(len(self.store.get_action(aid)['source_links']),2)

    def test_unavailable_context_never_supplies_additional_anchor_words(self):
        aid,old=self.seed();row=dict(self.store.db.execute('SELECT * FROM sources WHERE id=?',(old['id'],)).fetchone())
        src=self.ingest(source('fresh'))
        for state in ('stale','deleted','retained','quote_absent','retained_metadata'):
            with self.subTest(state=state),self.store.db:
                self.store.db.execute('UPDATE sources SET payload=?,content_hash=?,deleted=?,status=? WHERE id=?',
                    (row['payload'],row['content_hash'],row['deleted'],row['status'],old['id']))
                if state=='stale':self.store.db.execute('UPDATE sources SET content_hash=? WHERE id=?',('changed-version',old['id']))
                elif state=='deleted':self.store.db.execute('UPDATE sources SET deleted=1 WHERE id=?',(old['id'],))
                elif state=='retained':self.store.db.execute("UPDATE sources SET status='retained' WHERE id=?",(old['id'],))
                else:
                    payload=json.loads(row['payload'])
                    if state=='quote_absent':payload['text']=TEXT
                    else:payload['metadata']={'retained':True}
                    self.store.db.execute('UPDATE sources SET payload=? WHERE id=?',(json.dumps(payload),old['id']))
                matched,candidate,_=self.matching(aid,src)
                self.assertEqual(candidate['evidence'][0]['kind'],'historical_quote')
                self.assertEqual(candidate['evidence'][0]['text'],SHORT)
                self.assertEqual(matched,(None,'uncertain'))

    def test_original_historical_quote_survives_source_revision(self):
        aid,old=self.seed(quote=TEXT,body=TEXT)
        with self.store.db:self.store.db.execute('UPDATE sources SET content_hash=? WHERE id=?',('new-version',old['id']))
        matched,candidate,_=self.matching(aid)
        self.assertEqual(candidate['evidence'][0]['kind'],'historical_quote')
        self.assertEqual(candidate['evidence'][0]['source_hash'],old['content_hash'])
        self.assertEqual(matched[0]['id'],aid)

    def test_context_requires_an_original_recorded_quote(self):
        aid,old=self.seed()
        with self.store.db:self.store.db.execute("UPDATE action_sources SET source_quote='' WHERE action_id=?",(aid,))
        self.assertEqual(self.store.get_action(aid)['evidence'],[])

    def test_context_does_not_authorize_unrelated_paragraph_or_forged_proof(self):
        aid,_=self.seed();src=self.ingest(source('fresh'))
        matched,candidate,response=self.matching(aid,src)
        self.assertEqual(matched[0]['id'],aid)
        item=prepare_source_proposals(src,[action()],LEARNER,NOW)[0]
        for changed in ({'evidence_id':'not-registered'},{'candidate_quote':TEXT},
                        {'anchors':['serce']},{'source_quote':'Invented quotation'},
                        {'source_quote':'Klasa 1x'}):
            with self.subTest(changed=changed):
                self.assertEqual(verified_match(src,item,[candidate],dict(response,**changed)),(None,'uncertain'))

    def test_proof_cannot_change_typed_purpose_or_recipient(self):
        aid,_=self.seed(action_identity={'action_type':'attend','object':'papierowe serce','occasion':'kiermasz wiosenny','evidence':BODY})
        self.assertEqual(self.matching(aid)[0],(None,'distinct'))
        candidate=self.store.get_action(aid)
        src=self.ingest(source('another'));item=prepare_source_proposals(src,[action()],LEARNER,NOW)[0]
        candidate=deepcopy(candidate);candidate['payload']['action_identity']=item['action_identity']
        candidate['payload']['recipient']={'scope':'class','class_id':'2y'}
        response=ContextRuntime().match_source_action(src,item,[candidate])
        self.assertEqual(verified_match(src,item,[candidate],response),(None,'distinct'))
        self.assertEqual(verified_match(src,item,[candidate,candidate],response),(None,'uncertain'))

    def test_untyped_day_off_cannot_match_ceremony_from_another_paragraph(self):
        saved='Tego dnia nie ma lekcji dla uczniów.'
        ceremony='Klasa 1x: pasowanie pierwszoklasistów i uroczystość szkolna.'
        aid,_=self.seed(quote=saved,body=saved+' '+ceremony,temporal_kind='event')
        src=self.ingest(source('ceremony',text=ceremony))
        raw=action(temporal_kind='event',source_quote=ceremony,
            action_identity={'action_type':'attend','object':'pasowanie pierwszoklasistów','occasion':'uroczystość szkolna','evidence':ceremony})
        item=prepare_source_proposals(src,[raw],LEARNER,NOW)[0];candidate=self.store.get_action(aid);proof=candidate['evidence'][0]
        response={'decision':'match','action_id':aid,'evidence_id':proof['id'],'source_quote':ceremony,
                  'candidate_quote':proof['text'],'anchors':['pasowanie pierwszoklasistów','uroczystość szkolna']}
        self.assertNotIn('action_identity',candidate['payload'])
        self.assertEqual(verified_match(src,item,[candidate],response),(None,'uncertain'))

    def test_room_time_and_generic_context_are_not_substantive_anchors(self):
        aid,_=self.seed(quote='Klasa 1x spotka się w sali o podanej godzinie.',
                        body='Klasa 1x spotka się w sali o podanej godzinie. '+TEXT)
        src=self.ingest(source('generic',text='Klasa 1x spotka się w sali o podanej godzinie. '+TEXT))
        item=prepare_source_proposals(src,[action(source_quote=src['text'])],LEARNER,NOW)[0]
        candidate=self.store.get_action(aid);proof=candidate['evidence'][0]
        result={'decision':'match','action_id':aid,'evidence_id':proof['id'],'source_quote':src['text'],
                'candidate_quote':proof['text'],'anchors':['Klasa 1x','sali','godzinie']}
        self.assertEqual(verified_match(src,item,[candidate],result),(None,'uncertain'))

    def test_dated_past_day_off_links_its_own_history_without_operations(self):
        text='Klasa 1x ma dzień wolny. Nie odbywają się lekcje.'
        aid,_=self.seed(quote=text,body=text,temporal_kind='event',start='2030-02-20',end='2030-02-21')
        src=self.ingest(source('past-dayoff',kind='school_event',text=text))
        raw=action(temporal_kind='event',start='2030-02-20',end='2030-02-21',source_quote=text,
            action_identity={'action_type':'inform','object':'dzień wolny','occasion':'Nie odbywają się lekcje','evidence':text})
        class DayOffRuntime(ContextRuntime):
            def match_source_action(inner,src,item,candidates):
                chosen=next(x for x in candidates if x['id']==aid);proof=chosen['evidence'][0]
                return {'decision':'match','action_id':aid,'evidence_id':proof['id'],'source_quote':text,
                        'candidate_quote':proof['text'],'anchors':['dzień wolny','Nie odbywają się lekcje']}
        result=SourcePlanner(self.store,DayOffRuntime([raw])).analyze(src,NOW)
        self.assertEqual(result['outcomes'][0]['action_id'],aid)
        self.assertEqual(result['outcomes'][0]['outcome'],'linked')
        self.assertEqual(len(self.store.list_actions()),1)
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_context_is_bounded_around_original_quote_not_whole_inbox(self):
        aid,_=self.seed(body='x'*5000+BODY+'y'*5000)
        proof=self.store.get_action(aid)['evidence'][0]
        self.assertLessEqual(len(proof['text']),2000)
        self.assertIn(SHORT,proof['text']);self.assertIn(TEXT,proof['text'])
        self.assertNotIn('x'*1500,proof['text'])

    def test_candidate_evidence_change_invalidates_active_and_preview_plan(self):
        aid,old=self.seed();original=dict(self.store.db.execute('SELECT * FROM sources WHERE id=?',(old['id'],)).fetchone())
        for preview in (False,True):
            for mutation in ('link','hash','deleted','retained'):
                with self.subTest(preview=preview,mutation=mutation):
                    src=self.ingest(source('race-'+str(preview)+'-'+mutation));runtime=ContextRuntime();method=runtime.match_source_action
                    def racing(*args):
                        result=method(*args)
                        with self.store.db:
                            if mutation=='link':self.store.db.execute("UPDATE action_sources SET source_hash='changed-link' WHERE action_id=?",(aid,))
                            elif mutation=='hash':self.store.db.execute("UPDATE sources SET content_hash='changed-source' WHERE id=?",(old['id'],))
                            elif mutation=='deleted':self.store.db.execute('UPDATE sources SET deleted=1 WHERE id=?',(old['id'],))
                            else:self.store.db.execute("UPDATE sources SET status='retained' WHERE id=?",(old['id'],))
                        return result
                    runtime.match_source_action=racing
                    try:
                        with self.assertRaises(ValueError):SourcePlanner(self.store,runtime).analyze(src,NOW,preview=preview)
                        self.assertEqual(len(self.store.list_actions()),1)
                    finally:
                        with self.store.db:
                            self.store.db.execute('UPDATE action_sources SET source_hash=? WHERE action_id=?',(old['content_hash'],aid))
                            self.store.db.execute('UPDATE sources SET content_hash=?,deleted=?,status=? WHERE id=?',
                                (original['content_hash'],original['deleted'],original['status'],old['id']))

    def test_fetch_timestamp_is_not_matching_authority(self):
        aid,old=self.seed();before=self.store.source_decision_snapshot()
        with self.store.db:self.store.db.execute("UPDATE sources SET fetched_at='2030-03-02T12:00:00+01:00' WHERE id=?",(old['id'],))
        self.assertEqual(before,self.store.source_decision_snapshot())

    def test_past_fact_with_wrong_recipient_cannot_link_history(self):
        aid,_=self.seed();self.store.queue_operations(NOW);src=self.ingest(source('past'))
        bad=action(start='2029-03-12',recipient={'scope':'class','class_id':'2y','evidence':'Klasa 1x'})
        runtime=ContextRuntime([bad]);result=SourcePlanner(self.store,runtime).analyze(src,NOW)
        self.assertNotEqual(result['outcomes'][0]['action_id'],aid)
        self.assertNotIn(('match',1),runtime.calls)
        self.assertEqual(len(self.store.get_action(aid)['source_links']),1)
        self.assertEqual(self.store.queue_operations(NOW),[])


class SourceInstructionTests(unittest.TestCase):
    def test_schema_selects_dedicated_instructions_without_legacy_changes(self):
        runtime=CodexRuntime(TEST_CONFIG,read_only=True)
        for schema,tools,expected in (
            ('analysis.json',(),'analysis.md'),('source_analysis.json',(),'source_facts.md'),
            ('source_match.json',(),'source_match.md'),('source_verify.json',(),'source_verify.md'),
            ('tool.json',('list_calendars',),'executor.md')):
            with self.subTest(schema=schema):
                args=runtime._args('/private/fictional-eval',schema,tools)
                setting=next(a for a in args if a.startswith('model_instructions_file='))
                self.assertEqual(Path(json.loads(setting.split('=',1)[1])).name,expected)
                if not tools:self.assertIn('apps',args);self.assertIn('read-only',args)

    def test_source_prompt_version_and_registered_candidate_evidence(self):
        runtime=CodexRuntime(TEST_CONFIG,read_only=True)
        src=dict(source(),id='fictional-source',content_hash='fictional-hash')
        with patch.object(runtime,'_invoke',return_value={'proposals':[action()],'decision_reason':'Synthetic dated facts.'})as invoke:
            runtime.analyze_source(src,LEARNER)
            self.assertEqual(runtime.last_analysis_metadata['prompt_version'],SOURCE_PROMPT_VERSION)
            self.assertEqual(invoke.call_args.args[1],'source_analysis.json')
        proof={'id':'proof','source_id':'legacy-source','source_hash':'bound-version','source_quote':SHORT,'text':BODY,'kind':'current_context'}
        candidate={'id':'canonical','payload':action(),'evidence':[proof],'source_links':[]}
        with patch.object(runtime,'_invoke',return_value={'decision':'uncertain'})as invoke:
            runtime.match_source_action(src,action(),[candidate])
            document=json.loads(invoke.call_args.args[0].split('\n',1)[1])
            self.assertEqual(document['candidates'][0]['evidence'],[proof])
            self.assertNotIn('source_quotes',document['candidates'][0])
            self.assertNotIn('source_quote',document['candidates'][0]['action'])
            self.assertEqual(document['source']['title'],src['title'])


if __name__=='__main__':unittest.main()

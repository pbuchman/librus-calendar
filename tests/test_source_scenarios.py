"""Fictional regressions for the approved source-reconciliation scenarios."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from app.core import AppStore,digest
from app.source_planner import SourcePlanner
from app.source_store import source_identity
from test_source_planner import NOW,LEARNER,KINDS,action,source
from test_support import TEST_CONFIG,event_fingerprint


class ScriptedRuntime:
    read_only=True
    def __init__(self,items):
        self.items=items;self.calls=0;self.last_analysis_metadata={}
    def analyze_source(self,src,context):
        self.calls+=1
        self.last_analysis_metadata={'model':'synthetic','effort':'high','prompt_version':'scenario-v1','decision_reason':'Explicit fictional scenario.'}
        return json.loads(json.dumps(self.items))
    def match_source_action(self,src,item,candidates):
        matches=[candidate for candidate in candidates if all(
            candidate['payload'].get('action_identity',{}).get(key)==item.get('action_identity',{}).get(key)
            for key in ('action_type','object','occasion')) and (
                not item.get('alternative_group') or candidate['payload'].get('start')==item.get('start'))]
        if len(matches)!=1:return {'decision':'distinct' if not matches else 'uncertain'}
        candidate=matches[0];identity=item['action_identity']
        return {'decision':'match','action_id':candidate['id'],'source_quote':item['source_quote'],
                'candidate_quote':candidate['payload']['source_quote'],'anchors':[identity['object'],identity['occasion']]}


def typed(text,object,occasion,action_type='bring',**changes):
    item=action(source_quote=text,recipient={'scope':'all','class_id':None,'learner_id':None,'evidence':text},
                action_identity={'action_type':action_type,'object':object,'occasion':occasion,'evidence':text},optional=False)
    item.update(changes);return item


class ApprovedSourceScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=AppStore(Path(self.tmp.name)/'state.sqlite3',config=replace(TEST_CONFIG,multi_source_mode='active'))
        self.store.set_setting('source_activation_at','fictional-evaluation')
        self.store.set_setting('source_activation_learner',LEARNER['id'])
        self.store.set_setting('source_activation_binding',digest([TEST_CONFIG.calendar_id,TEST_CONFIG.calendar_owner]))
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def apply(self,key,text,items,kind='announcement',published='2030-03-01T10:00:00+01:00'):
        data=source(key,kind,text,published_at=published)
        inventory={s['id']:s for s in self.store.list_sources(500)if s['kind']!='message'}
        inventory[source_identity(kind,key,LEARNER['id'])]=data
        self.store.ingest_sources({'sources':list(inventory.values()),'complete_kinds':KINDS,'learner':LEARNER,'status':'ok'},NOW)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',('message',NOW.isoformat(),NOW.isoformat(),'ok',None,1))
        normalized=self.store.get_source(source_identity(kind,key,LEARNER['id']))
        runtime=ScriptedRuntime(items)
        return SourcePlanner(self.store,runtime).analyze(normalized,NOW),runtime

    def test_materials_and_later_homework_keep_distinct_purposes_and_dates(self):
        text='Przynieście papierowe koła na warsztaty plastyczne 23 marca. Ćwiczenia domowe oddajemy na lekcję polskiego 28 marca.'
        materials=typed(text,'papierowe koła','warsztaty plastyczne',start='2030-03-23',temporal_kind='reminder')
        homework=typed(text,'ćwiczenia domowe','lekcję polskiego',action_type='submit',start='2030-03-28',temporal_kind='deadline')
        self.apply('materials',text,[materials,homework])
        self.assertEqual({a['payload']['start'] for a in self.store.list_actions()},{'2030-03-23','2030-03-28'})
        self.assertEqual(len(self.store.queue_operations(NOW)),2)
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_ceremony_human_hours_are_preserved_and_day_off_rehearsal_distinct(self):
        text='Pasowanie uczniów to uroczystość klasowa 14 marca. Próba występu to przygotowanie artystyczne 13 marca. Dzień wolny oznacza przerwę szkolną 14 marca.'
        ceremony=typed(text,'pasowanie uczniów','uroczystość klasowa',action_type='attend',temporal_kind='event',start='2030-03-14T09:00:00+01:00',end='2030-03-14T10:30:00+01:00',all_day=False)
        rehearsal=typed(text,'próba występu','przygotowanie artystyczne',action_type='attend',temporal_kind='event',start='2030-03-13T09:00:00+01:00',end='2030-03-13T10:00:00+01:00',all_day=False)
        day_off=typed(text,'dzień wolny','przerwę szkolną',action_type='inform',temporal_kind='event',start='2030-03-14',end='2030-03-15')
        result,_=self.apply('ceremony',text,[ceremony,rehearsal,day_off])
        pid=result['outcomes'][0]['proposal_id'];self.store.resolve_proposal(pid,'approve',{'end':'2030-03-14T11:00:00+01:00'})
        operation=next(op for op in self.store.queue_operations(NOW)if op['proposal_id']==pid)
        snapshot=dict(operation['event'],id='fictional-ceremony-event')
        self.store.record_result(operation['operation_id'],{'status':'applied','event_id':snapshot['id'],'snapshot':snapshot,'fingerprint':event_fingerprint(snapshot)})
        self.apply('new-ceremony',text,[dict(ceremony,end='2030-03-14T12:00:00+01:00')],published='2030-03-02T10:00:00+01:00')
        self.assertEqual(len(self.store.list_actions()),3)
        self.assertEqual(self.store.list_events()[0]['snapshot'],snapshot)
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_lost_items_deadline_restores_exact_hour_without_duplicate(self):
        text='Rzeczy znalezione odbieramy przy szkolnej szatni do 16 marca do godziny 16:00.'
        item=typed(text,'rzeczy znalezione','szkolnej szatni',action_type='collect',start='2030-03-16')
        self.apply('lost-older',text,[item])
        self.apply('lost-newer',text,[dict(item,due_at='2030-03-16T16:00:00+01:00')],published='2030-03-02T10:00:00+01:00')
        actions=self.store.list_actions();self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['payload']['due_at'],'2030-03-16T16:00:00+01:00')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_rejected_dance_is_not_resurrected_from_new_source(self):
        text='Zajęcia tańca obejmują taniec nowoczesny 20 marca o 16:00.'
        item=typed(text,'zajęcia tańca','taniec nowoczesny',action_type='attend',activity_scope='extracurricular',temporal_kind='event',start='2030-03-20T16:00:00+01:00',end=None,all_day=False)
        result,_=self.apply('dance',text,[item]);self.store.resolve_proposal(result['outcomes'][0]['proposal_id'],'ignore')
        result,_=self.apply('dance-repeat',text,[item],published='2030-03-02T10:00:00+01:00')
        self.assertEqual(result['outcomes'][0]['outcome'],'ignored')
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertEqual(len(self.store.list_actions()),1)

    def test_training_is_one_offer_five_choices_and_at_most_one_operation(self):
        text='Szkolenie rodziców to oferta warsztatów. Wybierz jeden termin: 5, 8, 12, 19 albo 26 marca o 16:00.'
        items=[typed(text,'szkolenie rodziców','oferta warsztatów',action_type='attend',activity_scope='extracurricular',temporal_kind='event',start=f'2030-03-{day:02d}T16:00:00+01:00',end=None,all_day=False,
            alternative_group='fictional-training',alternative_group_evidence=text,offer_identity={'label':'szkolenie rodziców','evidence':text})for day in (5,8,12,19,26)]
        self.apply('training',text,items)
        groups=self.store.list_proposal_groups(current=NOW);self.assertEqual(len(groups),1)
        group=groups[0];self.assertEqual(len(group['members']),5);self.assertEqual(self.store.queue_operations(NOW),[])
        self.store.resolve_proposal_group(group['id'],'select',group['members'][1]['id'],expected_revision=group['revision'],current=NOW)
        self.assertEqual(len(self.store.queue_operations(NOW)),1)
        self.apply('training-repeat',text,items,published='2030-03-02T10:00:00+01:00')
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertEqual(len(self.store.list_proposal_groups(current=NOW)),1)

    def test_wrong_class_and_substitution_prepare_zero_operations(self):
        text='Klasa 9z przygotowuje dekorację na szkolny festyn 20 marca.'
        item=typed(text,'dekorację','szkolny festyn',recipient={'scope':'other','class_id':'9z','learner_id':None,'evidence':text},start='2030-03-20')
        self.apply('wrong-class',text,[item])
        unused,runtime=self.apply('substitution','Zastępstwo 3. lekcji.',[],kind='substitution')
        self.assertEqual(runtime.calls,0)
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_prepare_and_bring_one_deliverable_is_one_reminder_with_separate_consent(self):
        text='Przygotujcie i przynieście papierowe koła na warsztaty plastyczne 23 marca. Zgodę rodziców na wyjście muzealne oddajemy do 18 marca.'
        items=[typed(text,'papierowe koła','warsztaty plastyczne',start='2030-03-23',temporal_kind='reminder'),
               typed(text,'zgodę rodziców','wyjście muzealne',action_type='submit',start='2030-03-18',temporal_kind='deadline')]
        self.apply('one-deliverable',text,items)
        self.assertEqual(len(self.store.list_actions()),2)
        self.assertEqual(len(self.store.queue_operations(NOW)),2)

    def test_activation_after_exact_deadline_prepares_no_operation(self):
        text='Rzeczy znalezione odbieramy przy szkolnej szatni do 16 marca do godziny 16:00.'
        item=typed(text,'rzeczy znalezione','szkolnej szatni',action_type='collect',start='2030-03-16',due_at='2030-03-16T16:00:00+01:00')
        self.apply('expired-lost-items',text,[item])
        self.assertEqual(self.store.queue_operations(NOW.replace(day=16,hour=17)),[])


if __name__=='__main__':unittest.main()

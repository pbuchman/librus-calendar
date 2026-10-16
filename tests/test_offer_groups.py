"""Synthetic alternatives, transaction races and zero-write administration."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.core import AppStore, WARSAW, digest
from app.data_admin import migrate_decisions, prune_state
from app.offer_groups import group_existing
from test_support import TEST_CONFIG

NOW = datetime(2030, 3, 6, 12, tzinfo=WARSAW)
EVIDENCE = 'Zajęcia mają terminy 5, 8 i 12 marca. To alternatywne terminy jednego udziału.'
MESSAGE = {'id':'synthetic-offer','subject':'Dodatkowe zajęcia','text':EVIDENCE,'sent_at':'2030-03-06T09:00:00+01:00'}


def option(start='2030-03-08T16:00:00+01:00', **changes):
    return dict({'kind':'create','activity_scope':'extracurricular','temporal_kind':'event','due_at':None,
        'title':'Trening','description':'Zajęcia dodatkowe','start':start,'end':None,'all_day':False,
        'confidence':'high','needs_review':False,'review_reason':'','source_quote':EVIDENCE,'event_id':None,
        'alternative_group':'one-training','alternative_group_evidence':EVIDENCE}, **changes)


class OfferGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite'
        self.store = AppStore(self.path,config=TEST_CONFIG)
        self.store.save_message(MESSAGE)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create_group(self, past=False):
        items = [option(),option('2030-03-12T16:00:00+01:00')]
        if past:
            items.insert(0,option('2030-03-05T16:00:00+01:00'))
        self.store.save_proposals(MESSAGE['id'],items,NOW)
        return self.store.list_proposal_groups(current=NOW)[0]

    def decide(self, group, selected=None, decision='select', store=None):
        return (store or self.store).resolve_proposal_group(group['id'],decision,
            selected or (group['members'][0]['id'] if decision=='select' else None),
            expected_revision=group['revision'],current=NOW)

    def state(self):
        return {name:[tuple(row) for row in self.store.db.execute('SELECT * FROM '+name+' ORDER BY 1')]
                for name in ('proposals','operations','decision_intents','proposal_groups','proposal_group_members')}

    def test_explicit_model_group_is_manual_complete_and_chronological(self):
        group = self.create_group(past=True)
        self.assertEqual(group['status'],'open')
        self.assertEqual([member['start'][8:10] for member in group['members']],['05','08','12'])
        self.assertEqual(group['members'][0]['ineligible_reason'],'past')
        self.assertFalse(group['members'][0]['eligible'])
        self.assertTrue(all(member['status']=='review' for member in group['members'][1:]))
        self.assertEqual(self.store.queue_operations(NOW),[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def test_past_selection_and_wrong_revision_leave_entire_group_unchanged(self):
        group = self.create_group(past=True)
        before = self.state()
        with self.assertRaises(ValueError):
            self.decide(group)
        self.assertEqual(before,self.state())
        with self.assertRaises(ValueError):
            self.store.resolve_proposal_group(group['id'],'select',group['members'][1]['id'],expected_revision=0,current=NOW)
        self.assertEqual(before,self.state())

    def test_choose_one_is_atomic_idempotent_and_remembers_every_rejection(self):
        group = self.create_group()
        chosen = self.decide(group)
        self.assertEqual(chosen['status'],'selected')
        statuses = {member['id']:member['status'] for member in chosen['members']}
        self.assertEqual(list(statuses.values()).count('pending'),1)
        self.assertEqual(list(statuses.values()).count('ignored'),1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0],2)
        before = self.state()
        self.assertEqual(self.decide(group),chosen)
        self.assertEqual(before,self.state())
        with self.assertRaises(ValueError):
            self.decide(group,group['members'][1]['id'])
        self.assertEqual(before,self.state())
        queued = self.store.queue_operations(NOW)
        self.assertEqual(len(queued),1)
        self.assertEqual(queued[0]['proposal_id'],chosen['selected_proposal_id'])
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_reject_all_is_durable_and_idempotent(self):
        group = self.create_group(past=True)
        result = self.decide(group,decision='reject')
        self.assertEqual(result['status'],'rejected')
        self.assertTrue(all(member['user_rejected'] for member in result['members']))
        before = self.state()
        self.decide(group,decision='reject')
        self.assertEqual(before,self.state())
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_individual_action_and_forged_pending_queue_claim_cannot_bypass_group(self):
        group = self.create_group()
        proposal_id = group['members'][0]['id']
        for decision in ('approve','ignore','confirm_cancel'):
            with self.assertRaises(ValueError):
                self.store.resolve_proposal(proposal_id,decision)
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?",(proposal_id,))
        self.assertEqual(self.store.queue_operations(NOW),[])
        # Inject a preexisting pending journal to exercise claim independently.
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?",(proposal_id,))
            operation={'operation_id':'synthetic-operation','proposal_id':proposal_id,'calendar_id':TEST_CONFIG.calendar_id,'action':'create','event_id':None}
            self.store.db.execute("INSERT INTO operations(id,proposal_id,payload,status,created_at,updated_at) VALUES (?,?,?,'pending','2030','2030')",('synthetic-operation',proposal_id,json.dumps(operation)))
        self.store.set_writes_paused(False)
        self.assertFalse(self.store.claim_operation('synthetic-operation'))
        self.assertEqual(self.store.db.execute("SELECT attempts FROM operations").fetchone()[0],0)

    def test_selection_transaction_failure_rolls_back_all_members_and_intents(self):
        group = self.create_group()
        before = self.state()
        from app.data_admin import remember_decision
        calls=[]
        def fail_second(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                raise RuntimeError('synthetic persistence failure')
            return remember_decision(*args,**kwargs)
        with patch('app.data_admin.remember_decision',side_effect=fail_second),self.assertRaises(RuntimeError):
            self.decide(group)
        self.assertEqual(before,self.state())

    def test_two_connections_conflicting_selections_allow_exactly_one(self):
        group = self.create_group()
        other = AppStore(self.path,config=TEST_CONFIG)
        barrier=threading.Barrier(2)
        outcomes=[]
        def choose(store,proposal_id):
            barrier.wait()
            try:
                self.decide(group,proposal_id,store=store)
                outcomes.append('selected')
            except ValueError:
                outcomes.append('conflict')
        workers=[threading.Thread(target=choose,args=(store,member['id'])) for store,member in zip((self.store,other),group['members'])]
        try:
            for worker in workers: worker.start()
            for worker in workers:
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            self.assertCountEqual(outcomes,['selected','conflict'])
            self.assertEqual(len(self.store.queue_operations(NOW)),1)
        finally:
            other.close()

    def test_simultaneous_identical_double_submit_is_idempotent(self):
        group=self.create_group()
        other=AppStore(self.path,config=TEST_CONFIG)
        barrier=threading.Barrier(2)
        outcomes=[]
        def choose(store):
            barrier.wait()
            outcomes.append(self.decide(group,store=store)['selected_proposal_id'])
        workers=[threading.Thread(target=choose,args=(store,)) for store in (self.store,other)]
        try:
            for worker in workers: worker.start()
            for worker in workers: worker.join(timeout=3)
            self.assertEqual(outcomes,[group['members'][0]['id']]*2)
            self.assertEqual(self.store.get_proposal_group(group['id'])['revision'],2)
        finally:
            other.close()

    def test_source_change_refuses_stale_decision_and_preserves_prior_group(self):
        group=self.create_group()
        self.store.save_message(dict(MESSAGE,text=EVIDENCE+' Zmiana treści.'))
        before=self.state()
        self.assertTrue(self.store.get_proposal_group(group['id'],current=NOW)['stale'])
        with self.assertRaises(ValueError): self.decide(group)
        self.assertEqual(before,self.state())

    def test_closed_group_survives_reanalysis_field_omission_and_changed_model_marker(self):
        group=self.create_group()
        self.decide(group)
        with self.assertRaises(ValueError): self.store.cached_reanalysis_scope(MESSAGE['id'])
        # Rebuild may reanalyze matching source. Durable signatures restore all
        # human actions even when the model omits or renames grouping metadata.
        for group_key in (None,'model-renamed-key'):
            raw=[option(),option('2030-03-12T16:00:00+01:00')]
            for item in raw:
                item['alternative_group']=group_key
                item['alternative_group_evidence']=EVIDENCE if group_key else None
            self.store.save_proposals(MESSAGE['id'],raw,NOW)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0],2)
            self.assertEqual(len(self.store.list_proposal_groups()),1)
            self.assertEqual(len(self.store.queue_operations(NOW)),1 if group_key is None else 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0],1)

    def test_changed_valid_quote_and_group_key_cannot_create_second_choice_or_write(self):
        source=EVIDENCE+' Drugi potwierdzony fragment źródła.'
        self.store.save_message(dict(MESSAGE,text=source))
        group=self.create_group()
        self.decide(group)
        changed=[option(source_quote='Drugi potwierdzony fragment źródła.',alternative_group='changed-key'),
                 option('2030-03-12T16:00:00+01:00',source_quote='Drugi potwierdzony fragment źródła.',alternative_group='changed-key')]
        self.store.save_proposals(MESSAGE['id'],changed,NOW)
        self.assertEqual(len(self.store.list_proposal_groups()),1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0],4)
        ambiguous=[item for item in self.store.list_proposals() if item.get('blocked_by_group_decision')]
        self.assertEqual(len(ambiguous),2)
        for item in ambiguous:
            with self.assertRaises(ValueError):self.store.resolve_proposal(item['id'],'approve')
        with self.assertRaises(ValueError):
            self.decide(group,group['members'][1]['id'])
        from app.sync import SyncController
        from test_sync import FakeClient,FakeRuntime
        runtime=FakeRuntime(propose=False)
        self.store.set_writes_paused(False)
        controller=SyncController(self.store,FakeClient(count=0),runtime)
        self.assertEqual(controller.run(current=NOW)['calendar_writes'],1)
        self.assertEqual(controller.run(current=NOW)['calendar_writes'],0)
        self.assertEqual(len(runtime.executed),1)
        self.assertEqual(runtime.executed[0]['proposal_id'],group['members'][0]['id'])
        self.assertEqual(len(self.store.list_events()),1)

    def test_omitted_group_marker_and_changed_quote_cannot_reapprove_closed_member(self):
        source=EVIDENCE+' Inny prawdziwy fragment.'
        self.store.save_message(dict(MESSAGE,text=source))
        group=self.create_group()
        self.decide(group)
        changed=[option(source_quote='Inny prawdziwy fragment.',alternative_group=None,alternative_group_evidence=None),
                 option('2030-03-12T16:00:00+01:00',source_quote='Inny prawdziwy fragment.',alternative_group=None,alternative_group_evidence=None)]
        items=self.store.save_proposals(MESSAGE['id'],changed,NOW)
        self.assertTrue(all(item.get('blocked_by_group_decision') for item in items))
        with self.assertRaises(ValueError):self.store.resolve_proposal(items[1]['id'],'approve')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_ambiguous_new_group_dates_after_decision_are_non_approvable_manual_review(self):
        group=self.create_group()
        self.decide(group)
        changed=[option('2030-03-15T16:00:00+01:00',alternative_group='changed-key'),
                 option('2030-03-18T16:00:00+01:00',alternative_group='changed-key')]
        result=self.store.save_proposals(MESSAGE['id'],changed,NOW)
        self.assertEqual(len(self.store.list_proposal_groups()),1)
        self.assertTrue(all(item['status']=='review' and item.get('blocked_by_group_decision') for item in result))
        with self.assertRaises(ValueError):self.store.resolve_proposal(result[0]['id'],'approve')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_two_known_independent_offers_in_one_source_can_each_choose_one(self):
        raw=[option(),option('2030-03-12T16:00:00+01:00'),
             option('2030-03-15T16:00:00+01:00',alternative_group='separate-offer'),
             option('2030-03-18T16:00:00+01:00',alternative_group='separate-offer')]
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        groups=self.store.list_proposal_groups(current=NOW)
        self.decide(groups[0])
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        self.decide(groups[1])
        self.assertEqual(len(self.store.queue_operations(NOW)),2)

    def test_two_independent_offers_with_shared_start_keep_complete_membership_after_replay(self):
        evidence_a='Wybierz jeden termin oferty A.'
        evidence_b='Wybierz jeden termin oferty B.'
        self.store.save_message(dict(MESSAGE,text=evidence_a+' '+evidence_b))
        broad_quote=evidence_a+' '+evidence_b
        raw=[option('2030-03-10T16:00:00+01:00',source_quote=broad_quote,alternative_group_evidence=evidence_a),
             option('2030-03-12T16:00:00+01:00',source_quote=broad_quote,alternative_group_evidence=evidence_a),
             option('2030-03-10T16:00:00+01:00',source_quote=broad_quote,alternative_group='separate-offer',alternative_group_evidence=evidence_b),
             option('2030-03-14T16:00:00+01:00',source_quote=broad_quote,alternative_group='separate-offer',alternative_group_evidence=evidence_b)]
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        groups=self.store.list_proposal_groups(current=NOW)
        first=next(group for group in groups if group['group_key']=='one-training')
        second=next(group for group in groups if group['group_key']=='separate-offer')
        self.decide(first)
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        second=self.store.get_proposal_group(second['id'],current=NOW)
        self.assertEqual(len(second['members']),2)
        self.assertEqual(second['status'],'open')
        self.decide(second)
        self.assertEqual(len(self.store.queue_operations(NOW)),2)

    def test_unknown_or_omitted_group_scope_with_broad_quote_never_inherits_other_offer(self):
        evidence_a='Wybierz jeden termin oferty A.'
        evidence_b='Wybierz jeden termin oferty B.'
        broad=evidence_a+' '+evidence_b
        self.store.save_message(dict(MESSAGE,text=broad))
        raw=[option('2030-03-10T16:00:00+01:00',source_quote=broad,alternative_group_evidence=evidence_a),
             option('2030-03-12T16:00:00+01:00',source_quote=broad,alternative_group_evidence=evidence_a),
             option('2030-03-10T16:00:00+01:00',source_quote=broad,alternative_group='second-offer',alternative_group_evidence=evidence_b),
             option('2030-03-14T16:00:00+01:00',source_quote=broad,alternative_group='second-offer',alternative_group_evidence=evidence_b)]
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        first=next(group for group in self.store.list_proposal_groups(current=NOW) if group['group_key']=='one-training')
        self.decide(first)
        for key in ('renamed-second',None):
            replay=[dict(item) for item in raw]
            for item in replay[2:]:
                item['alternative_group']=key
                item['alternative_group_evidence']=evidence_b if key else None
            result=self.store.save_proposals(MESSAGE['id'],replay,NOW)
            for item in result[2:]:
                self.assertFalse(item.get('user_approved'))
                self.assertTrue(item.get('blocked_by_group_decision'))
                with self.assertRaises(ValueError):self.store.resolve_proposal(item['id'],'approve')
            self.assertEqual(len({item['id'] for item in result}),4)
        self.assertEqual(len(self.store.list_proposal_groups()),2)
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_selected_member_can_explicitly_recover_review_while_siblings_stay_closed(self):
        group=self.create_group()
        selected=self.decide(group)
        chosen_id=selected['selected_proposal_id']
        self.store.set_writes_paused(False)
        operation=self.store.queue_operations(NOW)[0]
        self.assertTrue(self.store.claim_operation(operation['operation_id']))
        self.store.record_result(operation['operation_id'],{'status':'review','error':'Synthetic Calendar conflict.'})
        self.assertEqual(self.store.get_proposal_group(group['id'])['status'],'selected')
        approved=self.store.resolve_proposal(chosen_id,'approve')
        self.assertEqual(approved['status'],'pending')
        sibling=next(item for item in group['members'] if item['id']!=chosen_id)
        with self.assertRaises(ValueError):self.store.resolve_proposal(sibling['id'],'approve')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)
        self.store.save_message(dict(MESSAGE,text=EVIDENCE+' Zmiana.'))
        with self.assertRaises(ValueError):self.store.resolve_proposal(chosen_id,'approve')

    def test_ambiguous_replacement_can_be_dismissed_without_reopening_closed_group(self):
        group=self.create_group()
        self.decide(group)
        items=self.store.save_proposals(MESSAGE['id'],[option('2030-03-15T16:00:00+01:00',alternative_group='changed-key'),option('2030-03-18T16:00:00+01:00',alternative_group='changed-key')],NOW)
        ignored=self.store.resolve_proposal(items[0]['id'],'ignore')
        self.assertTrue(ignored['user_rejected'])
        self.assertEqual(ignored['status'],'ignored')
        self.assertEqual(self.store.get_proposal_group(group['id'])['status'],'selected')
        self.assertEqual(len(self.store.queue_operations(NOW)),1)

    def test_successful_source_change_retires_untouched_prior_group_without_human_rejection(self):
        old=self.create_group()
        self.store.save_message(dict(MESSAGE,text=EVIDENCE+' Aktualizacja.'))
        self.store.save_proposals(MESSAGE['id'],[option(),option('2030-03-12T16:00:00+01:00')],NOW)
        groups=self.store.list_proposal_groups(current=NOW)
        self.assertEqual(len(groups),1)
        self.assertNotEqual(groups[0]['id'],old['id'])
        self.assertFalse(groups[0]['stale'])
        old_ids={member['id'] for member in old['members']}
        previous=[item for item in self.store.list_proposals() if item['id'] in old_ids]
        self.assertTrue(all(item['status']=='superseded' and not item.get('user_rejected') for item in previous))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0],0)

    def test_prune_retires_open_offer_instead_of_leaving_undismissable_review(self):
        self.create_group()
        prune_state(self.store,days=1,apply=True,current=datetime(2030,5,1,tzinfo=WARSAW))
        self.assertEqual(self.store.list_proposal_groups(),[])
        self.assertEqual(self.store.status()['proposals_review'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0],0)

    def test_analysis_reserves_before_reading_closed_group_decisions(self):
        group=self.create_group()
        other=AppStore(self.path,config=TEST_CONFIG)
        other.db.execute('PRAGMA busy_timeout=25')
        original=self.store._prepare_proposals
        outcomes=[]
        def concurrent():
            try:self.decide(group,store=other)
            except sqlite3.OperationalError:outcomes.append('locked')
        def preparing(*args,**kwargs):
            worker=threading.Thread(target=concurrent);worker.start();worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            return original(*args,**kwargs)
        try:
            with patch.object(self.store,'_prepare_proposals',side_effect=preparing):
                self.store.save_proposals(MESSAGE['id'],[option(),option('2030-03-12T16:00:00+01:00')],NOW)
            self.assertEqual(outcomes,['locked'])
        finally:other.close()

    def test_prune_reserves_before_group_and_member_eligibility_reads(self):
        group=self.create_group()
        other=AppStore(self.path,config=TEST_CONFIG)
        other.db.execute('PRAGMA busy_timeout=25')
        original=self.store._retire_open_groups
        outcomes=[]
        def concurrent():
            try:self.decide(group,store=other)
            except sqlite3.OperationalError:outcomes.append('locked')
        def retire(*args,**kwargs):
            worker=threading.Thread(target=concurrent);worker.start();worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            return original(*args,**kwargs)
        try:
            with patch.object(self.store,'_retire_open_groups',side_effect=retire):
                prune_state(self.store,days=1,apply=True,current=datetime(2030,5,1,tzinfo=WARSAW))
            self.assertEqual(outcomes,['locked'])
            self.assertEqual(self.store.list_proposal_groups(),[])
        finally:other.close()

    def test_weak_group_claim_stays_ungrouped_manual_and_no_writes(self):
        raw=[option(alternative_group_evidence='A nonexistent literal quote.'),option('2030-03-12T16:00:00+01:00',alternative_group_evidence='A nonexistent literal quote.')]
        result=self.store.save_proposals(MESSAGE['id'],raw,NOW)
        self.assertEqual(self.store.list_proposal_groups(),[])
        self.assertTrue(all(item['status']=='review' and not item.get('alternative_group') for item in result))
        self.assertEqual(self.store.queue_operations(NOW),[])

    def test_series_and_independent_obligations_are_not_grouped_by_title(self):
        raw=[option(alternative_group=None,alternative_group_evidence=None),option('2030-03-12T16:00:00+01:00',alternative_group=None,alternative_group_evidence=None),
             option('2030-03-08',activity_scope='school',temporal_kind='deadline',all_day=True,alternative_group=None,alternative_group_evidence=None)]
        self.store.save_proposals(MESSAGE['id'],raw,NOW)
        self.assertEqual(len(self.store.list_proposals()),3)
        self.assertEqual(self.store.list_proposal_groups(),[])

    def test_group_members_independent_of_proposal_and_message_page_limits(self):
        group=self.create_group()
        for index in range(110):
            self.store.save_message(dict(MESSAGE,id='new-'+str(index)))
            self.store.save_proposals('new-'+str(index),[option(alternative_group=None,alternative_group_evidence=None)],NOW)
        self.assertEqual(len(self.store.list_proposals()),100)
        self.assertFalse(any(item['id'] in {member['id'] for member in group['members']} for item in self.store.list_proposals()))
        complete=self.store.get_proposal_group(group['id'],current=NOW)
        self.assertEqual(len(complete['members']),2)
        self.assertEqual(len(self.store.list_proposal_groups()),1)

    def test_prune_then_transfer_and_rebuild_preserve_minimal_decision(self):
        group=self.create_group(past=True)
        self.decide(group,group['members'][1]['id'])
        self.store.set_setting('librus_account_hash','synthetic-account-hash')
        prune_state(self.store,days=1,apply=True,current=datetime(2030,5,1,tzinfo=WARSAW))
        retained=self.store.get_proposal_group(group['id'],current=NOW)
        self.assertEqual(retained['source_evidence'],'')
        self.assertEqual(len(retained['members']),3)
        target=AppStore(Path(self.temp.name)/'reconstructed.sqlite',config=TEST_CONFIG)
        try:
            migrate_decisions(target,self.path)
            transferred=target.get_proposal_group(group['id'],current=NOW)
            self.assertEqual(transferred['status'],'selected')
            self.assertEqual(transferred['selected_proposal_id'],group['members'][1]['id'])
            self.assertEqual(len(transferred['members']),3)
            self.assertTrue(all(not member['source_quote'] for member in transferred['members']))
            target.save_message(MESSAGE)
            target.save_proposals(MESSAGE['id'],[option('2030-03-05T16:00:00+01:00'),option(),option('2030-03-12T16:00:00+01:00')],NOW)
            self.assertEqual(len(target.list_proposal_groups()),1)
            self.assertEqual(target.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0],3)
            migrate_decisions(target,self.path)
        finally:
            target.close()

    def test_additive_open_of_legacy_database_preserves_proposal_rows(self):
        self.store.save_proposals(MESSAGE['id'],[option(alternative_group=None,alternative_group_evidence=None)],NOW)
        before=[tuple(row) for row in self.store.db.execute('SELECT * FROM proposals')]
        self.store.close()
        legacy=sqlite3.connect(self.path)
        legacy.execute('DROP TABLE proposal_group_members')
        legacy.execute('DROP TABLE proposal_groups')
        legacy.commit();legacy.close()
        self.store=AppStore(self.path,config=TEST_CONFIG)
        self.assertEqual(before,[tuple(row) for row in self.store.db.execute('SELECT * FROM proposals')])
        self.assertEqual(self.store.list_proposal_groups(),[])


class ExistingGroupPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        Path(self.temp.name).chmod(0o700)
        self.store=AppStore(Path(self.temp.name)/'state.sqlite',config=TEST_CONFIG)
        self.store.save_message(MESSAGE)
        self.items=self.store.save_proposals(MESSAGE['id'],[option(alternative_group=None,alternative_group_evidence=None),option('2030-03-12T16:00:00+01:00',alternative_group=None,alternative_group_evidence=None)],NOW)
        self.plan=Path(self.temp.name)/'plan.json'

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def preview(self):
        return group_existing(self.store,plan_path=self.plan,message_id=MESSAGE['id'],proposal_ids=[item['id'] for item in self.items],source_evidence=EVIDENCE)

    def test_preview_apply_rerun_keep_payloads_ids_and_journal_untouched(self):
        before=[tuple(row) for row in self.store.db.execute('SELECT * FROM proposals ORDER BY id')]
        self.assertEqual(self.preview()['status'],'preview')
        self.assertEqual(self.plan.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.list_proposal_groups(),[])
        self.assertEqual(group_existing(self.store,plan_path=self.plan,apply=True)['status'],'applied')
        self.assertEqual(group_existing(self.store,plan_path=self.plan,apply=True)['status'],'already_applied')
        self.assertEqual(before,[tuple(row) for row in self.store.db.execute('SELECT * FROM proposals ORDER BY id')])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decision_intents').fetchone()[0],0)

    def test_changed_payload_or_source_refuses_without_partial_group(self):
        self.preview()
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET updated_at='changed' WHERE id=?",(self.items[0]['id'],))
        with self.assertRaises(ValueError):group_existing(self.store,plan_path=self.plan,apply=True)
        self.assertEqual(self.store.list_proposal_groups(),[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM proposal_group_members').fetchone()[0],0)

    def test_repeated_applied_plan_refuses_source_or_proposal_or_human_state_changes(self):
        self.preview()
        group_existing(self.store,plan_path=self.plan,apply=True)
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET updated_at='changed' WHERE id=?",(self.items[0]['id'],))
        with self.assertRaises(ValueError):group_existing(self.store,plan_path=self.plan,apply=True)
        self.store.save_message(dict(MESSAGE,text=EVIDENCE+' Zmienione źródło.'))
        with self.assertRaises(ValueError):group_existing(self.store,plan_path=self.plan,apply=True)

    def test_no_overwrite_and_attempted_or_even_unattempted_journal_refused(self):
        self.preview()
        with self.assertRaises(FileExistsError):self.preview()
        self.plan.unlink()
        with self.store.db:
            self.store.db.execute("INSERT INTO operations(id,proposal_id,payload,status,created_at,updated_at) VALUES ('op',?,'{}','pending','2030','2030')",(self.items[0]['id'],))
        with self.assertRaises(ValueError):self.preview()
        self.assertFalse(self.plan.exists())

    def test_cli_private_request_and_plan_do_not_initialize_any_external_client(self):
        from app.cli import main
        request=Path(self.temp.name)/'request.json'
        request.write_text(json.dumps({'message_id':MESSAGE['id'],'proposal_ids':[item['id'] for item in self.items],'source_evidence':EVIDENCE}))
        request.chmod(0o600)
        with patch('app.codex_runtime.CodexRuntime',side_effect=AssertionError('No runtime')),patch('app.librus_client.LibrusClient',side_effect=AssertionError('No client')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--state',str(self.store.path),'group-existing','--request-file',str(request),'--plan-file',str(self.plan)],config=TEST_CONFIG),0)
            rendered=output.getvalue()
            self.assertNotIn(EVIDENCE,rendered)
            self.assertNotIn(MESSAGE['id'],rendered)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--state',str(self.store.path),'group-existing','--plan-file',str(self.plan),'--apply'],config=TEST_CONFIG),0)
            self.assertEqual(json.loads(output.getvalue())['calendar_writes'],0)


if __name__=='__main__':unittest.main()

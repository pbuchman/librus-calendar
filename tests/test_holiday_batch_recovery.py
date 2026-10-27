"""Fictional offline HTML, SQLite and CLI boundary recovery contracts."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.agenda import _prepare_item, assemble_agenda_snapshot, read_calendar
from app.codex_runtime import CodexRuntime, RuntimeFailure, event_fingerprint
from app.core import AppStore
from app.health_state import read_monitoring_state, sync_diagnostics
from app.source_collectors import collect_calendar, collect_sources
from app.source_store import source_identity
from app.sync import SyncController, SyncInterrupted
from test_support import TEST_CONFIG
from test_source_collectors import FakeClient, calendar_cell, page, CURRENT
from test_agenda import LEARNER, NOW, action, item


def holiday_detail(scope='School', publication='2028-05-01 10:20:30'):
    return page('<table><tr><td>Zakres</td><td>' + scope + '</td></tr><tr><td>Data dodania</td><td>' + publication + '</td></tr></table>')


def holiday_client(days, *, title='Synthetic holiday'):
    client = FakeClient()
    client.events = {}
    for year, month, day in days:
        client.events.setdefault((year, month), {})[day] = calendar_cell(title, '/terminarz/szczegoly_wolne/87002')
    client.details = {'/terminarz/szczegoly_wolne/87002': holiday_detail()}
    return client


class HolidayCollectionTests(TestCase):
    def collect(self, days, current=datetime(2028, 12, 20, tzinfo=timezone.utc)):
        batch = collect_sources(holiday_client(days), current=current)
        self.assertEqual(batch['status'], 'ok')
        return batch

    def test_cross_year_month_sorted_dates_and_gaps_have_no_invented_days(self):
        batch = self.collect([(2028,12,31),(2029,1,1),(2029,1,3)])
        source, = [source for source in batch['sources'] if source['kind']=='school_event']
        self.assertEqual(source['metadata']['event_dates'], ['2028-12-31','2029-01-01','2029-01-03'])
        self.assertEqual(source['metadata']['event_periods'], [
            {'start':'2028-12-31','end':'2029-01-02'}, {'start':'2029-01-03','end':'2029-01-04'}])
        self.assertIn('2029-01-03', source['text'])
        self.assertNotIn('2029-01-02', source['text'])
        other = self.collect([(2028,12,31),(2029,1,1)])['sources'][1]
        self.assertNotEqual(source['metadata']['content_hash'], other['metadata']['content_hash'])
        self.assertEqual(source['id'], other['id'])

    def test_holiday_collision_and_nonholiday_duplicate_fail_closed(self):
        for changed in ('title','scope','publication','duplicate_day','normal_route'):
            client = holiday_client([(2028,5,16),(2028,5,17)])
            if changed == 'title':
                client.events[(2028,5)][17] = calendar_cell('Different holiday', '/terminarz/szczegoly_wolne/87002')
            elif changed == 'duplicate_day':
                client.events[(2028,5)][16] *= 2
            elif changed == 'normal_route':
                client.events[(2028,5)] = {day: calendar_cell() for day in (16,17)}
                from test_source_collectors import detail
                client.details['/terminarz/szczegoly/87001'] = detail()
            else:
                original = client.follow
                reads = []
                def follow(url):
                    result = original(url)
                    if '/szczegoly_wolne/' in url:
                        reads.append(url)
                        if len(reads)>1:
                            result.text = holiday_detail(scope='Other' if changed=='scope' else 'School',
                                publication='2028-05-02 10:20:30' if changed=='publication' else '2028-05-01 10:20:30')
                    return result
                client.follow = follow
            with self.subTest(changed=changed):
                batch = collect_sources(client, current=CURRENT)
                self.assertEqual(batch['status'], 'partial')
                self.assertNotIn('school_event',batch['complete_kinds'])
                self.assertIn(batch['kind_errors']['school_event'], ('calendar_identity_ambiguous','calendar_publication_ambiguous'))

    def test_moving_window_keeps_current_dates_only_and_preserves_legacy_ids_and_intent(self):
        days = [(2028,5,16),(2028,5,17)]
        first = self.collect(days, CURRENT)
        with tempfile.TemporaryDirectory() as directory:
            store = AppStore(Path(directory)/'state.sqlite3', config=TEST_CONFIG)
            try:
                # Simulate the older one-day holiday payload, without grouped schema.
                legacy = copy.deepcopy(first)
                source = next(s for s in legacy['sources'] if s['kind']=='school_event')
                from app.source_audience import AUDIENCE_KEY, mint_calendar_audience
                source['text'] = 'Zakres: School'
                for key in ('event_dates','event_periods','holiday_signature','holiday_detail_text'):
                    source['metadata'].pop(key,None)
                source['metadata'][AUDIENCE_KEY] = mint_calendar_audience(source, legacy['learner'])
                store.ingest_sources(legacy)
                sid = source_identity(source['kind'],source['upstream_id'],source['learner_id'])
                with store.db:
                    store.db.execute('INSERT INTO canonical_decisions VALUES (?,?,?)',('kept-action',json.dumps({'user_rejected':True}),'2028-05-01'))
                store.ingest_sources(first)
                self.assertEqual(store.get_source(sid)['metadata']['event_dates'],['2028-05-16','2028-05-17'])
                before = tuple(store.db.execute('SELECT * FROM canonical_decisions').fetchone())
                # Direct calendar read with a bounded edge supplies no stale outside date.
                from datetime import date
                result = collect_calendar(holiday_client(days), learner_id=source['learner_id'], fetched_at='2028-05-17',
                                          start=date(2028,5,17),end=date(2028,5,20))
                edge = dict(first, sources=result, window_start='2028-05-17',window_end='2028-05-20')
                store.ingest_sources(edge)
                self.assertEqual(store.get_source(sid)['metadata']['event_dates'],['2028-05-17'])
                self.assertEqual(before,tuple(store.db.execute('SELECT * FROM canonical_decisions').fetchone()))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM sources WHERE upstream_id='holiday:87002'").fetchone()[0],1)
            finally:
                store.close()

    def test_specific_kind_failure_is_preserved_without_provider_error_text(self):
        client = holiday_client([(2028,5,16),(2028,5,17)])
        client.events[(2028,5)][17] = calendar_cell('Conflict', '/terminarz/szczegoly_wolne/87002')
        batch = collect_sources(client,current=CURRENT)
        with tempfile.TemporaryDirectory() as directory:
            store=AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
            try:
                store.ingest_sources(batch)
                codes = dict(store.db.execute('SELECT kind,last_error FROM source_health'))
                self.assertEqual(codes['school_event'],'calendar_identity_ambiguous')
                self.assertEqual(codes['substitution'],'calendar_identity_ambiguous')
                self.assertIsNone(codes['announcement'])
                # Failed learner collection takes the controller fallback path.
                client.profiles=[page('<p>Unavailable profile</p>')]
                client.collect_sources = lambda **kwargs: collect_sources(client,**kwargs)
                SyncController(store,client)._source_collection(CURRENT)
                codes=dict(store.db.execute('SELECT kind,last_error FROM source_health'))
                self.assertEqual(codes['school_event'],'learner_context_unverified')
                self.assertNotIn('private',json.dumps(codes))
            finally:store.close()


class BatchCalendarTests(TestCase):
    def expected(self,n=13):
        return {f'fictional-{index}':{'id':f'fictional-{index}', 'summary':'Fictional',
            'start':{'date':'2030-03-30'},'end':{'date':'2030-04-02'}} for index in range(n)}

    def process(self, responses):
        self.calls=[]
        def run(args,prompt,timeout):
            self.calls.append((args,prompt,timeout))
            data=responses[len(self.calls)-1]
            requested=list(self.expected())[10*(len(self.calls)-1):10*len(self.calls)]
            arguments={'calendar_id':TEST_CONFIG.calendar_id,'event_ids':requested}
            record={'type':'item.completed','item':{'type':'mcp_tool_call','tool':'google_calendar.batch_read_event',
                'arguments':arguments,'result':{'structured_content':data}}}
            return 0,json.dumps(record),''
        return run

    def test_thirteen_exact_ids_use_two_cli_calls_reordered_dtos_and_shared_dst_normalization(self):
        expected=self.expected()
        projected=[dict(ev,start='2030-03-30T00:00:00',end='2030-04-02T00:00:00') for ev in expected.values()]
        runtime=CodexRuntime(TEST_CONFIG,run_process=self.process([
            {'responses':list(reversed(projected[:10]))},{'responses':list(reversed(projected[10:]))}]),read_only=True)
        diagnostics=[]
        runtime.invocation_sink=lambda item:diagnostics.append(dict(item))
        actual=runtime.read_events(expected)
        self.assertEqual(set(actual),set(expected))
        self.assertEqual(len(self.calls),2)
        self.assertEqual([call[2] for call in self.calls],[300,300])
        self.assertEqual([d['requested_count'] for d in diagnostics],[10,10,3,3])
        self.assertEqual([d['received_count'] for d in diagnostics],[0,10,0,3])
        for eid in expected:
            self.assertEqual(actual[eid]['start'],{'date':'2030-03-30'})
            self.assertEqual(actual[eid]['end'],{'date':'2030-04-02'})
            self.assertEqual(event_fingerprint(actual[eid]),event_fingerprint(expected[eid]))
        self.assertIn(f'apps.{TEST_CONFIG.calendar_connector}.tools.batch_read_event.enabled=true', self.calls[0][0])
        self.assertIn(f'apps.{TEST_CONFIG.calendar_connector}.destructive_enabled=false',self.calls[0][0])

    def test_partial_foreign_duplicate_error_text_and_auth_fail_closed(self):
        expected=self.expected(2)
        valid=list(expected.values())
        variants=[{'responses':valid[:1]}, {'responses':[valid[0],dict(valid[1],id='foreign')]},
            {'responses':[valid[0],valid[0]]}, {'responses':[valid[0],{'id':'fictional-1','error':'404 not found'}]},
            {'responses':[valid[0],{'id':'fictional-1','status':404}]},
            {'responses':[valid[0],{'event_id':'fictional-1','status':401,'error':'unauthorized'}]}]
        for data in variants:
            runtime=CodexRuntime(TEST_CONFIG)
            runtime._tool=MagicMock(return_value=data)
            with self.subTest(data=data),self.assertRaises(RuntimeFailure):runtime.read_events(expected)
            self.assertEqual(runtime._tool.call_count,1)
        runtime=CodexRuntime(TEST_CONFIG)
        runtime._tool=MagicMock(side_effect=RuntimeFailure('codex_permission_denied'))
        with self.assertRaisesRegex(RuntimeFailure,'codex_permission_denied'):runtime.read_events(expected)
        self.assertEqual(runtime._tool.call_count,1)

    def test_observed_anonymous_404_requires_complete_ordered_siblings_and_numeric_json_code(self):
        expected=self.expected(3)
        good=list(expected.values())
        envelope={'error':'Failed to read event','detail':'404: HTTP request failed (Response: '+json.dumps({
            'error':{'errors':[{'domain':'global','reason':'notFound','message':'Not Found'}],
                     'code':404,'message':'Not Found'}})+'\n)'}
        runtime=CodexRuntime(TEST_CONFIG)
        runtime._tool=MagicMock(return_value={'responses':[good[0],envelope,good[2]]})
        actual=runtime.read_events(expected)
        self.assertIsNone(actual['fictional-1'])
        self.assertEqual(actual['fictional-2']['id'],'fictional-2')
        invalid=[{'responses':[good[2],envelope,good[0]]},
            {'responses':[good[0],envelope]},
            {'responses':[good[0],dict(envelope,detail=envelope['detail'].replace('"code": 404','"code": 401')),good[2]]},
            {'responses':[good[0],dict(envelope,detail='404 not found'),good[2]]},
            {'responses':[good[0],dict(envelope,detail=envelope['detail'].replace('404:','401:')),good[2]]},
            {'responses':[good[0],dict(envelope,error='Unauthorized'),good[2]]}]
        for data in invalid:
            runtime._tool=MagicMock(return_value=data)
            with self.subTest(data=data),self.assertRaises(RuntimeFailure):runtime.read_events(expected)
            self.assertEqual(runtime._tool.call_count,1)

    def test_empty_managed_inventory_uses_no_tool_and_timed_midnight_remains_timed(self):
        runtime=CodexRuntime(TEST_CONFIG)
        runtime._tool=MagicMock()
        self.assertEqual(runtime.read_events({}),{})
        runtime._tool.assert_not_called()
        timed={'id':'fictional-0','start':'2030-03-30T00:00:00+01:00','end':'2030-03-30T01:00:00+01:00'}
        runtime._tool.return_value={'responses':[timed]}
        self.assertEqual(runtime.read_events({'fictional-0':timed})['fictional-0']['start'],timed['start'])


class DiagnosticTests(TestCase):
    def diagnostic(self,**changes):
        value=dict(phase='reconcile',state='running',requested_count=10,received_count=0,
            duration_seconds=0.0,process_code=None,started_count=0,completed_count=0)
        return dict(value,**changes)

    def test_old_database_nullable_migration_durable_start_end_privacy_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite3'
            store=AppStore(path,config=TEST_CONFIG)
            store.close()
            db=sqlite3.connect(path)
            with db:
                db.execute('ALTER TABLE sync_runs DROP COLUMN diagnostics')
                db.execute("INSERT INTO sync_runs(id,started_at,finished_at,stage,result) VALUES ('old','2020-01-01T00:00:00+00:00','2020-01-01T00:00:01+00:00','complete','ok')")
            db.close()
            self.assertIsNone(read_monitoring_state(path)['sync_diagnostics'])
            store=AppStore(path,config=TEST_CONFIG)
            try:
                self.assertIsNone(store.db.execute("SELECT diagnostics FROM sync_runs WHERE id='old'").fetchone()[0])
                run=store.start_sync_run()
                self.assertIsNone(store.db.execute("SELECT id FROM sync_runs WHERE id='old'").fetchone())
                store.record_sync_diagnostic(run,self.diagnostic())
                self.assertEqual(read_monitoring_state(path)['sync_diagnostics']['invocations'][0]['state'],'running')
                store.record_sync_diagnostic(run,self.diagnostic(state='failed',process_code=-9,started_count=1))
                store.finish_sync_run(run,'failed','codex_timeout')
                saved=read_monitoring_state(path)['sync_diagnostics']
                self.assertEqual(saved['invocations'][0]['started_count'],1)
                self.assertEqual(saved['invocations'][0]['process_code'],-9)
                for mutation in ({'event_id':'private'},{'arguments':{}},{'raw_text':'private'},{'phase':'private'}):
                    with self.assertRaises(ValueError):sync_diagnostics({'invocations':[dict(self.diagnostic(),**mutation)],'omitted_count':0})
                self.assertNotIn('private',json.dumps(saved))
            finally:store.close()

    def test_abandoned_invocation_closes_and_bounded_history_has_omission_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store=AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
            try:
                run=store.start_sync_run()
                for _ in range(514):
                    store.record_sync_diagnostic(run,self.diagnostic())
                    store.record_sync_diagnostic(run,self.diagnostic(state='completed',received_count=10))
                value=store.status()['sync_diagnostics']
                self.assertEqual(len(value['invocations']),512)
                self.assertEqual(value['omitted_count'],2)
                store.record_sync_diagnostic(run,self.diagnostic())
                store.start_sync_run()
                value=json.loads(store.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?',(run,)).fetchone()[0])
                self.assertEqual(value['invocations'][-1]['state'],'interrupted')
            finally:store.close()

    def test_timeout_and_interrupt_capture_before_during_after_actual_tool_counts(self):
        args={'calendar_id':TEST_CONFIG.calendar_id,'event_ids':['fictional-0']}
        def record(kind):
            item={'id':'call-1','type':'mcp_tool_call','tool':'google_calendar.batch_read_event','arguments':args,'status':'completed' if kind=='item.completed' else 'in_progress'}
            if kind=='item.completed':item['result']={'structured_content':{'responses':list(BatchCalendarTests().expected(1).values())}}
            return json.dumps({'type':kind,'item':item})
        for output,counts in (('',(0,0)),(record('item.started'),(1,0)),(record('item.started')+'\n'+record('item.completed'),(1,1))):
            for failure in (subprocess.TimeoutExpired('codex',300),SyncInterrupted('sync_interrupted')):
                child=MagicMock(pid=12345,returncode=-9)
                child.communicate.side_effect=[failure,(output,'private stderr')]
                runtime=CodexRuntime(TEST_CONFIG)
                records=[]
                runtime.invocation_sink=lambda data:records.append(dict(data))
                with self.subTest(counts=counts,failure=type(failure)),patch('app.codex_runtime.subprocess.Popen',return_value=child),patch('app.codex_runtime.os.killpg'):
                    with self.assertRaises((RuntimeFailure,SyncInterrupted)):runtime.read_events(BatchCalendarTests().expected(1))
                self.assertEqual((records[-1]['started_count'],records[-1]['completed_count']),counts)
                self.assertEqual(records[-1]['process_code'],-9)
                self.assertEqual(records[0]['state'],'running')
                self.assertIn(records[-1]['state'],('failed','interrupted'))
                self.assertNotIn('private',json.dumps(records))


class HolidayBoundaryIntegrationTests(TestCase):
    def test_boundary_shortening_reviews_only_target_and_retains_exact_existing_ids(self):
        for old_start,old_end,new_start,new_end,window_start,window_end,all_day in (
            ('2030-03-20','2030-04-05','2030-03-25','2030-04-05','2030-03-25','2030-04-30',True),
            ('2030-03-20','2030-04-05','2030-03-20','2030-04-01','2030-03-01','2030-03-31',True),
            ('2030-03-20','2030-04-05','2030-03-25T10:00:00+01:00','2030-03-25T11:00:00+01:00','2030-03-25','2030-04-30',False),
            ('2030-03-20','2030-04-05','2030-03-31T10:00:00+02:00','2030-03-31T11:00:00+02:00','2030-03-01','2030-03-31',False),
            ('2030-03-20','2030-04-05','2030-03-20T10:00:00+01:00',None,'2030-03-25','2030-04-30',False)):
            with self.subTest(boundary=(old_start,old_end)),tempfile.TemporaryDirectory() as directory:
                store=AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
                try:
                    # Older payload needs no grouping signature or provenance migration.
                    sources=[dict(kind='school_event',upstream_id='holiday:fictional',learner_id=LEARNER['id'],
                        title='Fictional holiday',text='Observed dates only',metadata={
                            'event_dates':[new_start.split('T')[0]],'window_start':window_start,'window_end':window_end}),
                        dict(kind='announcement',upstream_id='fictional-duty',learner_id=LEARNER['id'],
                            title='Fictional duty',text='Class 1x bring paper on March 12.',metadata={})]
                    store.ingest_sources({'learner':LEARNER,'sources':sources,'complete_kinds':[]},NOW)
                    existing_action=dict(action(),start=old_start,end=old_end,temporal_kind='event')
                    event=dict(id='kept-event',summary='Fictional holiday',start={'date':old_start},end={'date':old_end})
                    with store.db:
                        store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                            ('kept-action','kept-proposal',json.dumps(existing_action),'applied',NOW.isoformat()))
                        store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                            ('kept-event',TEST_CONFIG.calendar_id,'kept-message','kept-proposal',json.dumps(event),
                             event_fingerprint(event),'kept-marker',NOW.isoformat(),'active'))
                    snapshot=assemble_agenda_snapshot(store,{'kept-event':event},current=NOW)
                    holiday=next(s for s in snapshot['sources'] if s['kind']=='school_event')
                    duty=next(s for s in snapshot['sources'] if s['kind']=='announcement')
                    update=item(snapshot,'update',action_id='kept-action',action=dict(existing_action,start=new_start,end=new_end,all_day=all_day),
                        sources=[{'source_id':holiday['id'],'source_hash':holiday['content_hash']}])
                    independent=item(snapshot,sources=[{'source_id':duty['id'],'source_hash':duty['content_hash']}])
                    prepared,proposal=_prepare_item(store,snapshot,update,'fictional-plan',0,NOW)
                    other,other_proposal=_prepare_item(store,snapshot,independent,'fictional-plan',1,NOW)
                    self.assertEqual(prepared['operation'],'review')
                    self.assertEqual(prepared['action_id'],'kept-action')
                    self.assertEqual(proposal['status'],'review')
                    self.assertEqual(proposal['event_id'],'kept-event')
                    self.assertIn('Observed holiday window',proposal['review_reason'])
                    self.assertEqual(other['operation'],'create')
                    self.assertEqual(other_proposal['status'],'pending')
                    self.assertEqual(store.db.execute('SELECT id FROM events').fetchone()[0],'kept-event')
                finally:store.close()

    def test_grouped_holiday_replay_has_zero_additional_model_calls_or_writes(self):
        from app.agenda import AgendaPlanner
        from test_agenda_integration import Calendar
        batch=collect_sources(holiday_client([(2028,12,31),(2029,1,1)]),
                              current=datetime(2028,12,20,tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as directory:
            store=AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
            try:
                current=datetime(2028,12,20,tzinfo=timezone.utc)
                store.ingest_sources(batch,current)
                store.set_writes_paused(False)
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',
                        ('message',current.isoformat(),current.isoformat(),'ok',None,1))
                def select(snapshot):
                    holiday=next(s for s in snapshot['sources'] if s['kind']=='school_event')
                    interval=holiday['metadata']['event_periods'][0]
                    return [item(snapshot,action=dict(action(),title='Fictional holiday',start=interval['start'],
                        end=interval['end'],temporal_kind='event'))]
                runtime=Calendar(select)
                planner=AgendaPlanner(store,runtime)
                first=planner.run(current)
                self.assertEqual(first['status'],'applied')
                store.queue_operations(current)
                for operation in store.pending_operations():
                    self.assertTrue(store.claim_operation(operation['operation_id'],expected_operation=operation))
                    store.record_result(operation['operation_id'],runtime.execute(operation),agenda_calendar=planner.calendar,current=current)
                self.assertEqual(len(runtime.writes),1)
                first_event=store.list_events()[0]['id']
                store.ingest_sources(batch,current)
                second=planner.run(current)
                self.assertEqual(second['status'],'unchanged')
                self.assertEqual(second['model_calls'],0)
                self.assertEqual(runtime.calls,1)
                self.assertEqual(store.queue_operations(current),[])
                self.assertEqual(len(runtime.writes),1)
                self.assertEqual(store.list_events()[0]['id'],first_event)
                # The next day's equally complete scan changes its window only.
                from app.agenda_incremental import assemble_incremental_snapshot, content_baseline_hash
                before_full=assemble_agenda_snapshot(store,runtime.events,current=current)
                before_incremental,before_contract=assemble_incremental_snapshot(store,runtime.events,current=current)
                next_day=current+timedelta(days=1)
                next_batch=collect_sources(holiday_client([(2028,12,31),(2029,1,1)]),current=next_day)
                self.assertEqual(store.ingest_sources(next_batch,next_day)['changed'],0)
                same_day_full=assemble_agenda_snapshot(store,runtime.events,current=current)
                _,same_day_contract=assemble_incremental_snapshot(store,runtime.events,current=current)
                self.assertEqual(content_baseline_hash(before_full),content_baseline_hash(same_day_full))
                self.assertNotEqual(before_full['snapshot_hash'],same_day_full['snapshot_hash'])
                self.assertNotEqual(before_contract['local_state_hash'],same_day_contract['local_state_hash'])
                altered=copy.deepcopy(same_day_full)
                next(s for s in altered['sources'] if s['kind']=='school_event')['metadata']['event_dates'].append('2029-01-02')
                self.assertNotEqual(content_baseline_hash(same_day_full),content_baseline_hash(altered))
                after_full=assemble_agenda_snapshot(store,runtime.events,current=next_day)
                after_incremental,after_contract=assemble_incremental_snapshot(store,runtime.events,current=next_day)
                self.assertEqual(content_baseline_hash(before_full),content_baseline_hash(after_full))
                self.assertNotEqual(before_contract['local_state_hash'],after_contract['local_state_hash'])
                holiday_after=next(s for s in after_full['sources'] if s['kind']=='school_event')
                holiday_before=next(s for s in before_full['sources'] if s['kind']=='school_event')
                self.assertNotEqual(holiday_before['metadata']['window_start'],holiday_after['metadata']['window_start'])
                third=planner.run(next_day)
                self.assertEqual(third['status'],'unchanged')
                self.assertEqual(third['model_calls'],0)
                self.assertEqual(runtime.calls,1)
                self.assertEqual(store.queue_operations(next_day),[])
                self.assertEqual(len(runtime.writes),1)
            finally:store.close()

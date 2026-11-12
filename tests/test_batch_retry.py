"""Fictional batch-read retry budget and private structural diagnostics."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from app.codex_runtime import CodexRuntime, RuntimeFailure
from app.core import AppStore
from app.health_state import sync_diagnostics
from app.sync import SyncController, SyncInterrupted
from test_support import TEST_CONFIG
from test_sync import FakeClient
from test_core import NOW


def events(count=1):
    return {f'fictional-{n}': dict(id=f'fictional-{n}', summary='Fictional',
        start={'date': '2030-03-01'}, end={'date': '2030-03-02'}) for n in range(count)}


def output(kind='empty', message='{"ok":true}'):
    records = [{'type': 'turn.completed'}]
    if message is not None:
        records.insert(0, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': message}})
    if kind == 'empty':
        return 0, '\n'.join(map(json.dumps, records)), ''
    return records


class BatchRetryTests(TestCase):
    def runtime(self, kinds):
        self.calls, self.records = [], []
        def run(args, prompt, timeout):
            self.calls.append((args, prompt, timeout))
            request = json.loads(prompt.split('\n')[-1])
            kind = kinds[len(self.calls)-1]
            if isinstance(kind, tuple):
                return kind
            if kind == 'empty':
                return output()
            records = output('records')
            data = {'responses': [events(20)[eid] for eid in request['arguments']['event_ids']]}
            if kind == 'partial':
                data['responses'] = []
            records.insert(0, {'type': 'item.completed', 'item': dict(type='mcp_tool_call',
                tool='google_calendar.'+request['tool'], arguments=request['arguments'], result={'structured_content': data})})
            return 0, '\n'.join(map(json.dumps, records)), ''
        runtime = CodexRuntime(TEST_CONFIG, run_process=run)
        runtime.invocation_sink = lambda item: self.records.append(dict(item))
        return runtime

    @patch('app.codex_runtime.time.sleep')
    def test_zero_calls_retries_exact_batch_in_new_ephemeral_session_and_recovers(self, sleep):
        runtime = self.runtime(['empty', 'success'])
        self.assertEqual(runtime.read_events(events()), events())
        sleep.assert_called_once_with(5)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1:], self.calls[1][1:])
        self.assertNotEqual(self.calls[0][0][self.calls[0][0].index('-C')+1], self.calls[1][0][self.calls[1][0].index('-C')+1])
        self.assertEqual([r['state'] for r in self.records], ['running','failed','running','completed'])
        self.assertEqual([r['attempt_number'] for r in self.records], [1,1,2,2])
        self.assertEqual(self.records[1]['retry_decision'], 'retry')
        self.assertTrue(self.records[-1]['recovered'])
        self.assertEqual(self.records[1]['final_answer_classification'], 'exact_ok_true')
        self.assertTrue(self.records[1]['turn_completed'])

    @patch('app.codex_runtime.time.sleep')
    def test_twice_empty_fails_and_budget_spans_later_batches_and_read_events(self, sleep):
        for kinds, count, again in ((['empty','empty'],1,False),
                                    (['empty','success','empty'],11,False),
                                    (['empty','success','empty'],1,True)):
            runtime = self.runtime(kinds)
            with self.subTest(kinds=kinds, again=again):
                with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_count'):
                    runtime.read_events(events(count))
                    if again:
                        runtime.read_events(events())
                self.assertEqual(len(self.calls), len(kinds))
                self.assertEqual(self.records[-1]['retry_decision'], 'exhausted')
                self.assertFalse(self.records[-1]['recovered'])

    @patch('app.codex_runtime.time.sleep')
    def test_successful_batches_are_not_repeated_and_explicit_reset_restores_budget(self, sleep):
        runtime = self.runtime(['success','empty','success','empty','success'])
        self.assertEqual(runtime.read_events(events(11)), events(11))
        self.assertEqual([r['batch_index'] for r in self.records], [1,1,2,2,2,2])
        runtime.reset_batch_retry_budget()
        runtime.read_events(events())
        self.assertEqual(self.records[-1]['batch_index'], 1)
        self.assertEqual(len(self.calls), 5)

    @patch('app.codex_runtime.time.sleep')
    def test_invalid_retried_payload_is_failure_not_recovery(self, sleep):
        runtime = self.runtime(['empty', 'partial'])
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_calendar_snapshot_incomplete'):
            runtime.read_events(events())
        self.assertEqual(self.records[-1]['state'], 'failed')
        self.assertFalse(self.records[-1]['recovered'])

    @patch('app.codex_runtime.time.sleep')
    def test_forbidden_failures_do_not_retry(self, sleep):
        for failure in ((1,'',''), (0,'not json',''),
                        (0,json.dumps({'type':'unknown'}),''),
                        (0,json.dumps({'type':'turn.failed','error':'private'}),''),
                        (0,'','permission denied')):
            runtime = self.runtime([failure])
            with self.subTest(failure=failure), self.assertRaises(RuntimeFailure):
                runtime.read_events(events())
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.records[-1]['retry_decision'], 'not_eligible')
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_non_batch_tools_and_writes_never_retry(self, sleep):
        for name in ('read_event','create_event','update_event','delete_event','search_events'):
            runtime = self.runtime(['empty'])
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeFailure,'unexpected_tool_count'):
                runtime._tool(name, {'calendar_id': TEST_CONFIG.calendar_id})
            self.assertEqual(len(self.calls),1)
        sleep.assert_not_called()

    @patch('app.codex_runtime.time.sleep')
    def test_sink_failure_prevents_dispatch_or_retry(self, sleep):
        for fail_at in (1,2,3):
            runtime = self.runtime(['empty','success'])
            def sink(record):
                self.records.append(dict(record))
                if len(self.records) == fail_at:
                    raise ValueError('private sink error')
            runtime.invocation_sink = sink
            with self.subTest(fail_at=fail_at), self.assertRaises(ValueError):
                runtime.read_events(events())
            self.assertEqual(len(self.calls), 0 if fail_at == 1 else 1)

    def test_interrupted_wait_never_dispatches_second_attempt(self):
        runtime = self.runtime(['empty','success'])
        with patch('app.codex_runtime.time.sleep', side_effect=SyncInterrupted('sync_timeout')):
            with self.assertRaises(SyncInterrupted): runtime.read_events(events())
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.records[-1]['recovered'])

    @patch('app.codex_runtime.time.sleep')
    def test_final_answer_classification_is_structural_and_private(self, sleep):
        for message, classification in ((None,'missing'), ('{"ok":true}','exact_ok_true'),
            ('{"ok":false}','exact_ok_false'), ('{"ok":1}','other'), ('{"ok":"true"}','other'),
            ('{"ok":true,"private":"secret"}','other'), ('{"ok":true,"ok":false}','other'),
            ('private school message token','other')):
            runtime = self.runtime([output(message=message), 'success'])
            runtime.read_events(events())
            self.assertEqual(self.records[1]['final_answer_classification'],classification)
            self.assertNotIn('secret',json.dumps(self.records))
            self.assertNotIn('school message',json.dumps(self.records))
        duplicate = output()[1]+'\n'+json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'secret'}})
        runtime = self.runtime([(0,duplicate,''),'success'])
        runtime.read_events(events())
        self.assertEqual(self.records[1]['final_answer_classification'],'multiple')

    @patch('app.codex_runtime.time.sleep')
    def test_durable_both_attempts_validate_and_old_records_still_validate(self,sleep):
        with tempfile.TemporaryDirectory() as directory:
            store=AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
            try:
                run=store.start_sync_run()
                runtime=self.runtime(['empty','success'])
                runtime.invocation_sink=lambda record:store.record_sync_diagnostic(run,dict(record,phase='reconcile'))
                runtime.read_events(events())
                saved=store.status()['sync_diagnostics']
                self.assertEqual(len(saved['invocations']),2)
                self.assertTrue(saved['invocations'][1]['recovered'])
                sync_diagnostics(saved)
                old={key:value for key,value in saved['invocations'][0].items() if key in {
                    'phase','state','requested_count','received_count','duration_seconds','process_code','started_count','completed_count'}}
                sync_diagnostics({'invocations':[old],'omitted_count':0})
                for key,value in (('batch_index',True),('attempt_number',3),('retry_decision','secret'),
                    ('recovered',1),('turn_completed',1),('final_answer_classification','secret'),('parse_error_count',-1)):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        sync_diagnostics({'invocations':[dict(old,**{key:value})],'omitted_count':0})
            finally: store.close()

    @patch('app.codex_runtime.time.sleep')
    def test_observed_calls_incomplete_arguments_and_tool_errors_never_retry(self, sleep):
        arguments = {'calendar_id': TEST_CONFIG.calendar_id, 'event_ids': list(events())}
        call = dict(id='fictional-call', type='mcp_tool_call', tool='google_calendar.batch_read_event',
                    arguments=arguments, result={'structured_content': {'responses': list(events().values())}})
        completed = {'type':'item.completed', 'item':call}
        variants = [
            [{'type':'item.started','item':dict(call,status='in_progress')}],
            [{'type':'item.updated','item':call}],
            [completed,completed],
            [{'type':'item.completed','item':dict(call,arguments={})}],
            [{'type':'item.completed','item':dict(call,tool='google_calendar.create_event')}],
            [{'type':'item.completed','item':dict(call,error='private tool failure')}],
            [{'type':'item.completed','item':dict(call,result=None)}],
        ]
        for records in variants:
            runtime = self.runtime([(0,'\n'.join(map(json.dumps,records)),'')])
            with self.subTest(records=records), self.assertRaises(RuntimeFailure):
                runtime.read_events(events())
            self.assertEqual(len(self.calls),1)
            self.assertEqual(self.records[-1]['retry_decision'],'not_eligible')
        sleep.assert_not_called()

    def test_wait_respects_outer_deadline_before_second_dispatch(self):
        runtime = self.runtime(['empty','success'])
        runtime.reset_batch_retry_budget(deadline=10)
        now = [8]
        def sleep(seconds):
            now[0] += seconds
        with patch('app.codex_runtime.time.monotonic', side_effect=lambda:now[0]), patch('app.codex_runtime.time.sleep', side_effect=sleep) as wait:
            with self.assertRaises(SyncInterrupted): runtime.read_events(events())
        wait.assert_called_once_with(2)
        self.assertEqual(len(self.calls),1)

    @patch('app.codex_runtime.time.sleep')
    def test_full_sync_alone_updates_success_and_failure_counters_and_resets_budget(self, sleep):
        for outcome in ('ok','partial','failed'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                store = AppStore(Path(directory)/'state.sqlite3',config=TEST_CONFIG)
                try:
                    previous = store.start_sync_run()
                    store.finish_sync_run(previous,'ok')
                    previous = store.start_sync_run()
                    store.finish_sync_run(previous,'failed','codex_failed')
                    baseline = store.status()['last_successful_sync']
                    runtime = self.runtime(['empty','success','empty','success'])
                    runtime.probe = lambda: {'calendar_id': TEST_CONFIG.calendar_id, 'access_role':'owner'}
                    controller = SyncController(store,FakeClient(count=0),runtime)
                    def run(dry_run,current):
                        controller._stage('reconcile')
                        runtime.read_events(events())
                        self.assertEqual(store.status()['last_successful_sync'],baseline)
                        self.assertEqual(store.status()['consecutive_failures'],1)
                        if outcome == 'failed':
                            raise RuntimeFailure('codex_failed')
                        return {'status':outcome, **({'error_code':'discovery_timeout'} if outcome == 'partial' else {})}
                    controller._run = run
                    result = controller.run(current=NOW)
                    self.assertEqual(result['status'],outcome)
                    self.assertEqual(result['state']['consecutive_failures'],0 if outcome == 'ok' else 2)
                    if outcome == 'ok':
                        self.assertNotEqual(result['state']['last_successful_sync'],baseline)
                    else:
                        self.assertEqual(result['state']['last_successful_sync'],baseline)
                    diagnostics=result['state']['sync_diagnostics']['invocations']
                    self.assertEqual([item['attempt_number'] for item in diagnostics],[1,2])
                    self.assertTrue(diagnostics[-1]['recovered'])
                    # Reuse the same runtime for another sync, proving reset at start.
                    controller._run = lambda dry,current: (runtime.read_events(events()) and {'status':'ok'})
                    result = controller.run(current=NOW)
                    self.assertEqual(result['status'],'ok')
                    self.assertEqual(len(self.calls),4)
                    self.assertEqual(result['state']['sync_diagnostics']['invocations'][-1]['batch_index'],1)
                finally: store.close()

    def test_large_or_deep_final_answer_never_blocks_valid_tool_result(self):
        for message in (' '*4096+'{"ok":true}', '['*2000+'0'+']'*2000):
            runtime=self.runtime(['success'])
            original=runtime.run_process
            def run(*args):
                code, stdout, stderr=original(*args)
                records=[json.loads(line) for line in stdout.splitlines()]
                next(record['item'] for record in records if record.get('item',{}).get('type') == 'agent_message')['text']=message
                return code,'\n'.join(map(json.dumps,records)),stderr
            runtime.run_process=run
            with self.subTest(length=len(message)):
                self.assertEqual(runtime.read_events(events()),events())
                self.assertEqual(self.records[-1]['final_answer_classification'],'other')

    def test_final_answer_parser_recursion_is_only_other(self):
        with patch('app.codex_runtime.json.loads', side_effect=RecursionError):
            self.assertEqual(CodexRuntime._final_answer_classification('{}'),'other')

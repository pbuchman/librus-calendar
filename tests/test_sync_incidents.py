import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import incidents
from app.cli import main
from app.core import AppStore
from test_support import TEST_CONFIG


class IncidentReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = AppStore(self.path, config=TEST_CONFIG)
        self.addCleanup(self.store.close)

    def run_report(self, *args):
        with redirect_stdout(io.StringIO()) as output:
            rc = main(['--state', str(self.path), 'sync-diagnostics', *args], config=TEST_CONFIG)
        self.assertEqual(rc, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_after_success_lookup_by_alert_time_and_id(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'analysis'):
            incidents.checkpoint('sync', 'analysis')
            for _ in range(2):
                incidents.emit('sync', 'analysis', 'agenda_plan_invalid', 'rejected')
        self.store.finish_sync_run(run, 'partial', 'agenda_plan_invalid')
        alert = datetime.now(timezone.utc).isoformat()
        success = self.store.start_sync_run()
        self.store.finish_sync_run(success, 'ok')
        by_time = self.run_report('--at', alert)
        self.assertEqual(by_time, self.run_report('--run', run))
        self.assertEqual(by_time, self.run_report())
        self.assertEqual(by_time['run']['id'], run)
        self.assertEqual(by_time['next_success']['id'], success)
        event, = by_time['report']['events']
        self.assertEqual((event['reason'], event['count']), ('agenda_plan_invalid', 2))
        self.assertEqual(self.run_report('--at', '2000-01-01T00:00:00Z')['status'], 'not_found')

    def test_repetitions_separated_by_other_evidence_keep_first_and_last_time(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'analysis'):
            incidents.emit('sync', 'analysis', 'fetch_failed', 'rejected')
            incidents.emit('sync', 'analysis', 'agenda_plan_invalid', 'rejected')
            incidents.emit('sync', 'analysis', 'fetch_failed', 'rejected')
        self.store.finish_sync_run(run, 'partial', 'fetch_failed')
        repeated, other = self.run_report()['report']['events']
        self.assertEqual(repeated['count'], 2)
        self.assertLessEqual(repeated['at'], other['at'])
        self.assertLessEqual(other['at'], repeated['last_at'])

    def test_never_commit_materialization_or_leak_untrusted_values(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'analysis'):
            self.store.db.execute("INSERT INTO settings VALUES ('test_uncommitted','yes')")
            incidents.emit('sync', 'analysis', 'agenda_plan_invalid', 'rejected')
            incidents.emit('sync', 'analysis', 'private-canary-name', 'rejected', private_key='synthetic-canary-value')
            self.assertTrue(self.store.db.in_transaction)
            self.store.db.rollback()
        self.assertIsNone(self.store._setting('test_uncommitted'))
        self.store.finish_sync_run(run, 'failed', 'agenda_plan_invalid')
        report = self.run_report('--run', run)
        self.assertNotIn('private-canary', json.dumps(report))
        self.assertEqual(report['report']['events'][0]['reason'], 'agenda_plan_invalid')

    def test_busy_database_buffers_until_the_next_checkpoint(self):
        run = self.store.start_sync_run()
        traced = []
        self.store.db.set_trace_callback(traced.append)
        self.addCleanup(self.store.db.set_trace_callback, None)
        self.store.db.execute('PRAGMA busy_timeout=50')
        with sqlite3.connect(self.path, timeout=0) as blocker:
            with incidents.recording(self.store, run, lambda: 'fetch'):
                blocker.execute('BEGIN IMMEDIATE')
                incidents.emit('sync', 'fetch', 'fetch_failed', 'rejected')
                self.assertIsNone(blocker.execute('SELECT 1 FROM sync_incidents').fetchone())
                blocker.rollback()
                incidents.checkpoint('sync', 'fetch')
        self.assertFalse(any('INSERT INTO sync_incidents' in sql for sql in traced))
        self.assertEqual(self.store.db.execute('PRAGMA busy_timeout').fetchone()[0], 50)
        self.assertEqual(self.run_report('--run', run)['report']['events'][0]['reason'], 'fetch_failed')

    def test_limit_first_last_failure_and_omissions(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'fetch'):
            self.store.db.execute('BEGIN')
            for n in range(380):
                incidents.emit('sync', 'fetch', 'fetch_failed', 'rejected', logical_call_index=n + 1)
            incidents.observe('sync', 'fetch', 'operations_unknown', 'unverified')
            self.store.db.rollback()
        self.store.finish_sync_run(run, 'partial', 'fetch_failed')
        raw, = self.store.db.execute('select report from sync_incidents where run_id=?', (run,)).fetchone()
        self.assertLessEqual(len(raw.encode()), 32768)
        report = self.run_report('--run', run)['report']
        events = [i for i in report['events'] if i['kind']=='incident']
        self.assertEqual([events[0]['evidence']['logical_call_index'],events[-1]['evidence']['logical_call_index']],[1,380])
        self.assertGreater(report['omitted'], 0)

    def test_abandoned_checkpoint_and_retention(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'fetch'):
            incidents.checkpoint('sync', 'fetch')
        alert_before_recovery = datetime.now(timezone.utc).isoformat()
        fresh = self.store.start_sync_run()
        self.store.finish_sync_run(fresh, 'ok')
        report = self.run_report()
        self.assertEqual(self.run_report('--at', alert_before_recovery), report)
        self.assertEqual(report['run']['result'], 'interrupted')
        self.assertEqual(report['report']['checkpoint']['operation'], 'fetch')
        cutoff = (datetime.now(timezone.utc)-timedelta(days=91)).isoformat()
        self.store.db.execute('update sync_runs set started_at=? where id=?',(cutoff,run))
        self.store.db.commit()
        self.store.start_sync_run()
        self.assertIsNone(self.store.db.execute('select 1 from sync_incidents where run_id=?',(run,)).fetchone())

    def test_legacy_read_only_and_no_configuration_required_for_explicit_state(self):
        run = self.store.start_sync_run()
        self.store.finish_sync_run(run, 'partial', 'unexpected_tool_arguments')
        self.store.db.execute('drop table sync_incidents')
        self.store.db.commit()
        before = self.path.read_bytes()
        with patch('app.cli.load_config',side_effect=AssertionError('opened config')), patch('app.cli.AppStore',side_effect=AssertionError('opened writer')), redirect_stdout(io.StringIO()) as out:
            rc = main(['--state',str(self.path),'sync-diagnostics','--run',run])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(out.getvalue())['detail_status'],'unavailable')
        self.assertEqual(before,self.path.read_bytes())

    def test_tampered_report_is_rejected_without_printing_its_contents(self):
        run = self.store.start_sync_run()
        with incidents.recording(self.store, run, lambda: 'analysis'):
            incidents.emit('sync', 'analysis', 'agenda_plan_invalid', 'rejected')
        self.store.finish_sync_run(run, 'failed', 'agenda_plan_invalid')
        with self.store.db:
            self.store.db.execute(
                "UPDATE sync_incidents SET report=json_set(report, '$.events[0].reason', ?) WHERE run_id=?",
                ('private-canary', run))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--state', str(self.path), 'sync-diagnostics', '--run', run]), 2)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'invalid_diagnostic_request_or_evidence'})

    def test_missing_database_has_read_stage_and_is_never_created(self):
        missing = self.path.parent / 'private-canary.sqlite3'
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--state', str(missing), 'sync-diagnostics']), 1)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'unavailable', 'stage': 'incident_read'})
        self.assertFalse(missing.exists())

    def test_pre_run_journal_never_interpolates_error(self):
        from app.config import ConfigurationError
        with patch('app.cli.load_config',side_effect=ConfigurationError('private-canary')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['sync']),2)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['stage'],'configuration')
        self.assertNotIn('private-canary',str(receipt))

    def test_full_controller_failure_next_success_and_replay_from_cli_only(self):
        from app.codex_runtime import CodexRuntime, RuntimeFailure
        from app.sync import SyncController
        from app.monitoring import sanitize_state
        from app.health_state import read_monitoring_state
        from test_core import NOW
        from test_sync import FakeClient, FakeRuntime

        # The production controller reads the active managed events in database order.
        ro = FakeRuntime(propose=False)
        self.store.set_writes_paused(False)
        ids = ['private-fixture-first', 'private-fixture-second']
        for eid in ids:
            event = {'id':eid,'summary':'private-fixture-title','start':{'date':'2030-04-09'},
                     'end':{'date':'2030-04-10'}}
            self.store.db.execute('insert into events values (?,?,?,?,?,?,?,?,?)',
                                  (eid,TEST_CONFIG.calendar_id,'synthetic-message','synthetic-proposal',json.dumps(event),'fp','marker',NOW.isoformat(),'active'))
        self.store.db.commit()

        def runner(actual_ids):
            def run_process(args, prompt, timeout):
                request = json.loads(prompt.splitlines()[-1])
                arguments = dict(request['arguments'], event_ids=actual_ids)
                item = dict(type='mcp_tool_call', id='synthetic-call', tool='google_calendar.batch_read_event',
                            arguments=arguments, result={'structured_content': {'responses':[]}})
                records = [dict(type=phase, item=item) for phase in ('item.started','item.completed')]
                return 0, '\n'.join(map(json.dumps,records)), ''
            return run_process

        runtime = CodexRuntime(TEST_CONFIG,run_process=runner(ids[::-1]),read_only=True)
        runtime.probe = ro.probe
        result = SyncController(self.store,FakeClient(count=0),runtime).run(current=NOW)
        self.assertEqual(result['error_code'],'unexpected_tool_arguments')
        alert = datetime.now(timezone.utc).isoformat()
        self.assertEqual(SyncController(self.store,FakeClient(count=0),ro).run(current=NOW)['status'],'ok')
        report = self.run_report('--at',alert)
        self.assertEqual(report['run']['id'],result['run_id'])
        self.assertIsNotNone(report['next_success'])
        self.assertNotIn('private-fixture',json.dumps(report))
        self.assertNotIn('private-fixture',json.dumps(result['state']['sync_diagnostics']))
        exported = sanitize_state(read_monitoring_state(self.path), datetime.now(timezone.utc))
        self.assertNotIn('sync_incidents',exported)
        self.assertNotIn('private-fixture',json.dumps(exported))
        by_phase = {row['evidence']['lifecycle']:row['evidence'] for row in report['report']['events']
                    if row['reason']=='request_mismatch'}
        self.assertEqual(set(by_phase),{'started','completed'})
        comparison = by_phase['completed']['event_ids']
        replay = CodexRuntime(TEST_CONFIG,run_process=runner(comparison['observed']),read_only=True)
        with self.assertRaisesRegex(RuntimeFailure,'unexpected_tool_arguments'):
            replay.read_events(dict.fromkeys(comparison['expected'],{}))

    def test_source_json_fragment_retrievable_after_recovery(self):
        from types import SimpleNamespace
        from app.librus_client import LibrusClient, DiscoveryParsingError
        from test_core import NOW
        from test_sync import FakeClient, FakeRuntime
        from app.sync import SyncController

        client = object.__new__(LibrusClient)
        modified = SimpleNamespace(json=lambda: {'data': {'fixture-canary': 'fixture-canary'}},
                                   headers={}, status_code=200, is_redirect=False, content=b'')
        self.store.set_writes_paused(False)
        with patch.object(client, 'collect_sources', side_effect=FakeClient(count=0).collect_sources), \
             patch.object(client, 'request',return_value=modified):
            failed = SyncController(self.store,client,FakeRuntime(propose=False)).run(current=NOW)
        self.assertEqual(failed['status'],'partial')
        recovered = SyncController(self.store,FakeClient(count=0),FakeRuntime(propose=False)).run(current=NOW)
        self.assertEqual(recovered['status'],'ok')
        report = self.run_report()
        self.assertEqual(report['run']['id'],failed['run_id'])
        row = next(e for e in report['report']['events'] if e['reason']=='message_page_shape')
        example = row['fictional_fragment']
        self.assertEqual(example['format'],'json')
        self.assertNotIn('fixture-canary',json.dumps(report))
        synthetic = example['observed']
        with patch.object(client,'request', return_value=SimpleNamespace(json=lambda:synthetic, is_redirect=False)), \
             self.assertRaises(DiscoveryParsingError):
            client.messages()

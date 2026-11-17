"""Fictional children exercise process observations without calling Codex."""
import copy
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app.codex_runtime import CodexRuntime, RuntimeFailure
from app.health_state import sync_diagnostics
from test_support import TEST_CONFIG


class ProcessObservationTests(unittest.TestCase):
    def runtime(self):
        return CodexRuntime(TEST_CONFIG)

    def child(self, runtime, code, timeout=.25, prompt='fictional input'):
        return runtime._run_process([sys.executable, '-u', '-c', code], prompt, timeout)

    def test_silent_timeout_has_observed_stage_and_application_stop_cause(self):
        runtime = self.runtime()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            self.child(runtime, 'import time; time.sleep(10)')
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(evidence['last_stage'], 'process_started')
        self.assertEqual(evidence['stop_cause'], 'app_timeout')
        self.assertEqual(evidence['error_category'], 'unknown')
        self.assertGreater(evidence['quiet_seconds'], .15)
        self.assertLess(time.monotonic()-started, 2)

    def test_stalls_before_during_after_tool_keep_actual_milestones(self):
        lines = [dict(type='thread.started', thread_id='fictional-thread'), dict(type='turn.started'),
                 dict(type='item.started', item=dict(type='mcp_tool_call', id='fictional-tool',
                       arguments={'private': 'SCHOOL_SENTINEL'}, tool='google_calendar.batch_read_event')),
                 dict(type='item.completed', item=dict(type='mcp_tool_call', result={'text':'SCHOOL_SENTINEL'}))]
        for count, stage in ((2, 'turn_started'), (3, 'tool_started'), (4, 'tool_completed')):
            runtime = self.runtime()
            code = 'import time; print(' + repr('\n'.join(map(json.dumps, lines[:count]))) + ', flush=True); time.sleep(10)'
            with self.subTest(stage=stage), self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
                self.child(runtime, code)
            observation = runtime.last_invocation_metadata['process_observation']
            self.assertEqual(observation['last_stage'], stage)
            self.assertEqual(observation['event_counts']['thread.started'], 1)
            self.assertNotIn('SCHOOL_SENTINEL', json.dumps(observation))
            self.assertNotIn('fictional-thread', json.dumps(observation))

    def test_real_child_large_input_concurrent_pipes_utf8_fragments_and_final_output(self):
        runtime = self.runtime()
        code = '''import os, sys, threading, time
time.sleep(1.1)
thread = threading.Thread(target=lambda: os.write(2, b'x'*200000)); thread.start()
for piece in [b'{"type":"turn.started"}\\n', '{"type":"item.completed","item":{"type":"agent_message","text":"ż"}}\\n'.encode()]:
 for byte in piece: os.write(1, bytes([byte]))
assert len(sys.stdin.buffer.read()) == 400000
thread.join()
'''
        rc, stdout, stderr = self.child(runtime, code, timeout=3, prompt='x'*400000)
        self.assertEqual(rc, 0)
        self.assertIn('ż', stdout)
        self.assertEqual(len(stderr), 200000)
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(evidence['stdout_bytes'], len(stdout.encode()))
        self.assertEqual(evidence['stderr_bytes'], len(stderr.encode()))
        self.assertEqual(evidence['invalid_line_count'], 0)
        self.assertEqual(evidence['stop_cause'], 'normal_exit')

    def test_cumulative_partial_utf8_unknown_invalid_and_unterminated_line_are_counted_once(self):
        from app.codex_diagnostics import ProcessObservation
        observation = ProcessObservation()
        first = b'{"type":"turn.started"}\n'
        full = first + '{"type":"item.updated","item":{"type":"reasoning","text":"ż SCHOOL_SENTINEL"}}\n'.encode()
        cut = full.index('ż'.encode()) + 1
        observation.observe(first, b'')
        observation.observe(full[:cut], b'')
        observation.observe(full, b'')
        observation.observe(full+b'garbage\n{"type":"SCHOOL_SENTINEL"}\n{"type":', b'')
        observation.finish(0, 'normal_exit')
        summary = observation.summary()
        self.assertEqual(summary['event_counts']['turn.started'], 1)
        self.assertEqual(summary['invalid_line_count'], 1)
        self.assertEqual(summary['unknown_event_count'], 1)
        self.assertEqual(summary['partial_line_bytes'], len(b'{"type":'))
        self.assertNotIn('SCHOOL_SENTINEL', json.dumps(observation.artifact()))

    def test_explicit_error_survives_timeout_without_model_text_classification(self):
        runtime = self.runtime()
        events = [dict(type='item.completed', item=dict(type='agent_message', text='permission denied SCHOOL_SENTINEL')),
                  dict(type='error', message='rate limit SCHOOL_SENTINEL')]
        with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            self.child(runtime, 'import time; print('+repr('\n'.join(map(json.dumps, events)))+', flush=True); time.sleep(10)')
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual((evidence['error_category'], evidence['error_origin']), ('codex_limit', 'protocol'))
        self.assertEqual(evidence['stop_cause'], 'app_timeout')
        self.assertNotIn('SCHOOL_SENTINEL', json.dumps(evidence))

    def test_fragmented_stderr_and_explicit_transport_errors_do_not_read_model_or_results(self):
        from app.codex_diagnostics import ProcessObservation
        for raw, expected in ((b'rate limit', 'codex_limit'), (b'HTTP 503', 'codex_http_5xx'),
                (b'connection reset by peer', 'codex_transport_connection'),
                (b'temporary failure in name resolution', 'codex_transport_dns'),
                (b'tls handshake failed', 'codex_transport_tls'), (b'request timed out', 'codex_upstream_timeout')):
            observation = ProcessObservation()
            for byte in raw:
                observation.feed('stderr', bytes([byte]))
            self.assertEqual(observation.summary()['error_category'], expected)
        observation = ProcessObservation()
        for item_type in ('reasoning', 'agent_message', 'mcp_tool_call'):
            observation.feed('stdout', (json.dumps(dict(type='item.completed',item=dict(type=item_type,
                text='HTTP 503 connection reset', result=dict(error='rate limit'))))+'\n').encode())
        self.assertEqual(observation.summary()['error_category'], 'unknown')

    def test_complete_final_line_without_newline_and_truncated_line_differ(self):
        runtime = self.runtime()
        self.child(runtime, "import sys; sys.stdout.write(" + repr('{"type":"turn.completed"}') + ")")
        summary = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(summary['last_stage'], 'turn_completed')
        self.assertEqual(summary['event_counts']['turn.completed'], 1)
        self.assertEqual(summary['partial_line_bytes'], 0)
        self.child(runtime, "import sys; sys.stdout.write(" + repr('{"type":') + ")")
        summary = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(summary['partial_line_bytes'], 8)
        self.assertEqual(summary['event_counts'], {})

    def test_optional_observer_exception_does_not_stop_child_or_hide_result(self):
        runtime = self.runtime()
        with patch('app.codex_diagnostics.ProcessObservation.feed', side_effect=OSError('SCHOOL_SENTINEL')):
            rc, stdout, _ = self.child(runtime, "print(" + repr('{"type":"turn.completed"}') + ")")
        self.assertEqual(rc, 0)
        self.assertIn('turn.completed', stdout)
        self.assertEqual(runtime.last_invocation_metadata['process_observation']['observation_status'], 'observation_failed')

    def test_invalid_utf8_is_not_accepted_by_successful_observation(self):
        runtime = self.runtime()
        with self.assertRaisesRegex(RuntimeFailure, 'codex_protocol_invalid'):
            self.child(runtime, 'import os; os.write(1, bytes([255]))')

    def test_spawn_failure_does_not_claim_child_started(self):
        runtime = self.runtime()
        with self.assertRaises(OSError):
            runtime._run_process(['/nonexistent-fictional-codex'], '', 1)
        summary = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(summary['last_stage'], 'invocation_created')
        self.assertEqual(summary['stop_cause'], 'unknown')

    def test_total_timeout_is_not_renewed_by_output_and_next_invocation_is_isolated(self):
        runtime = self.runtime()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            self.child(runtime, 'import time\nwhile True: print(\'{"type":"turn.started"}\',flush=True); time.sleep(.03)', timeout=1.2)
        self.assertLess(time.monotonic()-started, 2.2)
        first = runtime.last_invocation_metadata['process_observation']
        self.assertGreater(first['event_counts']['turn.started'], 10)
        self.child(runtime, 'pass')
        second = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(second['stdout_bytes'], 0)
        self.assertEqual(second['event_counts'], {})
        self.assertEqual(second['stop_cause'], 'normal_exit')

    def test_write_spawn_error_is_safe_but_postspawn_io_error_is_uncertain(self):
        args = dict(calendar_id=TEST_CONFIG.calendar_id)
        started = dict(type='item.started', item=dict(type='mcp_tool_call',
            id='fictional-call', tool='google_calendar.create_event', arguments=args))
        code = 'import time; print('+repr(json.dumps(started))+',flush=True); time.sleep(10)'
        runtime = self.runtime()
        with patch.object(runtime, '_args', return_value=['fictional-codex']), patch('app.codex_runtime.subprocess.Popen', side_effect=OSError('SCHOOL_SENTINEL')):
            with self.assertRaises(RuntimeFailure) as caught:
                runtime._invoke('Fictional input', 'tool.json', ('create_event',), 'create_event', args)
        self.assertEqual(caught.exception.code, 'codex_unavailable')
        self.assertFalse(caught.exception.possible_write)
        for failure_type in (OSError, subprocess.SubprocessError):
            runtime = self.runtime()
            original_pump = runtime._pump_process
            launched = []
            def io_failure(process, prompt, timeout, observation):
                launched.append(process)
                try:
                    original_pump(process, prompt, .1, observation)
                except subprocess.TimeoutExpired:
                    raise failure_type('SCHOOL_SENTINEL')
            with self.subTest(failure=failure_type.__name__), patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c',code]), patch.object(runtime, '_pump_process', side_effect=io_failure):
                with self.assertRaises(RuntimeFailure) as caught:
                    runtime._invoke('Fictional input', 'tool.json', ('create_event',), 'create_event', args)
            self.assertEqual(caught.exception.code, 'codex_unavailable')
            self.assertTrue(caught.exception.possible_write)
            self.assertEqual(launched[0].returncode, -9)
            self.assertEqual(runtime.last_invocation_metadata['started_count'], 1)
            self.assertEqual(runtime.last_invocation_metadata['process_observation']['stop_cause'], 'unknown')

    def test_cleanup_drain_error_preserves_timeout_partial_evidence_and_reaps_child(self):
        from app.sync import SyncInterrupted
        args = dict(calendar_id=TEST_CONFIG.calendar_id)
        started = dict(type='item.started', item=dict(type='mcp_tool_call',
            id='fictional-call', tool='google_calendar.create_event', arguments=args))
        code = 'import time; print('+repr(json.dumps(started))+',flush=True); time.sleep(10)'
        for interrupted in (False, True):
            runtime = self.runtime()
            original_pump = runtime._pump_process
            launched = []
            cancellation = SyncInterrupted('sync_timeout')
            def timeout_then_drain_error(process, prompt, timeout, observation):
                launched.append(process)
                def failed_drain():
                    raise OSError('SCHOOL_SENTINEL')
                process.communicate = failed_drain
                try:
                    return original_pump(process, prompt, .1, observation)
                except subprocess.TimeoutExpired:
                    if interrupted:
                        raise cancellation
                    raise
            with self.subTest(interrupted=interrupted), patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c',code]), patch.object(runtime, '_pump_process', side_effect=timeout_then_drain_error):
                with self.assertRaises(SyncInterrupted if interrupted else RuntimeFailure) as caught:
                    runtime._invoke('Fictional input', 'tool.json', ('create_event',), 'create_event', args)
            self.assertEqual(caught.exception.code, 'sync_timeout' if interrupted else 'codex_timeout')
            if interrupted:
                self.assertIs(caught.exception, cancellation)
            else:
                self.assertTrue(caught.exception.possible_write)
            self.assertEqual(launched[0].returncode, -9)
            with self.assertRaises(ChildProcessError):
                os.waitpid(launched[0].pid, os.WNOHANG)
            self.assertEqual(runtime.last_invocation_metadata['started_count'], 1)
            self.assertEqual(runtime.last_invocation_metadata['process_observation']['stop_cause'], 'sync_interruption' if interrupted else 'app_timeout')
            self.assertNotIn('SCHOOL_SENTINEL', json.dumps(runtime.last_invocation_metadata))

    def test_signal_minus_nine_does_not_claim_oom(self):
        runtime = self.runtime()
        rc, _, _ = self.child(runtime, 'import os,signal; os.kill(os.getpid(),signal.SIGKILL)')
        self.assertEqual(rc, -9)
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(evidence['stop_cause'], 'signal')
        self.assertEqual(evidence['error_category'], 'unknown')

    def test_interrupt_during_process_start_observation_reaps_child(self):
        from app.sync import SyncInterrupted
        launched = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process
        runtime = self.runtime()
        with patch('app.codex_runtime.subprocess.Popen', side_effect=launch), patch('app.codex_diagnostics.ProcessObservation.process_started', side_effect=SyncInterrupted('sync_timeout')):
            with self.assertRaises(SyncInterrupted):
                self.child(runtime, 'import time; time.sleep(10)', timeout=5)
        self.assertEqual(launched[0].poll(), -9)
        self.assertEqual(runtime.last_invocation_metadata['process_observation']['stop_cause'], 'sync_interruption')

    def test_whole_sync_interrupt_reaps_real_child_and_keeps_partial_evidence(self):
        from app.sync import execution_deadline, SyncInterrupted
        runtime = self.runtime()
        with execution_deadline(1), self.assertRaises(SyncInterrupted):
            self.child(runtime, 'import time; print(\'{"type":"turn.started"}\',flush=True); time.sleep(10)', timeout=5)
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(evidence['stop_cause'], 'sync_interruption')
        self.assertEqual(evidence['event_counts']['turn.started'], 1)
        self.assertEqual(runtime.last_invocation_metadata['process_code'], -9)


class PrivateTraceTests(unittest.TestCase):
    def test_permissions_refresh_correlation_privacy_and_retention(self):
        from app.codex_diagnostics import ProcessObservation, PrivateCodexCapture
        with tempfile.TemporaryDirectory() as folder:
            store = PrivateCodexCapture(Path(folder)/'state.db')
            observation = ProcessObservation(capture=store, model='gpt-6.1-sol', effort='high', cli_version='1.2.3')
            artifact_id = observation.summary()['artifact_id']
            observation.observe(b'{"type":"thread.started","thread_id":"fictional-thread"}\n', b'')
            observation.observe(b'{"type":"thread.started","thread_id":"fictional-thread"}\n{"type":"error","message":"quota SCHOOL_SENTINEL https://private.invalid"}\n', b'')
            observation.finish(0, 'normal_exit')
            directory = Path(folder)/'codex-diagnostics'
            path = directory/('codex-'+artifact_id+'.json')
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            payload = json.loads(path.read_text())
            self.assertEqual(payload['thread_id'], 'fictional-thread')
            self.assertEqual(payload['provider_request_id_status'], 'not_exposed')
            self.assertNotIn('fictional-thread', json.dumps(observation.summary()))
            self.assertNotIn('SCHOOL_SENTINEL', path.read_text())
            self.assertNotIn('https:', path.read_text())
            unrelated = directory/'unrelated.json'; unrelated.write_text('keep')
            with patch('app.codex_diagnostics.MAX_FILES', 3):
                for _ in range(5):
                    observation = ProcessObservation(capture=store)
                    observation.finish(1, 'normal_exit')
                    observation.finalize('failed')
            self.assertEqual(len(list(directory.glob('codex-*.json'))), 3)
            self.assertTrue(unrelated.exists())

    def test_timeline_bound_keeps_first_last_and_aggregates(self):
        from app.codex_diagnostics import ProcessObservation
        observation = ProcessObservation()
        output = b''
        for _ in range(400):
            output += b'{"type":"turn.started"}\n'
            observation.observe(output, b'')
        observation.finish(0, 'normal_exit')
        artifact = observation.artifact()
        self.assertLessEqual(len(json.dumps(artifact).encode()), 128*1024)
        self.assertEqual(len(artifact['timeline']), 256)
        self.assertGreater(artifact['omitted_count'], 100)
        self.assertEqual(artifact['summary']['event_counts']['turn.started'], 400)
        self.assertEqual(artifact['summary']['last_stage'], 'turn_started')

    def test_symlink_directory_parent_and_artifact_are_rejected(self):
        from app.codex_diagnostics import ProcessObservation, PrivateCodexCapture
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'actual').mkdir(); (root/'linked').symlink_to(root/'actual', target_is_directory=True)
            for base in (root/'linked', root):
                if base == root:
                    (root/'codex-diagnostics').symlink_to(root/'actual', target_is_directory=True)
                observation = ProcessObservation(capture=PrivateCodexCapture(base/'state.db'))
                self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
                self.assertFalse(list((root/'actual').iterdir()))

    def test_capture_and_optional_progress_failures_are_visible_without_masking_timeout(self):
        runtime = CodexRuntime(TEST_CONFIG)
        runtime.private_diagnostic_capture = lambda *args: (_ for _ in ()).throw(OSError('SCHOOL_SENTINEL'))
        runtime._observation_progress = lambda value: (_ for _ in ()).throw(OSError('SCHOOL_SENTINEL'))
        with self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            runtime._run_process([sys.executable,'-c','import time; time.sleep(10)'], '', .15)
        evidence = runtime.last_invocation_metadata['process_observation']
        self.assertEqual(evidence['artifact_status'], 'capture_failed')
        self.assertEqual(evidence['progress_status'], 'update_failed')
        self.assertNotIn('SCHOOL_SENTINEL', json.dumps(evidence))

    def test_file_symlink_unsafe_mode_size_and_busy_lock_fail_closed(self):
        from app.codex_diagnostics import ProcessObservation, PrivateCodexCapture
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); directory = root/'codex-diagnostics'; directory.mkdir(mode=0o700)
            destination = root/'outside'; destination.write_text('UNCHANGED')
            fixed = 'a'*32; (directory/('codex-'+fixed+'.json')).symlink_to(destination)
            with patch('app.codex_diagnostics.secrets.token_hex', return_value=fixed):
                observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
            self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
            self.assertEqual(destination.read_text(), 'UNCHANGED')
            directory.chmod(0o755)
            observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
            self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
            directory.chmod(0o700)
            with patch('app.codex_diagnostics.MAX_BYTES', 1):
                observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
            self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
            fd = os.open(directory, os.O_RDONLY|os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                started = time.monotonic()
                observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
                self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
                self.assertLess(time.monotonic()-started, .5)
            finally:
                os.close(fd)

    def test_repeated_failed_initial_reservations_stay_bounded(self):
        from app.codex_diagnostics import ProcessObservation, PrivateCodexCapture
        real_open = os.open
        def fail_temporary(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith('.codex-'):
                raise OSError('SCHOOL_SENTINEL')
            return real_open(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('app.codex_diagnostics.MAX_FILES', 2), patch('app.codex_diagnostics.os.open', side_effect=fail_temporary):
                for _ in range(7):
                    observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
                    self.assertEqual(observation.summary()['artifact_status'], 'capture_failed')
                    self.assertLessEqual(len(list((root/'codex-diagnostics').glob('codex-*.json'))), 3)

    def test_only_owned_matching_stale_temporaries_are_removed(self):
        from app.codex_diagnostics import ProcessObservation, PrivateCodexCapture
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); directory = root/'codex-diagnostics'; directory.mkdir(mode=0o700)
            stale = directory/('.codex-'+'a'*32+'.tmp'); stale.write_bytes(b'incomplete'); stale.chmod(0o600)
            public = directory/('.codex-'+'b'*32+'.tmp'); public.write_text('keep'); public.chmod(0o644)
            link = directory/('.codex-'+'c'*32+'.tmp'); link.symlink_to(public)
            unrelated = directory/'unrelated.tmp'; unrelated.write_text('keep'); unrelated.chmod(0o600)
            observation = ProcessObservation(capture=PrivateCodexCapture(root/'state.db'))
            self.assertEqual(observation.summary()['artifact_status'], 'written')
            self.assertFalse(stale.exists())
            self.assertTrue(public.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(unrelated.exists())

    def test_resource_samples_keep_numeric_history_when_later_unavailable(self):
        from app.codex_diagnostics import ProcessObservation
        observation = ProcessObservation()
        observation.process_started(os.getpid())
        samples = [entry for entry in observation.artifact()['timeline'] if entry['stage']=='resource_sample']
        self.assertIsInstance(samples[-1]['rss_bytes'], int)
        self.assertGreaterEqual(samples[-1]['cpu_seconds'], 0)
        observation.pid = -1
        observation.last_sample -= 5
        observation.sample()
        samples = [entry for entry in observation.artifact()['timeline'] if entry['stage']=='resource_sample']
        self.assertIsInstance(samples[-2]['rss_bytes'], int)
        self.assertIsNone(samples[-1]['rss_bytes'])

    def test_success_deleted_only_after_strict_validation_and_failure_burst_keeps_failures(self):
        from app.codex_diagnostics import PrivateCodexCapture
        args = dict(calendar_id=TEST_CONFIG.calendar_id, event_id='fictional')
        event = dict(type='item.completed', item=dict(type='mcp_tool_call',
            tool='google_calendar.read_event', arguments=args,
            result=dict(structured_content=dict(id='fictional'))))
        code = 'print('+repr(json.dumps(event))+')'
        with tempfile.TemporaryDirectory() as folder:
            runtime = CodexRuntime(TEST_CONFIG)
            runtime.private_diagnostic_capture = PrivateCodexCapture(Path(folder)/'state.db')
            saved = []
            runtime.invocation_sink = lambda value: saved.append(value)
            with patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c',code]):
                def invalid(value):
                    raise RuntimeFailure('calendar_result_unreadable')
                with self.assertRaises(RuntimeFailure):
                    runtime._invoke('SCHOOL_SENTINEL', 'tool.json', ('read_event',), 'read_event', args, validate=invalid)
                failed_id = saved[-1]['process_observation']['artifact_id']
                failed_path = Path(folder)/'codex-diagnostics'/('codex-'+failed_id+'.json')
                self.assertTrue(failed_path.exists())
                with patch('app.codex_diagnostics.MAX_FILES', 1):
                    for _ in range(3):
                        self.assertEqual(runtime._invoke('SCHOOL_SENTINEL', 'tool.json', ('read_event',), 'read_event', args), {'id':'fictional'})
                        summary = saved[-1]['process_observation']
                        self.assertEqual(summary['artifact_status'], 'removed_on_success')
                        self.assertNotIn('artifact_id', summary)
                        self.assertTrue(failed_path.exists())
            files = list((Path(folder)/'codex-diagnostics').glob('codex-*.json'))
            self.assertEqual(files, [failed_path])
            self.assertNotIn('SCHOOL_SENTINEL', failed_path.read_text()+json.dumps(saved))

    def test_planner_reference_validation_failure_retains_trace(self):
        from app.codex_diagnostics import PrivateCodexCapture
        plan = dict(snapshot_hash='a'*64, decision_reason='Fictional plan.', items=[])
        event = dict(type='item.completed', item=dict(type='agent_message', text=json.dumps(plan)))
        with tempfile.TemporaryDirectory() as folder:
            runtime = CodexRuntime(TEST_CONFIG)
            runtime.private_diagnostic_capture = PrivateCodexCapture(Path(folder)/'state.db')
            saved = []
            runtime.invocation_sink = lambda value: saved.append(value)
            with patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c','print('+repr(json.dumps(event))+')']), patch('app.agenda.agenda_prompt', return_value='Fictional input'), patch('app.agenda.validate_agenda_plan', side_effect=RuntimeFailure('agenda_plan_stale')):
                with self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_stale'):
                    runtime.plan_agenda({})
            self.assertEqual((saved[-1]['state'], saved[-1]['error_code']), ('failed', 'agenda_plan_stale'))
            summary = runtime.last_invocation_metadata['process_observation']
            self.assertEqual(summary['artifact_status'], 'written')
            path = Path(folder)/'codex-diagnostics'/('codex-'+summary['artifact_id']+'.json')
            self.assertEqual(json.loads(path.read_text())['invocation_state'], 'failed')

    def test_mandatory_final_audit_failure_recreates_retained_trace(self):
        from app.codex_diagnostics import PrivateCodexCapture
        args = dict(calendar_id=TEST_CONFIG.calendar_id, event_id='fictional')
        event = dict(type='item.completed', item=dict(type='mcp_tool_call',
            tool='google_calendar.read_event', arguments=args,
            result=dict(structured_content=dict(id='fictional'))))
        with tempfile.TemporaryDirectory() as folder:
            runtime = CodexRuntime(TEST_CONFIG)
            runtime.private_diagnostic_capture = PrivateCodexCapture(Path(folder)/'state.db')
            def sink(value):
                if value['state'] == 'completed':
                    raise OSError('SCHOOL_SENTINEL')
            runtime.invocation_sink = sink
            with patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c','print('+repr(json.dumps(event))+')']):
                with self.assertRaises(OSError):
                    runtime._invoke('SCHOOL_SENTINEL', 'tool.json', ('read_event',), 'read_event', args)
            summary = runtime.last_invocation_metadata['process_observation']
            self.assertEqual(summary['artifact_status'], 'written')
            path = Path(folder)/'codex-diagnostics'/('codex-'+summary['artifact_id']+'.json')
            self.assertEqual(json.loads(path.read_text())['invocation_state'], 'audit_failed')
            self.assertEqual(json.loads(path.read_text())['summary']['artifact_status'], 'written')
            self.assertNotIn('SCHOOL_SENTINEL', path.read_text())

    def test_progress_replaces_running_db_entry_and_final_audit_remains_mandatory(self):
        from app.core import AppStore
        from app.codex_diagnostics import PrivateCodexCapture
        args = dict(calendar_id=TEST_CONFIG.calendar_id, event_id='fictional')
        event = dict(type='item.completed', item=dict(type='mcp_tool_call',
            tool='google_calendar.read_event', arguments=args,
            result=dict(structured_content=dict(id='fictional'))))
        code = 'import time; print('+repr(json.dumps(dict(type='turn.started')))+',flush=True); time.sleep(.3); print('+repr(json.dumps(event))+',flush=True)'
        with tempfile.TemporaryDirectory() as folder:
            store = AppStore(Path(folder)/'state.db', config=TEST_CONFIG)
            try:
                run_id = store.start_sync_run()
                runtime = CodexRuntime(TEST_CONFIG)
                saved = []
                def sink(value):
                    saved.append(value)
                    store.record_sync_diagnostic(run_id, dict(value, phase='reconcile'))
                runtime.invocation_sink = sink
                runtime.private_diagnostic_capture = PrivateCodexCapture(store.path)
                with patch.object(runtime, '_args', return_value=[sys.executable,'-u','-c',code]), patch('app.codex_diagnostics.REFRESH_SECONDS', .05):
                    runtime._invoke('SCHOOL_SENTINEL', 'tool.json', ('read_event',), 'read_event', args)
                raw = store.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?',(run_id,)).fetchone()[0]
                diagnostics = json.loads(raw)
                self.assertEqual(len(diagnostics['invocations']), 1)
                self.assertGreater(len(saved), 3)
                self.assertEqual(diagnostics['invocations'][0]['state'], 'completed')
                self.assertNotIn('SCHOOL_SENTINEL', raw)
            finally:
                store.close()

    def test_public_summary_rejects_private_and_malformed_fields(self):
        from app.codex_diagnostics import ProcessObservation
        summary = ProcessObservation().summary()
        item = dict(phase='calendar_probe', state='running', requested_count=1, received_count=0,
                    duration_seconds=0, process_code=None, started_count=0, completed_count=0,
                    process_observation=summary)
        sync_diagnostics(dict(invocations=[item], omitted_count=0))
        for key, value in [('thread_id','SCHOOL_SENTINEL'), ('last_stage','SCHOOL_SENTINEL'),
                           ('quiet_seconds',float('nan')), ('stdout_bytes',True),
                           ('event_counts',{'SCHOOL_SENTINEL':1}), ('artifact_id','../../private')]:
            broken = copy.deepcopy(item); broken['process_observation'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                sync_diagnostics(dict(invocations=[broken], omitted_count=0))

if __name__ == '__main__':
    unittest.main()

"""Preview work has its own bounded, resumable queue after legacy Calendar work."""
from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from app.core import AppStore
from app.health_state import read_connection_state
from app.source_planner import SourcePlanner
from app.sync import SyncController, SyncInterrupted, _PreviewBudget, _PreviewBudgetExceeded
from test_source_planner import KINDS, LEARNER, action, source
from test_support import TEST_CONFIG
from test_sync import FakeClient, FakeRuntime, NOW


class SourceClient(FakeClient):
    def __init__(self, sources=(), *, count=1, delay=0, trace=None):
        super().__init__(count=count)
        self.sources, self.delay, self.trace = list(sources), delay, trace
        self.collections = 0

    def collect_sources(self, **kwargs):
        self.collections += 1
        if self.trace is not None:
            self.trace.append('collection')
        time.sleep(self.delay)
        return {'sources': self.sources, 'complete_kinds': KINDS,
                'learner': LEARNER, 'status': 'ok'}


class SourceRuntime(FakeRuntime):
    def __init__(self, *, delay=0, actions=(), trace=None):
        super().__init__()
        self.delay, self.actions, self.trace = delay, list(actions), trace
        self.source_calls, self.match_calls = [], []

    def analyze(self, message, events):
        if self.trace is not None:
            self.trace.append('legacy_analysis')
        return super().analyze(message, events)

    def execute(self, operation):
        if self.trace is not None:
            self.trace.append('legacy_write')
        return super().execute(operation)

    def analyze_source(self, src, context):
        self.source_calls.append(src['id'])
        time.sleep(self.delay)
        return json.loads(json.dumps(self.actions))

    def match_source_action(self, src, item, candidates):
        self.match_calls.append(src['id'])
        return {'decision': 'distinct'}


class SourceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.temp.name)/'private/state.sqlite3',
                              config=replace(TEST_CONFIG, multi_source_mode='preview'))
        self.store.set_writes_paused(False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def active_snapshot(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM '+table)]
                for table in ('proposals', 'operations', 'canonical_actions', 'action_sources',
                              'canonical_decisions', 'decision_intents', 'events')}

    def run_sync(self, client, runtime, *, timeout=30):
        return SyncController(self.store, client, runtime, timeout_seconds=timeout).run(current=NOW)

    def await_preview_expiry(self, entered, phase):
        # Arm the installed soft handler only at the intended blocking call.
        # SQLite fsync before/after that call gets the generous whole-run budget.
        entered.append(phase)
        signal.setitimer(signal.ITIMER_REAL, 0.02)
        time.sleep(1)
        self.fail('The real preview alarm did not interrupt its target call.')

    def test_preview_window_uses_half_remaining_time_and_ten_minute_cap(self):
        for remaining, expected in ((30, 15), (45*60, 10*60)):
            with self.subTest(remaining=remaining):
                controller=SyncController(self.store, SourceClient(count=0), SourceRuntime())
                controller._run_deadline=1000+remaining
                with patch('app.sync.time.monotonic',return_value=1000), \
                     patch('app.sync._preview_deadline',return_value=nullcontext()) as deadline, \
                     patch.object(controller,'_source_collection') as collection:
                    controller._preview_sources(NOW)
                deadline.assert_called_once_with(expected)
                budget=collection.call_args.kwargs['preview_budget']
                self.assertEqual((budget.deadline,budget.max_calls),(1000+expected,31))

    def test_slow_analysis_cannot_starve_legacy_analysis_or_write(self):
        trace = []
        entered = []
        runtime = SourceRuntime(trace=trace)
        legacy_analyze=runtime.analyze
        def slow_legacy(*args):
            # Reproduce work before preview that exceeds the old .2s test budget.
            time.sleep(0.25)
            return legacy_analyze(*args)
        runtime.analyze=slow_legacy
        runtime.analyze_source = lambda *args: self.await_preview_expiry(entered, 'analysis')
        result = self.run_sync(SourceClient([source()], trace=trace), runtime)
        self.assertEqual(entered, ['analysis'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual((result['analyzed'], result['calendar_writes']), (1, 1))
        self.assertEqual(trace[:3], ['legacy_analysis', 'legacy_write', 'collection'])
        self.assertIsNotNone(result['state']['last_successful_sync'])
        self.assertEqual(result['sources']['error_code'], 'source_preview_time_budget_exhausted')
        failed = self.store.list_source_previews(status='failed')
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['last_error'], 'source_preview_time_budget_exhausted')
        self.assertEqual(len(self.store.pending_sources(preview=True)), 1)

    def test_collection_is_bounded_and_records_visible_deferred_health(self):
        entered = []
        client = SourceClient([source()])
        client.collect_sources = lambda **kwargs: self.await_preview_expiry(entered, 'collection')
        result = self.run_sync(client, SourceRuntime())
        self.assertEqual(entered, ['collection'])
        self.assertEqual((result['status'], result['calendar_writes']), ('ok', 1))
        self.assertEqual(result['sources']['model_calls'], 0)
        self.assertEqual(self.store.source_status()['last_error'], 'source_preview_time_budget_exhausted')
        self.assertEqual(self.store.source_status()['health']['announcement']['last_result'], 'partial')
        self.assertEqual(read_connection_state(self.store.db)['sources']['kinds']['announcement']['last_error'],
                         'source_preview_time_budget_exhausted')
        self.assertEqual(self.store.pending_sources(preview=True), [])

    def test_shared_model_call_cap_keeps_ready_work_and_resumes_queue(self):
        rows = [source(str(index)) for index in range(45)] + [source('sub', kind='substitution')]
        client, runtime = SourceClient(rows, count=0), SourceRuntime()
        first = self.run_sync(client, runtime)
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['sources']['model_calls'], 31)
        self.assertEqual(first['sources']['analyzed'], 32)  # one zero-call substitution
        self.assertEqual(first['sources']['error_code'], 'source_preview_call_budget_exhausted')
        self.assertEqual(read_connection_state(self.store.db)['sources']['kinds']['announcement']['last_error'],
                         'source_preview_call_budget_exhausted')
        ready = {row['source_id'] for row in self.store.list_source_previews(status='ready')}
        self.assertEqual(len(ready), 32)
        self.assertEqual(self.store.list_source_previews(status='failed'), [])
        self.assertEqual(len(self.store.pending_sources(preview=True)), 14)
        second = self.run_sync(client, runtime)
        self.assertEqual(second['sources']['status'], 'ok')
        self.assertEqual(second['sources']['model_calls'], 14)
        self.assertEqual(self.store.pending_sources(preview=True), [])
        self.assertEqual(len(runtime.source_calls), len(set(runtime.source_calls)))
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.pending_operations(), [])

    def test_call_cap_counts_matching_and_never_commits_partial_source(self):
        rows = [source(str(index)) for index in range(18)]
        runtime = SourceRuntime(actions=[action(), action(title='Another action', start='2030-03-13')])
        with patch('app.sync._PreviewBudget', side_effect=lambda seconds, calls: _PreviewBudget(seconds, 3)):
            result = self.run_sync(SourceClient(rows, count=0), runtime)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(runtime.source_calls)+len(runtime.match_calls), 3)
        self.assertGreater(len(runtime.match_calls), 0)
        self.assertEqual(len(self.store.list_source_previews(status='failed')), 1)
        self.assertEqual(self.active_snapshot(), {table: [] for table in self.active_snapshot()})
        second = self.run_sync(SourceClient(rows, count=0), runtime)
        self.assertEqual(second['sources']['status'], 'ok')
        self.assertEqual(self.store.pending_sources(preview=True), [])

    def test_timeout_during_matching_preserves_only_complete_previews(self):
        original, changed = source('original'), source('changed')
        self.store.ingest_sources({'sources': [original, changed], 'complete_kinds': KINDS,
                                  'learner': LEARNER, 'status': 'ok'}, NOW)
        initial = next(src for src in self.store.pending_sources(preview=True) if src['upstream_id']=='original')
        SourcePlanner(self.store, SourceRuntime(actions=[action()])).analyze(initial, NOW, preview=True)
        before = self.active_snapshot()
        entered = []
        runtime = SourceRuntime(actions=[action()])
        runtime.match_source_action = lambda *args: self.await_preview_expiry(entered, 'matching')
        fail_source=self.store.fail_source
        def slow_failure_record(*args,**kwargs):
            # Slow persistence after soft expiry must not consume a tiny outer
            # test deadline and disguise a successfully contained preview.
            time.sleep(0.25)
            return fail_source(*args,**kwargs)
        with patch.object(self.store,'fail_source',side_effect=slow_failure_record):
            result = self.run_sync(SourceClient([original, changed], count=0), runtime)
        self.assertEqual(entered, ['matching'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.active_snapshot(), before)
        self.assertEqual(len(self.store.list_source_previews(status='ready')), 1)
        failed = self.store.list_source_previews(status='failed')
        self.assertEqual(len(failed), 1)
        from app.source_preview import SOURCE_PREVIEW_PLANNER_VERSION
        self.assertEqual(json.loads(failed[0]['payload']), {'planner_version': SOURCE_PREVIEW_PLANNER_VERSION})
        self.assertEqual(result['sources']['model_calls'], 2)

    def test_soft_expiry_immediately_after_commit_keeps_ready_preview(self):
        analyze=SourcePlanner.analyze
        def committed_then_expired(planner,*args,**kwargs):
            analyze(planner,*args,**kwargs)
            raise _PreviewBudgetExceeded('source_preview_time_budget_exhausted')
        with patch.object(SourcePlanner,'analyze',committed_then_expired):
            result=self.run_sync(SourceClient([source()],count=0),SourceRuntime())
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['sources']['failures'],0)
        self.assertEqual(len(self.store.list_source_previews(status='ready')),1)
        self.assertEqual(self.store.list_source_previews(status='failed'),[])
        self.assertEqual(self.store.pending_sources(preview=True),[])
        self.assertEqual(self.active_snapshot(),{table:[] for table in self.active_snapshot()})

    def test_sigterm_during_preview_remains_whole_run_interruption(self):
        runtime = SourceRuntime()
        entered = []
        def terminated(*args):
            entered.append('analysis')
            os.kill(os.getpid(), signal.SIGTERM)
        runtime.analyze_source = terminated
        result = self.run_sync(SourceClient([source()],delay=0.25), runtime)
        self.assertEqual(entered, ['analysis'])
        self.assertEqual((result['status'], result['error_code']), ('interrupted', 'sync_interrupted'))
        self.assertEqual(len(runtime.executed), 1)
        self.assertIsNone(result['state']['last_successful_sync'])
        self.assertEqual(self.store.list_source_previews(), [])

    def test_outer_timeout_still_wins_during_soft_timeout_cleanup(self):
        runtime = SourceRuntime()
        entered = []
        def slow_cleanup(*args):
            try:
                self.await_preview_expiry(entered, 'analysis')
            except _PreviewBudgetExceeded:
                entered.append('soft_cleanup')
                # The nested handler must re-arm the original real outer timer.
                time.sleep(10)
                raise
        runtime.analyze_source = slow_cleanup
        result = self.run_sync(SourceClient([source()]), runtime, timeout=5)
        self.assertEqual(entered, ['analysis', 'soft_cleanup'])
        self.assertEqual((result['status'], result['error_code']), ('interrupted', 'sync_timeout'))
        self.assertIsNone(result['state']['last_successful_sync'])
        self.assertEqual(len(runtime.executed), 1)

    def test_explicit_global_interruption_is_never_softened(self):
        runtime = SourceRuntime()
        entered = []
        def interrupted(*args):
            entered.append('analysis')
            raise SyncInterrupted('sync_timeout')
        runtime.analyze_source = interrupted
        result = self.run_sync(SourceClient([source()]), runtime)
        self.assertEqual(entered, ['analysis'])
        self.assertEqual((result['status'], result['error_code']), ('interrupted', 'sync_timeout'))
        self.assertEqual(self.store.list_source_previews(), [])

    def test_background_thread_defers_preview_without_calling_blocking_work(self):
        client, runtime = SourceClient([source()]), SourceRuntime()
        with patch('app.sync.threading.current_thread', return_value=object()):
            result = self.run_sync(client, runtime)
        self.assertEqual((result['status'], result['calendar_writes']), ('ok', 1))
        self.assertEqual(client.collections, 0)
        self.assertEqual(runtime.source_calls, [])
        self.assertEqual(result['sources']['error_code'], 'source_preview_worker_required')
        self.assertEqual(read_connection_state(self.store.db)['sources']['kinds']['announcement']['last_error'],
                         'source_preview_worker_required')

    def test_active_collection_and_analysis_still_gate_full_success(self):
        self.store.config = replace(self.store.config, multi_source_mode='active')
        self.store.set_setting('source_activation_at', 'synthetic')
        runtime = SourceRuntime()
        entered = []
        def active_timeout(*args):
            entered.append('active_analysis')
            signal.setitimer(signal.ITIMER_REAL, 0.02)
            time.sleep(1)
            self.fail('The real whole-run alarm did not interrupt active analysis.')
        runtime.analyze_source = active_timeout
        result = self.run_sync(SourceClient([source()]), runtime)
        self.assertEqual(entered, ['active_analysis'])
        self.assertEqual((result['status'], result['error_code']), ('interrupted', 'sync_timeout'))
        self.assertEqual(runtime.analyzed, [])
        self.assertEqual(runtime.executed, [])
        self.assertIsNone(result['state']['last_successful_sync'])


if __name__ == '__main__':
    unittest.main()

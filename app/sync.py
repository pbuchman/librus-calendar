"""Bounded inbox reconciliation, durable analysis queue, and guarded execution."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import fcntl
import os
import signal
import threading
import time
from pathlib import Path
from .core import AppStore, WARSAW, analysis_metadata, as_datetime, utcnow
from .health_state import ERROR_CODES, error_code
from .source_preview import current_preview_sql


def safe_error(error):
    # Errors may contain URLs or private API bodies: record type only.
    return type(error).__name__ + ': operation failed; private response omitted.'


class SyncInterrupted(BaseException):
    def __init__(self, code):
        self.code = code


class _PreviewBudgetExceeded(BaseException):
    """Unwind read-only preview work without disguising a whole-run signal."""
    def __init__(self, code):
        self.code = code


@contextmanager
def _preview_deadline(seconds):
    """A nested soft alarm preserves the earlier whole-run alarm on unwind."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    outer_end = started + previous_timer[0] if previous_timer[0] else None

    def restore_outer():
        signal.signal(signal.SIGALRM, previous_handler)
        remaining = max(0.000001, outer_end - time.monotonic()) if outer_end else 0
        signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])

    def expired(signum, frame):
        # Re-arm before raising: child-process cleanup is also subject to the
        # whole-run timeout, and must never turn that timeout into soft expiry.
        outer_expired = outer_end is not None and time.monotonic() >= outer_end
        restore_outer()
        if outer_expired:
            raise SyncInterrupted('sync_timeout')
        raise _PreviewBudgetExceeded('source_preview_time_budget_exhausted')

    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0]) if previous_timer[0] else seconds)
        yield
    finally:
        restore_outer()


class _PreviewBudget:
    def __init__(self, seconds, max_calls):
        self.deadline = time.monotonic() + seconds
        self.max_calls, self.calls = max_calls, 0
        self.source, self.source_started = None, False

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _PreviewBudgetExceeded('source_preview_time_budget_exhausted')

    def call(self, operation, *args, **kwargs):
        self.check()
        if self.calls >= self.max_calls:
            raise _PreviewBudgetExceeded('source_preview_call_budget_exhausted')
        self.calls += 1
        self.source_started = True
        result = operation(*args, **kwargs)
        self.check()
        return result


class _PreviewRuntime:
    """Share the extraction/matching/verification budget and forward metadata."""
    def __init__(self, runtime, budget):
        object.__setattr__(self, '_runtime', runtime)
        object.__setattr__(self, '_budget', budget)

    def __getattr__(self, name):
        operation = getattr(self._runtime, name)
        if name in ('analyze_source', 'match_source_action', 'verify_source_action','recover_source_action_evidence'):
            return lambda *args, **kwargs: self._budget.call(operation, *args, **kwargs)
        return operation

    def __setattr__(self, name, value):
        setattr(self._runtime, name, value)

    def can_verify_source_action(self,reserve_calls=0):
        self._budget.check()
        return self._budget.calls+1+max(0,int(reserve_calls))<=self._budget.max_calls

    def can_recover_source_action_evidence(self,reserve_calls=0):
        self._budget.check()
        return self._budget.calls+2+max(0,int(reserve_calls))<=self._budget.max_calls

    def reserve_source_plan(self,action_count):
        self._budget.check()
        # Small sources fit a fresh run even if every action needs both checks.
        # Do not cache them permanently unverified due only to queue position.
        needed=(3 if hasattr(self._runtime,'recover_source_action_evidence')else 2)*max(0,int(action_count))
        if 1+needed<=self._budget.max_calls and self._budget.calls+needed>self._budget.max_calls:
            raise _PreviewBudgetExceeded('source_preview_call_budget_exhausted')


@contextmanager
def execution_deadline(seconds):
    """CLI workers record systemd termination and a whole-run 45 minute timeout."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def interrupted(signum, frame):
        raise SyncInterrupted('sync_timeout' if signum == signal.SIGALRM else 'sync_interrupted')
    try:
        for sig in previous:
            signal.signal(sig, interrupted)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


@contextmanager
def sync_lock(state_path):
    """The same nonblocking lock protects sync and explicit cached reanalysis."""
    fd = os.open(Path(state_path).parent/'sync.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def reanalyze_cached(state_path, message_ids, *, apply=False, runtime_factory=None, current=None, config=None):
    """CLI entry: lock before opening/migrating state or initializing a runtime."""
    state_path = Path(state_path).expanduser()
    with sync_lock(state_path) as acquired:
        if not acquired:
            return {'status': 'already_running', 'calendar_writes': 0}
        if not state_path.is_file():
            raise FileNotFoundError('Cached state is required.')
        store = AppStore(state_path, config=config)
        try:
            return SyncController(store, runtime_factory=runtime_factory)._reanalyze_locked(message_ids, apply=apply, current=current)
        finally:
            store.close()


class SyncController:
    def __init__(self, store, client=None, runtime=None, max_messages=100, history_days=30,
                 *, client_factory=None, runtime_factory=None, timeout_seconds=45*60):
        self.store, self.client, self.runtime = store, client, runtime
        self.client_factory, self.runtime_factory = client_factory, runtime_factory
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 45*60)
        self.max_messages = min(max(int(max_messages), 1), 100)
        self.history_days = min(max(int(history_days), 1), 30)

    def _stage(self, stage):
        self.stage = stage
        self.store.sync_stage(self.run_id, stage)

    def _in_window(self, value, cutoff):
        if not value:
            return True
        try:
            return as_datetime(value) >= cutoff
        except (ValueError, TypeError):
            # Unknown dates must not silently disappear; analysis will resolve ambiguity.
            return True

    def _analyze(self, message):
        # Do not let metadata left by an earlier successful call label this call.
        self.runtime.last_analysis_metadata = None
        proposals = self.runtime.analyze(message, self.store.list_events(500))
        if not isinstance(proposals, list) or len(proposals) > 30:
            raise ValueError('Unexpected analysis result shape.')
        metadata = analysis_metadata(getattr(self.runtime, 'last_analysis_metadata', None), len(proposals))
        return proposals, metadata

    def _source_collection(self,current,*,preview_budget=None,result=None):
        """Supplement existing message discovery without widening its credentials."""
        mode=self.store.config.multi_source_mode
        if mode=='off':return {'mode':'off','status':'off','analyzed':0,'failures':0}
        result=result if result is not None else {'mode':mode,'status':'ok','analyzed':0,'failures':0}
        if mode=='active'and self.store._setting('source_activation_incomplete',''):
            result.update(status='partial',activation_required=True,activation_incomplete=True)
            return result
        try:
            batch=self.client.collect_sources(current=current,past_days=30,future_days=60)
            result.update(self.store.ingest_sources(batch,current))
        except Exception:
            from .source_store import KINDS
            from .core import utcnow
            stamp=utcnow()
            with self.store.lock,self.store.db:
                for kind in KINDS-{'message'}:
                    self.store.db.execute('INSERT INTO source_health VALUES (?,?,NULL,?,?,0) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_result=excluded.last_result,last_error=excluded.last_error,learner_verified=0',
                        (kind,stamp,'failed','source_collection_failed'))
                for key,value in (('source_last_collection_at',stamp),('source_last_collection_result','failed'),('source_last_error','source_collection_failed'),('source_learner_verified','false')):
                    self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,value))
            result.update(status='partial',failures=1)
            return result
        from .source_planner import SourcePlanner
        planner=SourcePlanner(self.store,_PreviewRuntime(self.runtime,preview_budget) if preview_budget else self.runtime)
        if mode=='active' and not self.store._setting('source_activation_at'):
            result.update(status='partial',activation_required=True)
            return result
        for source in self.store.pending_sources(self.max_messages,preview=mode=='preview'):
            if preview_budget:
                preview_budget.check()
                # Informational substitutions need no model call. Leave other
                # unstarted sources pending when the shared call budget is full.
                if source['kind']!='substitution' and preview_budget.calls>=preview_budget.max_calls:
                    result.update(status='partial',error_code='source_preview_call_budget_exhausted')
                    continue
                preview_budget.source,preview_budget.source_started=source,False
            try:
                planner.analyze(source,current,preview=mode=='preview')
                result['analyzed']+=1
            except Exception:
                self.store.fail_source(source['id'],preview=mode=='preview')
                result['failures']+=1
            if preview_budget:
                preview_budget.source=None
        if result['failures'] or (mode=='active' and self.store.pending_sources(1)):
            result['status']='partial'
        if preview_budget and self.store.pending_sources(1,preview=True):
            result['status']='partial'
        return result

    def _record_preview_deferred(self,code):
        """Persist technical evidence without changing active queues or health."""
        from .source_store import KINDS
        stamp=utcnow()
        with self.store.lock,self.store.db:
            for kind in KINDS-{'message'}:
                self.store.db.execute('INSERT INTO source_health VALUES (?,?,NULL,?,?,?) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_result=excluded.last_result,last_error=excluded.last_error',
                    (kind,stamp,'partial',code,int(self.store._setting('source_learner_verified')=='true')))
            for key,value in (('source_last_collection_result','partial'),('source_last_error',code)):
                self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,value))

    def _preview_sources(self,current):
        result={'mode':'preview','status':'ok','analyzed':0,'failures':0}
        # Python alarms can preempt blocking collectors/model calls only on the
        # main thread. Other callers leave preview queued for the CLI worker.
        if threading.current_thread() is not threading.main_thread():
            result.update(status='partial',error_code='source_preview_worker_required')
            self._record_preview_deferred(result['error_code'])
            return result
        remaining=self._run_deadline-time.monotonic()
        if remaining<=0:
            raise SyncInterrupted('sync_timeout')
        seconds=min(10*60,remaining/2)
        budget=_PreviewBudget(seconds,31)
        try:
            with _preview_deadline(seconds):
                self._source_collection(current,preview_budget=budget,result=result)
        except _PreviewBudgetExceeded as error:
            if budget.source is not None and budget.source_started:
                # The alarm can arrive after the planner's transaction commits
                # but before control returns here. Never downgrade durable work.
                with self.store.lock:
                    completed=self.store.db.execute(f"SELECT 1 FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE s.id=? AND s.content_hash=? AND p.status='ready' AND s.deleted=0 AND ({current_preview_sql()})",
                        (budget.source['id'],budget.source['content_hash'])).fetchone()
                    if not completed:
                        self.store.fail_source(budget.source['id'],code=error.code,preview=True)
                        result['failures']+=1
            result.update(status='partial',error_code=error.code)
        if result.get('error_code'):
            self._record_preview_deferred(result['error_code'])
        result['model_calls']=budget.calls
        return result

    def reanalyze(self, message_ids, *, apply=False, current=None):
        """No discovery, login, probe, queue, recovery or calendar execution."""
        with sync_lock(self.store.path) as acquired:
            if not acquired:
                return {'status': 'already_running', 'calendar_writes': 0}
            return self._reanalyze_locked(message_ids, apply=apply, current=current)

    def _reanalyze_locked(self, message_ids, *, apply=False, current=None):
        current = current or datetime.now(WARSAW)
        ids = list(dict.fromkeys(str(value).strip() for value in message_ids))
        if not ids or any(not value for value in ids) or len(ids) > 30:
            raise ValueError('Select between one and thirty explicit cached message IDs.')
        result = {'status': 'applied' if apply else 'preview', 'apply': bool(apply), 'calendar_writes': 0, 'messages': []}
        if self.store.config.multi_source_mode=='active':
            result.update(status='refused',messages=[{'message_id':mid,'status':'refused','reason':'Active multi-source mode requires canonical source analysis; cached legacy reanalysis is disabled.'} for mid in ids])
            return result
        # Validate every selected source before initializing external analysis. A
        # protected target makes the whole explicit request a refusal.
        scopes = {}
        for message_id in ids:
            try:
                scopes[message_id] = self.store.cached_reanalysis_scope(message_id)
            except ValueError as error:
                result['messages'].append({'message_id': message_id, 'status': 'refused', 'reason': str(error)})
        if result['messages']:
            result['status'] = 'refused'
            return result
        if self.runtime_factory is not None:
            self.runtime = self.runtime_factory()
        for message_id in ids:
            try:
                with execution_deadline(self.timeout_seconds):
                    proposals, metadata = self._analyze(self.store.get_message(message_id))
                prepared = self.store.prepare_cached_reanalysis(message_id, proposals, current)
                superseded = []
                if apply:
                    applied = self.store.replace_cached_proposals(message_id, proposals, current, metadata,
                                                                  expected_source_hash=scopes[message_id]['source_hash'])
                    prepared, superseded = applied['proposals'], applied['superseded_ids']
                fields = ('id', 'kind', 'temporal_kind', 'activity_scope', 'title', 'start', 'end', 'due_at', 'all_day', 'status', 'review_reason')
                result['messages'].append({'message_id': message_id, 'status': 'applied' if apply else 'preview',
                                           'analysis_metadata': metadata, 'proposal_count': len(prepared),
                                           'proposals': [{key: item.get(key) for key in fields} for item in prepared],
                                           'superseded_ids': superseded})
            except SyncInterrupted as error:
                result['status'] = 'interrupted'
                result['messages'].append({'message_id': message_id, 'status': 'interrupted', 'error_code': error.code})
                break
            except Exception as error:
                result['status'] = 'partial'
                result['messages'].append({'message_id': message_id, 'status': 'failed', 'error_code': error_code(error, 'analysis')})
        return result

    def run(self, dry_run=False, current=None):
        current = current or datetime.now(WARSAW)
        lock_path = self.store.path.parent/'sync.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'already_running'}
            self.run_id = self.store.start_sync_run()
            self.stage = 'starting'
            started = time.monotonic()
            self._run_deadline = started + self.timeout_seconds
            result = {}
            try:
                with execution_deadline(self.timeout_seconds):
                    self.store.recover_interrupted()
                    if self.client_factory is not None:
                        self._stage('credentials')
                        self.client = self.client_factory(self._stage)
                    if self.runtime_factory is not None:
                        self._stage('runtime')
                        self.runtime = self.runtime_factory()
                    self._stage('calendar_probe')
                    self.runtime.probe_attempt_sink = lambda attempt: self.store.record_probe_attempt(self.run_id, attempt)
                    probe = self.runtime.probe()
                    if not isinstance(probe, dict) or probe.get('calendar_id') != self.store.calendar_id or probe.get('access_role') not in ('owner', 'writer'):
                        raise ValueError('Calendar write access was not verified.')
                    result = self._run(dry_run, current)
                    from .data_admin import prune_state
                    result['retention'] = prune_state(self.store, days=self.history_days, apply=True, current=current)
            except SyncInterrupted as error:
                result = {'status': 'interrupted', 'error_code': error.code}
                self.store.recover_interrupted()
            except (KeyboardInterrupt, SystemExit):
                result = {'status': 'interrupted', 'error_code': 'sync_interrupted'}
                self.store.recover_interrupted()
            except Exception as error:
                result = {'status': 'failed', 'error_code': error_code(error, self.stage)}
                self.store.recover_interrupted()
            if result.get('error_stage'):
                self._stage(result.pop('error_stage'))
            elif result['status'] in ('ok', 'paused', 'dry_run'):
                self._stage('complete')
            self.store.finish_sync_run(self.run_id, result['status'], result.get('error_code'), time.monotonic() - started)
            result['run_id'] = self.run_id
            result['state'] = self.store.status()
            return result
        finally:
            os.close(fd)

    def _run(self, dry_run, current):
        result = {'status': 'ok', 'listed': 0, 'fetched': 0, 'changed': 0, 'fetch_failures': 0,
                  'analyzed': 0, 'analysis_failures': 0, 'operations_applied': 0, 'operations_review': 0,
                  'operations_unknown': 0, 'dry_run': bool(dry_run), 'calendar_writes': 0}
        cutoff = current - timedelta(days=self.history_days)
        self._stage('discovery')
        # Retries are durable independently of pagination and therefore survive moving pages.
        selected = list(self.store.retry_message_ids(min(20, self.max_messages)))
        seen = set(selected)
        snippets_by_id = {}
        page_size = min(10, self.max_messages)
        stored_size = self.store._setting('discovery_page_size')
        if stored_size != str(page_size):
            # A page cursor is meaningful only with the page size that produced it.
            self.store.set_setting('discovery_page', '2')
            self.store.set_setting('discovery_page_size', str(page_size))
        resume = int(self.store._setting('discovery_page', '2'))
        page = 1
        pages_seen = set()
        finished = False
        consumed = 0
        snippets = []
        list_failed = False
        while len(selected) < self.max_messages:
            if page in pages_seen:
                break
            pages_seen.add(page)
            last_page = page
            try:
                snippets, total = self.client.messages(limit=page_size, page=page)
            except Exception as error:
                self.store.set_setting('last_error', safe_error(error))
                result.update(status='partial', error_code=error_code(error, 'discovery'), error_stage='discovery')
                list_failed = True
                break
            result['listed'] += len(snippets)
            if not snippets:
                finished = True
                break
            in_window = 0
            consumed = 0
            for snippet in snippets:
                consumed += 1
                if not self._in_window(snippet.get('sendDate'), cutoff):
                    continue
                in_window += 1
                try:
                    message_id = self.store.note_discovered(snippet)
                except ValueError:
                    result['fetch_failures'] += 1
                    result.update(error_code='fetch_failed', error_stage='discovery')
                    continue
                snippets_by_id[message_id] = snippet
                if message_id not in seen:
                    selected.append(message_id)
                    seen.add(message_id)
                if len(selected) >= self.max_messages:
                    break
            if in_window == 0 or len(snippets) < page_size or (isinstance(total, int) and page * page_size >= total):
                finished = True
                break
            page = max(2, resume) if page == 1 else page + 1
        if not list_failed and pages_seen:
            next_page = last_page if consumed < len(snippets) else last_page + 1
            self.store.set_setting('discovery_page', '2' if finished else str(max(2, next_page)))
            if finished:
                self.store.set_setting('discovery_initialized_at', utcnow())
        self._stage('fetch')
        unread_probe = None
        for message_id in selected[:self.max_messages]:
            try:
                data = self.client.message(message_id)
                changed = self.store.save_message(data)
                result['fetched'] += 1
                result['changed'] += int(changed)
                snippet = snippets_by_id.get(message_id)
                if unread_probe is None and snippet is not None and not snippet.get('readDate'):
                    unread_probe = {'message_id': message_id, 'before_read_date': snippet.get('readDate'), 'detail_read_date': data.get('read_date_returned')}
            except Exception as error:
                self.store.fail_message(message_id, 'fetch', safe_error(error))
                result['fetch_failures'] += 1
                result.update(error_code=error_code(error, 'fetch'), error_stage='fetch')
        if unread_probe:
            # Observe the normal read's effect without changing the server read state.
            try:
                after = self.client.message(unread_probe['message_id'])
                unread_probe['after_read_date'] = after.get('read_date_returned')
                unread_probe['observation'] = 'unread_before_read_after' if unread_probe['after_read_date'] else 'no_read_date_observed'
                self.store.set_setting('read_marking_observation', __import__('json').dumps(unread_probe))
                result['read_marking_observation'] = unread_probe['observation']
            except Exception:
                result['read_marking_observation'] = 'unverified_after_fetch_error'
        else:
            result['read_marking_observation'] = 'unverified_no_unread_message_observed'
        stamp=utcnow()
        message_fetch_complete=(not list_failed and not result['fetch_failures'] and
            (finished or (self.store._setting('discovery_initialized_at') and result['changed']==0)))
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO source_health VALUES (?,?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_successful_fetch=COALESCE(excluded.last_successful_fetch,source_health.last_successful_fetch),last_result=excluded.last_result,last_error=excluded.last_error,learner_verified=excluded.learner_verified',
                ('message',stamp,stamp if message_fetch_complete else None,'ok' if message_fetch_complete else 'partial',None if message_fetch_complete else 'source_collection_incomplete',int(self.store._setting('source_learner_verified')=='true')))
        self._stage('analysis')
        if self.store.config.multi_source_mode!='preview':
            result['sources']=self._source_collection(current)
        for message in self.store.analysis_messages(self.max_messages):
            if self.store.config.multi_source_mode=='active'and self.store._setting('source_activation_incomplete',''):
                # Never route a failed canonical activation through the legacy
                # analyzer. Leave its pending analysis and human intent intact.
                result['sources'].update(status='partial',activation_required=True,activation_incomplete=True)
                continue
            try:
                if self.store.config.multi_source_mode=='active' and self.store._setting('source_activation_at'):
                    from .source_planner import SourcePlanner
                    if self.store._setting('source_learner_verified')!='true':
                        raise ValueError('Selected learner context is unverified.')
                    source=self.store.get_source('message:'+message['id'])
                    SourcePlanner(self.store,self.runtime).analyze(source,current)
                else:
                    proposals, metadata = self._analyze(message)
                    self.store.save_proposals(message['id'], proposals, current, metadata)
                result['analyzed'] += 1
            except Exception as error:
                self.store.fail_message(message['id'], 'analysis', safe_error(error))
                result['analysis_failures'] += 1
                result.update(error_code=error_code(error, 'analysis'), error_stage='analysis')
        self._stage('queue')
        self.store.queue_operations(current)
        if not dry_run:
            # Unknown outcomes can only be read/reconciled, even if writes are paused.
            self._stage('reconcile')
            for operation in self.store.reconcile_operations(self.max_messages):
                try:
                    outcome = self.runtime.execute(operation)
                except Exception:
                    outcome = {'status': 'unknown', 'error': 'Reconciliation failed; no new write permitted.'}
                self.store.record_result(operation['operation_id'], outcome)
                if isinstance(outcome, dict) and outcome.get('status') in ('failed', 'unknown') and outcome.get('error') in ERROR_CODES:
                    result.update(error_code=outcome['error'], error_stage='reconcile')
            if not self.store.status()['writes_paused']:
                self._stage('write')
                for operation in self.store.pending_operations(self.max_messages):
                    if operation.get('action') == 'cancel' and (operation.get('prior_snapshot') or {}).get('_content_pruned'):
                        from .codex_runtime import event_fingerprint
                        try:
                            snapshot = self.runtime.read_event(operation['event_id'], operation['prior_snapshot'])
                            if event_fingerprint(snapshot, self.store.config.calendar_owner) != operation.get('expected_fingerprint'):
                                self.store.record_result(operation['operation_id'], {'status': 'review', 'error': 'manual_edit_conflict',
                                    'event_id': snapshot.get('id'), 'snapshot': snapshot,
                                    'fingerprint': event_fingerprint(snapshot, self.store.config.calendar_owner)})
                                result['operations_review'] += 1
                                continue
                            operation = self.store.hydrate_cancel_operation(operation['operation_id'], snapshot)
                        except Exception:
                            self.store.record_result(operation['operation_id'], {'status': 'failed', 'error': 'Cancellation baseline could not be verified; no write attempted.'})
                            continue
                    if not self.store.claim_operation(operation['operation_id'], expected_operation=operation):
                        continue
                    try:
                        outcome = self.runtime.execute(operation)
                    except Exception:
                        outcome = {'status': 'unknown', 'error': 'Execution interrupted; reconcile before retry.'}
                    self.store.record_result(operation['operation_id'], outcome)
                    status = outcome.get('status') if isinstance(outcome, dict) else 'unknown'
                    if isinstance(outcome, dict) and status in ('failed', 'unknown') and outcome.get('error') in ERROR_CODES:
                        result.update(error_code=outcome['error'], error_stage='write')
                    if status in ('applied','review','unknown'):
                        result['operations_' + status] += 1
                    result['calendar_writes'] += int(status == 'applied' and not outcome.get('reconciled', False))
        if self.store.config.multi_source_mode=='preview':
            result['sources']=self._preview_sources(current)
        if self.store.config.multi_source_mode=='active' and result['sources']['status']!='ok':
            result.update(status='partial',error_code='analysis_failed',error_stage='analysis')
        if result['fetch_failures'] or result['analysis_failures']:
            result['status'] = 'partial'
        state = self.store.status()
        discovery_incomplete = not finished and (not self.store._setting('discovery_initialized_at') or result['changed'] > 0)
        if result['status'] == 'ok' and (state['messages_pending'] or discovery_incomplete):
            result.update(status='partial', error_code='messages_pending' if state['messages_pending'] else 'discovery_incomplete',
                          error_stage='analysis' if state['messages_pending'] else 'discovery')
        if not dry_run and state['operations_unknown']:
            result.update(status='partial', error_code=result.get('error_code', 'operations_unknown'), error_stage=result.get('error_stage', 'reconcile'))
        if result['status'] == 'ok':
            if dry_run:
                result['status'] = 'dry_run'
            elif state['writes_paused']:
                result['status'] = 'paused'
            elif state['operations_pending']:
                result.update(status='partial', error_code=result.get('error_code', 'operations_pending'), error_stage='write')
        return result

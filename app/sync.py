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


def safe_error(error):
    # Errors may contain URLs or private API bodies: record type only.
    return type(error).__name__ + ': operation failed; private response omitted.'


class SyncInterrupted(BaseException):
    def __init__(self, code):
        self.code = code


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

    def _source_collection(self, current):
        """Collect only; all three channels feed the one agenda task."""
        batch = None
        try:
            batch = self.client.collect_sources(current=current, past_days=30, future_days=60)
            return self.store.ingest_sources(batch, current)
        except Exception as error:
            stamp = utcnow()
            from .source_store import KINDS
            from .health_state import SOURCE_ERROR_CODES
            failures = batch.get('kind_errors', {}) if isinstance(batch, dict) else {}
            code = getattr(error, 'code', None)
            fallback = code if isinstance(code, str) and code in SOURCE_ERROR_CODES else 'source_collection_failed'
            safe = {kind: value for kind, value in failures.items()
                    if kind in KINDS and isinstance(value, str) and value in SOURCE_ERROR_CODES} if isinstance(failures, dict) else {}
            with self.store.lock, self.store.db:
                for kind in KINDS - {'message'}:
                    self.store.db.execute('INSERT INTO source_health VALUES (?,?,NULL,?,?,0) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_result=excluded.last_result,last_error=excluded.last_error,learner_verified=0',
                        (kind, stamp, 'failed', safe.get(kind, fallback)))
                for key, value in (('source_last_collection_at', stamp), ('source_last_collection_result', 'failed'), ('source_last_error', next(iter(safe.values()), fallback))):
                    self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
            return {'status': 'partial', 'failures': 1}

    def reanalyze(self, message_ids, *, apply=False, current=None):
        return self._reanalyze_locked(message_ids, apply=apply, current=current)

    def _reanalyze_locked(self, message_ids, *, apply=False, current=None):
        return {'status': 'refused', 'calendar_writes': 0,
                'error_code': 'agenda_whole_snapshot_required',
                'messages': [{'message_id': str(mid), 'status': 'refused',
                              'reason': 'Use the complete agenda preview; cached subset analysis is retired.'} for mid in message_ids]}

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
                    if not dry_run:
                        self.store.recover_interrupted()
                    if not dry_run and self.client_factory is not None:
                        self._stage('credentials')
                        self.client = self.client_factory(self._stage)
                    if self.runtime_factory is not None:
                        self._stage('runtime')
                        self.runtime = self.runtime_factory()
                    self.runtime.invocation_sink = lambda item: self.store.record_sync_diagnostic(self.run_id, dict(item, phase=self.stage))
                    self._stage('calendar_probe')
                    self.runtime.probe_attempt_sink = lambda attempt: self.store.record_probe_attempt(self.run_id, attempt)
                    probe = self.runtime.probe()
                    if not isinstance(probe, dict) or probe.get('calendar_id') != self.store.calendar_id or probe.get('access_role') not in ('owner', 'writer'):
                        raise ValueError('Calendar write access was not verified.')
                    result = self._run(dry_run, current)
                    from .data_admin import prune_state
                    if not dry_run:
                        result['retention'] = prune_state(self.store, days=self.history_days, apply=True, current=current)
            except SyncInterrupted as error:
                result = {'status': 'interrupted', 'error_code': error.code}
                if not dry_run:
                    self.store.recover_interrupted()
            except (KeyboardInterrupt, SystemExit):
                result = {'status': 'interrupted', 'error_code': 'sync_interrupted'}
                if not dry_run:
                    self.store.recover_interrupted()
            except Exception as error:
                result = {'status': 'failed', 'error_code': error_code(error, self.stage)}
                if not dry_run:
                    self.store.recover_interrupted()
            if result.get('error_stage'):
                self._stage(result.pop('error_stage'))
            elif result['status'] in ('ok', 'paused', 'dry_run'):
                self._stage('complete')
            self.store.finish_sync_run(self.run_id, result['status'], result.get('error_code'), time.monotonic() - started)
            result['run_id'] = self.run_id
            result['state'] = self.store.status()
            result['state'].pop('calendar_id', None)
            return result
        finally:
            os.close(fd)

    def _run(self, dry_run, current):
        result = {'status': 'ok', 'listed': 0, 'fetched': 0, 'changed': 0, 'fetch_failures': 0,
                  'analyzed': 0, 'analysis_failures': 0, 'operations_applied': 0, 'operations_review': 0,
                  'operations_unknown': 0, 'dry_run': bool(dry_run), 'calendar_writes': 0}
        from .agenda import AgendaPlanner, read_calendar, agenda_summary
        planner = AgendaPlanner(self.store, self.runtime)
        if dry_run:
            result.update(status='dry_run', agenda=agenda_summary(planner.run(current, preview=True)))
            return result
        # Unknown writes are resolved read-only BEFORE collecting/planning. A
        # recovered own success may advance the existing baseline safely.
        self._stage('reconcile')
        calendar = read_calendar(self.store, self.runtime)
        for operation in self.store.reconcile_operations(self.max_messages):
            try:
                outcome = self.runtime.execute(operation)
            except Exception:
                outcome = {'status': 'unknown', 'error': 'Reconciliation failed; no new write permitted.'}
            self.store.record_result(operation['operation_id'], outcome, agenda_calendar=calendar, current=current)
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
        if not message_fetch_complete and not result.get('error_code'):
            result.update(error_code='discovery_incomplete', error_stage='discovery')
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO source_health VALUES (?,?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_successful_fetch=COALESCE(excluded.last_successful_fetch,source_health.last_successful_fetch),last_result=excluded.last_result,last_error=excluded.last_error,learner_verified=excluded.learner_verified',
                ('message',stamp,stamp if message_fetch_complete else None,'ok' if message_fetch_complete else 'partial',None if message_fetch_complete else 'source_collection_incomplete',int(self.store._setting('source_learner_verified')=='true')))
        self._stage('analysis')
        result['sources'] = self._source_collection(current)
        try:
            result['agenda'] = agenda_summary(planner.run(current))
            result['analyzed'] = int(result['agenda']['model_calls'])
        except Exception as error:
            result.update(status='partial', analysis_failures=1,
                          error_code=result.get('error_code') or error_code(error, 'analysis'),
                          error_stage=result.get('error_stage') or 'analysis')
            return result
        self._stage('queue')
        self.store.queue_operations(current)
        if not dry_run:
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
                    self.store.record_result(operation['operation_id'], outcome, agenda_calendar=planner.calendar, current=current)
                    status = outcome.get('status') if isinstance(outcome, dict) else 'unknown'
                    if isinstance(outcome, dict) and status in ('failed', 'unknown') and outcome.get('error') in ERROR_CODES:
                        result.update(error_code=outcome['error'], error_stage='write')
                    if status in ('applied','review','unknown'):
                        result['operations_' + status] += 1
                    result['calendar_writes'] += int(status == 'applied' and not outcome.get('reconciled', False))
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

"""Bounded inbox reconciliation, durable analysis queue, and guarded execution."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import fcntl
import os
import signal
import threading
import time
from pathlib import Path
from .core import AppStore, WARSAW, analysis_metadata, as_datetime, digest, utcnow
from .health_state import ERROR_CODES, error_code, collection_attempt

DISCOVERY_CURSOR_VERSION = '3'


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

    def _collection_diagnostic(self, item):
        self.store.record_sync_diagnostic(self.run_id, dict(item, phase=self.stage))

    def _in_window(self, value, cutoff):
        if not value:
            return True
        try:
            return as_datetime(value) >= cutoff
        except (ValueError, TypeError):
            # Unknown dates must not silently disappear; analysis will resolve ambiguity.
            return True

    def _discover_messages(self, cutoff, result):
        from .librus_client import DiscoveryParsingError

        page_size = min(10, self.max_messages)
        if (self.store._setting('discovery_cursor_version') != DISCOVERY_CURSOR_VERSION
                or self.store._setting('discovery_page_size') != str(page_size)):
            # Old page cursors and completion markers did not prove every
            # position was visited. Page numbers also depend on page size.
            with self.store.lock, self.store.db:
                self.store.db.execute("""DELETE FROM settings WHERE key IN (
                    'discovery_initialized_at', 'discovery_page_prefix', 'discovery_preceding_page')""")
                self.store.db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                    (('discovery_cursor_version', DISCOVERY_CURSOR_VERSION), ('discovery_page_size', str(page_size)),
                     ('discovery_page', '1'), ('discovery_page_offset', '0'), ('discovery_retry_turn', 'retry')))

        page = int(self.store._setting('discovery_page'))
        offset = int(self.store._setting('discovery_page_offset'))
        prefix = self.store._setting('discovery_page_prefix', '')
        preceding = self.store._setting('discovery_preceding_page', '').split()
        retry_turn = self.max_messages > 1 or self.store._setting('discovery_retry_turn') == 'retry'
        retry_limit = min(20, max(1, self.max_messages // 3))
        selected = self.store.retry_message_ids(retry_limit) if retry_turn else []
        next_turn = 'scan' if self.max_messages == 1 and selected else 'retry'
        seen = set(selected)
        snippets_by_id = {}
        finished = list_failed = boundary_lost = False

        def get_page(number):
            nonlocal list_failed
            try:
                with collection_attempt(self._collection_diagnostic, source_kind='message', collection_step='message_list'):
                    snippets, total = self.client.messages(limit=page_size, page=number)
                    if not isinstance(snippets, list) or len(snippets) > page_size or any(not isinstance(s, dict) for s in snippets):
                        raise DiscoveryParsingError('Unexpected inbox page schema.')
            except Exception as error:
                result.update(status='partial', error_code=error_code(error, 'discovery'), error_stage='discovery')
                list_failed = True
                return None, None
            result['listed'] += len(snippets)
            return snippets, total

        def note(snippet):
            try:
                message_id = self.store.note_discovered(snippet)
            except ValueError:
                result['fetch_failures'] += 1
                result.update(error_code='fetch_failed', error_stage='discovery')
                return None
            snippets_by_id[message_id] = snippet
            return message_id

        def snippet_id(snippet):
            return str(snippet.get('messageId') or snippet.get('id') or '').strip()

        head, head_total = None, None
        recent = []
        if page > 1 or len(selected) == self.max_messages:
            # Observe the head even at limit one. Unfetched head items enter
            # the durable retry queue. Keep a detail slot for the history
            # cursor whenever retries have not already used this run's slot.
            head, head_total = get_page(1)
            recent.extend(head or ())
        if preceding and page > 1 and not list_failed and len(selected) < self.max_messages:
            previous, _ = (head, head_total) if page == 2 else get_page(page - 1)
            if previous is not None:
                current_boundary = [digest(snippet_id(snippet)) for snippet in previous]
                if current_boundary != preceding:
                    # A deletion can shift unread items back across the cursor.
                    # With no shared ID, the old page number cannot anchor a resume.
                    known = set(preceding)
                    if known.isdisjoint(current_boundary):
                        page, offset, prefix, preceding = 1, 0, '', []
                        boundary_lost = True
                    else:
                        recent.extend(snippet for snippet, identity in zip(previous, current_boundary) if identity not in known)
                        preceding = current_boundary
        recent_ids = {note(snippet) for snippet in recent if self._in_window(snippet.get('sendDate'), cutoff)}
        recent_ids.discard(None)
        refresh_slots = min(max(1, self.max_messages // 5), max(0, self.max_messages - len(selected) - 1))
        refresh = sorted(recent_ids - seen, key=lambda mid: (self.store.get_message(mid)['fetched_at'] or '', mid))
        selected.extend(refresh[:refresh_slots])
        seen.update(selected)

        # Bound list requests even if the provider repeats previously seen IDs.
        for _ in range(self.max_messages // page_size + 2):
            if list_failed or len(selected) == self.max_messages:
                break
            snippets, total = (head, head_total) if page == 1 and head is not None else get_page(page)
            if list_failed:
                break
            ids = [snippet_id(snippet) for snippet in snippets]
            if offset and (offset > len(snippets) or digest(ids[:offset]) != prefix):
                # A shifted page can put unseen messages before the old offset.
                offset = 0
            eligible = [self._in_window(snippet.get('sendDate'), cutoff) for snippet in snippets]
            while offset < len(snippets):
                if eligible[offset]:
                    snippet = snippets[offset]
                    message_id = ids[offset]
                    if message_id not in seen:
                        if len(selected) == self.max_messages:
                            break
                        if note(snippet) is not None:
                            selected.append(message_id)
                            seen.add(message_id)
                    else:
                        snippets_by_id[message_id] = snippet
                offset += 1
            if offset < len(snippets):
                prefix = digest(ids[:offset])
                break
            if not any(eligible) or len(snippets) < page_size or (isinstance(total, int) and page * page_size >= total):
                page, offset, prefix, preceding, finished = 1, 0, '', [], True
                break
            page, offset, prefix, preceding = page + 1, 0, '', [digest(message_id) for message_id in ids]

        with self.store.lock, self.store.db:
            if boundary_lost:
                self.store.db.execute("DELETE FROM settings WHERE key='discovery_initialized_at'")
            self.store.db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                (('discovery_page', str(page)), ('discovery_page_offset', str(offset)),
                 ('discovery_page_prefix', prefix), ('discovery_preceding_page', ' '.join(preceding)),
                 ('discovery_retry_turn', next_turn)))
        return selected, snippets_by_id, finished, list_failed

    def _source_collection(self, current):
        """Collect only; all three channels feed the one agenda task."""
        batch = None
        previous_sink = getattr(self.client, 'collection_diagnostic_sink', None)
        previous_private_sink = getattr(self.client, 'private_form_diagnostic_sink', None)
        try:
            from .calendar_form_diagnostics import PrivateFormCapture
            self.client.private_form_diagnostic_sink = PrivateFormCapture(self.store.path)
            self.client.collection_diagnostic_sink = self._collection_diagnostic if getattr(self, 'run_id', None) else previous_sink
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
        finally:
            self.client.collection_diagnostic_sink = previous_sink
            self.client.private_form_diagnostic_sink = previous_private_sink

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
                    reset_batch_retry = getattr(self.runtime, 'reset_batch_retry_budget', None)
                    if reset_batch_retry is not None:
                        reset_batch_retry(deadline=self._run_deadline)
                    from .codex_diagnostics import PrivateCodexCapture
                    self.runtime.private_diagnostic_capture = PrivateCodexCapture(self.store.path)
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
        selected, snippets_by_id, finished, list_failed = self._discover_messages(cutoff, result)
        self._stage('fetch')
        unread_probe = None
        for message_id in selected:
            try:
                with collection_attempt(self._collection_diagnostic, source_kind='message', collection_step='message_body'):
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
                with collection_attempt(self._collection_diagnostic, source_kind='message', collection_step='message_observation'):
                    after = self.client.message(unread_probe['message_id'])
                unread_probe['after_read_date'] = after.get('read_date_returned')
                unread_probe['observation'] = 'unread_before_read_after' if unread_probe['after_read_date'] else 'no_read_date_observed'
                self.store.set_setting('read_marking_observation', __import__('json').dumps(unread_probe))
                result['read_marking_observation'] = unread_probe['observation']
            except Exception:
                result['read_marking_observation'] = 'unverified_after_fetch_error'
        else:
            result['read_marking_observation'] = 'unverified_no_unread_message_observed'
        stamp = utcnow()
        retry_pending = bool(self.store.retry_message_ids(1))
        with self.store.lock, self.store.db:
            if finished and not list_failed:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_initialized_at',?)", (stamp,))
            elif result['changed'] or retry_pending:
                self.store.db.execute("DELETE FROM settings WHERE key='discovery_initialized_at'")
            discovery_incomplete = not self.store._setting('discovery_initialized_at')
            message_fetch_complete = not (list_failed or result['fetch_failures'] or retry_pending or discovery_incomplete)
            if not message_fetch_complete and not result.get('error_code'):
                result.update(error_code='discovery_incomplete', error_stage='discovery')
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

"""Private, bounded structural evidence for one synchronization and its offline reader."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from importlib import import_module
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys

from .health_state import (ERROR_CODES, FORM_DIAGNOSTIC_CODES, MESSAGE_DIAGNOSTIC_CODES,
                           RESULTS, SOURCE_ERROR_CODES, STAGES)

LIMIT = 32768
FORMAT = 1
_DOMAINS = {'source': '.source_incidents', 'calendar': '.calendar_incidents', 'planner': '.planner_incidents'}
_OPERATIONS = STAGES | {'retention', 'recovery', 'message_page', 'message_scan'}
_REASONS = ERROR_CODES | FORM_DIAGNOSTIC_CODES | MESSAGE_DIAGNOSTIC_CODES | SOURCE_ERROR_CODES | {
    'retry_succeeded', 'page_observed', 'scan_complete', 'scan_budget', 'page_boundary_changed',
    'page_prefix_changed', 'evidence_omitted'}
_DECISIONS = {'rejected', 'unverified', 'retry', 'recovered', 'stopped', 'continue', 'restart_scan', 'restart_page'}
_COUNTS = {'page', 'limit', 'offset', 'returned', 'selected', 'logical_call_index',
           'attempt_number', 'batch_index', 'expected_count', 'observed_count'}
_sink = ContextVar('sync_incident_sink', default=None)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _require(condition):
    if not condition:
        raise ValueError('Invalid incident evidence.')


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(Z|[+-][0-9]{2}:[0-9]{2})', value))
    stamp = datetime.fromisoformat(value)
    _require(stamp.utcoffset() is not None)
    return stamp.astimezone(timezone.utc).isoformat()


def _release():
    # Production runs an isolated interpreter in releases/<sha>/.venv.
    directory = Path(sys.prefix).resolve().parent
    if directory.parent.name == 'releases' and re.fullmatch('[a-f0-9]{40}', directory.name):
        return directory.name
    return 'development'


def _validator(domain):
    _require(domain in _DOMAINS)
    return import_module(_DOMAINS[domain], __package__)


def _check_operation(domain, operation):
    allowed = _OPERATIONS if domain == 'sync' else _validator(domain).OPERATIONS
    _require(isinstance(operation, str) and operation in allowed)


def _check_event(event):
    domain, operation = event['domain'], event['operation']
    _check_operation(domain, operation)
    reason, decision, evidence = event['reason'], event['decision'], event['evidence']
    _require(isinstance(evidence, dict))
    if domain == 'sync':
        _require(reason in _REASONS and decision in _DECISIONS and set(evidence) <= _COUNTS)
        _require(all(type(n) is int and 0 <= n <= 1000000 for n in evidence.values()))
    else:
        _validator(domain).validate_event(operation, reason, decision, evidence)


def active():
    return _sink.get() is not None


@contextmanager
def capture(sink, *, observation=None, progress=None):
    """Bind to the invoking sync only; test/preview calls cannot inherit another run."""
    token = _sink.set((sink, observation if observation is not None else sink, progress))
    try:
        yield
    finally:
        _sink.reset(token)


def _deliver(index, domain, operation, reason, decision, evidence):
    sinks = _sink.get()
    if sinks is None:
        return
    try:
        event = dict(domain=domain, operation=operation, reason=reason, decision=decision, evidence=evidence)
        _check_event(event)
        sinks[index](event)
    except Exception:
        # Optional evidence must never change a Calendar decision, or leak a bad value.
        logging.getLogger(__name__).warning('sync_incident_evidence_unavailable')


def emit(domain, operation, reason, decision, **evidence):
    _deliver(0, domain, operation, reason, decision, evidence)


def observe(domain, operation, reason, decision, **evidence):
    _deliver(1, domain, operation, reason, decision, evidence)


def checkpoint(domain, operation):
    sinks = _sink.get()
    if sinks is None or sinks[2] is None:
        return
    try:
        _check_operation(domain, operation)
        sinks[2](domain, operation)
    except Exception:
        logging.getLogger(__name__).warning('sync_incident_checkpoint_unavailable')


class _Recorder:
    def __init__(self, store, run, stage):
        self.store, self.run, self.stage = store, run, stage
        self.first_at = self.last_at = None
        self.dirty = False
        self.writer = None
        self.report = dict(version=FORMAT, release=_release(), checkpoint=None, events=[], omitted=0)

    def progress(self, domain, operation):
        self.report['checkpoint'] = dict(at=_now(), stage=self.stage(), domain=domain, operation=operation)
        self.dirty = True
        self.flush()

    def append(self, event, kind):
        stamp = _now()
        if kind == 'incident':
            self.first_at = self.first_at or stamp
            self.last_at = stamp
        events = self.report['events']
        stage = self.stage()
        _require(stage in STAGES)
        current = self.report['checkpoint']
        step = {key: current[key] for key in ('domain', 'operation')} if current else None
        fields = dict(kind=kind, stage=stage, step=step, **event)
        if len(_json(fields)) > LIMIT // 4:
            # Refuse arbitrary truncation of structural evidence. Preserve its location.
            fields = dict(kind=kind, stage=stage, step=step, domain='sync', operation=stage,
                          reason='evidence_omitted', decision='unverified', evidence={})
            self.report['omitted'] += 1
        for previous in reversed(events):
            if all(previous[key] == value for key, value in fields.items()):
                previous['count'] += 1
                previous['last_at'] = max(stamp, previous['last_at'])
                break
        else:
            events.append(dict(at=stamp, last_at=stamp, count=1, **fields))
        self._bound()
        self.dirty = True
        self.flush()

    def _bound(self):
        events = self.report['events']
        # Reserve space for changes to the last checkpoint and omitted count.
        while len(_json(self.report)) > LIMIT - 256:
            failures = [i for i, e in enumerate(events) if e['kind'] == 'incident']
            protected = {len(events) - 1}
            if failures:
                latest = max(failures, key=lambda index: events[index]['last_at'])
                protected.update((failures[0], latest))
            index = next(i for i in range(len(events)) if i not in protected)
            self.report['omitted'] += events.pop(index)['count']

    def flush(self):
        # A caller may still roll back its own transaction. Retain the evidence
        # in memory and flush at the next independent boundary.
        if not self.dirty or self.store.db.in_transaction:
            return
        if self.writer is None:
            uri = self.store.path.resolve().as_uri() + '?mode=rw'
            self.writer = sqlite3.connect(uri, uri=True, timeout=0)
        try:
            with self.writer:
                self.writer.execute("""INSERT INTO sync_incidents(run_id,first_at,last_at,report)
                    VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                    first_at=excluded.first_at,last_at=excluded.last_at,report=excluded.report""",
                    (self.run, self.first_at, self.last_at, _json(self.report)))
        except sqlite3.OperationalError as error:
            # Optional reports cannot spend the worker's deadline waiting for
            # SQLite. A later checkpoint retries the complete buffered report.
            if error.sqlite_errorcode & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return
            raise
        self.dirty = False


@contextmanager
def recording(store, run, stage):
    recorder = _Recorder(store, run, stage)
    with capture(lambda e: recorder.append(e, 'incident'),
                 observation=lambda e: recorder.append(e, 'observation'), progress=recorder.progress):
        try:
            yield
        finally:
            try:
                recorder.flush()
            except Exception:
                logging.getLogger(__name__).warning('sync_incident_flush_unavailable')
            finally:
                if recorder.writer is not None:
                    recorder.writer.close()


def _validated_report(raw):
    _require(isinstance(raw, str) and len(raw.encode()) <= LIMIT)
    report = json.loads(raw)
    _require(set(report) == {'version','release','events','checkpoint','omitted'}
             and type(report['version']) is int and report['version'] == FORMAT)
    _require(report['release'] == 'development' or re.fullmatch('[a-f0-9]{40}', report['release']))
    _require(type(report['omitted']) is int and report['omitted'] >= 0)
    check = report['checkpoint']
    if check is not None:
        _require(set(check) == {'stage','domain','operation','at'} and check['stage'] in STAGES)
        _check_operation(check['domain'], check['operation'])
        _timestamp(check['at'])
    _require(isinstance(report['events'], list))
    for event in report['events']:
        _require(set(event) == {'domain','operation','reason','decision','evidence','kind','count','stage','step','at','last_at'})
        _require(event['kind'] in ('incident','observation') and event['stage'] in STAGES
                 and type(event['count']) is int and event['count'] > 0)
        _require(_timestamp(event['at']) <= _timestamp(event['last_at']))
        step = event['step']
        if step is not None:
            _require(set(step) == {'domain', 'operation'})
            _check_operation(step['domain'], step['operation'])
        _check_event(event)
    return report


def _include_source_fixtures(report):
    """Render fictional examples on read, with the same per-report size budget."""
    from .source_incidents import fictional_fragment

    for event in report['events']:
        if event['kind'] != 'incident' or event['domain'] != 'source':
            continue
        fragment = fictional_fragment(event['operation'], event['reason'], event['evidence'])
        if fragment is not None:
            event['fictional_fragment'] = fragment
            if len(_json(report)) > LIMIT:
                del event['fictional_fragment']


def _run_summary(row):
    _require(re.fullmatch('[a-f0-9]{32}', row['id']) and row['result'] in RESULTS
             and row['stage'] in STAGES and (row['error_code'] is None or row['error_code'] in ERROR_CODES))
    return dict(id=row['id'], started_at=_timestamp(row['started_at']),
                finished_at=_timestamp(row['finished_at']) if row['finished_at'] else None,
                stage=row['stage'], result=row['result'], error_code=row['error_code'])


def read_incident(path, *, run=None, at=None):
    """Select an incident without opening AppStore or initializing the application."""
    if run is not None:
        _require(isinstance(run,str) and re.fullmatch('[0-9a-f]{32}',run) and at is None)
    when = _timestamp(at) if at is not None else _now()
    uri = Path(path).expanduser().resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'sync_runs' not in tables:
            return {'status':'not_found'}
        columns = 'r.id,r.started_at,r.finished_at,r.stage,r.result,r.error_code'
        # An abandoned worker is closed by the *next* run. Alerts during the
        # interrupted run must remain attributable before that recovery time.
        failure = ("CASE WHEN r.result='interrupted' AND r.error_code='worker_interrupted' THEN r.started_at "
                   "WHEN r.result IN ('partial','failed','interrupted') THEN r.finished_at END")
        if 'sync_incidents' in tables:
            query = f'SELECT {columns},i.report FROM sync_runs r LEFT JOIN sync_incidents i ON i.run_id=r.id'
            instant = f'COALESCE(i.first_at, {failure})'
        else:
            query = f'SELECT {columns},NULL AS report FROM sync_runs r'
            instant = failure
        if run is not None:
            row = db.execute(query+' WHERE r.id=?', (run,)).fetchone()
        else:
            # Sync workers share one exclusive lock. first_at anchors a run even
            # when later repetitions in the same report occur after the alert.
            row = db.execute(query+f' WHERE {instant} <= ? ORDER BY {instant} DESC,r.rowid DESC LIMIT 1',
                             (when,)).fetchone()
        if row is None:
            return {'status':'not_found'}
        selected = _run_summary(row)
        later = db.execute(f"SELECT {columns} FROM sync_runs r WHERE r.result='ok' AND r.started_at>? ORDER BY r.started_at,r.rowid LIMIT 1",
                           (row['started_at'],)).fetchone()
        report = _validated_report(row['report']) if row['report'] else None
        if report is not None:
            _include_source_fixtures(report)
        return dict(status='found', run=selected, detail_status='available' if report else 'unavailable',
                    report=report, next_success=_run_summary(later) if later else None)

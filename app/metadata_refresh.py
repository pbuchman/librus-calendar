"""Explicit, journaled metadata updates for existing application-owned events."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .core import (AppStore, PREFIX, contains_marker, digest, has_external_attendees,
                   librus_title, synchronization_description, utcnow)
from .sync import SyncInterrupted, execution_deadline, sync_lock

VERSION = 'librus-branding-v1'


def refresh_metadata(state_path, event_ids, *, apply=False, runtime_factory=None, current=None, config=None):
    """CLI entry locks before opening state or initializing Calendar access."""
    state_path = Path(state_path).expanduser()
    with sync_lock(state_path) as acquired:
        if not acquired:
            return {'status': 'already_running', 'calendar_writes': 0}
        if not state_path.is_file():
            raise FileNotFoundError('Existing calendar state is required.')
        store = AppStore(state_path, config=config)
        try:
            return MetadataRefreshController(store, runtime_factory=runtime_factory)._run_locked(event_ids, apply=apply, current=current)
        finally:
            store.close()


class MetadataRefreshController:
    def __init__(self, store, runtime=None, *, runtime_factory=None):
        self.store, self.runtime, self.runtime_factory = store, runtime, runtime_factory

    def run(self, event_ids, *, apply=False, current=None):
        with sync_lock(self.store.path) as acquired:
            if not acquired:
                return {'status': 'already_running', 'calendar_writes': 0}
            return self._run_locked(event_ids, apply=apply, current=current)

    def _event(self, event_id):
        from .codex_runtime import event_fingerprint
        row = self.store.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not row or row['state'] != 'active' or row['calendar_id'] != self.store.calendar_id:
            raise ValueError('Metadata refresh requires an active event on the allowed calendar.')
        snapshot = json.loads(row['snapshot'])
        marker = row['marker']
        if (snapshot.get('id') != event_id or not re.fullmatch(r'librus-calendar:[A-Za-z0-9_-]{8,128}', marker)
                or not contains_marker(snapshot.get('description'), marker) or has_external_attendees(snapshot, self.store.config.calendar_owner)
                or snapshot.get('recurrence') or event_fingerprint(snapshot, owner_email=self.store.config.calendar_owner) != row['fingerprint']):
            raise ValueError('Stored ownership, fingerprint, attendees or recurrence requires manual review.')
        for operation in self.store.db.execute("SELECT payload FROM operations WHERE status IN ('pending','inflight','unknown')"):
            if json.loads(operation[0]).get('event_id') == event_id:
                raise ValueError('An existing event operation must be resolved before metadata refresh.')
        return {**dict(row), 'snapshot': snapshot}

    def _journal(self, event_id):
        return self.store.db.execute('SELECT * FROM metadata_refresh WHERE event_id=? AND version=?', (event_id, VERSION)).fetchone()

    def _prepare(self, event, stamp):
        from .codex_runtime import normalize_event
        normalized = normalize_event(event['snapshot'], owner_email=self.store.config.calendar_owner)
        desired = {key: normalized[key] for key in ('summary', 'description', 'location', 'start', 'end', 'reminders')}
        desired['summary'] = librus_title(normalized['summary'])
        desired['description'] = synchronization_description(event['snapshot'].get('description'), stamp, event['marker'])
        # The executor writes only these fields. It sends no attendees/visibility,
        # retains the existing ID, and validates the live baseline before update.
        return {'operation_id': event['marker'][len(PREFIX):], 'action': 'update', 'calendar_id': self.store.calendar_id,
                'marker': event['marker'], 'event_id': event['id'], 'expected_fingerprint': event['fingerprint'],
                'event': desired, 'message_id': event['message_id'], 'proposal_id': event['proposal_id'],
                'source_message_ids': [event['message_id']], 'deletion_approved': False,
                'prior_snapshot': event['snapshot'], 'synchronization_started_at': stamp}

    def _persist_intent(self, event_id, operation):
        stamp = utcnow()
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            event = self._event(event_id)
            if event['fingerprint'] != operation['expected_fingerprint']:
                raise ValueError('Stored event changed after metadata preview.')
            self.store.db.execute('INSERT OR IGNORE INTO metadata_refresh(id,event_id,version,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                (digest([VERSION, event_id])[:32], event_id, VERSION, json.dumps(operation, ensure_ascii=False), 'pending', stamp, stamp))
        return self._journal(event_id)

    def _claim(self, journal_id):
        with self.store.lock, self.store.db:
            if self.store._setting('writes_paused', 'true') == 'true':
                return False
            cursor = self.store.db.execute("UPDATE metadata_refresh SET status='inflight',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'", (utcnow(), journal_id))
            return bool(cursor.rowcount)

    def _record(self, journal, outcome):
        from .codex_runtime import event_fingerprint
        operation = json.loads(journal['payload'])
        status = outcome.get('status') if isinstance(outcome, dict) else None
        if status not in ('applied', 'review', 'unknown', 'failed'):
            outcome, status = {'status': 'unknown', 'error': 'Invalid metadata execution result.'}, 'unknown'
        snapshot = outcome.get('snapshot')
        if status == 'applied':
            if (not isinstance(snapshot, dict) or outcome.get('event_id') != journal['event_id']
                    or snapshot.get('id') != journal['event_id'] or snapshot.get('status') == 'cancelled'
                    or has_external_attendees(snapshot, self.store.config.calendar_owner) or not contains_marker(snapshot.get('description'), operation['marker'])
                    or outcome.get('fingerprint') != event_fingerprint(snapshot, owner_email=self.store.config.calendar_owner)
                    or event_fingerprint(snapshot, owner_email=self.store.config.calendar_owner) != event_fingerprint(operation['event'], owner_email=self.store.config.calendar_owner)):
                outcome, status = {'status': 'unknown', 'error': 'Metadata update lacks exact verified event evidence.'}, 'unknown'
        if journal['status'] in ('unknown', 'inflight') and status == 'failed':
            outcome, status = {'status': 'unknown', 'error': 'Uncertain metadata write cannot authorize a retry.'}, 'unknown'
        stamp = utcnow()
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            if status == 'applied':
                row = self.store.db.execute('SELECT * FROM events WHERE id=? AND calendar_id=? AND state=\'active\'', (journal['event_id'], self.store.calendar_id)).fetchone()
                if not row or row['marker'] != operation['marker'] or row['fingerprint'] != operation['expected_fingerprint']:
                    outcome, status = {'status': 'review', 'error': 'Stored event changed while metadata update was being verified.'}, 'review'
                else:
                    prior = json.loads(row['snapshot'])
                    # Preserve readonly/private fields omitted by the connector's
                    # projection; replace writable aliases with verified values.
                    merged = {**prior, **snapshot, **operation['event']}
                    if event_fingerprint(merged, owner_email=self.store.config.calendar_owner) != outcome['fingerprint']:
                        outcome, status = {'status': 'review', 'error': 'Verified metadata snapshot conflicts with preserved event fields.'}, 'review'
                    else:
                        self.store.db.execute('UPDATE events SET snapshot=?,fingerprint=?,updated_at=? WHERE id=? AND calendar_id=?',
                            (json.dumps(merged, ensure_ascii=False), outcome['fingerprint'], stamp, journal['event_id'], self.store.calendar_id))
            state = 'pending' if status == 'failed' else status
            self.store.db.execute('UPDATE metadata_refresh SET status=?,result=?,last_error=?,updated_at=? WHERE id=?',
                (state, json.dumps(outcome, ensure_ascii=False), str(outcome.get('error') or '')[:500], stamp, journal['id']))
        return status

    def _run_locked(self, event_ids, *, apply=False, current=None):
        from .codex_runtime import event_fingerprint
        ids = list(dict.fromkeys(str(value).strip() for value in event_ids))
        if not ids or len(ids) > 30 or any(not value for value in ids):
            raise ValueError('Select one to thirty explicit event IDs.')
        result = {'status': 'applied' if apply else 'preview', 'version': VERSION, 'calendar_writes': 0, 'events': []}
        plans = []
        stamp = (current or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        for event_id in ids:
            try:
                event = self._event(event_id)
                journal = self._journal(event_id)
                operation = json.loads(journal['payload']) if journal else self._prepare(event, stamp)
                plans.append((event, journal, operation))
                result['events'].append({'event_id': event_id, 'status': journal['status'] if journal else 'planned',
                    'title_before': event['snapshot'].get('summary', event['snapshot'].get('title')),
                    'title_after': operation['event']['summary'], 'synchronization_started_at': operation['synchronization_started_at'],
                    'marker': operation['marker'], 'expected_fingerprint': operation['expected_fingerprint'],
                    'desired_fingerprint': event_fingerprint(operation['event'], owner_email=self.store.config.calendar_owner)})
            except ValueError as error:
                result['events'].append({'event_id': event_id, 'status': 'refused', 'reason': str(error)})
        if any(item['status'] == 'refused' for item in result['events']):
            result['status'] = 'refused'
            return result
        if not apply:
            return result
        if all(journal is not None and journal['status'] == 'applied' for event, journal, operation in plans):
            return result
        if self.runtime_factory is not None:
            self.runtime = self.runtime_factory()
        with execution_deadline(45*60):
            probe = self.runtime.probe()
            if not isinstance(probe, dict) or probe.get('calendar_id') != self.store.calendar_id or probe.get('access_role') not in ('owner', 'writer'):
                raise ValueError('Calendar write access was not verified.')
            for index, (event, journal, operation) in enumerate(plans):
                if journal is not None and journal['status'] in ('applied', 'review'):
                    if journal['status'] == 'review':
                        result['status'] = 'partial'
                    continue
                journal = journal or self._persist_intent(event['id'], operation)
                if journal['status'] == 'inflight':
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE metadata_refresh SET status='unknown',last_error='Interrupted metadata update; read-only reconciliation required.',updated_at=? WHERE id=? AND status='inflight'", (utcnow(), journal['id']))
                    journal = self._journal(event['id'])
                reconcile = journal['status'] == 'unknown'
                if not reconcile and not self._claim(journal['id']):
                    result['events'][index]['status'] = 'paused'
                    result['status'] = 'paused'
                    continue
                try:
                    outcome = self.runtime.execute({**operation, 'reconcile_only': True} if reconcile else operation)
                except (SyncInterrupted, KeyboardInterrupt, SystemExit):
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE metadata_refresh SET status='unknown',last_error='Interrupted metadata update; read-only reconciliation required.',updated_at=? WHERE id=? AND status='inflight'", (utcnow(), journal['id']))
                    result['status'], result['events'][index]['status'] = 'interrupted', 'unknown'
                    break
                except Exception:
                    outcome = {'status': 'unknown', 'error': 'Metadata execution failed; reconcile before retry.'}
                status = self._record(journal, outcome)
                result['events'][index]['status'] = status
                result['calendar_writes'] += int(status == 'applied' and not reconcile)
                if status != 'applied':
                    result['status'] = 'partial'
        return result

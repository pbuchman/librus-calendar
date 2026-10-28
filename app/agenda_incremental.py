"""Pure incremental input selection and its explicit checkpoint contract.

Selection accepts an existing read-only SQLite connection, never initializes
a database and never calls a provider or Calendar. The explicit commit helper
is used only inside the planner materialization transaction. The complete local state remains the CAS authority;
only the model request replaces already processed bodies with a source catalog.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from contextlib import nullcontext

from .agenda import assemble_agenda_snapshot, agenda_prompt
from .core import digest

INCREMENTAL_VERSION = 'simple-agenda-v8-future-preferences'
COMPATIBLE_FULL_VERSIONS = frozenset({'simple-agenda-v4-shared-learner'})
LEDGER_TABLE = 'agenda_processed_sources'
LEDGER_SCHEMA = '''CREATE TABLE IF NOT EXISTS agenda_processed_sources(
 source_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, version TEXT NOT NULL,
 plan_id TEXT NOT NULL, processed_at TEXT NOT NULL)'''


def source_version(source):
    """Retention/deletion are changes even if a provider keeps content_hash."""
    retained = source['status'] == 'retained' or bool(source.get('metadata', {}).get('retained'))
    return {'source_id': source['id'], 'content_hash': source['content_hash'],
            'deleted': bool(source['deleted']), 'retained': retained,
            'body_available': not retained and not source['deleted'] and bool(source.get('text'))}


def _rows(db, statement):
    cursor = db.execute(statement)
    names = [field[0] for field in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _compatible_full_plan(metadata):
    return (metadata.get('prompt_version') in COMPATIBLE_FULL_VERSIONS
            and metadata.get('model') == 'gpt-6.1-sol' and metadata.get('effort') == 'high')


def _bootstrap_versions(db, sources, analyzed):
    """Only the preceding compatible full planner proves historical coverage.

    Its applied transaction marked *all* then-current rows at its created_at.
    A source must still be analyzed with that exact stamp. Pending, failed,
    retained and deleted rows never qualify: pruning preserves the old stamp. An older content version cannot be
    inferred from an arbitrary action link or a successful sync timestamp.
    """
    compatible = {}
    for row in _rows(db, "SELECT id,created_at,metadata FROM agenda_plans WHERE status='applied'"):
        metadata = json.loads(row['metadata'])
        if _compatible_full_plan(metadata):
            compatible[row['created_at']] = row['id']
    return {source['id']: {'version': source_version(source),
                            'plan_id': compatible[analyzed[source['id']]]}
            for source in sources if source['status'] == 'analyzed'
            and not source_version(source)['retained'] and not source['deleted'] and analyzed.get(source['id']) in compatible}


def assemble_incremental_snapshot(database, calendar_events, *, current=None, owner_email='', enforce_budget=True):
    """Return (model_snapshot, contract), with no writes and one coherent read.

    A normal sqlite3 read-only connection works even before the additive ledger
    exists. `contract` is local only and must never be sent as model input. Live
    integration commits processed_versions together with local materialization,
    after comparing local_state_hash under BEGIN IMMEDIATE. Preview/failure never
    commits either bootstrap_versions or processed_versions.
    """
    db = getattr(database, 'db', database)
    lock = getattr(database, 'lock', None)
    with lock if lock is not None else nullcontext():
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute('BEGIN')
        try:
            full = assemble_agenda_snapshot(database, calendar_events, current=current,
                                            owner_email=owner_email, enforce_budget=False)
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ledger = {row['source_id']: row for row in _rows(db, 'SELECT * FROM ' + LEDGER_TABLE)} if LEDGER_TABLE in names else {}
            authority_baseline = json.loads(dict(db.execute('SELECT key,value FROM settings')).get('agenda_authority_baseline', '{}'))
            source_rows = _rows(db, 'SELECT id,upstream_id,analyzed_at FROM sources')
            analyzed = {row['id']: row['analyzed_at'] for row in source_rows}
            bootstrap = _bootstrap_versions(db, full['sources'], analyzed)
            operations = _rows(db, "SELECT id,proposal_id,status,attempts,payload,result FROM operations WHERE status IN ('pending','review','unknown','inflight') ORDER BY id")
            versions = {source['id']: source_version(source) for source in full['sources']}
            # Ledger changes are included in the local CAS, never in byte-budgeted
            # input. A concurrent checkpoint invalidates this selected request.
            local_state = {'snapshot': {key: value for key, value in full.items()
                                        if key not in ('observation', 'snapshot_hash')},
                           'ledger': ledger, 'bootstrap': bootstrap, 'operations': operations,
                           'incremental_version': INCREMENTAL_VERSION, 'authority_baseline': authority_baseline}
            local_hash = digest(local_state)
            selected, reasons = [], {}
            affected, authority_versions = set(), {}
            compatible_times = [row['created_at'] for row in _rows(db, "SELECT metadata,created_at FROM agenda_plans WHERE status='applied'")
                if _compatible_full_plan(json.loads(row['metadata']))]
            def instant(value):
                try:
                    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
                except (ValueError, TypeError, AttributeError):
                    return float('inf')
            cutoff = max((instant(value) for value in compatible_times), default=float('-inf'))
            decision_times = {row['action_id']: row['updated_at'] for row in _rows(db, 'SELECT action_id,updated_at FROM canonical_decisions')}
            intent_times = {row['id']: row['updated_at'] for row in _rows(db, 'SELECT id,updated_at FROM decision_intents')}
            proposal_times = {row['id']: row['updated_at'] for row in _rows(db, 'SELECT id,updated_at FROM proposals')}
            for entry in full['agenda']:
                controlled = any(entry['action'].get(flag) for flag in ('user_approved', 'user_edited', 'user_rejected', 'deletion_approved'))
                authority = {'decisions': entry['decisions'],
                    'human_action': entry['action'] if controlled else {}, 'calendar': entry['calendar']}
                authority_versions[entry['id']] = digest(authority)
                if entry['id'] in authority_baseline:
                    changed = authority_baseline[entry['id']] != authority_versions[entry['id']]
                else:
                    # The preceding full-plan transaction already saw older
                    # decisions. New decisions/edits and fresh external state
                    # need their linked available bodies on the first delta.
                    stamps = [value for value in (decision_times.get(entry['id']), intent_times.get(entry['proposal_id'])) if value]
                    if controlled and proposal_times.get(entry['proposal_id']):
                        stamps.append(proposal_times[entry['proposal_id']])
                    changed = (any(instant(value) > cutoff for value in stamps)
                               or bool(entry['decisions'] or controlled) and not stamps
                               or any(event['externally_changed'] for event in entry['calendar']))
                if changed:
                    affected.update(link['source_id'] for link in entry['links'])
                    if entry.get('origin', {}).get('message_id'):
                        affected.add('message:' + entry['origin']['message_id'])
            for source in full['sources']:
                sid, version = source['id'], versions[source['id']]
                prior = ledger.get(sid)
                prior_version = json.loads(prior['version']) if prior else bootstrap.get(sid, {}).get('version')
                why = []
                if prior_version != version:
                    why.append('new_or_changed')
                if sid in affected and version['body_available']:
                    why.append('human_or_calendar_context')
                if why:
                    selected.append({**copy.deepcopy(source), 'body_available': version['body_available'],
                                     'source_role': 'changed' if 'new_or_changed' in why else 'context'})
                    reasons[sid] = why
            projected = {key: copy.deepcopy(value) for key, value in full.items()
                         if key not in ('sources', 'snapshot_hash')}
            projected.update(planner_version=INCREMENTAL_VERSION, snapshot_hash=local_hash,
                sources=selected, source_catalog=[{'id': source['id'], 'kind': source['kind'],
                    **{key: value for key, value in versions[source['id']].items() if key != 'source_id'}}
                    for source in full['sources']], unsettled_operations=operations)
            # request_hash hashes the ACTUAL serialized request, not local bodies.
            prompt = agenda_prompt(projected) if enforce_budget else None
            contract = {'local_state_hash': local_hash, 'full_snapshot_hash': content_baseline_hash(full), 'request_hash': hashlib.sha256(prompt.encode('utf-8')).hexdigest() if prompt else None,
                'processed_versions': [versions[source['id']] for source in selected],
                'bootstrap_versions': bootstrap, 'selection_reasons': reasons, 'authority_versions': authority_versions,
                'message_versions': {row['id']: row['upstream_id'] for row in source_rows},
                'catalog_count': len(versions), 'source_count': len(selected),
                'input_bytes': len(prompt.encode('utf-8')) if prompt else None}
            return projected, contract
        finally:
            if own_transaction:
                db.rollback()


def content_baseline_hash(snapshot):
    """Reuse excludes observation/day clocks; CAS and requests retain the day."""
    content = {key: value for key, value in snapshot.items()
               if key not in ('observation', 'snapshot_hash', 'as_of_date')}
    content['sources'] = [{**source, 'metadata': {
        key: value for key, value in source.get('metadata', {}).items()
        if key not in ('window_start', 'window_end')}} for source in snapshot['sources']]
    return digest(content)


def commit_processed_sources(store, snapshot, contract, plan_id, stamp):
    """Called only inside materialization's checked write transaction."""
    versions = {source['source_id']: source for source in contract['processed_versions']}
    selected = {source['id']: source_version(source) for source in snapshot['sources']}
    if versions != selected or contract['local_state_hash'] != snapshot['snapshot_hash']:
        from .codex_runtime import RuntimeFailure
        raise RuntimeFailure('agenda_plan_stale')
    records = {sid: {'version': version, 'plan_id': plan_id} for sid, version in versions.items()}
    records.update({sid: value for sid, value in contract['bootstrap_versions'].items() if sid not in records})
    for sid, record in records.items():
        source = store.get_source(sid)
        if not source or source_version(source) != record['version']:
            from .codex_runtime import RuntimeFailure
            raise RuntimeFailure('agenda_plan_stale')
        version = record['version']
        store.db.execute('INSERT OR REPLACE INTO agenda_processed_sources VALUES (?,?,?,?,?)',
            (sid, digest(version), json.dumps(version, sort_keys=True), record['plan_id'], stamp))
        if sid not in versions:
            continue  # Bootstrap records prior proof without rewriting history.
        store.db.execute("UPDATE sources SET status=CASE WHEN status='retained' THEN status WHEN deleted=1 THEN 'deleted' ELSE 'analyzed' END,analyzed_at=?,last_error=NULL WHERE id=? AND content_hash=?",
                         (stamp, sid, version['content_hash']))
        if source['kind'] == 'message':
            # Exact real message ID AND mirrored payload hash. Never synthetic
            # messages, unrelated pending fetches, or a newer message version.
            store.db.execute("UPDATE messages SET analyzed_hash=payload_hash,status='analyzed',last_error=NULL,pending_since=NULL,analyzed_at=? WHERE id=? AND payload_hash=? AND status<>'retained'",
                             (stamp, source['upstream_id'], version['content_hash']))

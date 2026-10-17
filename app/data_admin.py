"""Direct decision transfer, read-only reconstruction, and bounded private state."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .core import WARSAW, as_datetime, digest, has_external_attendees, start_key, utcnow
from .codex_runtime import event_fingerprint, normalize_event, RuntimeFailure
from .sync import execution_deadline, safe_error, sync_lock, SyncInterrupted

FLAGS = ('user_approved', 'user_edited', 'user_rejected', 'deletion_approved')
UNRESOLVED = ('pending', 'review', 'inflight', 'unknown')


def intent_signature(item):
    quote = re.sub(r'\s+', ' ', str(item.get('source_quote') or '')).strip()
    return {'kind': item.get('kind'), 'start': start_key(item),
            'temporal_kind': item.get('temporal_kind', 'event'),
            'quote_hash': digest(quote) if quote else None, 'event_id': item.get('event_id')}


def minimal_proposal(item):
    """Keep a chosen action, never the original teacher quote or AI reasoning."""
    keep = ('id', 'message_id', 'kind', 'title', 'start', 'end', 'all_day', 'temporal_kind',
            'due_at', 'activity_scope', 'canonical_action_id', 'source_id', 'source_hash', 'source_kind', 'source_version_at', 'optional', 'recipient_basis', 'deferred_unknown_operation', 'action_identity', 'recipient', 'event_id', 'default_duration', 'alternative_group_id', 'blocked_by_group_decision', 'blocked_by_offer_group', *FLAGS)
    result = {key: item[key] for key in keep if key in item}
    if isinstance(result.get('action_identity'),dict):result['action_identity']={k:v for k,v in result['action_identity'].items() if k!='evidence'}
    if isinstance(result.get('recipient'),dict):result['recipient']={k:v for k,v in result['recipient'].items() if k!='evidence'}
    result.update(description=item.get('description', '') if item.get('user_approved') or item.get('user_edited') else '',
                  source_quote='', confidence='high' if item.get('user_approved') else 'low',
                  needs_review=False, review_reason='', status=item.get('status', 'review'))
    return result


def minimal_snapshot(snapshot, marker):
    # The original writable-field fingerprint remains the conflict baseline. The
    # source quote in a Calendar description is unnecessary local history.
    keys = ('id', 'summary', 'title', 'start', 'end', 'all_day', 'status', 'htmlLink', 'html_link',
            'web_link', 'url', 'reminders', 'location', 'recurrence', 'attendees')
    return {**{key: snapshot[key] for key in keys if key in snapshot}, 'description': marker, '_content_pruned': True}


def decision_event_ids(db, proposal_id, item, calendar_id):
    """Minimal exact ownership links, including a create's settled result."""
    event_ids = {row['id'] for row in db.execute('SELECT id FROM events WHERE proposal_id=? AND calendar_id=?',
                                               (proposal_id, calendar_id))}
    if item.get('event_id'):
        event_ids.add(str(item['event_id']))
    for row in db.execute("SELECT payload,result FROM operations WHERE proposal_id=? AND status='applied'", (proposal_id,)):
        operation = json.loads(row['payload'])
        result = json.loads(row['result']) if row['result'] else {}
        if operation.get('calendar_id') == calendar_id and result.get('event_id'):
            event_ids.add(str(result['event_id']))
    return event_ids


def remember_decision(store, row, item, original=None):
    if not any(item.get(flag) is True for flag in FLAGS):
        return
    existing = store.db.execute('SELECT payload FROM decision_intents WHERE id=?', (row['id'],)).fetchone()
    previous = json.loads(existing[0]) if existing else {}
    record = {'proposal': minimal_proposal(item),
              'signature': previous.get('signature') or intent_signature(original or item),
              'event_ids': sorted(set(previous.get('event_ids', ()))
                                  | decision_event_ids(store.db, row['id'], item, store.calendar_id))}
    store.db.execute('INSERT INTO decision_intents VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                     (row['id'], row['message_id'], row['source_hash'], json.dumps(record, ensure_ascii=False), utcnow()))


def protect_decision(store, message, item):
    rows = store.db.execute('SELECT * FROM decision_intents WHERE message_id=?', (message['id'],)).fetchall()
    rows = [row for row in rows if json.loads(row['payload'])['proposal'].get('kind') == item.get('kind')
            and (item.get('kind') == 'create' or json.loads(row['payload'])['proposal'].get('event_id') == item.get('event_id'))]
    if not rows:
        return item
    signature = json.loads(json.dumps(intent_signature(item)))
    # Explicit dismissal of a standalone blocked replacement is safe to retain
    # across cosmetic model changes. This path can only restore rejection, and
    # must never take a decision away from a different established offer.
    known_group = store.db.execute('SELECT id FROM proposal_groups WHERE message_id=? AND source_hash=? AND group_key=?',
        (message['id'],message['payload_hash'],item.get('alternative_group'))).fetchone() if item.get('alternative_group') else None
    rejected = []
    for row in rows:
        record = json.loads(row['payload'])
        decision = record['proposal']
        blocked = decision.get('blocked_by_group_decision')
        if (row['source_hash'] != message['payload_hash'] or record.get('signature') != signature
                or decision.get('user_rejected') is not True or not blocked
                or (known_group and known_group['id'] != blocked)
                or store.db.execute('SELECT 1 FROM proposal_group_members WHERE proposal_id=?',(row['id'],)).fetchone()):
            continue
        if store.db.execute("SELECT 1 FROM proposal_groups WHERE id=? AND message_id=? AND source_hash=? AND status<>'open'",
                            (blocked,message['id'],message['payload_hash'])).fetchone():
            rejected.append((row,blocked))
    if rejected:
        row,blocked = min(rejected,key=lambda pair:pair[0]['id'])
        item = dict(item,id=row['id'],status='ignored',user_rejected=True,user_approved=False,
            needs_review=False,blocked_by_group_decision=blocked,review_reason='Explicit prior user rejection retained.')
        item.pop('alternative_group',None)
        item.pop('alternative_group_evidence',None)
        return item
    if item.get('alternative_group'):
        group = store.db.execute('SELECT id FROM proposal_groups WHERE message_id=? AND source_hash=? AND group_key=?',
            (message['id'],message['payload_hash'],item['alternative_group'])).fetchone()
        if group:
            member_ids = {row[0] for row in store.db.execute('SELECT proposal_id FROM proposal_group_members WHERE group_id=?',(group['id'],))}
        else:
            # A renamed marker needs the same literal offer evidence. A broad
            # source quote and coinciding date do not identify a different offer.
            evidence = re.sub(r'\s+', ' ', str(item.get('alternative_group_evidence') or '')).strip()
            candidates = [row for row in store.db.execute(
                'SELECT m.proposal_id,g.id AS group_id,g.source_evidence FROM proposal_group_members m JOIN proposal_groups g ON g.id=m.group_id WHERE g.message_id=? AND g.source_hash=?',
                (message['id'],message['payload_hash'])) if evidence and re.sub(r'\s+',' ',row['source_evidence']).strip()==evidence]
            member_ids = {row['proposal_id'] for row in candidates} if len({row['group_id'] for row in candidates})==1 else set()
        rows = [row for row in rows if row['id'] in member_ids]
        if not rows:
            return item
    else:
        possible_groups = set()
        for known in store.db.execute('SELECT p.*,m.group_id FROM proposals p JOIN proposal_group_members m ON m.proposal_id=p.id WHERE p.message_id=? AND p.source_hash=?',
                                      (message['id'],message['payload_hash'])):
            if json.loads(json.dumps(intent_signature(store._proposal(known)))) == signature:
                possible_groups.add(known['group_id'])
        if len(possible_groups)>1:
            return dict(item,status='review',needs_review=True,
                review_reason='A prior offer decision cannot be matched to one offer; explicit reconciliation required.')
    matches = [row for row in rows if json.loads(row['payload']).get('signature') == signature]
    if len(matches) == 1 and matches[0]['source_hash'] == message['payload_hash']:
        decision = json.loads(matches[0]['payload'])['proposal']
        if decision.get('user_rejected'):
            return dict(item, id=matches[0]['id'], status='ignored', user_rejected=True, review_reason='Explicit prior user rejection retained.')
        # A previously applied approval is never a new operation, regardless of
        # current model wording. Unwritten approvals retain the edited action.
        retained_status = decision.get('status', 'review')
        retained_status = retained_status if retained_status in ('pending', 'applied') else 'review'
        # Calendar reconstruction can discover a matching event whose previous
        # write was never recorded locally. Approval does not permit duplicating it.
        if retained_status == 'pending' and any(start_key(event['snapshot']) == start_key(decision)
                for event in store.list_events(100000, include_cancelled=True) if event['message_id'] == message['id']):
            retained_status = 'review'
        return {**item, **decision, 'id': matches[0]['id'], 'message_id': message['id'],
                'source_quote': item.get('source_quote', ''),
                'status': retained_status, 'needs_review': retained_status == 'review',
                'review_reason': 'Prior approval retained; reconstructed state needs confirmation before execution.' if retained_status == 'review' else ''}
    # AI output IDs include wording, and edits may have changed the chosen date.
    # An uncertain match must never silently resurrect a rejected/chosen action.
    return dict(item, status='review', needs_review=True,
                review_reason='Prior human decision for this source requires an explicit match; no automatic replay.')


def human_event_review_reason(store, item):
    """Require fresh approval before changing an event governed by human intent.

    Callers supply normalized model output or the trusted persisted proposal.
    A source may own several events: association, explicit target IDs and applied
    operation evidence take precedence over the conservative date fallback.
    """
    if item.get('kind') not in ('update', 'cancel') or item.get('status') not in ('pending', 'review'):
        return None
    if (item.get('user_approved') is True and item.get('user_rejected') is not True
            and (item['kind'] != 'cancel' or item.get('deletion_approved') is True)):
        return None
    target = store.db.execute('SELECT * FROM events WHERE id=? AND calendar_id=?',
                              (item.get('event_id'), store.calendar_id)).fetchone()
    if not target:
        return None
    records = {}
    for row in store.db.execute('SELECT * FROM proposals'):
        chosen = store._proposal(row)
        if any(chosen.get(flag) is True for flag in FLAGS):
            records[row['id']] = chosen
    for row in store.db.execute('SELECT * FROM decision_intents'):
        chosen = json.loads(row['payload'])['proposal']
        if any(chosen.get(flag) is True for flag in FLAGS):
            records[row['id']] = {**chosen, 'id': row['id'], 'message_id': row['message_id'],
                                  '_decision_event_ids': json.loads(row['payload']).get('event_ids', ())}
    reason = 'A prior human decision governs this Calendar event; explicitly approve this new change.'
    source_events = None
    for chosen in records.values():
        known_ids = chosen.get('_decision_event_ids')
        if known_ids:
            if target['id'] in known_ids:
                return reason
            continue
        if chosen.get('event_id'):
            if chosen['event_id'] == target['id']:
                return reason
            continue
        if chosen['id'] == target['proposal_id']:
            return reason
        if chosen.get('kind') != 'create' or chosen.get('message_id') != target['message_id']:
            continue
        if source_events is None:
            source_events = list(store.db.execute('SELECT * FROM events WHERE message_id=? AND calendar_id=?',
                                                 (target['message_id'], store.calendar_id)))
        linked = {event['id'] for event in source_events if event['proposal_id'] == chosen['id']}
        for operation in store.db.execute("SELECT result FROM operations WHERE proposal_id=? AND status='applied'", (chosen['id'],)):
            result = json.loads(operation['result']) if operation['result'] else {}
            if result.get('event_id'):
                linked.add(str(result['event_id']))
        if linked:
            if target['id'] in linked:
                return reason
            continue
        chosen_start = start_key(chosen)
        matches = {event['id'] for event in source_events if chosen_start is not None
                   and start_key(json.loads(event['snapshot'])) == chosen_start}
        if matches:
            if target['id'] in matches:
                return reason
            continue
        return 'Prior human event ownership cannot be matched unambiguously; explicitly review this change.'
    return None


def migrate_decisions(store, source_path, *, allow_legacy_binding=False, verified_calendar=None):
    """Read the old database directly; no export, database copy, or source write."""
    source_path = Path(source_path).expanduser().resolve(strict=True)
    if source_path == store.path.resolve() or os.path.samefile(source_path, store.path):
        raise ValueError('Decision transfer requires distinct databases.')
    info = source_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Decision source must be an owned regular database.')
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Decision source failed integrity verification.')
        settings = dict(source.execute('SELECT key,value FROM settings'))
        events = list(source.execute('SELECT * FROM events'))
        binding, owner_binding = settings.get('calendar_binding'), settings.get('calendar_owner_binding')
        legacy = not binding and not owner_binding
        if legacy:
            probe = verified_calendar or {}
            if (not allow_legacy_binding or not events or not store.config.calendar_owner
                    or probe.get('calendar_id') != store.calendar_id or probe.get('access_role') != 'owner'
                    or any(row['calendar_id'] != store.calendar_id for row in events)):
                raise ValueError('Legacy decision source requires explicit authorization, matching event ownership, and a verified Calendar owner probe.')
        elif binding != store.calendar_id or owner_binding != store.config.calendar_owner:
            raise ValueError('Decision source calendar identity does not match.')
        account = settings.get('librus_account_hash')
        if not account or (store._setting('librus_account_hash') and store._setting('librus_account_hash') != account):
            raise ValueError('Decision source Librus account identity does not match.')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        decisions = {}
        if 'decision_intents' in tables:
            decisions = {row['id']: dict(row) for row in source.execute('SELECT * FROM decision_intents')}
        groups = [dict(row) for row in source.execute("SELECT * FROM proposal_groups WHERE status<>'open'")] if 'proposal_groups' in tables else []
        group_members = [dict(row) for row in source.execute('SELECT * FROM proposal_group_members')] if 'proposal_group_members' in tables else []
        group_ids = {row['id'] for row in groups}
        group_members = [row for row in group_members if row['group_id'] in group_ids]
        proposals = list(source.execute('SELECT * FROM proposals'))
        for row in proposals:
            item = json.loads(row['payload'])
            item.update(id=row['id'], message_id=row['message_id'], status=row['status'])
            if any(item.get(flag) is True for flag in FLAGS):
                previous = json.loads(decisions[row['id']]['payload']) if row['id'] in decisions else {}
                record = {'proposal': minimal_proposal(item), 'signature': previous.get('signature') or intent_signature(item),
                          'event_ids': previous.get('event_ids', [])}
                decisions[row['id']] = {**dict(row), 'payload': json.dumps(record, ensure_ascii=False)}
        for row in decisions.values():
            record = json.loads(row['payload'])
            record['event_ids'] = sorted(set(record.get('event_ids', ()))
                                         | decision_event_ids(source, row['id'], record['proposal'], store.calendar_id))
            row['payload'] = json.dumps(record, ensure_ascii=False)
        if any(row['calendar_id'] != store.calendar_id for row in events):
            raise ValueError('Foreign Calendar event ownership in decision source.')
        unresolved = list(source.execute("SELECT * FROM operations WHERE status IN ('pending','review','inflight','unknown')"))
        # A reviewed operation left behind by user rejection has no external
        # uncertainty; transferring it would falsely report an unresolved write.
        unresolved = [row for row in unresolved if row['attempts'] > 0 or row['status'] in ('inflight', 'unknown')]
        unresolved_ids = {row['proposal_id'] for row in unresolved}
        metadata = list(source.execute("SELECT * FROM metadata_refresh WHERE status IN ('pending','review','inflight','unknown')")) if 'metadata_refresh' in tables else []
        stamp = utcnow()

        def proposal_age(row, status):
            values = dict(row)
            # Decision-only sources have no exact eligibility evidence. Their
            # initialization (or last known record time) is conservative, not a
            # newly invented eligibility time at transfer.
            baseline = values.get('created_at') or settings.get('health_initialized_at') or values['updated_at']
            pending_since = (values.get('pending_since') or baseline) if status == 'pending' else None
            return baseline, pending_since

        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            old_account = store._setting('librus_account_hash')
            if old_account and old_account != account:
                raise ValueError('Target account changed during transfer.')
            store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('librus_account_hash', account))
            for row in decisions.values():
                current = store.db.execute('SELECT payload,source_hash FROM decision_intents WHERE id=?', (row['id'],)).fetchone()
                if current:
                    prior, transferred = json.loads(current['payload']), json.loads(row['payload'])
                    if (current['source_hash'] != row['source_hash']
                            or {k:v for k,v in prior.items() if k != 'event_ids'} != {k:v for k,v in transferred.items() if k != 'event_ids'}):
                        raise ValueError('Target contains a conflicting human decision.')
                    transferred['event_ids'] = sorted(set(prior.get('event_ids', ())) | set(transferred['event_ids']))
                    row['payload'] = json.dumps(transferred, ensure_ascii=False)
                store.db.execute('INSERT INTO decision_intents VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                                 (row['id'], row['message_id'], row['source_hash'], row['payload'], row['updated_at']))
                item = json.loads(row['payload'])['proposal']
                item['status'] = 'applied' if item.get('status') == 'applied' else ('ignored' if item.get('user_rejected') else item.get('status', 'review'))
                created_at, pending_since = proposal_age(row, item['status'])
                store.db.execute('INSERT OR IGNORE INTO proposals(id,message_id,source_hash,payload,status,created_at,updated_at,pending_since) VALUES (?,?,?,?,?,?,?,?)',
                                 (row['id'], row['message_id'], row['source_hash'], json.dumps(item, ensure_ascii=False), item['status'], created_at, row['updated_at'], pending_since))
            for group in groups:
                members = [row for row in group_members if row['group_id'] == group['id']]
                if len(members) < 2 or any(member['proposal_id'] not in decisions for member in members):
                    raise ValueError('Group decision membership is incomplete.')
                prior = store.db.execute('SELECT * FROM proposal_groups WHERE id=?', (group['id'],)).fetchone()
                immutable = ('message_id','source_hash','group_key','status','selected_proposal_id','revision')
                if prior and any(prior[key] != group[key] for key in immutable):
                    raise ValueError('Target contains a conflicting group decision.')
                store.db.execute('INSERT OR IGNORE INTO proposal_groups VALUES (?,?,?,?,?,?,?,?,?,?)',
                    tuple(group[key] if key != 'source_evidence' else '' for key in ('id','message_id','source_hash','group_key','source_evidence','status','selected_proposal_id','revision','created_at','updated_at')))
                for member in members:
                    prior_member = store.db.execute('SELECT group_id,position FROM proposal_group_members WHERE proposal_id=?', (member['proposal_id'],)).fetchone()
                    if prior_member and (prior_member['group_id'] != member['group_id'] or prior_member['position'] != member['position']):
                        raise ValueError('Target contains conflicting group membership.')
                    store.db.execute('INSERT OR IGNORE INTO proposal_group_members VALUES (?,?,?)',
                        (member['group_id'],member['proposal_id'],member['position']))
            for row in events:
                snapshot = json.loads(row['snapshot'])
                if not row['marker'] or not snapshot.get('id') or snapshot['id'] != row['id']:
                    raise ValueError('Decision source ownership lacks an exact event identity.')
                existing = store.db.execute('SELECT marker,message_id FROM events WHERE id=?', (row['id'],)).fetchone()
                if existing and (existing['marker'] != row['marker'] or existing['message_id'] != row['message_id']):
                    raise ValueError('Target contains conflicting event ownership.')
                kept = snapshot if row['proposal_id'] in unresolved_ids else minimal_snapshot(snapshot, row['marker'])
                state = row['state'] if 'state' in row.keys() else 'active'
                store.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                                 (row['id'], row['calendar_id'], row['message_id'], row['proposal_id'], json.dumps(kept, ensure_ascii=False),
                                  row['fingerprint'], row['marker'], row['updated_at'], state))
            for row in unresolved:
                original = next((p for p in proposals if p['id'] == row['proposal_id']), None)
                if not original:
                    raise ValueError('Unresolved operation has no proposal evidence.')
                created_at, pending_since = proposal_age(original, original['status'])
                store.db.execute('INSERT OR IGNORE INTO proposals(id,message_id,source_hash,payload,status,created_at,updated_at,pending_since) VALUES (?,?,?,?,?,?,?,?)',
                                 tuple(original[key] for key in ('id','message_id','source_hash','payload','status'))
                                 + (created_at, original['updated_at'], pending_since))
                existing = store.db.execute('SELECT payload FROM operations WHERE id=?', (row['id'],)).fetchone()
                if existing and existing[0] != row['payload']:
                    raise ValueError('Target contains conflicting operation intent.')
                values = dict(row)
                if values['status'] == 'inflight':
                    values['status'] = 'unknown'
                store.db.execute('INSERT OR IGNORE INTO operations(id,proposal_id,payload,status,attempts,result,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                 tuple(values[key] for key in ('id','proposal_id','payload','status','attempts','result','last_error','created_at','updated_at')))
            for row in metadata:
                existing = store.db.execute('SELECT payload FROM metadata_refresh WHERE id=?', (row['id'],)).fetchone()
                if existing and existing[0] != row['payload']:
                    raise ValueError('Target contains conflicting metadata operation intent.')
                values = dict(row)
                if values['status'] == 'inflight':
                    values['status'] = 'unknown'
                store.db.execute('INSERT OR IGNORE INTO metadata_refresh VALUES (?,?,?,?,?,?,?,?,?,?)',
                    tuple(values[key] for key in ('id','event_id','version','payload','status','attempts','result','last_error','created_at','updated_at')))
            store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('decision_transfer_verified_at', stamp))
            store.db.execute("INSERT OR IGNORE INTO settings VALUES ('rebuild_status','required')")
        items = [json.loads(row['payload'])['proposal'] for row in decisions.values()]
        return {'status': 'migrated', 'decisions': len(items), 'approved': sum(item.get('user_approved') is True for item in items),
                'edited': sum(item.get('user_edited') is True for item in items),
                'rejected': sum(item.get('user_rejected') is True for item in items),
                'events': len(events), 'unresolved_operations': len(unresolved), 'unresolved_metadata_operations': len(metadata),
                'legacy_binding_verified': legacy, 'calendar_writes': 0, 'message_bodies_copied': 0}
    finally:
        source.close()


def prune_state(store, *, days=30, apply=False, current=None):
    # UI decisions use a separate connection. Reserve before reading retention
    # eligibility or proposals so their latest trusted human flags cannot be lost.
    with store.lock:
        if apply:
            store.db.execute('PRAGMA secure_delete=ON')
            with store.db:
                if not store.db.in_transaction:
                    store.db.execute('BEGIN IMMEDIATE')
                return _prune_state_locked(store, days=days, apply=True, current=current)
        with store._group_snapshot():
            return _prune_state_locked(store, days=days, apply=False, current=current)


def _prune_state_locked(store, *, days=30, apply=False, current=None):
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError('Retention must be between one and thirty days.')
    current = current or datetime.now(WARSAW)
    cutoff = current - timedelta(days=days)
    old = []
    for row in store.db.execute('SELECT id,sent_at,status,payload FROM messages'):
        try:
            projected=store.db.execute("SELECT content_since,fetched_at FROM sources WHERE id=? AND kind<>'message'",(row['id'],)).fetchone()
            projection_expired=projected and as_datetime(projected['content_since'] or projected['fetched_at']) < cutoff
            if projection_expired or (row['sent_at'] and as_datetime(row['sent_at']) < cutoff):
                old.append(row)
        except (ValueError, TypeError):
            # Unknown dates may be unresolved evidence; never guess their age.
            continue
    ids = [row['id'] for row in old]
    dirty_ids = [row['id'] for row in old if row['status'] != 'retained' or json.loads(row['payload']).get('text')]
    result = {'status': 'applied' if apply else 'preview', 'days': days, 'messages_pruned': len(dirty_ids),
              'analysis_records_pruned': 0, 'calendar_writes': 0}
    if ids:
        result['analysis_records_pruned'] = sum(store.db.execute('SELECT COUNT(*) FROM analysis_audit WHERE message_id=?', (mid,)).fetchone()[0] for mid in ids)
    result.update(store.prune_source_content(current,apply=False))
    if not apply:
        return result
    with store.lock:
        store.db.execute('PRAGMA secure_delete=ON')
        with store.db:
            for row in old:
                mid = row['id']
                store._retire_open_groups(mid)
                for proposal in store.db.execute('SELECT * FROM proposals WHERE message_id=?', (mid,)).fetchall():
                    item = store._proposal(proposal)
                    remember_decision(store, proposal, item)
                    operation = store.db.execute('SELECT * FROM operations WHERE proposal_id=?', (proposal['id'],)).fetchone()
                    if operation and operation['status'] in ('inflight', 'unknown'):
                        continue
                    if operation and operation['status'] in ('pending', 'review'):
                        # Unwritten old-source operations cannot remain executable
                        # after their supporting quote is removed.
                        store.db.execute("UPDATE operations SET status='review',last_error='Source content retention expired; explicit review required.',updated_at=? WHERE id=?", (utcnow(), operation['id']))
                    item = minimal_proposal(item)
                    # Retire obsolete automatic analysis while retaining future
                    # actions, human choices, ownership and every journal record.
                    try:
                        future = item.get('start') and as_datetime(item['end'] or item['start']) >= current
                    except (ValueError, TypeError, KeyError):
                        future = True
                    linked = store.db.execute('SELECT 1 FROM events WHERE proposal_id=?', (proposal['id'],)).fetchone()
                    grouped = store.db.execute('SELECT 1 FROM proposal_group_members WHERE proposal_id=?', (proposal['id'],)).fetchone()
                    if not operation and not linked and not grouped and not future and not any(item.get(flag) for flag in FLAGS):
                        store.db.execute('DELETE FROM proposals WHERE id=?', (proposal['id'],))
                        continue
                    if item['status'] == 'pending':
                        item.update(status='review', needs_review=True, review_reason='Source content retention expired; explicit review required.')
                    store.db.execute('UPDATE proposals SET payload=?,status=?,pending_since=NULL WHERE id=?', (json.dumps(item, ensure_ascii=False), item['status'], proposal['id']))
                    if operation and (operation['status'] in ('applied', 'superseded')
                                      or (operation['status'] in ('pending', 'review') and operation['attempts'] == 0)):
                        payload = json.loads(operation['payload'])
                        kept = {key: payload.get(key) for key in ('operation_id','proposal_id','message_id','action','calendar_id','marker','event_id','source_message_ids')}
                        result_data = json.loads(operation['result']) if operation['result'] else {}
                        store.db.execute('UPDATE operations SET payload=?,result=?,last_error=NULL WHERE id=?',
                            (json.dumps(kept), json.dumps({key: result_data.get(key) for key in ('status','event_id','fingerprint')}), operation['id']))
                clean = {'id': mid, 'sent_at': row['sent_at'], 'text': '', 'retained': True}
                if mid in dirty_ids:
                    store.db.execute("UPDATE messages SET payload=?,status='retained',last_error=NULL,pending_since=NULL,analysis_metadata=NULL,analyzed_at=NULL WHERE id=?", (json.dumps(clean), mid))
                store.db.execute("UPDATE proposal_groups SET source_evidence='' WHERE message_id=?", (mid,))
                store.db.execute('DELETE FROM analysis_audit WHERE message_id=?', (mid,))
                for event in store.db.execute('SELECT * FROM events WHERE message_id=?', (mid,)).fetchall():
                    if store.db.execute("SELECT 1 FROM operations WHERE proposal_id=? AND status IN ('inflight','unknown')", (event['proposal_id'],)).fetchone():
                        continue
                    store.db.execute('UPDATE events SET snapshot=? WHERE id=?',
                        (json.dumps(minimal_snapshot(json.loads(event['snapshot']), event['marker']), ensure_ascii=False), event['id']))
                    for journal in store.db.execute("SELECT * FROM metadata_refresh WHERE event_id=? AND status IN ('applied','superseded')", (event['id'],)).fetchall():
                        payload = json.loads(journal['payload'])
                        outcome = json.loads(journal['result']) if journal['result'] else {}
                        kept = {key: payload.get(key) for key in ('operation_id','action','calendar_id','marker','event_id',
                            'expected_fingerprint','message_id','proposal_id','source_message_ids','synchronization_started_at')}
                        store.db.execute('UPDATE metadata_refresh SET payload=?,result=?,last_error=NULL WHERE id=?',
                            (json.dumps(kept), json.dumps({key: outcome.get(key) for key in ('status','event_id','fingerprint')}), journal['id']))
            result.update(store.prune_source_content(current,apply=True))
            store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('retention_last_applied_at', utcnow()))
        if dirty_ids or result['analysis_records_pruned'] or result.get('sources_pruned') or result.get('source_previews_pruned') or result.get('source_audits_pruned'):
            # Erase freed SQLite pages too; this is an in-place rewrite, no backup.
            store.db.execute('VACUUM')
    return result


def event_ownership(event):
    text = str(event.get('description') or '')
    markers = set(re.findall(r'librus-calendar:[A-Za-z0-9_-]{8,128}', re.sub(r'\s+', '', text)))
    sources = set(re.findall(r'Źródło\s*:\s*Librus\s*,\s*wiadomość\s+([A-Za-z0-9_-]{1,200})', text, re.IGNORECASE))
    return markers, sources


def calendar_boundaries_verified(snapshot, hint):
    """Reject missing values and midnight projections with an unknown date type."""
    normalized = normalize_event(snapshot)
    start, end = normalized['start'], normalized['end']
    try:
        if start.get('date') and end.get('date'):
            return snapshot.get('all_day') is not False and date.fromisoformat(end['date']) > date.fromisoformat(start['date'])
        if not start.get('dateTime') or not end.get('dateTime'):
            return False
        if snapshot.get('all_day') is True:
            return False
        first, last = (datetime.fromisoformat(value.replace('Z', '+00:00'))
                       for value in (start['dateTime'], end['dateTime']))
        if (first.tzinfo is None) != (last.tzinfo is None):
            return False
        if first.tzinfo is None and first.time() == last.time() == datetime.min.time():
            if not any(type(event.get('all_day')) is bool for event in (snapshot, hint)):
                return False
        # The same hard Warsaw checks also reject a naive DST gap or fold.
        return as_datetime(end['dateTime']) > as_datetime(start['dateTime'])
    except (ValueError, TypeError, OverflowError):
        return False


class RebuildController:
    """All pages and all cached retries; no Calendar write path exists here."""
    def __init__(self, store, client=None, runtime=None, *, client_factory=None, runtime_factory=None,
                 days=30, timeout_seconds=45*60):
        if type(days) is not int or not 1 <= days <= 30:
            raise ValueError('Rebuild history must be between one and thirty days.')
        self.store, self.client, self.runtime = store, client, runtime
        self.client_factory, self.runtime_factory = client_factory, runtime_factory
        self.days, self.timeout_seconds = days, min(float(timeout_seconds), 45*60)

    def run(self, current=None):
        current = current or datetime.now(WARSAW)
        with sync_lock(self.store.path) as acquired:
            if not acquired:
                return {'status': 'already_running', 'calendar_writes': 0}
            result = {'status': 'rebuilt', 'calendar_writes': 0, 'listed': 0, 'fetched': 0, 'analyzed': 0,
                      'fetch_failures': 0, 'analysis_failures': 0, 'events': 0, 'calendar_ambiguities': 0}
            try:
                self.store.set_setting('rebuild_last_attempt_at', utcnow())
                self.store.set_setting('rebuild_status', 'running')
                with execution_deadline(self.timeout_seconds):
                    if self.client_factory:
                        self.client = self.client_factory(lambda stage: None)
                    if self.runtime_factory:
                        self.runtime = self.runtime_factory()
                    if getattr(self.runtime, 'read_only', False) is not True:
                        raise RuntimeFailure('calendar_read_only_required')
                    probe = self.runtime.probe()
                    if not isinstance(probe, dict) or probe.get('calendar_id') != self.store.calendar_id:
                        raise RuntimeFailure('calendar_not_allowed')
                    self._calendar(result)
                    self._messages(result, current)
                    prune_state(self.store, days=self.days, apply=True, current=current)
                    if result['fetch_failures'] or result['analysis_failures']:
                        result['status'] = 'partial'
                    elif result['calendar_ambiguities']:
                        result['status'] = 'review'
                    if result['status'] == 'rebuilt':
                        self.store.set_setting('rebuild_completed_at', utcnow())
                    self.store.set_setting('rebuild_status', result['status'])
            except SyncInterrupted as error:
                result.update(status='interrupted', error_code=error.code)
                self.store.set_setting('rebuild_status', 'interrupted')
            except Exception as error:
                # Never emit an API response, private ID, or message body.
                result.update(status='partial', error_code=getattr(error, 'code', 'rebuild_failed'))
                self.store.set_setting('rebuild_status', 'partial')
            return result

    def _calendar(self, result):
        discovered = self.runtime.discover_events()
        if not isinstance(discovered, list):
            raise RuntimeFailure('calendar_search_incomplete')
        candidates = {str(event['id']): event for event in discovered if isinstance(event, dict) and event.get('id')}
        if len(candidates) != len(discovered):
            raise RuntimeFailure('calendar_search_incomplete')
        # Exact transferred IDs are verified even when connector search omitted
        # a modified description. Ownership is never inferred from a title.
        saved = {event['id']: event for event in self.store.list_events(100000, include_cancelled=True)}
        for event_id, row in saved.items():
            # Search may flatten all-day dates to ambiguous midnight strings.
            # The verified saved snapshot supplies only the temporal kind; the
            # read result still supplies every actual date and time value.
            hint = dict(row['snapshot'])
            discovered_hint = candidates.get(event_id, {})
            if type(discovered_hint.get('all_day')) is bool:
                hint['all_day'] = discovered_hint['all_day']
            candidates[event_id] = hint
        verified, problems = [], {}
        for event_id, hint in candidates.items():
            try:
                snapshot = self.runtime.read_event(event_id, hint)
            except RuntimeFailure as error:
                if error.code == 'calendar_event_not_found' and event_id in saved:
                    problems[event_id] = 'Transferred event deleted or unavailable; ownership retained.'
                    continue
                raise
            if not isinstance(snapshot, dict) or snapshot.get('id') != event_id:
                raise RuntimeFailure('calendar_event_id_mismatch')
            if type(snapshot.get('all_day')) is not bool and type(hint.get('all_day')) is bool:
                snapshot = dict(snapshot, all_day=hint['all_day'])
            markers, sources = event_ownership(snapshot)
            prior = saved.get(event_id)
            if prior and prior['marker'] in markers:
                sources.add(prior['message_id'])
            if len(markers) != 1 or len(sources) != 1 or has_external_attendees(snapshot, self.store.config.calendar_owner):
                problems[event_id] = 'Ambiguous application ownership or source identifiers; review required.'
                continue
            if not calendar_boundaries_verified(snapshot, hint):
                problems[event_id] = 'Calendar dates or temporal kind are missing or ambiguous; prior ownership and boundaries retained.'
                continue
            verified.append((event_id, snapshot, next(iter(markers)), next(iter(sources))))
        marker_counts = Counter(marker for _, _, marker, _ in verified)
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM calendar_discovery_review')
            for event_id, snapshot, marker, mid in verified:
                if marker_counts[marker] > 1:
                    problems[event_id] = 'Multiple Calendar event IDs share one operation marker; review required.'
                    continue
                prior = saved.get(event_id)
                fingerprint = event_fingerprint(snapshot, self.store.config.calendar_owner)
                if prior and prior['fingerprint'] and prior['fingerprint'] != fingerprint:
                    # Keep the pre-edit baseline; future write checks still detect
                    # a manual Calendar change until a user explicitly accepts it.
                    problems[event_id] = 'Manual Calendar edit preserved; explicit review required before updates.'
                    fingerprint = prior['fingerprint']
                pid = prior['proposal_id'] if prior else digest(['discovered-calendar', self.store.calendar_id, event_id, marker])[:32]
                # Read full remote content to verify ownership and its conflict
                # baseline, then retain only the event's operational fields.
                kept_snapshot = minimal_snapshot(snapshot, marker)
                self.store.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot,updated_at=excluded.updated_at,state=excluded.state',
                    (event_id, self.store.calendar_id, mid, pid, json.dumps(kept_snapshot, ensure_ascii=False), fingerprint,
                     marker, utcnow(), 'cancelled' if snapshot.get('status') == 'cancelled' else 'active'))
                result['events'] += 1
            for event_id, reason in problems.items():
                blocking = not reason.startswith('Manual Calendar edit preserved;')
                self.store.db.execute('INSERT INTO calendar_discovery_review VALUES (?,?,?,?)', (digest(event_id), reason, utcnow(), int(blocking)))
            self.store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('calendar_discovery_verified_at', utcnow()))
        result['calendar_ambiguities'] = len(problems)
        result['calendar_blocking_ambiguities'] = sum(not reason.startswith('Manual Calendar edit preserved;') for reason in problems.values())

    def _messages(self, result, current):
        cutoff = current - timedelta(days=self.days)
        page, page_size, seen_pages, seen_ids = 1, 100, set(), set()
        selected = []
        total_seen = 0
        while page <= 10000:
            snippets, total = self.client.messages(limit=page_size, page=page)
            if not isinstance(snippets, list) or len(snippets) > page_size or (total is not None and (type(total) is not int or total < 0)):
                raise RuntimeFailure('discovery_incomplete')
            signature = digest(snippets)
            if snippets and signature in seen_pages:
                raise RuntimeFailure('discovery_incomplete')
            seen_pages.add(signature)
            if not snippets:
                if isinstance(total, int) and total_seen < total:
                    raise RuntimeFailure('discovery_incomplete')
                break
            total_seen += len(snippets)
            result['listed'] += len(snippets)
            in_window = 0
            for snippet in snippets:
                mid = str(snippet.get('messageId') or snippet.get('id') or '').strip()
                if not mid or mid in seen_ids:
                    raise RuntimeFailure('discovery_incomplete')
                seen_ids.add(mid)
                try:
                    recent = not snippet.get('sendDate') or as_datetime(snippet['sendDate']) >= cutoff
                except (ValueError, TypeError):
                    recent = True
                if not recent:
                    continue
                in_window += 1
                self.store.note_discovered(snippet)
                selected.append(mid)
            self.store.set_setting('rebuild_discovery_page', str(page + 1))
            # Complete pagination even if an old page appears in the middle;
            # the connector has no explicit guaranteed chronological contract.
            if isinstance(total, int) and total_seen >= total:
                if total_seen != total:
                    raise RuntimeFailure('discovery_incomplete')
                break
            if len(snippets) < page_size:
                if isinstance(total, int) and total_seen < total:
                    raise RuntimeFailure('discovery_incomplete')
                break
            page += 1
        else:
            raise RuntimeFailure('discovery_incomplete')
        # Re-read all current source hashes, while already analyzed hashes prevent
        # repeat AI calls. A failure queue persists across interrupted runs.
        for mid in selected:
            try:
                self.store.save_message(self.client.message(mid))
                result['fetched'] += 1
            except Exception as error:
                self.store.fail_message(mid, 'fetch', safe_error(error))
                result['fetch_failures'] += 1
        ambiguity = bool(result['calendar_blocking_ambiguities'])
        for message in self.store.analysis_messages(100000):
            try:
                if message.get('sent_at') and as_datetime(message['sent_at']) < cutoff:
                    continue
                self.runtime.last_analysis_metadata = None
                proposals = self.runtime.analyze(message, self.store.list_events(100000))
                if not isinstance(proposals, list) or len(proposals) > 30:
                    raise ValueError('Unexpected analysis result shape.')
                if ambiguity:
                    proposals = [{**item, 'needs_review': True,
                                  'review_reason': 'Calendar discovery has ambiguous ownership; confirm before creating events.'} for item in proposals]
                self.store.save_proposals(message['id'], proposals, current, getattr(self.runtime, 'last_analysis_metadata', None))
                result['analyzed'] += 1
            except Exception as error:
                self.store.fail_message(message['id'], 'analysis', safe_error(error))
                result['analysis_failures'] += 1

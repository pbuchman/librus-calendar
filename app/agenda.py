"""One bounded, tool-free agenda task and its local durable materialization.

The POC accepts an existing AppStore or a SQLite read-only connection. Opening an
AppStore initializes/migrates it, so private production snapshots should use
sqlite3.connect('file:...?...mode=ro', uri=True) directly instead.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
from datetime import date, datetime, timedelta
import json

import jsonschema

from .codex_runtime import SCHEMAS, CodexRuntime, RuntimeFailure, normalize_event, event_fingerprint
from .core import WARSAW, digest, normalize_proposal, utcnow

AGENDA_VERSION = 'simple-agenda-v7-observed-holiday-dates'
AGENDA_MODEL = 'gpt-6.1-sol'
AGENDA_EFFORT = 'high'
# UTF-8 bytes are a conservative upper bound on text tokens. Reserve space for
# output, schema/protocol overhead and instructions; never silently trim input.
CONTEXT_TOKENS = 200_000
OUTPUT_RESERVE_TOKENS = 32_000
OVERHEAD_RESERVE_TOKENS = 8_000
MAX_INPUT_BYTES = CONTEXT_TOKENS - OUTPUT_RESERVE_TOKENS - OVERHEAD_RESERVE_TOKENS
MAX_ROWS = 5_000
ACTION_FIELDS = ('kind', 'title', 'description', 'start', 'end', 'all_day', 'temporal_kind', 'due_at',
                 'activity_scope', 'optional', 'recipient', 'action_identity', 'offer_identity', 'event_id',
                 'needs_review', 'review_reason',
                 'user_approved', 'user_edited', 'user_rejected', 'deletion_approved', 'default_duration',
                 'inventory_only', 'expired_without_authority', 'deferred_unknown_operation',
                 'blocked_by_group_decision', 'blocked_by_offer_group', 'alternative_group_id')
SOURCE_METADATA = ('identity_basis', 'identity_ambiguous', 'event_start', 'event_end', 'event_precision', 'event_dates', 'event_periods', 'window_start', 'window_end',
                   'publication_precision', 'scope', 'served_class_calendar', 'retained',
                   'image_count', 'document_link_count', 'link_count', 'attachment_count')


class AgendaContextOverflow(RuntimeFailure):
    def __init__(self, input_bytes):
        super().__init__('agenda_context_overflow')
        self.input_bytes = input_bytes
        self.input_limit_bytes = MAX_INPUT_BYTES


def _action(payload):
    # Absence means no assertion. Keep explicit all_day=false for timed entries;
    # empty technical/default flags need not repeat on every legacy action.
    result = {key: payload[key] for key in ACTION_FIELDS if key in payload
              and (payload[key] not in (None, '', False) or key == 'all_day')}
    if isinstance(result.get('action_identity'), dict):
        result['action_identity'] = {key: value for key, value in result['action_identity'].items() if key != 'evidence'}
    return result


def _decision(payload):
    # Saved decision timing/content remains authoritative even when the current
    # action was subsequently regenerated. Keep it once, without source copies.
    if isinstance(payload.get('proposal'), dict):
        return {**{key: value for key, value in payload.items() if key != 'proposal'},
                'proposal': _action(payload['proposal'])}
    return payload


def _agenda(tables, calendar_events, owner_email):
    proposals = {row['id']: row for row in tables['proposals']}
    entries = {}
    for row in tables['canonical_actions']:
        proposal = proposals.get(row['proposal_id'])
        payload = {**json.loads(row['payload']), **(json.loads(proposal['payload']) if proposal else {})}
        entries[row['proposal_id']] = {'id': row['id'], 'proposal_id': row['proposal_id'],
            'status': proposal['status'] if proposal else row['status'], 'action': _action(payload),
            'links': [], 'decisions': [], 'calendar': []}
    by_action = {entry['id']: entry for entry in entries.values()}
    by_proposal = dict(entries)
    # Historical proposal revisions join their original canonical action rather
    # than reappearing as new agenda obligations after an update/restart.
    for pid, row in proposals.items():
        payload = json.loads(row['payload'])
        entry = by_action.get(payload.get('canonical_action_id')) or by_action.get(pid) or entries.get(pid)
        if entry is None:
            entry = {'id': pid, 'proposal_id': pid, 'status': row['status'],
                     'action': _action(payload), 'links': [], 'decisions': [], 'calendar': []}
            entries[pid] = entry
            by_action[pid] = entry
        by_proposal[pid] = entry
        if entry['proposal_id'] == pid:
            entry['origin'] = {key: row[key] for key in ('message_id', 'source_hash')}
    for row in tables['action_sources']:
        if row['action_id'] in by_action:
            by_action[row['action_id']]['links'].append({key: row[key] for key in
                ('source_id', 'source_hash', 'source_quote', 'relation')})
    for row in tables['proposals']:
        entry = by_proposal[row['id']]
        if entry['proposal_id'] == row['id']:
            for ref in json.loads(row['payload']).get('agenda_sources', []):
                if not any(link['source_id'] == ref['source_id'] and link['source_hash'] == ref['source_hash'] for link in entry['links']):
                    entry['links'].append({**ref, 'source_quote': '', 'relation': 'supports'})
    for row in tables['canonical_actions'] + tables['proposals']:
        entry = by_action.get(row['id']) if 'proposal_id' in row else by_proposal.get(row['id'])
        payload = json.loads(row['payload'])
        for quote in (payload.get('source_quote'), (payload.get('action_identity') or {}).get('evidence')):
            if entry and quote and quote not in [link['source_quote'] for link in entry['links']]:
                entry.setdefault('legacy_quotes', [])
                if quote not in entry['legacy_quotes']:
                    entry['legacy_quotes'].append(quote)
    for table in ('canonical_decisions', 'decision_intents'):
        for row in tables[table]:
            entry = by_action.get(row['action_id']) if table == 'canonical_decisions' else by_proposal.get(row['id'])
            if entry is None:
                # Unmapped decisions are preserved for the model to avoid reviving
                # rejected legacy obligations; they cannot authorize a write.
                pid = row.get('id') or row['action_id']
                entry = entries.setdefault(pid, {'id': pid, 'proposal_id': pid, 'status': 'decision_only',
                    'action': {}, 'links': [], 'decisions': [], 'calendar': []})
            decision = _decision(json.loads(row['payload']))
            if decision not in entry['decisions']:
                entry['decisions'].append(decision)
    for row in tables['events']:
        eid = row['id']
        if row['state'] == 'active' and eid not in calendar_events:
            raise RuntimeFailure('agenda_calendar_snapshot_incomplete')
        observed = calendar_events.get(eid)
        if observed is not None and (not isinstance(observed, dict) or observed.get('id') != eid):
            raise RuntimeFailure('agenda_calendar_snapshot_invalid')
        entry = by_proposal.get(row['proposal_id']) or next(
            (candidate for candidate in entries.values() if candidate['action'].get('event_id') == eid), None)
        if entry is None:
            entry = entries.setdefault(row['proposal_id'], {'id': row['proposal_id'], 'proposal_id': row['proposal_id'],
                'status': 'inventory_only', 'action': {}, 'links': [], 'decisions': [], 'calendar': []})
        saved = json.loads(row['snapshot'])
        current = normalize_event(observed, owner_email) if observed else None
        entry['calendar'].append({'id': eid, 'state': row['state'], 'marker': row['marker'],
            'saved_fingerprint': row['fingerprint'], 'current': current,
            'externally_changed': (event_fingerprint(observed, owner_email) != row['fingerprint']) if observed else row['state'] == 'active'})
        if not entry['action']:
            normalized = normalize_event(saved, owner_email)
            entry['action'] = {'title': normalized['summary'], 'start': normalized['start'], 'end': normalized['end']}
    return sorted(entries.values(), key=lambda entry: entry['id'])


def assemble_agenda_snapshot(database, calendar_events, *, current=None, owner_email='', enforce_budget=True):
    """Read a coherent full snapshot; Calendar map covers every active saved ID.

    calendar_events is {event_id: freshly_read_event_or_None}. None means an
    observed not-found result, never a failed fetch. Fetch failures must abort the
    caller. No pagination, candidate ranking or historical semantic recovery.
    """
    current = current or datetime.now(WARSAW)
    if current.tzinfo is None or not isinstance(calendar_events, dict):
        raise RuntimeFailure('agenda_snapshot_invalid')
    db = getattr(database, 'db', database)
    lock = getattr(database, 'lock', None)
    def rows(table):
        cursor = db.execute('SELECT * FROM ' + table + ' ORDER BY 1')
        columns = [field[0] for field in cursor.description]
        found = [dict(zip(columns, row)) for row in cursor.fetchmany(MAX_ROWS + 1)]
        if len(found) > MAX_ROWS:
            raise RuntimeFailure('agenda_snapshot_overflow')
        return found
    with lock if lock is not None else nullcontext():
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute('BEGIN')
        try:
            settings = {row['key']: row['value'] for row in rows('settings')}
            context = json.loads(settings.get('source_learner_context', '{}'))
            context.update(pinned_id=settings.get('source_learner_id'),
                           verified=settings.get('source_learner_verified') == 'true')
            if not context.get('verified') or not context.get('id') or context['id'] != context['pinned_id']:
                raise RuntimeFailure('agenda_learner_unverified')
            sources = []
            for row in rows('sources'):
                if row['learner_id'] != context['id']:
                    raise RuntimeFailure('agenda_learner_mismatch')
                payload = json.loads(row['payload'])
                # Every row was checked against the verified snapshot learner;
                # repeating that same ID on each source adds no information.
                source = {key: row[key] for key in ('id', 'kind', 'content_hash', 'status', 'deleted', 'published_at', 'updated_at')}
                source.update({key: payload.get(key, '') for key in ('title', 'sender', 'text')})
                source['metadata'] = {key: value for key, value in (payload.get('metadata') or {}).items() if key in SOURCE_METADATA}
                if row['kind'] == 'message':
                    message = db.execute('SELECT payload FROM messages WHERE id=?', (row['upstream_id'],)).fetchone()
                    count = json.loads(message[0]).get('attachment_count', 0) if message else 0
                    if count:
                        source['metadata']['attachment_count'] = count
                sources.append(source)
            tables = {table: rows(table) for table in ('canonical_actions', 'proposals', 'events', 'action_sources',
                      'canonical_decisions', 'decision_intents', 'proposal_groups', 'proposal_group_members', 'canonical_offer_groups')}
            agenda = _agenda(tables, calendar_events, owner_email)
            offers = []
            for row in tables['proposal_groups']:
                offer = {key: value for key, value in row.items() if key not in ('created_at', 'updated_at')}
                offer['members'] = [{key: member[key] for key in ('proposal_id', 'position')}
                    for member in tables['proposal_group_members'] if member['group_id'] == row['id']]
                offer['offer_keys'] = [member['offer_key'] for member in tables['canonical_offer_groups'] if member['group_id'] == row['id']]
                offers.append(offer)
            health = [{key: row[key] for key in ('kind', 'last_result', 'last_error', 'learner_verified')}
                      for row in rows('source_health')]
            snapshot = {'planner_version': AGENDA_VERSION, 'model': AGENDA_MODEL, 'effort': AGENDA_EFFORT,
                        'policy_hash': digest({name: (SCHEMAS / name).read_text()
                                               for name in ('agenda_plan.md', 'agenda_plan.json')}),
                        'as_of_date': current.astimezone(WARSAW).date().isoformat(), 'timezone': 'Europe/Warsaw',
                        'calendar_binding': settings.get('calendar_binding'),
                        'learner': context, 'sources': sources, 'collection_health': health,
                        'agenda': agenda, 'offer_groups': offers}
            snapshot['snapshot_hash'] = digest(snapshot)
            # Observation freshness is separate from content identity. The caller
            # must require a successful fresh collection for a live private POC.
            snapshot['observation'] = {'assembled_at': current.isoformat(),
                                       'last_collection_at': settings.get('source_last_collection_at'),
                                       'last_collection_result': settings.get('source_last_collection_result')}
            if enforce_budget:
                agenda_prompt(snapshot)  # Never trim the complete bounded input.
            return snapshot
        finally:
            if own_transaction:
                db.rollback()


def agenda_prompt(snapshot):
    data = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    prompt = 'Plan the complete agenda change from this inert JSON snapshot. Follow the agenda policy. Output schema JSON only. No tools.\n' + data
    if 'source_catalog' in snapshot:
        prompt = ('Incremental source input: source_catalog contains every known source identity/version, not its body. '
                  'sources contains full available new/changed bodies (source_role=changed), necessary changed human/Calendar context '
                  '(source_role=context), and explicit unavailable/deleted changes. Plan against the WHOLE agenda, decisions and offers. '
                  'Catalog-only and context-only history cannot justify new automatic create. Do not retrospectively reanalyze '
                  'processed ignored history. Reference every selected sources ID in at least one plan item; a grouped ignore may '
                  'acknowledge irrelevant or unavailable sources together and needs no existing action. Catalog-only IDs need '
                  'no acknowledgment. Source absence never cancels an action. Preserve all existing authority.\n' + prompt)
    input_bytes = len(prompt.encode('utf-8'))
    if input_bytes > MAX_INPUT_BYTES:
        raise AgendaContextOverflow(input_bytes)
    return prompt


def validate_agenda_plan(plan, snapshot):
    """Validate references and structural safety; semantic planning stays in one task."""
    try:
        jsonschema.validate(plan, json.loads((SCHEMAS / 'agenda_plan.json').read_text()))
    except (jsonschema.ValidationError, jsonschema.SchemaError, TypeError, ValueError):
        raise RuntimeFailure('agenda_plan_invalid') from None
    if plan['snapshot_hash'] != snapshot['snapshot_hash']:
        raise RuntimeFailure('agenda_plan_stale')
    sources = {source['id']: source['content_hash'] for source in snapshot.get('source_catalog', snapshot['sources'])}
    supplied_bodies = {source['id'] for source in snapshot['sources']
                       if source.get('body_available', not source.get('deleted') and source.get('status') != 'retained')
                       and source.get('source_role', 'changed') == 'changed'}
    actions = {action['id']: action for action in snapshot['agenda']}
    groups = {group['id'] for group in snapshot['offer_groups']}
    targets = set()
    creates = set()
    for item in plan['items']:
        operation, aid = item['operation'], item['action_id']
        if aid is not None and aid not in actions:
            raise RuntimeFailure('agenda_action_reference_invalid')
        if operation in {'link', 'update'} and aid is None or operation == 'create' and aid is not None:
            raise RuntimeFailure('agenda_action_reference_invalid')
        if aid is not None and operation != 'ignore':
            if aid in targets:
                raise RuntimeFailure('agenda_conflicting_operations')
            targets.add(aid)
        candidates = item['candidate_action_ids']
        if len(set(candidates)) != len(candidates) or any(candidate not in actions for candidate in candidates):
            raise RuntimeFailure('agenda_action_reference_invalid')
        refs = item['sources']
        if not refs or len({ref['source_id'] for ref in refs}) != len(refs) or any(sources.get(ref['source_id']) != ref['source_hash'] for ref in refs):
            raise RuntimeFailure('agenda_source_reference_invalid')
        if operation == 'create':
            if 'source_catalog' in snapshot and not any(ref['source_id'] in supplied_bodies for ref in refs):
                raise RuntimeFailure('agenda_source_body_required')
            identity = digest([item['action'], sorted(refs, key=lambda ref: ref['source_id'])])
            if identity in creates:
                raise RuntimeFailure('agenda_conflicting_operations')
            creates.add(identity)
        if item['offer_group_id'] is not None and item['offer_group_id'] not in groups:
            raise RuntimeFailure('agenda_group_reference_invalid')
        action = item['action']
        if operation in {'create', 'update'} and action is None:
            raise RuntimeFailure('agenda_plan_invalid')
        if operation in {'link', 'ignore'} and action is not None:
            raise RuntimeFailure('agenda_plan_invalid')
        if (operation != 'review' and item['review_change'] is not None
                or operation == 'review' and item['review_change'] is None):
            raise RuntimeFailure('agenda_plan_invalid')
        if item['review_change'] in {'update', 'cancel'} and aid is None:
            raise RuntimeFailure('agenda_action_reference_invalid')
        if action is not None:
            CodexRuntime._validate_analysis_time(action)
        # Extra-school participation is always a human decision, not automatic.
        if operation == 'create' and action['activity_scope'] != 'school':
            raise RuntimeFailure('agenda_review_required')
    if 'source_catalog' in snapshot:
        handled = {ref['source_id'] for item in plan['items'] for ref in item['sources']}
        if not {source['id'] for source in snapshot['sources']} <= handled:
            raise RuntimeFailure('agenda_sources_unprocessed')
    return copy.deepcopy(plan)


def read_calendar(store, runtime):
    """Current managed state only. Failed reads never masquerade as not-found."""
    expected = {row['id']: json.loads(row['snapshot']) for row in
                store.db.execute("SELECT id,snapshot FROM events WHERE state='active' ORDER BY id").fetchall()}
    return runtime.read_events(expected)


def _message(store, source):
    if source['kind'] == 'message':
        return store.get_message(source['upstream_id'])
    from .source_store import source_message
    message = source_message(source)
    hashed = digest(message)
    store.db.execute("INSERT INTO messages(id,payload,payload_hash,status,sent_at,fetched_at) VALUES (?,?,?,'analyzed',?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,payload_hash=excluded.payload_hash,status='analyzed',sent_at=excluded.sent_at,fetched_at=excluded.fetched_at",
                     (message['id'], json.dumps(message, ensure_ascii=False), hashed, message.get('sent_at'), utcnow()))
    return store.get_message(message['id'])


def _holiday_window_shortening(entry, proposed, sources):
    """A bounded observation cannot automatically shorten an owned interval."""
    if not proposed:
        return False
    for source in sources:
        meta = source.get('metadata') or {}
        if not source.get('upstream_id', '').startswith('holiday:') or not meta.get('event_dates'):
            continue
        try:
            first = date.fromisoformat(meta['window_start'])
            last = date.fromisoformat(meta['window_end']) + timedelta(days=1)
            if proposed.get('all_day'):
                new_start = datetime.combine(date.fromisoformat(proposed['start']), datetime.min.time(), tzinfo=WARSAW)
                new_end = (datetime.combine(date.fromisoformat(proposed['end']), datetime.min.time(), tzinfo=WARSAW)
                           if proposed.get('end') else new_start + timedelta(days=1))
            else:
                new_start = datetime.fromisoformat(proposed['start'].replace('Z', '+00:00'))
                new_end = datetime.fromisoformat(proposed['end'].replace('Z', '+00:00')) if proposed.get('end') else new_start
                if new_start.tzinfo is None or new_end.tzinfo is None:
                    raise ValueError('Timed holiday boundaries require a timezone.')
            for event in entry['calendar']:
                current = event.get('current') or {}
                if event['state'] != 'active' or not current.get('start', {}).get('date') or not current.get('end', {}).get('date'):
                    continue
                old_start = date.fromisoformat(current['start']['date'])
                old_end = date.fromisoformat(current['end']['date'])
                old_first = datetime.combine(old_start, datetime.min.time(), tzinfo=WARSAW)
                old_last = datetime.combine(old_end, datetime.min.time(), tzinfo=WARSAW)
                if old_start < first and new_start > old_first or old_end > last and new_end < old_last:
                    return True
        except (KeyError, TypeError, ValueError):
            # Malformed holiday/window authority cannot admit an automatic change.
            return True
    return False


def _protected(entry):
    flags = ('user_approved', 'user_edited', 'user_rejected', 'deletion_approved')
    return (any(entry['action'].get(flag) for flag in flags) or bool(entry['decisions']))


def _rejected(entry):
    return entry['action'].get('user_rejected') or any(
        (decision.get('proposal') or decision).get('user_rejected') for decision in entry['decisions'])


def _prepare_item(store, snapshot, item, plan_id, index, current):
    """Convert one validated semantic decision to existing proposal vocabulary."""
    item = copy.deepcopy(item)
    entries = {entry['id']: entry for entry in snapshot['agenda']}
    entry = entries.get(item['action_id'])
    if entry and not store.db.execute('SELECT 1 FROM canonical_actions WHERE id=?', (entry['id'],)).fetchone():
        raise RuntimeFailure('agenda_action_reference_invalid')
    if all(store.get_source(ref['source_id'])['kind'] == 'substitution' for ref in item['sources']):
        item.update(operation='ignore', action=None, review_change=None, proposal_id=None,
                    reason='Substitutions are informational and do not create personal Calendar actions.')
        return item, None
    if entry and _rejected(entry) and item['operation'] != 'link':
        item.update(operation='ignore', action=None, review_change=None, reason='Saved rejection retained.')
    group = next((group for group in snapshot['offer_groups'] if group['id'] == item['offer_group_id']), None)
    if entry and group is None:
        group = next((group for group in snapshot['offer_groups']
                      if (group['id'] == entry['action'].get('alternative_group_id')
                          or any(member['proposal_id'] == entry['proposal_id'] for member in group['members']))), None)
        if group:
            item['offer_group_id'] = group['id']
    selected_change = False
    if group and entry and group['status'] == 'selected':
        selected = store.db.execute('SELECT payload FROM proposals WHERE id=?', (group['selected_proposal_id'],)).fetchone()
        selected_aid = (json.loads(selected['payload']).get('canonical_action_id') or group['selected_proposal_id']) if selected else None
        selected_change = (selected_aid == entry['id'] and len([event for event in entry['calendar'] if event['state'] == 'active']) == 1
                           and (item['operation'] == 'update' or item['operation'] == 'review' and item['review_change'] in ('update', 'cancel')))
    if group and group['status'] != 'open' and item['operation'] not in ('link', 'ignore'):
        if selected_change:
            item.update(operation='review', review_change=item['review_change'] or 'update')
        else:
            item.update(operation='ignore', action=None, review_change=None, reason='Saved complete offer decision retained.')
    if item['operation'] in ('link', 'ignore'):
        item['proposal_id'] = entry['proposal_id'] if entry else None
        return item, None
    if entry and item['operation'] == 'review' and item['review_change'] == 'cancel':
        row = store.db.execute('SELECT * FROM proposals WHERE id=?', (entry['proposal_id'],)).fetchone()
        approved = store._proposal(row) if row else {}
        active = [event for event in entry['calendar'] if event['state'] == 'active']
        uncertain = store.db.execute("SELECT 1 FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE (p.id=? OR json_extract(p.payload,'$.canonical_action_id')=?) AND o.status IN ('unknown','inflight') LIMIT 1",
                                     (entry['proposal_id'], entry['id'])).fetchone()
        # A cancellation's executable intent is its exact owned target. Model
        # prose cannot revoke an unchanged, explicitly confirmed pending intent.
        if (approved.get('status') == 'pending' and approved.get('kind') == 'cancel'
                and approved.get('user_approved') and approved.get('deletion_approved')
                and not item['candidate_action_ids'] and not item['new_offer_key']
                and item['offer_group_id'] == approved.get('alternative_group_id')
                and sorted(approved.get('agenda_sources', []), key=lambda ref: ref['source_id'])
                    == sorted(item['sources'], key=lambda ref: ref['source_id'])
                and len(active) == 1 and active[0]['id'] == approved.get('event_id')
                and not active[0]['externally_changed'] and not uncertain
                and not store._source_write_reason(approved)
                and not store._group_write_reason(approved['id'])):
            item.update(operation='link', action=None, review_change=None,
                        proposal_id=approved['id'], status='pending',
                        reason='Unchanged approved pending cancellation retained.')
            return item, None
    reason = item['reason']
    if item['operation'] == 'update' and entry and (_protected(entry) or any(event['externally_changed'] for event in entry['calendar'])):
        item.update(operation='review', review_change='update')
        reason = 'Existing human intent or external Calendar edit requires approval. ' + reason
    if item['operation'] == 'create' and item['candidate_action_ids']:
        item.update(operation='review', review_change='create')
        reason = 'Potential existing duplicate requires review. ' + reason
    refs = item['sources']
    sources = [store.get_source(ref['source_id']) for ref in refs]
    if item['operation'] == 'update' and entry and _holiday_window_shortening(entry, item['action'], sources):
        item.update(operation='review', review_change='update')
        reason = 'Observed holiday window cannot shorten the existing Calendar interval automatically. ' + reason
    if any(source['deleted'] or source['status'] == 'retained' for source in sources):
        item.update(operation='review', review_change=item['review_change'] or ('update' if entry else 'create'))
        reason = 'Supporting current source text is unavailable. ' + reason
    if item['new_offer_key'] or group:
        item.update(operation='review', review_change=item['review_change'] or ('update' if entry else 'create'))
    kind = item['review_change'] if item['operation'] == 'review' else item['operation']
    if kind == 'update' and entry and not entry['calendar']:
        # An unwritten obligation changes locally; it remains a create to Calendar.
        kind = 'create'
    active_events = [event for event in (entry or {}).get('calendar', []) if event['state'] == 'active']
    if kind in ('update', 'cancel') and len(active_events) != 1:
        item.update(operation='review', review_change=kind)
        reason = 'An exact owned Calendar target is required. ' + reason
    raw = dict(item['action'] or (entry or {}).get('action') or {})
    raw.update(kind=kind, confidence='high', source_quote='', needs_review=item['operation'] == 'review', review_reason=reason,
               user_approved=False, user_edited=False, user_rejected=False, deletion_approved=False,
               event_id=active_events[0]['id'] if kind in ('update', 'cancel') and len(active_events) == 1 else None)
    if item['action'] is None:
        raw.update(title=raw.get('title') or 'Termin lub zastosowanie do potwierdzenia', start=raw.get('start'),
                   end=raw.get('end'), all_day=raw.get('all_day', True), temporal_kind=raw.get('temporal_kind', 'event'),
                   due_at=raw.get('due_at'), activity_scope=raw.get('activity_scope', 'unknown'))
    try:
        proposal = normalize_proposal(raw, current)
    except (ValueError, TypeError, OverflowError):
        if item['operation'] != 'review':
            raise RuntimeFailure('agenda_plan_invalid') from None
        proposal = {**raw, 'status': 'review', 'needs_review': True}
    # A valid undated review must stay review, including when its placeholder
    # date is absent/past. It becomes executable only through explicit UI input.
    if item['operation'] == 'review':
        proposal.update(status='review', needs_review=True, review_reason=reason)
    if entry is None and item['operation'] == 'create':
        # Exact equality of the existing normalized schema fields only. Shared
        # source identity alone does not block independent duties in one notice.
        fields = ('title','description','start','end','all_day','temporal_kind','due_at','activity_scope','optional')
        ref_ids = {ref['source_id'] for ref in refs}
        for pending in store.db.execute("SELECT o.id,p.payload FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE o.status IN ('unknown','inflight')"):
            prior = json.loads(pending['payload'])
            prior_refs = {ref['source_id'] for ref in prior.get('agenda_sources', [])} | {prior.get('source_id')}
            if (prior.get('kind') == 'create' and ref_ids & prior_refs
                    and all(proposal.get(field) == prior.get(field) for field in fields)):
                existing = entries.get(prior.get('canonical_action_id') or prior.get('id'))
                if existing is None:
                    raise RuntimeFailure('agenda_action_reference_invalid')
                item.update(operation='link', action=None, review_change=None, action_id=existing['id'],
                            proposal_id=existing['proposal_id'],
                            reason='Identical prior creation retained; reconcile its existing journal. ' + reason)
                return item, None
    aid = item['action_id'] or digest(['agenda-action', plan_id, index])[:32]
    pid = digest(['agenda-proposal', plan_id, index])[:32]
    message = _message(store, sources[0])
    if not message:
        raise RuntimeFailure('agenda_source_reference_invalid')
    proposal.update(id=pid, message_id=message['id'], canonical_action_id=aid,
                    source_id=sources[0]['id'], source_hash=sources[0]['content_hash'], source_kind=sources[0]['kind'],
                    source_version_at=sources[0].get('updated_at') or sources[0].get('published_at'),
                    agenda_plan_id=plan_id, agenda_sources=refs, agenda_message_hash=message['payload_hash'],
                    agenda_learner={'id': snapshot['learner']['id'], 'class_id': snapshot['learner'].get('class_id')})
    if selected_change:
        proposal['alternative_group_id'] = group['id']
    # Unknown/inflight intent cannot be superseded or followed by another create.
    related = ([entry] if entry else []) + [entries[candidate] for candidate in item['candidate_action_ids']]
    for target in related:
        uncertain = store.db.execute("SELECT o.id FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE (p.id=? OR p.id=? OR json_extract(p.payload,'$.canonical_action_id')=?) AND o.status IN ('unknown','inflight')",
                                     (target['proposal_id'], target['id'], target['id'])).fetchone()
        if uncertain:
            proposal.update(status='review', needs_review=True, deferred_unknown_operation=uncertain[0],
                            review_reason='Prior write must be reconciled before this change can be approved.')
            break
    item.update(action_id=aid, proposal_id=pid, reason=reason, status=proposal['status'])
    return item, proposal


def materialize_plan(store, snapshot, plan, calendar, *, current, preview=False, metadata=None, checkpoint=None):
    """Persist plan + local materialization atomically, before existing queue code."""
    validate_agenda_plan(plan, snapshot)
    metadata = {key: value for key, value in (metadata or {}).items() if key in
                ('model','effort','prompt_version','input_bytes','item_count','source_count','catalog_count','request_hash','source_collection_at','sources_refreshed_by_preview')}
    pid = digest(['agenda-preview' if preview else 'agenda-plan', snapshot['snapshot_hash']])[:32]
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        incremental = 'source_catalog' in snapshot
        if incremental:
            from .agenda_incremental import assemble_incremental_snapshot
            live, live_checkpoint = assemble_incremental_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
            bound_fields = ('local_state_hash', 'processed_versions', 'bootstrap_versions', 'authority_versions',
                            'message_versions', 'catalog_count', 'source_count')
            if checkpoint is None or any(checkpoint.get(key) != live_checkpoint.get(key) for key in bound_fields):
                raise RuntimeFailure('agenda_plan_stale')
        else:
            live = assemble_agenda_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner)
        if live['snapshot_hash'] != snapshot['snapshot_hash']:
            raise RuntimeFailure('agenda_plan_stale')
        existing = store.db.execute('SELECT payload,status FROM agenda_plans WHERE id=?', (pid,)).fetchone()
        if existing:
            return {'status': existing['status'], 'plan_id': pid, 'items': json.loads(existing['payload'])['items'], 'reused': True}
        stamp = utcnow()
        if preview:
            payload = copy.deepcopy(plan)
            store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,NULL)',
                             (pid, snapshot['snapshot_hash'], json.dumps(payload, ensure_ascii=False), json.dumps(metadata or {}), 'preview', stamp))
            return {'status': 'preview', 'plan_id': pid, 'items': payload['items'], 'reused': False}
        prepared = [_prepare_item(store, snapshot, item, pid, index, current) for index, item in enumerate(plan['items'])]
        # An exact unknown-intent retention can resolve a model create to an
        # existing ID. Recheck targets after that application-side resolution.
        targets = [item['action_id'] for item, _ in prepared if item['operation'] != 'ignore']
        if len(targets) != len(set(targets)):
            raise RuntimeFailure('agenda_conflicting_operations')
        # Save the application-assigned plan BEFORE materializing any proposal.
        payload = {**plan, 'items': [item for item, proposal in prepared]}
        store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,NULL)',
                         (pid, snapshot['snapshot_hash'], json.dumps(payload, ensure_ascii=False), json.dumps(metadata or {}), 'applied', stamp))
        groups = {}
        for item, proposal in prepared:
            aid = item['action_id']
            if item['operation'] == 'ignore':
                continue
            if item['operation'] == 'link':
                row = store.db.execute('SELECT p.* FROM proposals p JOIN canonical_actions a ON a.proposal_id=p.id WHERE a.id=?', (aid,)).fetchone()
                attempted = store.db.execute("SELECT 1 FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE (p.id=? OR json_extract(p.payload,'$.canonical_action_id')=?) AND (o.attempts>0 OR o.status IN ('unknown','inflight','applied'))", (aid, aid)).fetchone()
                owned = store.db.execute('SELECT 1 FROM events WHERE proposal_id=?', (item['proposal_id'],)).fetchone()
                if row and row['status'] in ('pending', 'review') and not attempted and not owned:
                    # Fresh validated corroboration admits the same unwritten
                    # intent; retain consent, content, IDs and historical links.
                    admission = {'agenda_plan_id': pid, 'agenda_sources': item['sources'],
                                 'agenda_learner': {'id': snapshot['learner']['id'], 'class_id': snapshot['learner'].get('class_id')}}
                    updated = {**json.loads(row['payload']), **admission}
                    store.db.execute('UPDATE proposals SET payload=?,updated_at=? WHERE id=?', (json.dumps(updated, ensure_ascii=False), stamp, row['id']))
                    canonical = store.db.execute('SELECT payload FROM canonical_actions WHERE id=?', (aid,)).fetchone()
                    store.db.execute('UPDATE canonical_actions SET payload=?,updated_at=? WHERE id=?',
                                     (json.dumps({**json.loads(canonical['payload']), **admission}, ensure_ascii=False), stamp, aid))
            if proposal is not None:
                if item['offer_group_id'] and not proposal.get('alternative_group_id'):
                    # Do not mutate membership of an existing complete group.
                    proposal['blocked_by_offer_group'] = item['offer_group_id']
                if item['new_offer_key']:
                    groups.setdefault(item['new_offer_key'], []).append((item, proposal))
                prior = store.db.execute('SELECT proposal_id FROM canonical_actions WHERE id=?', (aid,)).fetchone()
                if prior:
                    store.db.execute("UPDATE proposals SET status='superseded',pending_since=NULL WHERE id=? AND status IN ('pending','review') AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.proposal_id=proposals.id AND (o.attempts>0 OR o.status IN ('unknown','inflight')))", (prior[0],))
                    store.db.execute("UPDATE operations SET status='superseded',updated_at=? WHERE proposal_id=? AND attempts=0 AND status IN ('pending','review')", (stamp, prior[0]))
                store.db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)',
                                 (proposal['id'], proposal['message_id'], proposal['agenda_message_hash'], json.dumps(proposal, ensure_ascii=False), proposal['status'], stamp, stamp, stamp if proposal['status'] == 'pending' else None))
                store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET proposal_id=excluded.proposal_id,payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at',
                                 (aid, proposal['id'], json.dumps(proposal, ensure_ascii=False), proposal['status'], stamp))
            for ref in item['sources']:
                # Historical literal quotes retain their original version. A new
                # plan's exact current supporting versions live in agenda_sources.
                store.db.execute('INSERT OR IGNORE INTO action_sources VALUES (?,?,?,?,?,?)',
                                 (aid, ref['source_id'], ref['source_hash'], '', 'supports', stamp))
        for key, members in groups.items():
            if len(members) < 2 or len({proposal['message_id'] for item, proposal in members}) != 1:
                raise RuntimeFailure('agenda_group_reference_invalid')
            gid = digest(['agenda-offer', pid, key])[:32]
            first = members[0][1]
            store.db.execute("INSERT INTO proposal_groups VALUES (?,?,?,?,?,'open',NULL,1,?,?)",
                             (gid, first['message_id'], first['agenda_message_hash'], key, members[0][0]['reason'], stamp, stamp))
            for position, (item, proposal) in enumerate(members):
                store.db.execute('INSERT INTO proposal_group_members VALUES (?,?,?)', (gid, proposal['id'], position))
        from .agenda_incremental import (assemble_incremental_snapshot, commit_processed_sources,
                                         content_baseline_hash)
        if incremental:
            commit_processed_sources(store, snapshot, checkpoint, pid, stamp)
            _, baseline_contract = assemble_incremental_snapshot(store, calendar, current=current,
                owner_email=store.config.calendar_owner, enforce_budget=False)
            store.db.execute("INSERT OR REPLACE INTO settings VALUES ('agenda_authority_baseline',?)",
                             (json.dumps(baseline_contract['authority_versions'], sort_keys=True),))
        else:
            # Compatibility for explicit full-snapshot local callers. Scope
            # status updates to the supplied source versions here as well.
            for source in snapshot['sources']:
                store.db.execute("UPDATE sources SET status=CASE WHEN status='retained' THEN status WHEN deleted=1 THEN 'deleted' ELSE 'analyzed' END,analyzed_at=?,last_error=NULL WHERE id=? AND content_hash=?", (stamp, source['id'], source['content_hash']))
                row = store.get_source(source['id'])
                if row['kind'] == 'message':
                    store.db.execute("UPDATE messages SET analyzed_hash=payload_hash,status='analyzed',last_error=NULL,pending_since=NULL,analyzed_at=? WHERE id=? AND payload_hash=? AND status<>'retained'", (stamp, row['upstream_id'], source['content_hash']))
        baseline = assemble_agenda_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('agenda_baseline_hash',?)", (content_baseline_hash(baseline),))
        return {'status': 'applied', 'plan_id': pid, 'items': payload['items'], 'reused': False}


class AgendaPlanner:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime
        self.calendar = {}

    def run(self, current=None, *, preview=False):
        current = current or datetime.now(WARSAW)
        try:
            health = {row['kind']: row['last_result'] for row in self.store.db.execute('SELECT * FROM source_health')}
            from .source_store import KINDS
            if self.store._setting('source_last_collection_result') != 'ok' or any(health.get(kind) != 'ok' for kind in KINDS):
                raise RuntimeFailure('agenda_collection_incomplete')
            if (self.store.config.analysis_model, self.store.config.analysis_effort) != (AGENDA_MODEL, AGENDA_EFFORT):
                raise RuntimeFailure('agenda_model_configuration_required')
            self.calendar = read_calendar(self.store, self.runtime)
            from .agenda_incremental import assemble_incremental_snapshot
            snapshot, checkpoint = assemble_incremental_snapshot(self.store, self.calendar, current=current, owner_email=self.store.config.calendar_owner, enforce_budget=False)
            if not preview and not snapshot['sources'] and checkpoint['full_snapshot_hash'] == self.store._setting('agenda_baseline_hash'):
                return {'status': 'unchanged', 'items': [], 'reused': True, 'model_calls': 0}
            prompt = agenda_prompt(snapshot)  # Always preflight before a provider adapter.
            import hashlib
            checkpoint.update(request_hash=hashlib.sha256(prompt.encode('utf-8')).hexdigest(), input_bytes=len(prompt.encode('utf-8')))
            cached = self.store.db.execute("SELECT payload,metadata FROM agenda_plans WHERE input_hash=? AND status='preview' ORDER BY created_at DESC LIMIT 1", (snapshot['snapshot_hash'],)).fetchone()
            if cached:
                plan, metadata = json.loads(cached['payload']), json.loads(cached['metadata'])
                fresh, calls = self.calendar, 0
            elif not snapshot['sources'] and not snapshot['agenda']:
                plan = {'snapshot_hash': snapshot['snapshot_hash'], 'decision_reason': 'Empty verified school agenda.', 'items': []}
                metadata, fresh, calls = {}, self.calendar, 0
            else:
                plan = self.runtime.plan_agenda(snapshot)
                metadata = getattr(self.runtime, 'last_agenda_metadata', None) or {}
                # A Calendar edit during inference invalidates the same input.
                fresh, calls = read_calendar(self.store, self.runtime), 1
            metadata = {**metadata, 'source_count': checkpoint['source_count'],
                        'catalog_count': checkpoint['catalog_count'],
                        'input_bytes': metadata.get('input_bytes', checkpoint['input_bytes']) if cached else checkpoint['input_bytes'],
                        'request_hash': metadata.get('request_hash', checkpoint['request_hash']) if cached else checkpoint['request_hash']}
            result = materialize_plan(self.store, snapshot, plan, fresh, current=current, preview=preview, checkpoint=checkpoint,
                                      metadata={**metadata,
                                                'source_collection_at': snapshot['observation']['last_collection_at'],
                                                'sources_refreshed_by_preview': False} if preview else metadata)
            self.calendar = fresh
            return {**result, 'reused': result['reused'] or bool(cached), 'model_calls': calls,
                    'source_count': checkpoint['source_count'], 'input_bytes': metadata['input_bytes']}
        except Exception as error:
            from .health_state import error_code
            code = error_code(error, 'analysis')
            # Safe aggregate diagnostics only: never persist the private input.
            failure_metadata = ({'input_bytes': error.input_bytes, 'input_limit_bytes': error.input_limit_bytes}
                                if isinstance(error, AgendaContextOverflow) else {})
            with self.store.lock, self.store.db:
                stamp = utcnow()
                self.store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,?)',
                    (digest(['agenda-failed', stamp]), locals().get('snapshot', {}).get('snapshot_hash', ''), '{}', json.dumps(failure_metadata), 'preview_failed' if preview else 'failed', stamp, code))
            raise


def agenda_summary(result):
    """Safe service/CLI output; private items stay in the authenticated store API."""
    summary = {key: result[key] for key in ('status', 'plan_id', 'reused', 'model_calls', 'source_count', 'input_bytes') if key in result}
    summary['counts'] = {operation: sum(item['operation'] == operation for item in result.get('items', []))
                         for operation in ('create', 'link', 'update', 'review', 'ignore')}
    return summary


def checkpoint_before_result(store, calendar, current):
    if calendar is None:
        return False
    snapshot = assemble_agenda_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
    from .agenda_incremental import content_baseline_hash
    return content_baseline_hash(snapshot) == store._setting('agenda_baseline_hash')


def checkpoint_after_result(store, calendar, current, advance, result):
    if calendar is None or result.get('status') != 'applied':
        return
    # Fresh reads cover active entries only. A settled cancellation must use
    # that same absence representation when advancing the own-write baseline.
    if result['snapshot'].get('status') == 'cancelled':
        calendar.pop(result['event_id'], None)
    else:
        calendar[result['event_id']] = result['snapshot']
    if advance:
        snapshot = assemble_agenda_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
        from .agenda_incremental import assemble_incremental_snapshot, content_baseline_hash
        _, checkpoint = assemble_incremental_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('agenda_authority_baseline',?)", (json.dumps(checkpoint['authority_versions'], sort_keys=True),))
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('agenda_baseline_hash',?)", (content_baseline_hash(snapshot),))

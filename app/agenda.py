"""One bounded, tool-free agenda task and its local durable materialization.

The POC accepts an existing AppStore or a SQLite read-only connection. Opening an
AppStore initializes/migrates it, so private production snapshots should use
sqlite3.connect('file:...?...mode=ro', uri=True) directly instead.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
from datetime import date, datetime, timedelta
from itertools import zip_longest
import json

import jsonschema

from .codex_runtime import SCHEMAS, CodexRuntime, RuntimeFailure, normalize_event, event_fingerprint
from .core import REVIEWED_CREATE_WITH_TARGET, WARSAW, digest, normalize_proposal, utcnow
from .incidents import active as incidents_active, checkpoint as incident_checkpoint, emit, observe
from .planner_incidents import CAS_SECTIONS, INPUT_SECTIONS, SCHEMA_PATH_FIELDS, SCHEMA_VALIDATORS, json_type

AGENDA_VERSION = 'simple-agenda-v9-school-reconfirmation'
AGENDA_MODEL = 'gpt-6.1-sol'
AGENDA_EFFORT = 'high'
# Operational limit for the complete UTF-8 request, not a provider token estimate.
# Measure the exact serialized input before calling; never silently trim it.
MAX_INPUT_BYTES = 320_000
MAX_ROWS = 5_000
CATALOG_COLUMNS = ('id', 'kind', 'content_hash', 'deleted', 'retained', 'body_available')
AGENDA_LINK_COLUMNS = ('source_id', 'source_hash', 'source_quote', 'relation')
INPUT_SIZE_SECTIONS = INPUT_SECTIONS
DECISION_FLAGS = ('user_approved', 'user_edited', 'user_rejected', 'deletion_approved')
ACTION_FIELDS = ('kind', 'title', 'description', 'start', 'end', 'all_day', 'temporal_kind', 'due_at',
                 'activity_scope', 'optional', 'recipient', 'action_identity', 'offer_identity', 'event_id',
                 'needs_review', 'review_reason',
                 'user_approved', 'user_edited', 'user_rejected', 'deletion_approved', 'default_duration',
                 'inventory_only', 'expired_without_authority', 'deferred_unknown_operation',
                 'blocked_by_group_decision', 'blocked_by_offer_group', 'alternative_group_id', 'preference_decision')
SOURCE_METADATA = ('identity_basis', 'identity_ambiguous', 'event_start', 'event_end', 'event_precision', 'event_dates', 'event_periods', 'window_start', 'window_end',
                   'publication_precision', 'scope', 'served_class_calendar', 'retained',
                   'image_count', 'document_link_count', 'link_count', 'attachment_count')


class AgendaContextOverflow(RuntimeFailure):
    def __init__(self, input_bytes, wire):
        super().__init__('agenda_context_overflow')
        self.input_bytes = input_bytes
        self.input_limit_bytes = MAX_INPUT_BYTES
        self.section_bytes = _input_section_bytes(input_bytes, wire)
        self.source_count = len(wire.get('sources', []))
        self.catalog_count = len(wire.get('source_catalog', {}).get('rows', []))
        self.agenda_count = len(wire.get('agenda', []))


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
    # Keep history intact, but name the latest canonical authority explicitly.
    # Flags suffice here: full saved facts already appear in decisions/action.
    canonical = {row['action_id']: json.loads(row['payload']) for row in tables['canonical_decisions']}
    intents = {row['id']: json.loads(row['payload']) for row in tables['decision_intents']}
    for entry in entries.values():
        decision = canonical.get(entry['id'], intents.get(entry['proposal_id']))
        if decision is not None:
            authority = decision.get('proposal') or decision
            entry['current_decision'] = {flag: True for flag in DECISION_FLAGS if authority.get(flag) is True}
    for row_position, row in enumerate(tables['events'], 1):
        eid = row['id']
        if row['state'] == 'active' and eid not in calendar_events:
            emit('planner', 'snapshot', 'missing_calendar_row', 'blocked', row_position=row_position)
            raise RuntimeFailure('agenda_calendar_snapshot_incomplete')
        observed = calendar_events.get(eid)
        if observed is not None and (not isinstance(observed, dict) or observed.get('id') != eid):
            emit('planner', 'snapshot', 'invalid_calendar_row', 'blocked', row_position=row_position,
                 received_type=json_type(observed))
            raise RuntimeFailure('agenda_calendar_snapshot_invalid')
        entry = by_proposal.get(row['proposal_id']) or next(
            (candidate for candidate in entries.values() if candidate['action'].get('event_id') == eid), None)
        if entry is None:
            entry = entries.setdefault(row['proposal_id'], {'id': row['proposal_id'], 'proposal_id': row['proposal_id'],
                'status': 'inventory_only', 'action': {}, 'links': [], 'decisions': [], 'calendar': []})
        saved = json.loads(row['snapshot'])
        current = normalize_event(observed, owner_email) if observed else None
        # A Calendar cancellation can keep all the writable content intact.
        # Observe its state here without changing the executor's content fingerprint.
        state = row['state'] if observed is None else ('cancelled' if observed.get('status') == 'cancelled' else 'active')
        externally_changed = (state != row['state'] or event_fingerprint(observed, owner_email) != row['fingerprint']
                              if observed is not None else row['state'] == 'active')
        entry['calendar'].append({'id': eid, 'state': state, 'marker': row['marker'],
            'saved_fingerprint': row['fingerprint'], 'current': current,
            'externally_changed': externally_changed})
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
        emit('planner', 'snapshot', 'invalid_snapshot_input', 'blocked', timezone_present=current.tzinfo is not None,
             calendar_map=isinstance(calendar_events, dict))
        raise RuntimeFailure('agenda_snapshot_invalid')
    db = getattr(database, 'db', database)
    lock = getattr(database, 'lock', None)
    def rows(table):
        cursor = db.execute('SELECT * FROM ' + table + ' ORDER BY 1')
        columns = [field[0] for field in cursor.description]
        found = [dict(zip(columns, row)) for row in cursor.fetchmany(MAX_ROWS + 1)]
        if len(found) > MAX_ROWS:
            emit('planner', 'snapshot', 'row_limit', 'blocked', table=table,
                 row_count=len(found), row_limit=MAX_ROWS)
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
                emit('planner', 'snapshot', 'learner_unverified', 'blocked', verified=context['verified'],
                     learner_present=bool(context.get('id')), pin_matches=context.get('id') == context['pinned_id'])
                raise RuntimeFailure('agenda_learner_unverified')
            sources = []
            for position, row in enumerate(rows('sources'), 1):
                if row['learner_id'] != context['id']:
                    emit('planner', 'snapshot', 'learner_mismatch', 'blocked', source_position=position)
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
            from .preferences import preference_snapshot
            snapshot['preferences'] = preference_snapshot(db)
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


def _input_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _input_section_bytes(input_bytes, wire):
    sizes = {key: len(_input_json(wire[key]).encode('utf-8')) if key in wire else 0
             for key in INPUT_SIZE_SECTIONS}
    return {**sizes, 'envelope': input_bytes - sum(sizes.values())}


def _agenda_wire_snapshot(snapshot):
    """Lossless tables on the wire only; internal snapshots remain dictionaries."""
    if not isinstance(snapshot, dict) or 'agenda_link_columns' in snapshot:
        emit('planner', 'snapshot', 'snapshot_wire_shape', 'blocked', received_type=json_type(snapshot))
        raise RuntimeFailure('agenda_snapshot_invalid')

    def rows(value, columns, reason, **positions):
        keys = set(columns)
        def invalid(actual, position):
            key = 'ref_position' if 'row_position' in positions else 'row_position'
            actual_keys = actual.keys() if type(actual) is dict else set()
            emit('planner', 'snapshot', reason, 'blocked', **positions,
                 **{key: position}, received_type=json_type(actual), missing_fields=sorted(keys - actual_keys),
                 unexpected_field_count=len(actual_keys - keys))
            raise RuntimeFailure('agenda_snapshot_invalid')

        if not isinstance(value, list):
            invalid(value, 0)
        result = []
        for position, row in enumerate(value, 1):
            if not isinstance(row, dict) or set(row) != keys:
                invalid(row, position)
            result.append([row[key] for key in columns])
        return result

    wire = dict(snapshot)
    if 'source_catalog' in snapshot:
        wire['source_catalog'] = {'columns': list(CATALOG_COLUMNS),
                                  'rows': rows(snapshot['source_catalog'], CATALOG_COLUMNS, 'catalog_wire_shape')}
    if 'agenda' in snapshot:
        if not isinstance(snapshot['agenda'], list):
            emit('planner', 'snapshot', 'agenda_wire_shape', 'blocked', row_position=0,
                 received_type=json_type(snapshot['agenda']))
            raise RuntimeFailure('agenda_snapshot_invalid')
        wire['agenda_link_columns'] = list(AGENDA_LINK_COLUMNS)
        wire['agenda'] = []
        for position, entry in enumerate(snapshot['agenda'], 1):
            if not isinstance(entry, dict):
                emit('planner', 'snapshot', 'agenda_wire_shape', 'blocked', row_position=position,
                     received_type=json_type(entry))
                raise RuntimeFailure('agenda_snapshot_invalid')
            packed = dict(entry)
            if 'links' in entry:
                packed['links'] = rows(entry['links'], AGENDA_LINK_COLUMNS, 'link_wire_shape', row_position=position)
            wire['agenda'].append(packed)
    return wire


def _input_budget_evidence(wire, input_bytes, section_bytes):
    sources = wire.get('sources', [])
    available = [source for source in sources if isinstance(source, dict)
                 and source.get('body_available', bool(source.get('text'))
                                and not source.get('deleted') and source.get('status') != 'retained')]
    context = sum(source.get('source_role') == 'context' for source in available)
    preferences = wire.get('preferences', {})
    examples = preferences.get('examples', []) if isinstance(preferences, dict) else []
    def row_count(name):
        value = wire.get(name)
        return len(value) if isinstance(value, list) else 0
    return dict(input_bytes=input_bytes, limit_bytes=MAX_INPUT_BYTES, section_bytes=section_bytes,
                source_count=len(sources), catalog_count=len(wire.get('source_catalog', {}).get('rows', [])),
                agenda_count=row_count('agenda'), changed_body_count=len(available) - context,
                context_body_count=context, unavailable_count=len(sources) - len(available),
                offer_count=row_count('offer_groups'), preference_example_count=len(examples) if isinstance(examples, list) else 0,
                unsettled_operation_count=row_count('unsettled_operations'))


def agenda_prompt(snapshot, *, report_budget=False):
    wire = _agenda_wire_snapshot(snapshot)
    data = _input_json(wire)
    prompt = ('Plan the complete agenda change from this inert JSON snapshot. Follow the agenda policy. Output schema JSON only. No tools. '
              'Wire tables: source_catalog uses columns/rows; agenda links rows use shared agenda_link_columns. '
              'Map each row positionally to its columns; other data is unchanged.\n' + data)
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
        error = AgendaContextOverflow(input_bytes, wire)
        if incidents_active():
            emit('planner', 'input_budget', 'overflow', 'blocked',
                 **_input_budget_evidence(wire, input_bytes, error.section_bytes))
        raise error
    if report_budget and incidents_active():
        observe('planner', 'input_budget', 'fits', 'accepted',
                **_input_budget_evidence(wire, input_bytes, _input_section_bytes(input_bytes, wire)))
    return prompt


def validate_agenda_plan(plan, snapshot):
    """Validate references and structural safety; semantic planning stays in one task."""
    incident_checkpoint('planner', 'plan_validation')
    try:
        jsonschema.validate(plan, json.loads((SCHEMAS / 'agenda_plan.json').read_text()))
    except jsonschema.ValidationError as error:
        path = [part if type(part) is int or part in SCHEMA_PATH_FIELDS else 'other'
                for part in list(error.absolute_path)[:8]]
        validator = error.validator if error.validator in SCHEMA_VALIDATORS else 'other'
        instance, constraint = error.instance, error.validator_value
        emit('planner', 'plan_validation', 'schema_shape', 'blocked', path=path, validator=validator,
             received_type=json_type(instance), actual_size=len(instance) if isinstance(instance, (list, str, dict)) else None,
             constraint_size=constraint if validator in {'maxLength', 'minLength', 'maxItems', 'minItems'} else None)
        raise RuntimeFailure('agenda_plan_invalid') from None
    except (jsonschema.SchemaError, TypeError, ValueError):
        emit('planner', 'plan_validation', 'schema_definition', 'blocked')
        raise RuntimeFailure('agenda_plan_invalid') from None
    if plan['snapshot_hash'] != snapshot['snapshot_hash']:
        emit('planner', 'plan_validation', 'output_snapshot_mismatch', 'blocked')
        raise RuntimeFailure('agenda_plan_stale')
    sources = {source['id']: source['content_hash'] for source in snapshot.get('source_catalog', snapshot['sources'])}
    supplied_bodies = {source['id'] for source in snapshot['sources']
                       if source.get('body_available', not source.get('deleted') and source.get('status') != 'retained')
                       and source.get('source_role', 'changed') == 'changed'}
    actions = {action['id']: action for action in snapshot['agenda']}
    groups = {group['id'] for group in snapshot['offer_groups']}
    targets = {}
    creates = {}
    for position, item in enumerate(plan['items'], 1):
        operation, aid = item['operation'], item['action_id']
        def invalid(code, reason, **evidence):
            emit('planner', 'plan_validation', reason, 'blocked', item_position=position,
                 item_operation=operation, **evidence)
            raise RuntimeFailure(code)

        if aid is not None and aid not in actions:
            invalid('agenda_action_reference_invalid', 'unknown_action')
        if operation in {'link', 'update'} and aid is None or operation == 'create' and aid is not None:
            invalid('agenda_action_reference_invalid', 'wrong_target_presence')
        if aid is not None and operation != 'ignore':
            if aid in targets:
                invalid('agenda_conflicting_operations', 'duplicate_target', prior_position=targets[aid])
            targets[aid] = position
        candidates = set()
        for ref_position, candidate in enumerate(item['candidate_action_ids'], 1):
            if candidate not in actions:
                invalid('agenda_action_reference_invalid', 'unknown_candidate', ref_position=ref_position)
            if candidate in candidates:
                invalid('agenda_action_reference_invalid', 'duplicate_candidate', ref_position=ref_position)
            candidates.add(candidate)
        refs = item['sources']
        if not refs:
            invalid('agenda_source_reference_invalid', 'no_source_references')
        seen_sources = set()
        for ref_position, ref in enumerate(refs, 1):
            ref_id = ref['source_id']
            if ref_id in seen_sources:
                invalid('agenda_source_reference_invalid', 'duplicate_source', ref_position=ref_position)
            if ref_id not in sources:
                invalid('agenda_source_reference_invalid', 'unknown_source', ref_position=ref_position)
            if sources[ref_id] != ref['source_hash']:
                invalid('agenda_source_reference_invalid', 'source_version_mismatch', ref_position=ref_position)
            seen_sources.add(ref_id)
        if operation == 'create':
            if 'source_catalog' in snapshot and not any(ref['source_id'] in supplied_bodies for ref in refs):
                invalid('agenda_source_body_required', 'available_changed_body_required', ref_count=len(refs))
            identity = digest([item['action'], sorted(refs, key=lambda ref: ref['source_id'])])
            if identity in creates:
                invalid('agenda_conflicting_operations', 'duplicate_create', prior_position=creates[identity])
            creates[identity] = position
        if item['offer_group_id'] is not None and item['offer_group_id'] not in groups:
            invalid('agenda_group_reference_invalid', 'unknown_group')
        action = item['action']
        if (operation in {'create', 'update'} and action is None
                or operation in {'link', 'ignore'} and action is not None):
            invalid('agenda_plan_invalid', 'wrong_action_presence')
        if (operation != 'review' and item['review_change'] is not None
                or operation == 'review' and item['review_change'] is None):
            invalid('agenda_plan_invalid', 'wrong_review_change_presence')
        if item['review_change'] in {'update', 'cancel'} and aid is None:
            invalid('agenda_action_reference_invalid', 'review_target_required')
        if action is not None:
            try:
                CodexRuntime._validate_analysis_time(action)
            except RuntimeFailure:
                emit('planner', 'plan_validation', 'invalid_temporal_fields', 'blocked', item_position=position,
                     item_operation=operation, all_day=action['all_day'], temporal_kind=action['temporal_kind'],
                     end_present=action['end'] is not None, due_present=action['due_at'] is not None)
                raise
        from .preferences import basis_outcome
        preference = basis_outcome(snapshot.get('preferences', {}), item.get('preference_basis'))
        # Unknown applicability never gains authority from a participation choice.
        # Insufficient/conflicting supplied preference evidence becomes review.
        if operation == 'create' and (action['activity_scope'] == 'unknown'
                or action['activity_scope'] != 'school' and preference is None):
            invalid('agenda_review_required', 'review_required', activity_scope=action['activity_scope'])
    if 'source_catalog' in snapshot:
        handled = {ref['source_id'] for item in plan['items'] for ref in item['sources']}
        unhandled = [position for position, source in enumerate(snapshot['sources'], 1) if source['id'] not in handled]
        if unhandled:
            emit('planner', 'plan_validation', 'selected_source_unhandled', 'blocked',
                 source_position=unhandled[0], unhandled_count=len(unhandled))
            raise RuntimeFailure('agenda_sources_unprocessed')
    return copy.deepcopy(plan)


def read_calendar(store, runtime, *, incident_operation='calendar_snapshot'):
    """Current managed state only. Failed reads never masquerade as not-found."""
    incident_checkpoint('planner', incident_operation)
    expected = {row['id']: json.loads(row['snapshot']) for row in
                store.db.execute("SELECT id,snapshot FROM events WHERE state='active' ORDER BY id").fetchall()}
    observe('planner', incident_operation, 'read_started', 'reading', requested_count=len(expected))
    observed = runtime.read_events(expected)
    if incidents_active():
        report = observed if type(observed) is dict else None
        observe('planner', incident_operation, 'read_finished', 'returned', requested_count=len(expected),
                observed_count=len(report) if report is not None else None,
                not_found_count=sum(event is None for event in report.values()) if report is not None else None,
                omitted_count=len(expected.keys() - report.keys()) if report is not None else None)
    return observed


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
    if entry.get('current_decision') is not None:
        return entry['current_decision'].get('user_rejected') is True
    # Older snapshots have no explicit current authority: retain conservative
    # rejection protection rather than guessing historical ordering.
    return entry['action'].get('user_rejected') or any(
        (decision.get('proposal') or decision).get('user_rejected') for decision in entry['decisions'])


def _reconsideration_review(snapshot, entry, item):
    # Semantics (new confirmation versus repetition) belong to the one planner.
    # This gate grants review only, with the exact changed body in this request.
    action = item.get('action') or {}
    if ('source_catalog' not in snapshot or item['operation'] != 'review'
            or item['review_change'] not in ('create', 'update')
            or action.get('activity_scope') != 'school' or entry['calendar']
            or item['candidate_action_ids'] or item['offer_group_id'] or item['new_offer_key']
            or any(entry['action'].get(flag) for flag in ('alternative_group_id', 'blocked_by_offer_group', 'blocked_by_group_decision'))):
        return False
    refs = {(ref['source_id'], ref['source_hash']) for ref in item['sources']}
    return any((source['id'], source['content_hash']) in refs
               and source.get('source_role') == 'changed' and source.get('body_available') is True
               and not source.get('deleted') and source.get('status') != 'retained'
               and bool(source.get('text')) for source in snapshot['sources'])


def _prepare_item(store, snapshot, item, plan_id, index, current):
    """Convert one validated semantic decision to existing proposal vocabulary."""
    item = copy.deepcopy(item)
    def invalid(code, reason):
        emit('planner', 'materialize', reason, 'blocked', item_position=index + 1, item_operation=item['operation'])
        raise RuntimeFailure(code) from None

    entries = {entry['id']: entry for entry in snapshot['agenda']}
    entry = entries.get(item['action_id'])
    if entry and not store.db.execute('SELECT 1 FROM canonical_actions WHERE id=?', (entry['id'],)).fetchone():
        invalid('agenda_action_reference_invalid', 'target_not_canonical')
    if all(store.get_source(ref['source_id'])['kind'] == 'substitution' for ref in item['sources']):
        item.update(operation='ignore', action=None, review_change=None, proposal_id=None,
                    reason='Substitutions are informational and do not create personal Calendar actions.')
        return item, None
    group = next((group for group in snapshot['offer_groups'] if group['id'] == item['offer_group_id']), None)
    if entry and group is None:
        group = next((group for group in snapshot['offer_groups']
                      if (group['id'] == entry['action'].get('alternative_group_id')
                          or any(member['proposal_id'] == entry['proposal_id'] for member in group['members']))), None)
        if group:
            item['offer_group_id'] = group['id']
    if entry and _rejected(entry):
        item['preference_basis'] = None
        if item['operation'] != 'link' and not _reconsideration_review(snapshot, entry, item):
            item.update(operation='ignore', action=None, review_change=None, reason='Saved rejection retained.')
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
            item.update(operation='ignore', action=None, review_change=None, preference_basis=None,
                        reason='Saved complete offer decision retained.')
    from .preferences import basis_outcome
    preference = basis_outcome(snapshot.get('preferences', {}), item.get('preference_basis'))
    if preference and (preference == 'uncertain'
                       or preference == 'accept' and item['operation'] != 'create'
                       or preference == 'reject' and item['operation'] != 'ignore'
                       or item['new_offer_key'] or group and group['status'] == 'open'):
        item.update(operation='review', review_change=item['review_change'] or ('update' if entry else 'create'),
                    reason='Preference evidence does not authorize this action; human review required. ' + item['reason'])
    if item['operation'] in ('link', 'ignore'):
        if preference == 'reject' and item['operation'] == 'ignore':
            item['reason'] = 'Automatic decline from ' + str(len(item['preference_basis']['example_ids'])) + ' independent future human choices. ' + item['reason']
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
    learned_accept = (preference == 'accept' and item['operation'] == 'create'
                      and not entry and not item['candidate_action_ids']
                      and not item['new_offer_key'] and not group)
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
        proposal = normalize_proposal(raw, current, participation_preference=learned_accept)
    except (ValueError, TypeError, OverflowError):
        if item['operation'] != 'review':
            invalid('agenda_plan_invalid', 'normalization_failed')
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
        for pending in store.db.execute(f"SELECT o.id,p.payload FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE o.status IN ('unknown','inflight') OR {REVIEWED_CREATE_WITH_TARGET}"):
            prior = json.loads(pending['payload'])
            prior_refs = {ref['source_id'] for ref in prior.get('agenda_sources', [])} | {prior.get('source_id')}
            if (prior.get('kind') == 'create' and ref_ids & prior_refs
                    and all(proposal.get(field) == prior.get(field) for field in fields)):
                existing = entries.get(prior.get('canonical_action_id') or prior.get('id'))
                if existing is None:
                    invalid('agenda_action_reference_invalid', 'retained_target_missing')
                item.update(operation='link', action=None, review_change=None, action_id=existing['id'],
                            proposal_id=existing['proposal_id'],
                            reason='Identical prior creation retained; reconcile its existing journal. ' + reason)
                return item, None
    aid = item['action_id'] or digest(['agenda-action', plan_id, index])[:32]
    pid = digest(['agenda-proposal', plan_id, index])[:32]
    message = _message(store, sources[0])
    if not message:
        invalid('agenda_source_reference_invalid', 'source_message_missing')
    proposal.update(id=pid, message_id=message['id'], canonical_action_id=aid,
                    source_id=sources[0]['id'], source_hash=sources[0]['content_hash'], source_kind=sources[0]['kind'],
                    source_version_at=sources[0].get('updated_at') or sources[0].get('published_at'),
                    agenda_plan_id=plan_id, agenda_sources=refs, agenda_message_hash=message['payload_hash'],
                    agenda_learner={'id': snapshot['learner']['id'], 'class_id': snapshot['learner'].get('class_id')})
    if learned_accept and proposal['status'] == 'pending':
        from .preferences import action_digest
        proof = {**item['preference_basis'], 'boundary_hash': snapshot['preferences']['boundary_hash'],
                 'action_hash': action_digest(proposal),
                 'examples_hash': digest(snapshot['preferences']['examples'])}
        proposal['preference_decision'] = proof
        item['preference_decision'] = proof
        reason = 'Automatic acceptance from ' + str(len(proof['example_ids'])) + ' independent future human choices. ' + reason
        proposal['review_reason'] = reason
    if selected_change:
        proposal['alternative_group_id'] = group['id']
    # An uncertain write or a known target without verified ownership cannot
    # authorize a second create for the same action.
    related = ([entry] if entry else []) + [entries[candidate] for candidate in item['candidate_action_ids']]
    for target in related:
        uncertain = store.db.execute(f"""SELECT o.id FROM operations o JOIN proposals p ON p.id=o.proposal_id
            WHERE (p.id=? OR p.id=? OR json_extract(p.payload,'$.canonical_action_id')=?)
            AND (o.status IN ('unknown','inflight') OR (? AND {REVIEWED_CREATE_WITH_TARGET}))""",
            (target['proposal_id'], target['id'], target['id'], kind == 'create')).fetchone()
        if uncertain:
            proposal.update(status='review', needs_review=True, deferred_unknown_operation=uncertain[0],
                            review_reason='Prior write must be reconciled before this change can be approved.')
            break
    item.update(action_id=aid, proposal_id=pid, reason=reason, status=proposal['status'])
    return item, proposal


def reject_stale_agenda(*failed_checks, before=None, after=None, source_position=0):
    if incidents_active():
        changed = [key for key in CAS_SECTIONS if before is not None and after is not None
                   and before.get(key) != after.get(key)]
        if 'sources' in changed:
            pairs = zip_longest(before['sources'], after['sources'])
            source_position = next(position for position, (old, new) in enumerate(pairs, 1) if old != new)
        emit('planner', 'materialization_cas', 'stale_snapshot', 'blocked', failed_checks=list(failed_checks),
             changed_sections=changed, source_position=source_position)
    raise RuntimeFailure('agenda_plan_stale')


def materialize_plan(store, snapshot, plan, calendar, *, current, preview=False, metadata=None, checkpoint=None):
    """Persist plan + local materialization atomically, before existing queue code."""
    validate_agenda_plan(plan, snapshot)
    metadata = {key: value for key, value in (metadata or {}).items() if key in
                ('model','effort','prompt_version','input_bytes','item_count','source_count','catalog_count','request_hash','source_collection_at','sources_refreshed_by_preview')}
    pid = digest(['agenda-preview' if preview else 'agenda-plan', snapshot['snapshot_hash']])[:32]
    incident_checkpoint('planner', 'materialize')
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        incremental = 'source_catalog' in snapshot
        if incremental:
            from .agenda_incremental import assemble_incremental_snapshot
            live, live_checkpoint = assemble_incremental_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner, enforce_budget=False)
            bound_fields = ('local_state_hash', 'processed_versions', 'bootstrap_versions', 'authority_versions',
                            'message_versions', 'catalog_count', 'source_count')
            if checkpoint is None:
                reject_stale_agenda('checkpoint_present')
            failed = [key for key in bound_fields if checkpoint.get(key) != live_checkpoint.get(key)]
            if failed:
                reject_stale_agenda(*failed, before=snapshot, after=live)
        else:
            live = assemble_agenda_snapshot(store, calendar, current=current, owner_email=store.config.calendar_owner)
        if live['snapshot_hash'] != snapshot['snapshot_hash']:
            reject_stale_agenda('snapshot_hash', before=snapshot, after=live)
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
        targets = {}
        for position, (item, _) in enumerate(prepared, 1):
            if item['operation'] != 'ignore':
                aid = item['action_id']
                if aid in targets:
                    emit('planner', 'materialize', 'resolved_target_conflict', 'blocked', item_position=position,
                         item_operation=item['operation'], prior_position=targets[aid])
                    raise RuntimeFailure('agenda_conflicting_operations')
                targets[aid] = position
        # Save the application-assigned plan BEFORE materializing any proposal.
        payload = {**plan, 'items': [item for item, proposal in prepared]}
        store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,NULL)',
                         (pid, snapshot['snapshot_hash'], json.dumps(payload, ensure_ascii=False), json.dumps(metadata or {}), 'applied', stamp))
        groups = {}
        body_versions = {(source['id'], source['content_hash']) for source in snapshot['sources']
                         if source.get('text') and not source.get('deleted') and source.get('status') != 'retained'}
        for item, proposal in prepared:
            aid = item['action_id']
            if item['operation'] == 'ignore':
                continue
            if item['operation'] == 'link':
                row = store.db.execute('SELECT p.* FROM proposals p JOIN canonical_actions a ON a.proposal_id=p.id WHERE a.id=?', (aid,)).fetchone()
                attempted = store.db.execute("SELECT 1 FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE (p.id=? OR json_extract(p.payload,'$.canonical_action_id')=?) AND (o.attempts>0 OR o.status IN ('unknown','inflight','applied'))", (aid, aid)).fetchone()
                owned = store.db.execute('SELECT 1 FROM events WHERE proposal_id=?', (item['proposal_id'],)).fetchone()
                linked_versions = {(ref['source_id'], ref['source_hash']) for ref in item['sources']}
                changed = store.changed_approved_message_refs(store._proposal(row)) if row else set()
                if (row and row['status'] in ('pending', 'review') and not attempted and not owned
                        and changed <= linked_versions & body_versions):
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
        for position, (key, members) in enumerate(groups.items(), 1):
            origins = len({proposal['message_id'] for item, proposal in members})
            if len(members) < 2 or origins != 1:
                emit('planner', 'materialize', 'incomplete_offer', 'blocked', group_position=position,
                     member_count=len(members), origin_count=origins)
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
        mode = 'preview' if preview else 'apply'
        phase = 'collection_gate'
        incident_checkpoint('planner', phase)
        try:
            health = {row['kind']: row['last_result'] for row in self.store.db.execute('SELECT * FROM source_health')}
            from .source_store import KINDS
            collection_ok = self.store._setting('source_last_collection_result') == 'ok'
            incomplete_kinds = sorted(kind for kind in KINDS if health.get(kind) != 'ok')
            if not collection_ok or incomplete_kinds:
                emit('planner', phase, 'source_health', 'blocked', collection_ok=collection_ok,
                     incomplete_kinds=incomplete_kinds)
                raise RuntimeFailure('agenda_collection_incomplete')
            phase = 'configuration_gate'
            model_matches = self.store.config.analysis_model == AGENDA_MODEL
            effort_matches = self.store.config.analysis_effort == AGENDA_EFFORT
            if not (model_matches and effort_matches):
                emit('planner', phase, 'model_effort', 'blocked', model_matches=model_matches, effort_matches=effort_matches)
                raise RuntimeFailure('agenda_model_configuration_required')
            phase = 'calendar_snapshot'
            self.calendar = read_calendar(self.store, self.runtime)
            phase = 'snapshot'
            incident_checkpoint('planner', phase)
            from .agenda_incremental import assemble_incremental_snapshot
            snapshot, checkpoint = assemble_incremental_snapshot(self.store, self.calendar, current=current, owner_email=self.store.config.calendar_owner, enforce_budget=False)
            if not preview and not snapshot['sources'] and checkpoint['full_snapshot_hash'] == self.store._setting('agenda_baseline_hash'):
                observe('planner', 'model', 'input_unchanged', 'skipped',
                        catalog_count=checkpoint['catalog_count'], agenda_count=len(snapshot['agenda']))
                return {'status': 'unchanged', 'items': [], 'reused': True, 'model_calls': 0}
            phase = 'input_budget'
            incident_checkpoint('planner', phase)
            prompt = agenda_prompt(snapshot, report_budget=True)  # Always preflight before a provider adapter.
            import hashlib
            checkpoint.update(request_hash=hashlib.sha256(prompt.encode('utf-8')).hexdigest(), input_bytes=len(prompt.encode('utf-8')))
            phase = 'model'
            incident_checkpoint('planner', phase)
            cached = self.store.db.execute("SELECT payload,metadata FROM agenda_plans WHERE input_hash=? AND status='preview' ORDER BY created_at DESC LIMIT 1", (snapshot['snapshot_hash'],)).fetchone()
            if cached:
                plan, metadata = json.loads(cached['payload']), json.loads(cached['metadata'])
                observe('planner', phase, 'preview_reused', 'skipped', item_count=_plan_item_count(plan))
                fresh, calls = self.calendar, 0
            elif not snapshot['sources'] and not snapshot['agenda']:
                plan = {'snapshot_hash': snapshot['snapshot_hash'], 'decision_reason': 'Empty verified school agenda.', 'items': []}
                metadata, fresh, calls = {}, self.calendar, 0
                observe('planner', phase, 'empty_agenda', 'skipped')
            else:
                plan = self.runtime.plan_agenda(snapshot)
                observe('planner', phase, 'plan_returned', 'returned', item_count=_plan_item_count(plan))
                metadata = getattr(self.runtime, 'last_agenda_metadata', None) or {}
                # A Calendar edit during inference invalidates the same input.
                phase = 'calendar_recheck'
                fresh, calls = read_calendar(self.store, self.runtime, incident_operation=phase), 1
            metadata = {**metadata, 'source_count': checkpoint['source_count'],
                        'catalog_count': checkpoint['catalog_count'],
                        'input_bytes': metadata.get('input_bytes', checkpoint['input_bytes']) if cached else checkpoint['input_bytes'],
                        'request_hash': metadata.get('request_hash', checkpoint['request_hash']) if cached else checkpoint['request_hash']}
            phase = 'materialize'
            result = materialize_plan(self.store, snapshot, plan, fresh, current=current, preview=preview, checkpoint=checkpoint,
                                      metadata={**metadata,
                                                'source_collection_at': snapshot['observation']['last_collection_at'],
                                                'sources_refreshed_by_preview': False} if preview else metadata)
            reason, decision = ('plan_reused', 'reused') if result['reused'] else ('plan_committed', 'committed')
            observe('planner', phase, reason, decision, mode=mode, item_count=len(result['items']))
            self.calendar = fresh
            return {**result, 'reused': result['reused'] or bool(cached), 'model_calls': calls,
                    'source_count': checkpoint['source_count'], 'input_bytes': metadata['input_bytes']}
        except Exception as error:
            from .health_state import error_code
            code = error_code(error, 'analysis')
            emit('planner', 'run', 'phase_failed', 'failed', phase=phase, error_code=code, mode=mode)
            # Safe aggregate diagnostics only: never persist the private input.
            failure_metadata = ({'input_bytes': error.input_bytes, 'input_limit_bytes': error.input_limit_bytes,
                                 'section_bytes': error.section_bytes, 'source_count': error.source_count,
                                 'catalog_count': error.catalog_count, 'agenda_count': error.agenda_count}
                                if isinstance(error, AgendaContextOverflow) else {})
            with self.store.lock, self.store.db:
                stamp = utcnow()
                self.store.db.execute('INSERT INTO agenda_plans VALUES (?,?,?,?,?,?,?)',
                    (digest(['agenda-failed', stamp]), locals().get('snapshot', {}).get('snapshot_hash', ''), '{}', json.dumps(failure_metadata), 'preview_failed' if preview else 'failed', stamp, code))
            raise


def _plan_item_count(plan):
    items = plan.get('items') if isinstance(plan, dict) else None
    return len(items) if isinstance(items, list) else 0


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

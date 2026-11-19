"""Closed technical evidence vocabulary for planner and journal incidents."""

from .health_state import ERROR_CODES, SOURCE_KINDS, error_code


PLAN_OPERATIONS = frozenset(('create', 'link', 'update', 'review', 'ignore'))
# Evidence positions are 1-based, with 0 meaning no row. JSON Schema
# paths retain their original 0-based array indices.
WRITE_ACTIONS = frozenset(('create', 'update', 'cancel', 'other'))
SCHEMA_PATH_FIELDS = frozenset((
    'snapshot_hash', 'decision_reason', 'items', 'operation', 'action_id', 'candidate_action_ids',
    'sources', 'reason', 'action', 'review_change', 'offer_group_id', 'new_offer_key',
    'preference_basis', 'source_id', 'source_hash', 'title', 'description', 'start', 'end',
    'all_day', 'temporal_kind', 'due_at', 'activity_scope', 'optional', 'outcome', 'example_ids', 'other',
))
SCHEMA_VALIDATORS = frozenset(('type', 'required', 'additionalProperties', 'maxLength', 'minLength',
                               'pattern', 'maxItems', 'minItems', 'enum', 'other'))
JSON_TYPES = frozenset(('array', 'object', 'string', 'boolean', 'number', 'null', 'other'))
CAS_FIELDS = ('checkpoint_present', 'local_state_hash', 'processed_versions', 'bootstrap_versions',
              'authority_versions', 'message_versions', 'catalog_count', 'source_count', 'snapshot_hash',
              'ledger_contract_versions', 'ledger_contract_hash', 'ledger_source_present', 'ledger_source_version')
INPUT_SECTIONS = ('sources', 'source_catalog', 'agenda', 'offer_groups', 'preferences', 'unsettled_operations')
CAS_SECTIONS = (*INPUT_SECTIONS, 'learner', 'collection_health', 'calendar_binding', 'policy_hash',
                'planner_version', 'model', 'effort', 'as_of_date', 'timezone')
SNAPSHOT_TABLES = frozenset(('settings', 'sources', 'canonical_actions', 'proposals', 'events',
                             'action_sources', 'canonical_decisions', 'decision_intents', 'proposal_groups',
                             'proposal_group_members', 'canonical_offer_groups', 'source_health'))
CALENDAR_ACTIONS = 'action position'
JOURNAL_FIELDS = (CALENDAR_ACTIONS + ' prior_status runtime_status runtime_code result_status journal_status '
                  'journal_checks baseline_advanced')
ITEM = 'item_operation item_position'
REF = ITEM + ' ref_position'
BUDGET_FIELDS = ('input_bytes limit_bytes section_bytes source_count catalog_count agenda_count '
                 'changed_body_count context_body_count unavailable_count offer_count preference_example_count '
                 'unsettled_operation_count')

# Every reason has a single decision and a fixed set of fields. Evidence never
# contains values obtained from the model or provider as unbounded text.
SPECS = {
    'collection_gate': {'source_health': ('blocked', 'collection_ok incomplete_kinds')},
    'configuration_gate': {'model_effort': ('blocked', 'model_matches effort_matches')},
    'calendar_snapshot': {'read_started': ('reading', 'requested_count'),
                          'read_finished': ('returned', 'requested_count observed_count not_found_count omitted_count')},
    'calendar_recheck': {'read_started': ('reading', 'requested_count'),
                         'read_finished': ('returned', 'requested_count observed_count not_found_count omitted_count')},
    'snapshot': {
        'invalid_snapshot_input': ('blocked', 'calendar_map timezone_present'),
        'row_limit': ('blocked', 'table row_count row_limit'),
        'learner_unverified': ('blocked', 'verified learner_present pin_matches'),
        'learner_mismatch': ('blocked', 'source_position'),
        'missing_calendar_row': ('blocked', 'row_position'),
        'invalid_calendar_row': ('blocked', 'row_position received_type'),
        'catalog_wire_shape': ('blocked', 'row_position received_type missing_fields unexpected_field_count'),
        'agenda_wire_shape': ('blocked', 'row_position received_type'),
        'link_wire_shape': ('blocked', 'row_position ref_position received_type missing_fields unexpected_field_count'),
        'snapshot_wire_shape': ('blocked', 'received_type'),
    },
    'input_budget': {'overflow': ('blocked', BUDGET_FIELDS), 'fits': ('accepted', BUDGET_FIELDS)},
    'plan_validation': {
        'schema_shape': ('blocked', 'path validator received_type actual_size constraint_size'),
        'schema_definition': ('blocked', ''),
        'output_snapshot_mismatch': ('blocked', ''),
        'unknown_action': ('blocked', ITEM),
        'wrong_target_presence': ('blocked', ITEM),
        'duplicate_target': ('blocked', ITEM + ' prior_position'),
        'unknown_candidate': ('blocked', REF),
        'duplicate_candidate': ('blocked', REF),
        'no_source_references': ('blocked', ITEM),
        'duplicate_source': ('blocked', REF),
        'unknown_source': ('blocked', REF),
        'source_version_mismatch': ('blocked', REF),
        'available_changed_body_required': ('blocked', ITEM + ' ref_count'),
        'duplicate_create': ('blocked', ITEM + ' prior_position'),
        'unknown_group': ('blocked', ITEM),
        'wrong_action_presence': ('blocked', ITEM),
        'wrong_review_change_presence': ('blocked', ITEM),
        'review_target_required': ('blocked', ITEM),
        'invalid_temporal_fields': ('blocked', ITEM + ' all_day temporal_kind end_present due_present'),
        'review_required': ('blocked', ITEM + ' activity_scope'),
        'selected_source_unhandled': ('blocked', 'source_position unhandled_count'),
    },
    'model': {'preview_reused': ('skipped', 'item_count'), 'empty_agenda': ('skipped', ''),
              'input_unchanged': ('skipped', 'catalog_count agenda_count'), 'plan_returned': ('returned', 'item_count')},
    'materialization_cas': {'stale_snapshot': ('blocked', 'failed_checks changed_sections source_position')},
    'materialize': {
        'target_not_canonical': ('blocked', ITEM),
        'normalization_failed': ('blocked', ITEM),
        'retained_target_missing': ('blocked', ITEM),
        'source_message_missing': ('blocked', ITEM),
        'resolved_target_conflict': ('blocked', ITEM + ' prior_position'),
        'incomplete_offer': ('blocked', 'group_position member_count origin_count'),
        'plan_committed': ('committed', 'mode item_count'),
        'plan_reused': ('reused', 'mode item_count'),
    },
    'run': {'phase_failed': ('failed', 'phase error_code mode')},
    'queue': {'queue_committed': ('committed', 'queued_count'), 'writes_paused': ('skipped', ''),
              'pending_selected': ('selected', 'operation_count limit'),
              'queue_failed': ('failed', 'error_code')},
    'claim': {
        'operation_not_pending': ('skipped', CALENDAR_ACTIONS),
        'proposal_not_pending': ('skipped', CALENDAR_ACTIONS),
        'source_mode_blocked': ('deferred', CALENDAR_ACTIONS),
        'source_write_deferred': ('deferred', CALENDAR_ACTIONS),
        'source_version_changed': ('deferred', CALENDAR_ACTIONS),
        'source_authority_review': ('review', CALENDAR_ACTIONS),
        'offer_decision_review': ('review', CALENDAR_ACTIONS),
        'payload_changed': ('skipped', CALENDAR_ACTIONS),
        'target_binding_changed': ('review', CALENDAR_ACTIONS + ' binding_fields'),
        'human_authority_review': ('review', CALENDAR_ACTIONS),
        'writes_paused': ('skipped', CALENDAR_ACTIONS),
        'conditional_update_missed': ('skipped', CALENDAR_ACTIONS + ' claim_checks'),
        'claimed': ('committed', CALENDAR_ACTIONS),
        'claim_transaction_failed': ('failed', CALENDAR_ACTIONS + ' error_code'),
    },
    'cancel_preflight': {'baseline_changed': ('review', CALENDAR_ACTIONS),
                         'baseline_unverified': ('blocked', CALENDAR_ACTIONS + ' error_code preflight_step'),
                         'baseline_hydrated': ('ready', CALENDAR_ACTIONS)},
    'reconcile': {'unknown_selected': ('selected', 'operation_count limit')},
    'dispatch': {'runtime_returned': ('returned', CALENDAR_ACTIONS + ' dispatch_mode runtime_status'),
                 'runtime_exception': ('unknown', CALENDAR_ACTIONS + ' dispatch_mode error_code')},
    'journal': {
        'invalid_runtime_result': ('committed', JOURNAL_FIELDS),
        'reconcile_retry_blocked': ('committed', JOURNAL_FIELDS),
        'missing_verified_event': ('committed', JOURNAL_FIELDS),
        'cancel_tombstone_mismatch': ('committed', JOURNAL_FIELDS),
        'ownership_or_attendee_mismatch': ('committed', JOURNAL_FIELDS),
        'reconciled': ('committed', JOURNAL_FIELDS),
        'reconcile_requires_review': ('committed', JOURNAL_FIELDS),
        'result_recorded': ('committed', JOURNAL_FIELDS),
        'commit_failed': ('failed', CALENDAR_ACTIONS + ' error_code'),
    },
}
OPERATIONS = frozenset(SPECS)

ENUMS = {
    'action': WRITE_ACTIONS, 'item_operation': PLAN_OPERATIONS, 'received_type': JSON_TYPES,
    'validator': SCHEMA_VALIDATORS, 'table': SNAPSHOT_TABLES, 'error_code': ERROR_CODES,
    'phase': OPERATIONS, 'mode': frozenset(('preview', 'apply')),
    'dispatch_mode': frozenset(('write_intent', 'read_only')),
    'preflight_step': frozenset(('read', 'fingerprint', 'hydrate', 'review_commit')),
    'runtime_status': frozenset(('applied', 'review', 'unknown', 'failed', 'invalid')),
    'runtime_code': ERROR_CODES | {'cancel_audit_snapshot_missing', 'cancelled_event_conflict',
        'delete_verification_failed', 'duplicate_marker', 'event_deleted_or_unavailable', 'event_not_owned',
        'existing_event_changed', 'known_event_marker_missing', 'manual_edit_conflict', 'write_id_missing',
        'write_marker_missing', 'write_not_reconciled', 'write_verification_mismatch', 'none', 'other'},
    'result_status': frozenset(('applied', 'review', 'unknown', 'failed')),
    'prior_status': frozenset(('pending', 'inflight', 'unknown', 'applied', 'review', 'superseded', 'other')),
    'journal_status': frozenset(('pending', 'applied', 'review', 'unknown')),
    'temporal_kind': frozenset(('event', 'deadline', 'reminder')),
    'activity_scope': frozenset(('school', 'extracurricular', 'unknown')),
}
ENUM_LISTS = {'incomplete_kinds': SOURCE_KINDS, 'failed_checks': CAS_FIELDS,
              'changed_sections': CAS_SECTIONS,
              'binding_fields': ('operation_id', 'proposal_id', 'calendar_id', 'action', 'event_id'),
              'missing_fields': ('id', 'kind', 'content_hash', 'deleted', 'retained', 'body_available',
                                 'source_id', 'source_hash', 'source_quote', 'relation'),
              'claim_checks': ('rebuild_clear', 'calendar_discovery_clear'),
              'journal_checks': ('event_id_present', 'snapshot_object', 'result_target_matches',
                                 'snapshot_target_matches', 'cancelled_status', 'attendees_absent',
                                 'ownership_marker_present')}
BOOL_FIELDS = frozenset(('collection_ok', 'model_matches', 'effort_matches', 'all_day', 'end_present',
                         'due_present', 'calendar_map', 'timezone_present', 'verified',
                         'learner_present', 'pin_matches', 'baseline_advanced'))
NULLABLE_COUNTS = frozenset(('actual_size', 'constraint_size', 'observed_count', 'not_found_count', 'omitted_count'))


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def validate_event(operation, reason, decision, evidence):
    if (type(operation) is not str or operation not in SPECS or type(reason) is not str
            or reason not in SPECS[operation] or type(evidence) is not dict):
        raise ValueError('Invalid planner incident.')
    expected_decision, fields = SPECS[operation][reason]
    if decision != expected_decision or set(evidence) != set(fields.split()):
        raise ValueError('Invalid planner incident fields.')
    for key, value in evidence.items():
        if key in ENUMS:
            valid = type(value) is str and value in ENUMS[key]
        elif key in ENUM_LISTS:
            allowed = ENUM_LISTS[key]
            valid = (type(value) is list and len(value) <= len(allowed)
                     and all(type(entry) is str and entry in allowed for entry in value)
                     and len(set(value)) == len(value))
        elif key in BOOL_FIELDS:
            valid = type(value) is bool
        elif key == 'path':
            valid = (type(value) is list and len(value) <= 8
                     and all(_count(part) or type(part) is str and part in SCHEMA_PATH_FIELDS for part in value))
        elif key == 'section_bytes':
            valid = (type(value) is dict and set(value) == {*INPUT_SECTIONS, 'envelope'}
                     and all(_count(size) for size in value.values()))
        else:
            valid = _count(value) or key in NULLABLE_COUNTS and value is None
        if not valid:
            raise ValueError('Invalid planner incident value.')
    return evidence


def json_type(value):
    return {list: 'array', dict: 'object', str: 'string', bool: 'boolean', int: 'number',
            float: 'number', type(None): 'null'}.get(type(value), 'other')


def operation_action(operation):
    action = operation.get('action') if type(operation) is dict else None
    return action if type(action) is str and action in WRITE_ACTIONS else 'other'


def execution_status(result):
    status = result.get('status') if type(result) is dict else None
    return status if type(status) is str and status in ENUMS['result_status'] else 'invalid'


def execution_code(result):
    value = result.get('error') if type(result) is dict else None
    if value is None:
        return 'none'
    return value if type(value) is str and value in ENUMS['runtime_code'] else 'other'


def incident_error_code(error):
    # Adapters may raise arbitrary exceptions. Diagnostic extraction cannot turn
    # a handled runtime exception into an uncaught sync failure.
    try:
        return error_code(error)
    except Exception:
        return 'operation_failed'

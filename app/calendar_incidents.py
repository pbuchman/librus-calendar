"""Structural Calendar/Codex incident evidence. No connector values leave this module."""
from collections import Counter
from itertools import islice

from jsonschema import ValidationError

READ_TOOLS = frozenset({'list_calendars', 'search_events', 'list_events', 'read_event', 'batch_read_event', 'fetch'})
WRITE_TOOLS = frozenset({'create_event', 'update_event', 'delete_event'})
ANALYSIS_OPERATIONS = frozenset({'agenda_plan', 'tool_free'})
OPERATIONS = READ_TOOLS | WRITE_TOOLS | ANALYSIS_OPERATIONS | {'other_tool'}
TOOLS = READ_TOOLS | WRITE_TOOLS
ARGUMENT_FIELDS = frozenset({
    'calendar_id', 'event_id', 'event_ids', 'max_results', 'query', 'timezone_str', 'time_min', 'time_max',
    'next_page_token', 'title', 'description', 'location', 'reminders', 'start_date', 'end_date',
    'start_time', 'end_time', 'attendees', 'visibility', 'guests_can_modify', 'add_google_meet', 'self_attendance',
})
RESPONSE_FIELDS = frozenset({
    'id', 'event', 'events', 'items', 'result', 'responses', 'calendars', 'access_role', 'accessRole',
    'start', 'start_date', 'start_time', 'end', 'end_date', 'end_time', 'status', 'all_day',
    'next_page_token', 'nextPageToken', 'error', 'errors', 'detail',
})
SCHEMA_FIELDS = frozenset({
    'snapshot_hash', 'decision_reason', 'items', 'operation', 'action_id', 'candidate_action_ids', 'sources',
    'reason', 'action', 'review_change', 'offer_group_id', 'new_offer_key', 'preference_basis',
    'source_id', 'source_hash', 'start', 'end', 'all_day', 'due_at', 'title', 'description', 'location',
})
TYPES = frozenset({'object', 'array', 'string', 'boolean', 'integer', 'number', 'null', 'missing', 'other'})
SCHEMA_RULES = frozenset({'type', 'required', 'additionalProperties', 'enum', 'pattern', 'minItems',
                         'maxItems', 'minimum', 'maximum', 'minLength', 'maxLength'})
LIFECYCLES = frozenset({'started', 'updated', 'completed'})
STATUSES = frozenset({'in_progress', 'running', 'pending', 'completed', 'failed', 'cancelled'})
PROBLEMS = frozenset({'record_json', 'record_shape', 'unknown_event', 'item_shape', 'unknown_item',
                      'tool_record', 'error_shape', 'result_shape', 'identity_shape', 'duplicate_lifecycle_id'})
RETRY_TRIGGERS = frozenset({'app_timeout', 'probe_tool_count', 'clean_batch_zero_calls',
                            'possible_write', 'other_failure', 'interrupted'})
FAILURE_CODES = frozenset({
    'calendar_configuration_required', 'calendar_event_id_mismatch', 'calendar_event_missing',
    'calendar_event_not_found', 'calendar_probe_audit_failed', 'calendar_result_unreadable',
    'calendar_search_incomplete', 'calendar_tool_failed', 'calendar_tool_incomplete',
    'calendar_write_access_missing', 'codex_configuration_failed', 'codex_failed', 'codex_limit',
    'codex_permission_denied', 'codex_protocol_invalid', 'codex_timeout', 'codex_unavailable',
    'invalid_analysis_json', 'sync_interrupted', 'sync_timeout', 'unexpected_tool', 'unexpected_tool_arguments',
    'unexpected_tool_count', 'analysis_tool_forbidden', 'agenda_calendar_snapshot_invalid',
    'agenda_calendar_snapshot_incomplete', 'other',
})
NOT_FOUND = frozenset({'not_found_404', 'not_found_410'})
ANONYMOUS_SHAPES = NOT_FOUND | {
    'none', 'unrecognized_fields', 'unrecognized_detail', 'invalid_body_json', 'invalid_body_shape', 'code_mismatch',
    'detail_oversize',
}
LIMIT = 12
ALIAS_LIMIT = LIMIT * 2
COUNT_LIMIT = 1_000_000
MISSING = object()


def value_type(value):
    if value is MISSING:
        return 'missing'
    return {dict: 'object', list: 'array', str: 'string', bool: 'boolean', int: 'integer',
            float: 'number', type(None): 'null'}.get(type(value), 'other')


def field_types(value, allowed):
    return {key: value_type(value[key]) for key in sorted(allowed) if key in value} if isinstance(value, dict) else {}


def count(value):
    return min(value, COUNT_LIMIT)


def failure_code(value):
    return value if isinstance(value, str) and value in FAILURE_CODES else 'other'


def invocation_operation(tool, schema=None):
    if tool is not None:
        return tool if tool in TOOLS else 'other_tool'
    return 'agenda_plan' if schema == 'agenda_plan.json' else 'tool_free'


def request_matches(expected_tool, expected, method, actual):
    """This comparison authorizes the call, independent of incident recording."""
    return (expected_tool is not None and method == 'google_calendar.' + expected_tool,
            isinstance(actual, dict) and actual == expected)

def _different(expected, observed):
    try:
        return expected != observed
    except RecursionError:
        return True


def id_relationship(expected, observed, *, observed_type='array', bindings=None, observed_count=None, aliases=None):
    """Share aliases across one attempt, with relationship counts over the retained sample."""
    bindings = bindings or {}
    aliases = {} if aliases is None else aliases
    tokens = []
    for values in (expected[:LIMIT], observed[:LIMIT]):
        row = []
        for value in values:
            if value is MISSING:
                alias = 'missing'
            elif not isinstance(value, str) or not value:
                alias = 'invalid_id'
            elif value in aliases:
                alias = aliases[value]
            elif len(aliases) == ALIAS_LIMIT:
                alias = 'unaliased'
            else:
                alias = aliases[value] = f'E{len(aliases) + 1}'
            row.append(alias)
        tokens.append(row)
    expected_aliases, observed_aliases = tokens
    for position, alias in bindings.items():
        observed_aliases[position] = alias
    bound = [expected_aliases[position] if alias in NOT_FOUND and position < len(expected_aliases) else alias
             for position, alias in enumerate(observed_aliases)]
    expected_ids = Counter(alias for alias in expected_aliases if alias.startswith('E'))
    observed_ids = Counter(alias for alias in bound if alias.startswith('E'))
    observed_count = len(observed) if observed_count is None else observed_count
    truncated = (len(expected) > LIMIT or observed_count > LIMIT
                 or 'unaliased' in expected_aliases or 'unaliased' in observed_aliases)
    order = ('truncated' if truncated else 'exact' if expected_aliases == bound else
             'permuted' if Counter(expected_aliases) == Counter(bound) else 'different')
    return dict(expected=expected_aliases, observed=observed_aliases, expected_count=count(len(expected)),
                observed_count=count(observed_count), missing_count=len(expected_ids.keys() - observed_ids.keys()),
                extra_count=len(observed_ids.keys() - expected_ids.keys()),
                duplicate_count=sum(quantity - 1 for quantity in observed_ids.values()),
                observed_type=observed_type, order=order)


def request_evidence(expected, method, actual, status, lifecycle, name_ok, args_ok, aliases):
    expected = expected if isinstance(expected, dict) else {}
    actual_type = value_type(actual)
    actual = actual if isinstance(actual, dict) else {}
    expected_fields = field_types(expected, ARGUMENT_FIELDS)
    observed_fields = field_types(actual, ARGUMENT_FIELDS)
    if not isinstance(method, str):
        method_label = value_type(method)
    elif method.startswith('google_calendar.') and method.removeprefix('google_calendar.') in TOOLS:
        method_label = method.removeprefix('google_calendar.')
    else:
        method_label = 'other'
    expected_calendar = expected.get('calendar_id', MISSING)
    calendar = actual.get('calendar_id', MISSING)
    evidence = dict(lifecycle=lifecycle, method=method_label, method_matches=name_ok, arguments_match=args_ok,
                    arguments_type=actual_type, expected_fields=expected_fields, observed_fields=observed_fields,
                    different_fields=sorted(key for key in expected_fields.keys() | observed_fields.keys()
                                            if _different(expected.get(key, MISSING), actual.get(key, MISSING))),
                    unknown_fields=count(len(actual) - len(observed_fields)),
                    calendar=('same' if calendar == expected_calendar else 'missing' if calendar is MISSING
                              else 'different' if isinstance(calendar, str) else 'wrong_type'),
                    status=(status if isinstance(status, str) and status in STATUSES
                            else 'none' if status is None else 'other'))
    for key in ('event_ids', 'event_id'):
        if key not in expected and key not in actual:
            continue
        expected_value, actual_value = expected.get(key, MISSING), actual.get(key, MISSING)
        if key == 'event_id':
            expected_values = [] if expected_value is MISSING else [expected_value]
            actual_values = [] if actual_value is MISSING else [actual_value]
        else:
            expected_values = expected_value if isinstance(expected_value, list) else []
            actual_values = actual_value if isinstance(actual_value, list) else []
        evidence[key] = id_relationship(expected_values, actual_values, observed_type=value_type(actual_value),
                                        aliases=aliases)
    return evidence


def schema_error(error):
    if not isinstance(error, ValidationError):
        return dict(rule='schema', path=[], value_type='other', fields={})
    path = ['index' if type(part) is int else part if part in SCHEMA_FIELDS else 'other'
            for part in islice(error.absolute_path, LIMIT)]
    return dict(rule=error.validator if error.validator in SCHEMA_RULES else 'other',
                path=path, value_type=value_type(error.instance),
                fields=field_types(error.instance, SCHEMA_FIELDS))


def _enum(values):
    return lambda value: type(value) is str and value in values


def _number(value):
    return type(value) is int and 0 <= value <= COUNT_LIMIT


def _boolean(value):
    return type(value) is bool


def _list(predicate, limit=LIMIT):
    return lambda values: type(values) is list and len(values) <= limit and all(predicate(value) for value in values)


def _shape(required, **optional):
    def check(value):
        if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - (required.keys() | optional.keys()):
            return False
        return all((required.get(key) or optional[key])(entry) for key, entry in value.items())
    return check


def _field_types(allowed):
    return lambda value: (type(value) is dict and len(value) <= len(allowed)
                          and all(_enum(allowed)(key) and _enum(TYPES)(entry) for key, entry in value.items()))


_ALIASES = {f'E{index}' for index in range(1, ALIAS_LIMIT + 1)} | NOT_FOUND | {'missing', 'invalid_id', 'unaliased'}
_IDS = _shape({
    'expected': _list(_enum(_ALIASES)), 'observed': _list(_enum(_ALIASES)),
    'expected_count': _number, 'observed_count': _number, 'missing_count': _number,
    'extra_count': _number, 'duplicate_count': _number, 'observed_type': _enum(TYPES),
    'order': _enum({'exact', 'permuted', 'different', 'truncated'}),
})

_CONTRACTS = {
    'request_mismatch': ({'rejected'}, _shape({
        'lifecycle': _enum(LIFECYCLES), 'method': _enum(TOOLS | TYPES), 'method_matches': _boolean,
        'arguments_match': _boolean, 'arguments_type': _enum(TYPES),
        'expected_fields': _field_types(ARGUMENT_FIELDS), 'observed_fields': _field_types(ARGUMENT_FIELDS),
        'different_fields': _list(_enum(ARGUMENT_FIELDS), len(ARGUMENT_FIELDS)),
        'unknown_fields': _number, 'calendar': _enum({'same', 'different', 'missing', 'wrong_type'}),
        'status': _enum(STATUSES | {'none', 'other'}), 'call': _number, 'attempt': _number,
    }, event_ids=_IDS, event_id=_IDS)),
    'request_mismatch_limit': ({'rejected'}, _shape({'omitted': _number})),
    'protocol_invalid': ({'rejected'}, _shape({
        'parse_errors': _number, 'unknown_events': _number, 'started': _number, 'updated': _number,
        'completed': _number, 'problems': _list(_enum(PROBLEMS)), 'possible_write': _boolean,
    })),
    'protocol_count': ({'rejected'}, _shape({
        'started': _number, 'updated': _number, 'completed': _number, 'incomplete': _number,
        'turn_completed': _boolean, 'possible_write': _boolean,
        'final_answer': _enum({'missing', 'multiple', 'exact_ok_true', 'exact_ok_false', 'other'}),
    })),
    'tool_result_rejected': ({'rejected'}, _shape({
        'shape': _enum({'lifecycle_error', 'result_object', 'content_parts', 'text_part', 'text_invalid_json',
                        'text_status_error', 'structured_error', 'calendar_list_shape', 'access_missing',
                        'read_identity', 'read_dto', 'search_event_shape', 'search_page_unproven', 'search_token_invalid'}),
        'code': _enum(FAILURE_CODES),
    }, fields=_field_types(RESPONSE_FIELDS), event_ids=_IDS, payload_type=_enum(TYPES))),
    'batch_rejected': ({'rejected'}, _shape({
        'rule': _enum({'response_count', 'id_shape', 'unknown_id', 'duplicate_id', 'anonymous_order',
                       'unreadable_dto', 'coverage'}),
        'event_ids': _IDS, 'fields': _field_types(RESPONSE_FIELDS),
        'anonymous_shapes': _list(_enum(ANONYMOUS_SHAPES)), 'position': _number,
        'payload_type': _enum(TYPES), 'response_type': _enum(TYPES),
        'not_found_statuses': _list(_shape({
            'position': _number, 'envelope': _enum({'404', '410'}),
            'body': _enum({'404', '410', 'missing', 'wrong_type', 'other'}),
        })),
    })),
    'analysis_missing': ({'rejected'}, _shape({})),
    'analysis_json': ({'rejected'}, _shape({'output_type': _enum(TYPES)})),
    'analysis_schema': ({'rejected'}, _shape({
        'rule': _enum(SCHEMA_RULES | {'other', 'schema'}),
        'path': _list(_enum(SCHEMA_FIELDS | {'index', 'other'})),
        'value_type': _enum(TYPES), 'fields': _field_types(SCHEMA_FIELDS),
    })),
    'analysis_tool_activity': ({'rejected'}, _shape({'activity': _enum({
        'mcp_tool_call', 'command_execution', 'file_change', 'web_search', 'image_generation',
    })})),
    'invocation': ({'retry', 'not_eligible', 'exhausted', 'deadline_exhausted', 'recovered', 'completed_after_retry'},
                   _shape({'trigger': _enum(RETRY_TRIGGERS), 'attempt': lambda value: _number(value) and value in {1, 2},
                           'call': _number, 'code': _enum(FAILURE_CODES), 'started': _number, 'completed': _number,
                           'observed_write': _boolean, 'possible_write': _boolean})),
    'write_dispatch': ({'acknowledged'}, _shape({})),
    'write_verification': ({'applied', 'review', 'unknown'}, _shape({
        'check': _enum({'read_back', 'cancelled', 'not_found', 'delete_still_present', 'returned_id_mismatch',
                        'missing_id', 'marker_missing', 'event_cancelled', 'fingerprint_mismatch',
                        'read_failed', 'dispatch_uncertain'}),
    }, code=_enum(FAILURE_CODES), event_ids=_IDS)),
}


def validate_event(operation, reason, decision, evidence):
    if not _enum(OPERATIONS)(operation) or type(reason) is not str or reason not in _CONTRACTS:
        raise ValueError('Invalid Calendar incident operation or reason')
    decisions, valid = _CONTRACTS[reason]
    if not _enum(decisions)(decision) or not valid(evidence):
        raise ValueError('Invalid Calendar incident decision or evidence')
    if reason.startswith('analysis_') and operation not in ANALYSIS_OPERATIONS:
        raise ValueError('Invalid analysis incident operation')
    if reason.startswith('write_') and operation not in WRITE_TOOLS:
        raise ValueError('Invalid write incident operation')
    if reason == 'batch_rejected' and operation != 'batch_read_event':
        raise ValueError('Invalid batch incident operation')

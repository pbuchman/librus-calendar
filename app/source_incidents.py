"""Closed vocabulary and observations for the Librus collection boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3
from urllib.parse import urlsplit

from .health_state import FORM_DIAGNOSTIC_CODES, SOURCE_ERROR_CODES, structural_evidence


_DECISIONS = {
    'login_entry': {'abort_login'},
    'login_oauth_bootstrap': {'abort_login'},
    'login_submit': {'abort_login'},
    'login_oauth_redirect': {'abort_login'},
    'login_message_session': {'abort_login'},
    'message_list': {'abort_page'},
    'message_body': {'continue_fetch'},
    'message_observation': {'unverified'},
    'message_ingest': {'continue_fetch'},
    'message_discovery': {'abort_page', 'skip_row'},
    'learner_profile': {'abort_collection'},
    'learner_recheck': {'discard_batch'},
    'announcement': {'skip_kind'},
    'calendar_initial': {'skip_kind'},
    'calendar_select': {'skip_kind'},
    'calendar_scan': {'skip_kind'},
    'calendar_detail': {'skip_kind'},
    'calendar_restore': {'restore_failed'},
    'source_ingest': {'reject_batch'},
}
OPERATIONS = frozenset(_DECISIONS)

_REASONS = SOURCE_ERROR_CODES | FORM_DIAGNOSTIC_CODES | {
    'http_failure', 'timeout', 'connection_failure', 'request_failed', 'unexpected_failure', 'database_failure',
    'unexpected_destination', 'redirect_limit', 'response_limit', 'login_redirect_missing', 'login_target_missing',
    'login_target_invalid', 'login_oauth_session_missing', 'login_message_session_missing', 'login_message_access_denied',
    'json_decode_failed', 'json_shape_invalid', 'message_page_shape', 'message_body_shape',
    'message_identity_mismatch', 'message_body_encoding', 'message_identity_missing', 'message_ingest_failed',
    'source_batch_shape', 'source_row_shape',
    'source_identity_invalid', 'source_identity_duplicate', 'source_body_shape', 'source_audience_invalid',
}

_FRAGMENTS = {
    'missing', 'empty', 'other', 'unknown_fields', 'missing_fields', 'conflicting_values',
    'trusted_endpoint', 'unexpected_destination', 'nonredirect_response', 'redirect_response',
    'redirect_location', 'oauth_target', 'oauth_cookie', 'messages_cookie', 'messages_access', 'access_denied',
    'json_object', 'invalid_json', 'array', 'object', 'string', 'number', 'boolean', 'null',
    'array_of_objects', 'not_array', 'item_not_object', 'above_limit',
    'requested_identity', 'other_identity', 'missing_identity', 'stable_identity', 'duplicate_identity',
    'strict_base64', 'invalid_base64', 'wrong_type',
    'learner_profile', 'single_profile_value', 'valid_provider_identity',
    'bound_learner', 'bound_class', 'other_learner', 'other_class', 'context_changed',
    'complete_html_document', 'calendar_form', 'one_calendar_table', 'one_detail_table',
    'full_month', 'incomplete_month', 'unique_calendar_day', 'duplicate_calendar_day',
    'selected_month', 'different_selection', 'available_selection', 'unsupported_selection',
    'matching_publication', 'different_publication', 'valid_calendar_day',
    'announcement_table', 'one_title', 'title_and_sender',
    'valid_date', 'invalid_date', 'single_field_value', 'unpaginated_view', 'pagination_detected',
    'calendar_event_cell', 'unknown_event_cell', 'calendar_detail_action', 'unexpected_detail_action',
    'bounded_batch', 'source_row', 'source_kind', 'unknown_kind', 'source_body', 'valid_audience',
    'invalid_audience', 'sqlite_error', 'trusted_response', 'unexpected_status',
}
_FIELDS = {
    'learner_name', 'learner_class', 'learner_school', 'learner_identity', 'learner_identity_basis',
    'announcement_sender', 'announcement_publication', 'announcement_body',
    'detail_date', 'detail_publication', 'detail_teacher', 'detail_lesson', 'detail_description',
    'detail_subject', 'detail_type', 'detail_scope',
}
_STEPS = {'open_view', 'read_profile', 'read_announcement', 'read_calendar', 'select_month',
          'read_detail', 'verify_learner', 'restore_view'}
_COUNT_FIELDS = {'expected_count', 'observed_count', 'unknown_count', 'limit'}

_current_source = ContextVar('current_source_incident', default=None)


def validate_event(operation, reason, decision, evidence):
    """Reject any evidence outside the finite structural vocabulary."""
    if (not isinstance(operation, str) or operation not in OPERATIONS or
            not isinstance(reason, str) or reason not in _REASONS or
            not isinstance(decision, str) or decision not in _DECISIONS[operation] or
            not isinstance(evidence, dict)):
        raise ValueError('Invalid source incident.')
    for key, value in evidence.items():
        if key in {'expected', 'actual'}:
            valid = isinstance(value, str) and value in _FRAGMENTS
        elif key == 'step':
            valid = isinstance(value, str) and value in _STEPS
        elif key == 'method':
            valid = isinstance(value, str) and value in {'get', 'post', 'other'}
        elif key == 'content_type':
            valid = isinstance(value, str) and value in {'json', 'html', 'plain_text', 'xml', 'other', 'unknown'}
        elif key == 'redirect_category':
            valid = (isinstance(value, str) and value in
                     {'absolute_https', 'absolute_http', 'relative', 'network_path', 'other', 'no_location', 'unknown'})
        elif key == 'redirect_hop':
            valid = type(value) is int and 0 <= value <= 11
        elif key in _COUNT_FIELDS:
            valid = type(value) is int and 0 <= value <= 1000000
        elif key == 'http_status':
            valid = type(value) is int and 100 <= value <= 599
        elif key in {'primary_failed', 'month_matches', 'year_matches'}:
            valid = type(value) is bool
        elif key in {'expected_fields', 'missing_fields', 'observed_fields', 'changed_fields'}:
            valid = (isinstance(value, list) and len(value) <= len(_FIELDS)
                     and all(isinstance(item, str) and item in _FIELDS for item in value)
                     and len(value) == len(set(value)))
        elif key == 'structure':
            structural_evidence(value)
            valid = True
        else:
            valid = False
        if not valid:
            raise ValueError('Invalid source incident evidence.')


def shape(value):
    if value is None:
        return 'null'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    return 'other'


def classified(error, reason, **evidence):
    """Keep the original error contract while attaching bounded diagnostics."""
    error._source_incident = (reason, evidence)
    return error


def _failure(error):
    from requests import exceptions

    incident = getattr(error, '_source_incident', None)
    if incident is not None:
        return incident
    evidence = {}
    if isinstance(error, (TimeoutError, exceptions.Timeout)):
        reason = 'timeout'
    elif isinstance(error, (ConnectionError, exceptions.ConnectionError)):
        reason = 'connection_failure'
    elif isinstance(error, exceptions.HTTPError):
        reason = 'http_failure'
    elif isinstance(error, exceptions.RequestException):
        reason = 'request_failed'
    elif isinstance(error, sqlite3.Error):
        reason = 'database_failure'
    else:
        reason = getattr(error, 'diagnostic_code', None)
        if not isinstance(reason, str) or reason not in _REASONS:
            reason = 'unexpected_failure'
        structure = getattr(error, 'evidence', None)
        if structure is not None:
            evidence['structure'] = structural_evidence(structure)
    status = (getattr(getattr(error, 'response', None), 'status_code', None)
              if isinstance(error, exceptions.HTTPError) else getattr(error, 'http_status', None))
    if type(status) is int and 100 <= status <= 599:
        evidence['http_status'] = status
    return reason, evidence


def preserve_classification(error, original):
    incident = getattr(original, '_source_incident', None)
    if incident is not None:
        error._source_incident = incident
    return error


def note_http_request(method, *, hop=None):
    current = _current_source.get()
    if current is None:
        return
    transport = current[2]
    previous_hop = transport.get('redirect_hop', 0)
    transport.clear()
    transport.update(method={'GET': 'get', 'POST': 'post'}.get(method, 'other'),
                     redirect_hop=previous_hop if hop is None else hop)


def _content_type(value):
    if not isinstance(value, str) or not value:
        return 'unknown'
    media_type = value.partition(';')[0].strip().casefold()
    if media_type == 'application/json' or media_type.endswith('+json'):
        return 'json'
    if media_type in {'text/html', 'application/xhtml+xml'}:
        return 'html'
    if media_type in {'application/xml', 'text/xml'} or media_type.endswith('+xml'):
        return 'xml'
    if media_type == 'text/plain':
        return 'plain_text'
    return 'other'


def _redirect_category(location):
    if not location:
        return 'no_location'
    if not isinstance(location, str):
        return 'unknown'
    try:
        target = urlsplit(location)
    except ValueError:
        return 'other'
    if target.scheme:
        return {'https': 'absolute_https', 'http': 'absolute_http'}.get(target.scheme, 'other')
    return 'network_path' if target.netloc else 'relative'


def note_http_response(response, method):
    current = _current_source.get()
    if current is None:
        return
    transport = current[2]
    try:
        status = getattr(response, 'status_code', None)
        if type(status) is int and 100 <= status <= 599:
            transport['http_status'] = status
        headers = getattr(response, 'headers', None)
        content_type = _content_type(headers.get('Content-Type')) if headers is not None else 'unknown'
        redirect = _redirect_category(headers.get('Location')) if headers is not None else 'unknown'
        transport.update(content_type=content_type, redirect_category=redirect)
        transport.setdefault('method', {'GET': 'get', 'POST': 'post'}.get(method, 'other'))
        transport.setdefault('redirect_hop', 0)
    except Exception:
        pass


@contextmanager
def source_step(operation, decision, **context):
    """Report the innermost source failure without changing normal error handling."""
    from .incidents import active, checkpoint, emit

    if not active():
        yield
        return
    current = _current_source.get()
    if current is not None and operation == 'message_body' and current[0] == 'message_observation':
        yield
        return
    reported = current[1] if current is not None else []
    transport = {}
    token = _current_source.set((operation, reported, transport))
    try:
        checkpoint('source', operation)
        yield
    except Exception as error:
        if not any(previous is error for previous in reported):
            try:
                reason, evidence = _failure(error)
                response = getattr(error, 'response', None)
                if response is not None:
                    note_http_response(response, 'OTHER')
                emit('source', operation, reason, decision, **(context | transport | evidence))
            except Exception:
                pass
            reported.append(error)
        raise
    finally:
        _current_source.reset(token)


def _example_document(content):
    return '<html><body>' + content + '</body></html>'


def _example_table(rows, *, heading='', css_class=''):
    attributes = ' class="' + css_class + '"' if css_class else ''
    title = '<tr><th colspan="2">' + heading + '</th></tr>' if heading else ''
    fields = ''.join('<tr><th>' + label + '</th><td>' + value + '</td></tr>' for label, value in rows)
    return _example_document('<table' + attributes + '>' + title + fields + '</table>')


def fictional_fragment(operation, reason, evidence):
    """Project validated incident categories into bounded, fictional parser input."""
    actual = evidence.get('actual')
    if operation == 'message_list' and reason == 'message_page_shape':
        if actual == 'missing':
            observed = {}
        elif actual == 'above_limit':
            return {'format': 'json', 'expected': {'data': [{}]}, 'observed': {'data': [{}, {}]}, 'limit': 1}
        else:
            examples = {'object': {}, 'null': None, 'string': 'Example', 'number': 1, 'boolean': False}
            if actual not in examples:
                return None
            observed = {'data': examples[actual]}
        return {'format': 'json', 'expected': {'data': []}, 'observed': observed}
    if operation in {'message_body', 'message_observation'}:
        message_id = 'example-requested'
        good = {'messageId': message_id, 'Message': 'RXhhbXBsZQ=='}
        changed = dict(good)
        if reason == 'message_identity_mismatch' and actual == 'other_identity':
            changed['messageId'] = 'example-other'
        elif reason == 'message_identity_mismatch' and actual == 'missing_identity':
            changed.pop('messageId')
        elif reason == 'message_body_encoding' and actual == 'invalid_base64':
            changed['Message'] = '!'
        elif reason == 'message_body_encoding' and actual == 'wrong_type':
            changed['Message'] = False
        elif reason == 'message_body_encoding' and actual == 'missing':
            changed.pop('Message')
        else:
            return None
        return {'format': 'json', 'expected': {'data': good}, 'observed': {'data': changed}, 'message_id': message_id}
    if operation == 'announcement' and reason == 'announcement_markup_invalid' and actual == 'missing_fields':
        fields = {'announcement_sender': ('Dodał', 'Example Teacher'),
                  'announcement_publication': ('Data publikacji', '2030-05-01'),
                  'announcement_body': ('Treść', 'Example announcement')}
        missing = set(evidence.get('missing_fields', ()))
        if not missing or not missing <= fields.keys():
            return None
        remaining = [row for role, row in fields.items() if role not in missing]
        if evidence.get('unknown_count'):
            remaining.append(('Unrecognized field', 'Example value'))
        return {'format': 'html',
                'expected': _example_table(fields.values(), heading='Example title', css_class='printable decorated'),
                'observed': _example_table(remaining, heading='Example title', css_class='printable decorated')}
    if operation in {'learner_profile', 'learner_recheck', 'calendar_scan', 'calendar_restore'} and reason == 'learner_context_unverified' and actual == 'missing_fields':
        fields = {'learner_name': ('Imię i nazwisko ucznia', 'Example Learner'),
                  'learner_class': ('Klasa', '5b'),
                  'learner_identity_basis': ('Nazwa szkoły', 'Example School')}
        missing = set(evidence.get('missing_fields', ()))
        if not missing or not missing <= fields.keys():
            return None
        return {'format': 'html', 'expected': _example_table(fields.values()),
                'observed': _example_table(row for role, row in fields.items() if role not in missing)}
    if reason in {'calendar_form_id', 'calendar_form_method', 'calendar_form_token_missing'}:
        forms = evidence.get('structure', {}).get('forms', ())
        if len(forms) != 1:
            return None
        form = ('<form id="scheduleForm" method="post" action="/terminarz">'
                '<input name="requestkey" type="hidden" value="fictional">'
                '<select name="miesiac"><option value="5" selected>May</option></select>'
                '<select name="rok"><option value="2030" selected>2030</option></select></form>')
        if reason == 'calendar_form_id':
            identity = forms[0]['identity_category']
            if identity not in {'other_id', 'missing_id'}:
                return None
            changed = form.replace(' id="scheduleForm"', ' id="exampleForm"' if identity == 'other_id' else '')
        elif reason == 'calendar_form_method':
            methods = {'get': 'get', 'dialog': 'dialog', 'other': 'example'}
            method = methods.get(forms[0]['method'])
            if method is None:
                return None
            changed = form.replace('method="post"', 'method="' + method + '"')
        else:
            changed = form.replace('value="fictional"', 'value=""')
        return {'format': 'html', 'expected': _example_document(form), 'observed': _example_document(changed)}
    return None

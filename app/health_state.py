"""Technical health metadata; the public reader never initializes or changes a DB."""
from datetime import datetime, timedelta, timezone
import math
import json
from pathlib import Path
from .source_preview import PREVIEW_RETRY_SETTINGS, current_preview_sql, preview_queue_sql
import sqlite3
import re

RESULTS = frozenset(('idle', 'running', 'ok', 'partial', 'failed', 'interrupted', 'paused', 'dry_run'))
COMPLETED_SYNC_RESULTS = frozenset(('ok', 'partial', 'failed', 'interrupted'))
STAGES = frozenset(('starting', 'credentials', 'login', 'runtime', 'calendar_probe', 'discovery',
                    'fetch', 'analysis', 'queue', 'reconcile', 'write', 'complete'))
ERROR_CODES = frozenset(('configuration_required', 'calendar_configuration_required', 'codex_binary_required', 'operation_failed', 'missing_private_file', 'invalid_credentials', 'account_mismatch',
    'login_failed', 'calendar_write_access_missing', 'discovery_failed', 'fetch_failed', 'analysis_failed',
    'discovery_incomplete', 'messages_pending', 'operations_pending', 'operations_unknown',
    'sync_timeout', 'sync_interrupted', 'worker_interrupted', 'codex_timeout', 'codex_unavailable', 'codex_limit', 'codex_failed',
    'calendar_event_not_found', 'calendar_tool_failed', 'calendar_result_unreadable',
    'analysis_tool_forbidden', 'calendar_event_id_mismatch', 'calendar_event_missing', 'calendar_not_allowed',
    'calendar_search_incomplete', 'deletion_requires_approval', 'event_ownership_missing', 'existing_event_required',
    'invalid_analysis_json', 'invalid_analysis_temporal_fields',
    'invalid_event_boundaries', 'invalid_event_duration', 'invalid_operation_marker',
    'message_too_long', 'operation_not_allowed', 'timezone_required', 'unexpected_tool',
    'unexpected_tool_arguments', 'unexpected_tool_count', 'unsafe_event_boundaries', 'unsafe_event_fields'))
ERROR_CODES |= frozenset(('codex_protocol_invalid', 'calendar_tool_incomplete', 'calendar_probe_audit_failed',
    'codex_permission_denied', 'codex_configuration_failed', 'discovery_timeout', 'discovery_http_failed', 'discovery_connection_failed', 'discovery_parsing_failed'))
ERROR_CODES |= frozenset(('agenda_collection_incomplete', 'agenda_context_overflow', 'agenda_snapshot_overflow',
    'agenda_snapshot_invalid', 'agenda_learner_unverified', 'agenda_learner_mismatch',
    'agenda_calendar_snapshot_incomplete', 'agenda_calendar_snapshot_invalid', 'agenda_plan_invalid', 'agenda_plan_stale',
    'agenda_action_reference_invalid', 'agenda_source_reference_invalid', 'agenda_source_body_required', 'agenda_sources_unprocessed', 'agenda_group_reference_invalid',
    'agenda_conflicting_operations', 'agenda_review_required', 'agenda_model_configuration_required', 'agenda_whole_snapshot_required'))
PROBE_RESULTS = frozenset(('running', 'ok', 'failed', 'interrupted'))
PROBE_FIELDS = ('attempt_number', 'model', 'effort', 'cli_version', 'started_at', 'completed_at', 'result',
    'safe_code', 'process_code', 'started_count', 'completed_count', 'parse_error_count', 'unknown_event_count',
    'incomplete_count', 'expected_tool_name', 'expected_tool_arguments')


DIAGNOSTIC_FIELDS = frozenset({'phase', 'state', 'requested_count', 'received_count',
    'duration_seconds', 'process_code', 'started_count', 'completed_count'})


def sync_diagnostics(value):
    """Bounded technical fields only; never tool names, IDs, arguments or text."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'invocations', 'omitted_count'}
            or not isinstance(value['invocations'], list) or len(value['invocations']) > 512
            or type(value['omitted_count']) is not int or value['omitted_count'] < 0):
        raise ValueError('Invalid sync diagnostics.')
    for item in value['invocations']:
        if (not isinstance(item, dict) or set(item) != DIAGNOSTIC_FIELDS
                or item['phase'] not in STAGES or item['state'] not in {'running','completed','failed','interrupted'}):
            raise ValueError('Invalid sync diagnostic fields.')
        for key in ('requested_count','received_count','started_count','completed_count'):
            if type(item[key]) is not int or not 0 <= item[key] <= 1000000:
                raise ValueError('Invalid sync diagnostic count.')
        duration = item['duration_seconds']
        code = item['process_code']
        if (type(duration) not in (int,float) or not math.isfinite(duration) or not 0 <= duration <= 7*86400
                or code is not None and (type(code) is not int or not -255 <= code <= 255)):
            raise ValueError('Invalid sync diagnostic process metadata.')
    if len(json.dumps(value)) > 131072:
        raise ValueError('Sync diagnostics exceed limit.')
    return value


def probe_metadata(value):
    """Strict whitelist for private-free technical attempt records."""
    if not isinstance(value, dict) or set(value) != set(PROBE_FIELDS):
        raise ValueError('Invalid calendar probe metadata fields.')
    result = dict(value)
    if type(result['attempt_number']) is not int or result['attempt_number'] not in (1, 2):
        raise ValueError('Invalid calendar probe attempt number.')
    if not isinstance(result['model'], str) or not re.fullmatch(r'gpt-[0-9][a-z0-9.-]{0,80}', result['model']):
        raise ValueError('Invalid calendar probe model.')
    if result['effort'] not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}:
        raise ValueError('Invalid calendar probe effort.')
    version = result['cli_version']
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,40})?', version)):
        raise ValueError('Invalid calendar probe CLI version.')
    if result['result'] not in PROBE_RESULTS or (result['safe_code'] is not None and result['safe_code'] not in ERROR_CODES):
        raise ValueError('Invalid calendar probe result.')
    for key in ('started_count', 'completed_count', 'parse_error_count', 'unknown_event_count', 'incomplete_count'):
        if result[key] is not None and (type(result[key]) is not int or not 0 <= result[key] <= 1000000):
            raise ValueError('Invalid calendar probe count.')
    code = result['process_code']
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise ValueError('Invalid calendar probe process code.')
    for key in ('expected_tool_name', 'expected_tool_arguments'):
        if result[key] is not None and type(result[key]) is not bool:
            raise ValueError('Invalid calendar probe evidence flag.')
    result['started_at'] = timestamp(result['started_at'])
    result['completed_at'] = timestamp(result['completed_at'])
    if not result['started_at'] or (result['result'] == 'running') != (result['completed_at'] is None):
        raise ValueError('Invalid calendar probe timestamps.')
    if result['completed_at'] and result['completed_at'] < result['started_at']:
        raise ValueError('Invalid calendar probe time order.')
    if ((result['result'] in {'running', 'ok'} and result['safe_code'] is not None)
            or (result['result'] in {'failed', 'interrupted'} and result['safe_code'] is None)):
        raise ValueError('Invalid calendar probe error code.')
    return result


def timestamp(value):
    if value in (None, ''):
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Health timestamp must include a timezone.')
    return parsed.astimezone(timezone.utc).isoformat()


def error_code(error, stage=None):
    code = getattr(error, 'code', None)
    if code in ERROR_CODES:
        return code
    if isinstance(error, FileNotFoundError):
        return 'missing_private_file'
    if stage == 'discovery':
        import requests
        if isinstance(error, (TimeoutError, requests.exceptions.Timeout)):
            return 'discovery_timeout'
        if isinstance(error, requests.exceptions.HTTPError):
            return 'discovery_http_failed'
        if isinstance(error, (ConnectionError, requests.exceptions.ConnectionError)):
            return 'discovery_connection_failed'
        if isinstance(error, (ValueError, TypeError, KeyError)):
            return 'discovery_parsing_failed'
    return {'credentials': 'invalid_credentials', 'login': 'login_failed', 'calendar_probe': 'calendar_write_access_missing',
            'discovery': 'discovery_failed', 'fetch': 'fetch_failed', 'analysis': 'analysis_failed'}.get(stage, 'operation_failed')


SOURCE_KINDS = ('message', 'announcement', 'school_event', 'substitution')
SOURCE_MODES = ('off', 'preview', 'active')
SOURCE_ERROR_CODES = frozenset(('source_collection_incomplete', 'source_collection_failed', 'source_analysis_failed', 'source_context_mismatch', 'source_changed', 'source_match_failed', 'source_operation_failed', 'source_response_limit', 'source_markup_invalid', 'source_fields_ambiguous', 'source_date_invalid', 'source_pagination_unsupported', 'announcement_markup_invalid', 'announcement_limit', 'announcement_identity_ambiguous', 'learner_context_ambiguous', 'learner_context_unverified', 'calendar_form_invalid', 'calendar_markup_invalid', 'calendar_date_invalid', 'calendar_detail_route_invalid', 'calendar_identity_unverified', 'calendar_limit', 'calendar_month_incomplete', 'calendar_detail_markup_invalid', 'source_session_invalid', 'calendar_window_unsupported', 'calendar_selection_failed', 'calendar_identity_ambiguous', 'calendar_publication_ambiguous', 'calendar_restore_failed', 'source_window_invalid', 'learner_collection_failed', 'announcement_collection_failed', 'school_event_collection_failed', 'learner_context_changed'))
SOURCE_ERROR_CODES |= frozenset(('source_preview_time_budget_exhausted', 'source_preview_call_budget_exhausted', 'source_preview_worker_required'))


def read_source_health(db, tables):
    """Emit aggregates only; retry eligibility uses private identity metadata, never source bodies."""
    if not {'sources', 'source_health', 'action_sources', 'source_previews'}.issubset(tables):
        return None
    # Older read-only schemas have no planner contract; they establish no readiness.
    current_preview=current_preview_sql() if 'payload' in {r[1] for r in db.execute('PRAGMA table_info(source_previews)')} else '0'
    settings=dict(db.execute('SELECT key,value FROM settings WHERE key IN (?,?,?,?)',PREVIEW_RETRY_SETTINGS))
    preview_queue,queue_args=preview_queue_sql(settings)
    engine_row = db.execute("SELECT value FROM settings WHERE key='agenda_engine'").fetchone()
    simple = bool(engine_row and engine_row[0] == 'simple-agenda')
    row = db.execute("SELECT value FROM settings WHERE key='multi_source_mode'").fetchone()
    mode = 'active' if simple else (row[0] if row else 'off')
    if mode not in SOURCE_MODES:
        raise ValueError('Invalid source mode.')
    kinds = {}
    for kind in SOURCE_KINDS:
        health = db.execute('SELECT last_successful_fetch,last_result,last_error FROM source_health WHERE kind=?', (kind,)).fetchone()
        counts = dict(db.execute('SELECT status,COUNT(*) FROM sources WHERE kind=? GROUP BY status', (kind,)))
        links = db.execute('SELECT COUNT(*) FROM action_sources a JOIN sources s ON s.id=a.source_id WHERE s.kind=?', (kind,)).fetchone()[0]
        result, code = (health[1], health[2]) if health else (None, None)
        if result is not None and result not in {'ok', 'partial', 'failed', 'running', 'unsupported', 'off'}:
            raise ValueError('Invalid source result.')
        if code is not None and code not in SOURCE_ERROR_CODES:
            code = 'source_operation_failed'
        pending, failed, preview_ready = counts.get('pending', 0), counts.get('failed', 0), 0 if simple else None
        if mode == 'preview' and kind != 'message':
            preview_counts = dict(db.execute(f'SELECT p.status,COUNT(*) FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE s.kind=? AND s.deleted=0 AND ({current_preview}) GROUP BY p.status', (kind,)))
            preview_ready = preview_counts.get('ready', 0)
            failed = preview_counts.get('failed', 0)
            pending = int(db.execute(f"SELECT COUNT(*) FROM sources s WHERE s.kind=? AND s.deleted=0 AND ({preview_queue}) AND NOT EXISTS (SELECT 1 FROM source_previews p WHERE p.source_id=s.id AND p.source_hash=s.content_hash AND p.status IN ('ready','failed') AND ({current_preview}))", (kind,*queue_args)).fetchone()[0])
        kinds[kind] = dict(mode='active' if kind == 'message' else mode,
            last_successful_fetch=timestamp(health[0]) if health else None,
            pending=pending, failed=failed, preview_ready=preview_ready, links=links,
            last_result=result, last_error=code)
    return dict(mode=mode, kinds=kinds, **({'engine': 'simple-agenda'} if simple else {}),
        preview_count=0 if simple else int(db.execute(f"SELECT COUNT(*) FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE p.status='ready' AND s.deleted=0 AND ({current_preview})").fetchone()[0]))


def read_agenda_health(db, tables):
    """Technical plan outcomes only: no payload, source ID, hash or model text."""
    if 'agenda_plans' not in tables:
        return None
    result = {'engine': 'simple-agenda'}
    for name, clause in (('latest_plan', "NOT IN ('preview','preview_failed')"),
                         ('latest_preview', "IN ('preview','preview_failed')")):
        row = db.execute('SELECT status,created_at,error_code FROM agenda_plans WHERE status ' + clause +
                         ' ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        if row:
            status, created_at, code = row
            if status not in {'applied', 'failed', 'preview', 'preview_failed'}:
                raise ValueError('Invalid agenda status.')
            result[name] = {'status': status, 'created_at': timestamp(created_at),
                            'error_code': code if code in ERROR_CODES else ('operation_failed' if code else None)}
        else:
            result[name] = None
    return result


def read_connection_state(db, now=None):
    """Read aggregate columns only, on the caller's existing consistent snapshot."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'settings', 'messages', 'proposals', 'operations'}.issubset(tables):
        raise ValueError('Unusable application health database.')
    keys = ('health_initialized_at', 'last_sync', 'last_successful_sync', 'consecutive_sync_failures',
            'writes_paused', 'writes_paused_since', 'last_backup_success_at', 'last_backup_attempt_at', 'last_backup_result')
    settings = dict(db.execute('SELECT key,value FROM settings WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys))
    count = lambda table, clause: int(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE ' + clause).fetchone()[0])
    messages_pending = count('messages', "status IN ('fetch_pending','fetch_failed','analysis_pending','analysis_failed')")
    pending_clause = "p.status='pending' AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.proposal_id=p.id AND o.status IN ('pending','inflight','unknown'))"
    operations_pending = count('operations', "status='pending'") + count('proposals p', pending_clause)
    metadata_pending = count('metadata_refresh', "status IN ('pending','inflight')") if 'metadata_refresh' in tables else 0
    metadata_unknown = count('metadata_refresh', "status='unknown'") if 'metadata_refresh' in tables else 0
    operations_pending += metadata_pending
    proposal_columns = {row[1] for row in db.execute('PRAGMA table_info(proposals)')}
    # The reader also supports unmigrated databases without initializing them.
    # Preparation/retry updates are not new eligibility transitions.
    proposal_stamp = 'COALESCE(p.pending_since,p.created_at)' if 'pending_since' in proposal_columns else 'p.created_at'
    oldest = db.execute("""SELECT MIN(stamp) FROM (
        SELECT COALESCE(""" + proposal_stamp + """,o.created_at) AS stamp FROM operations o
        LEFT JOIN proposals p ON p.id=o.proposal_id AND p.status='pending' WHERE o.status='pending'
        UNION ALL SELECT """ + proposal_stamp + ' FROM proposals p WHERE ' + pending_clause + ')').fetchone()[0]
    message_columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
    available_stamps = [column for column in ('pending_since', 'discovered_at', 'fetched_at') if column in message_columns]
    message_stamp = 'COALESCE(' + ','.join(available_stamps + ['?']) + ')'
    oldest_message = db.execute('SELECT MIN(' + message_stamp + ") FROM messages WHERE status IN ('fetch_pending','fetch_failed','analysis_pending','analysis_failed')",
                                (settings.get('health_initialized_at'),)).fetchone()[0]
    oldest = min((value for value in (oldest, oldest_message) if value), default=None)
    if 'metadata_refresh' in tables:
        metadata_oldest = db.execute("SELECT MIN(created_at) FROM metadata_refresh WHERE status IN ('pending','inflight')").fetchone()[0]
        oldest = min((value for value in (oldest, metadata_oldest) if value), default=None)
    last = None
    completed = dict(last_completed_result=None, last_completed_stage=None, last_completed_error_code=None)
    if 'sync_runs' in tables:
        cursor = db.execute('SELECT id,started_at,finished_at,stage,result,error_code,duration_seconds FROM sync_runs ORDER BY started_at DESC,rowid DESC LIMIT 1')
        row = cursor.fetchone()
        last = dict(zip((col[0] for col in cursor.description), row)) if row else None
        if last and 'diagnostics' in {row[1] for row in db.execute('PRAGMA table_info(sync_runs)')}:
            raw = db.execute('SELECT diagnostics FROM sync_runs WHERE id=?', (last['id'],)).fetchone()[0]
            last['diagnostics'] = sync_diagnostics(json.loads(raw)) if raw else None
        # A running attempt, preview or pause does not resolve an earlier sync
        # failure. Keep the latest finished actual sync, ordered by UTC instant.
        row = db.execute("SELECT result,stage,error_code,finished_at FROM sync_runs "
                         "WHERE finished_at IS NOT NULL AND result IN ('ok','partial','failed','interrupted') "
                         "ORDER BY julianday(finished_at) DESC,rowid DESC LIMIT 1").fetchone()
        if row:
            completed_result, completed_stage, completed_code, finished_at = row
            timestamp(finished_at)
            if ((completed_stage is not None and completed_stage not in STAGES)
                    or (completed_code is not None and completed_code not in ERROR_CODES)):
                raise ValueError('Invalid completed sync health metadata.')
            completed.update(last_completed_result=completed_result, last_completed_stage=completed_stage,
                             last_completed_error_code=completed_code)
    result = last['result'] if last else 'idle'
    stage = last['stage'] if last else None
    code = last['error_code'] if last else None
    if result not in RESULTS or (stage is not None and stage not in STAGES) or (code is not None and code not in ERROR_CODES):
        raise ValueError('Invalid technical health metadata.')
    duration = last['duration_seconds'] if last else None
    if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0):
        raise ValueError('Invalid health duration.')
    backup_result = settings.get('last_backup_result')
    if backup_result not in (None, 'running', 'ok', 'failed'):
        raise ValueError('Invalid backup health metadata.')
    failures = int(settings.get('consecutive_sync_failures', '0'))
    if failures < 0:
        raise ValueError('Invalid failure count.')
    probe = dict(calendar_probe_result=None, calendar_probe_attempts=None, calendar_probe_recovered=None,
                 calendar_probe_retries_24h=None, calendar_probe_recoveries_24h=None)
    if 'calendar_probe_attempts' in tables and 'sync_runs' in tables:
        # Read only whitelisted columns, never payloads or tool/private strings.
        cursor = db.execute('SELECT sync_run_id,' + ','.join(PROBE_FIELDS) + ' FROM calendar_probe_attempts')
        columns = [column[0] for column in cursor.description]
        attempts = []
        for row in cursor:
            values = dict(zip(columns, row))
            run_id = values.pop('sync_run_id')
            for flag in ('expected_tool_name', 'expected_tool_arguments'):
                if values[flag] in (0, 1) and values[flag] is not None:
                    values[flag] = bool(values[flag])
            attempts.append((run_id, probe_metadata(values)))
        if attempts:
            cutoff = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(hours=24)).isoformat()
            probe['calendar_probe_retries_24h'] = sum(item['attempt_number'] == 2 and item['started_at'] >= cutoff for _, item in attempts)
            probe['calendar_probe_recoveries_24h'] = sum(item['attempt_number'] == 2 and item['result'] == 'ok' and item['completed_at'] >= cutoff for _, item in attempts)
            latest = [item for run_id, item in attempts if last and run_id == last['id']]
            if latest:
                item = max(latest, key=lambda value: value['attempt_number'])
                probe.update(calendar_probe_result=item['result'], calendar_probe_attempts=len(latest),
                    calendar_probe_recovered=None if item['result'] == 'running' else item['result'] == 'ok' and item['attempt_number'] == 2)
    return dict(schema_version=1, sources=read_source_health(db, tables), agenda=read_agenda_health(db, tables), **probe, **completed, initialized_at=timestamp(settings.get('health_initialized_at')),
        last_attempt_at=timestamp(last['started_at'] if last else settings.get('last_sync')),
        last_successful_sync=timestamp(settings.get('last_successful_sync')),
        sync_diagnostics=last.get('diagnostics') if last else None,
        last_result=result, last_stage=stage, last_error_code=code, last_duration_seconds=duration,
        running_since=timestamp(last['started_at']) if last and result == 'running' else None,
        consecutive_failures=failures, writes_paused=settings.get('writes_paused', 'true') == 'true',
        writes_paused_since=timestamp(settings.get('writes_paused_since')),
        messages_pending=messages_pending, operations_pending=operations_pending,
        operations_unknown=count('operations', "status IN ('unknown','inflight')") + metadata_unknown, oldest_pending_at=timestamp(oldest),
        review_count=count('proposals', "status='review'") + (count('metadata_refresh', "status='review'") if 'metadata_refresh' in tables else 0),
        last_backup_success_at=timestamp(settings.get('last_backup_success_at')),
        last_backup_attempt_at=timestamp(settings.get('last_backup_attempt_at')), last_backup_result=backup_result)


def read_monitoring_state(path, now=None):
    """Strict read-only SQLite access, including legacy DBs. No AppStore/migrations."""
    # mode=ro also refuses a missing file instead of creating an empty database.
    uri = Path(path).expanduser().resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return read_connection_state(db, now=now)
    finally:
        db.close()

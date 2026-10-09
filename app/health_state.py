"""Technical health metadata; the public reader never initializes or changes a DB."""
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3

RESULTS = frozenset(('idle', 'running', 'ok', 'partial', 'failed', 'interrupted', 'paused', 'dry_run'))
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
    return {'credentials': 'invalid_credentials', 'login': 'login_failed', 'calendar_probe': 'calendar_write_access_missing',
            'discovery': 'discovery_failed', 'fetch': 'fetch_failed', 'analysis': 'analysis_failed'}.get(stage, 'operation_failed')


def read_connection_state(db):
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
    oldest = db.execute("""SELECT MIN(stamp) FROM (
        SELECT created_at AS stamp FROM operations WHERE status='pending'
        UNION ALL SELECT p.created_at FROM proposals p WHERE """ + pending_clause + ')').fetchone()[0]
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
    if 'sync_runs' in tables:
        cursor = db.execute('SELECT started_at,finished_at,stage,result,error_code,duration_seconds FROM sync_runs ORDER BY started_at DESC,rowid DESC LIMIT 1')
        row = cursor.fetchone()
        last = dict(zip((col[0] for col in cursor.description), row)) if row else None
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
    return dict(schema_version=1, initialized_at=timestamp(settings.get('health_initialized_at')),
        last_attempt_at=timestamp(last['started_at'] if last else settings.get('last_sync')),
        last_successful_sync=timestamp(settings.get('last_successful_sync')),
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
        return read_connection_state(db)
    finally:
        db.close()

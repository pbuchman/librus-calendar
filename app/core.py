"""Private durable state and hard calendar-write rules, independent of the model."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import threading
import uuid
from zoneinfo import ZoneInfo
from .health_state import ERROR_CODES, RESULTS, STAGES, read_connection_state

WARSAW = ZoneInfo('Europe/Warsaw')
from .config import AppConfig, ConfigurationError, load_config
PREFIX = 'librus-calendar:'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def contains_marker(description, marker):
    # Calendar connector descriptions can wrap a marker across arbitrary whitespace.
    compact = lambda value: re.sub(r'\s+', '', str(value or ''))
    return bool(marker) and compact(marker) in compact(description)


def has_external_attendees(snapshot, owner_email=''):
    attendees = snapshot.get('attendees') or []
    if not isinstance(attendees, list):
        return True
    # A connector may add the authenticated owner despite an omitted attendees field.
    # This exception requires both the verified primary email and Google's self flag.
    return any(not isinstance(item, dict) or str(item.get('email', '')).casefold() != owner_email.casefold()
               or not (item.get('self') is True or item.get('is_self') is True) for item in attendees)


def as_datetime(value):
    if not isinstance(value, str):
        raise ValueError('Date must be an ISO string.')
    value = value.strip()
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        # A local wall time during the spring DST gap is not a real time.
        local = result.replace(tzinfo=WARSAW)
        if local.astimezone(timezone.utc).astimezone(WARSAW).replace(tzinfo=None) != result:
            raise ValueError('The proposed local time falls in a DST gap.')
        if local.utcoffset() != result.replace(tzinfo=WARSAW, fold=1).utcoffset():
            raise ValueError('The proposed local time is ambiguous during DST change.')
        result = local
    return result.astimezone(WARSAW)


def start_key(candidate):
    """Normalize raw Google and actual connector string date representations."""
    value = candidate.get('start')
    if isinstance(value, dict):
        all_day = bool(value.get('date'))
        value = value.get('date') or value.get('dateTime')
    else:
        all_day = candidate.get('all_day') is True or (isinstance(value, str) and len(value.strip()) == 10)
    try:
        stamp = date.fromisoformat(value).isoformat() if all_day else as_datetime(value).isoformat()
    except (ValueError, TypeError, KeyError):
        return None
    return stamp, all_day


def requires_registration(message):
    text = str(message.get('subject') or '') + ' ' + str(message.get('text') or '')
    # Legacy fallback only: an optional classroom craft is not an enrollment offer.
    return bool(re.search(r'\b(?:zapis(?:y|ów|ach|anie|ania|ać|ac|ać)|zarejestr\w*|sign[ -]?up|registration required|requires registration|potwierdzeni\w*\s+udział\w*|potwierdzeni\w*\s+udzial\w*|zgłoszeni\w*\s+(?:na|do)\s+zaję\w*|zgloszeni\w*\s+(?:na|do)\s+zaje\w*)\b', text, re.IGNORECASE))


def librus_title(value):
    title = re.sub(r'^(?:\[(?:Szkoła|Librus)\]\s*)+', '', str(value or '').strip(), flags=re.IGNORECASE)
    return '[Librus] ' + title[:241]


def synchronization_label(stamp):
    parsed = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Synchronization preparation time must have a timezone.')
    return 'Synchronizacja rozpoczęta: ' + parsed.astimezone(WARSAW).strftime('%d.%m.%Y %H:%M:%S') + ' (Europe/Warsaw)'


def synchronization_description(description, stamp, marker):
    text = re.sub(r'Synchronizacja rozpoczęta:\s*\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}\s*\(Europe/Warsaw\)', '', str(description or ''))
    # Connector renderers may wrap the existing ownership marker. Retain its exact
    # identity once, last, so normal fingerprint canonicalization still applies.
    pattern = r'\s*'.join(re.escape(char) for char in marker)
    text = re.sub(pattern, '', text).rstrip()
    return text + '\n' + synchronization_label(stamp) + '\n' + marker


def deadline_datetime(value):
    """An exact school deadline needs a real, explicitly offset Warsaw wall time."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?[+-]\d{2}:\d{2}', value):
        raise ValueError('An exact deadline requires an explicit ISO timestamp and Warsaw offset.')
    parsed = datetime.fromisoformat(value)
    local = parsed.astimezone(WARSAW)
    if parsed.utcoffset() != local.utcoffset() or parsed.replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError('The deadline offset does not match Europe/Warsaw.')
    return local


def analysis_metadata(data, proposal_count):
    """Persist only the documented provenance fields; legacy runtimes remain unknown."""
    data = data if isinstance(data, dict) else {}
    result = {key: (data[key].strip()[:limit] if isinstance(data.get(key), str) and data[key].strip() else None)
              for key, limit in (('model', 120), ('effort', 40), ('prompt_version', 120), ('decision_reason', 2000))}
    if not result['decision_reason']:
        result['decision_reason'] = ('No proposals returned; the runtime supplied no decision reason.' if not proposal_count
                                     else 'Proposals returned; the runtime supplied no decision reason.')
    return result


def normalize_proposal(data, current=None):
    if not isinstance(data, dict):
        raise ValueError('Proposal must be an object.')
    current = current or datetime.now(WARSAW)
    kind = data.get('kind', 'create')
    if kind not in ('create', 'update', 'cancel', 'ignore'):
        raise ValueError('Unsupported proposal kind.')
    temporal_kind = data.get('temporal_kind', 'event')
    if temporal_kind not in ('event', 'deadline', 'reminder'):
        raise ValueError('Unsupported temporal kind.')
    activity_scope = data.get('activity_scope', 'unknown')
    if activity_scope not in ('school', 'extracurricular', 'unknown'):
        raise ValueError('Unsupported activity scope.')
    proposal = {key: data.get(key) for key in ('kind', 'title', 'description', 'start', 'end', 'all_day', 'confidence', 'needs_review', 'review_reason', 'source_quote', 'event_id')}
    proposal.update(kind=kind, title=str(data.get('title') or '').strip()[:250], description=str(data.get('description') or '')[:10000],
                    all_day=data.get('all_day') is True, confidence=data.get('confidence', 'low'),
                    needs_review=data.get('needs_review') is True, review_reason=str(data.get('review_reason') or '')[:1000],
                    source_quote=str(data.get('source_quote') or '')[:4000], default_duration=False,
                    temporal_kind=temporal_kind, due_at=data.get('due_at'), activity_scope=activity_scope,
                    deletion_approved=data.get('deletion_approved') is True, user_approved=data.get('user_approved') is True,
                    user_edited=data.get('user_edited') is True)
    if proposal['confidence'] not in ('high', 'medium', 'low'):
        proposal['confidence'] = 'low'
    if kind == 'ignore':
        proposal['status'] = 'ignored'
        return proposal
    if not proposal['title']:
        raise ValueError('An event title is required.')
    proposal['title'] = librus_title(proposal['title'])
    due = None
    if temporal_kind == 'deadline':
        if not proposal['all_day'] or not isinstance(proposal['start'], str) or len(proposal['start']) != 10:
            raise ValueError('A deadline must use a date-only, all-day calendar entry.')
        if proposal['due_at'] is not None:
            due = deadline_datetime(proposal['due_at'])
            if due.date().isoformat() != proposal['start']:
                raise ValueError('Deadline timestamp and calendar day do not match.')
            proposal['due_at'] = due.isoformat()
            label = 'do ' + due.strftime('%H:%M')
            if label not in proposal['title']:
                proposal['title'] += ' — ' + label
            detail = 'Termin: ' + due.date().isoformat() + ' ' + label + ' (Europe/Warsaw).'
            if detail not in proposal['description']:
                proposal['description'] = (proposal['description'].rstrip() + '\n' + detail).lstrip()
    elif proposal['due_at'] is not None:
        raise ValueError('Only a deadline can contain due_at.')
    if temporal_kind == 'reminder' and (not proposal['all_day'] or not isinstance(proposal['start'], str) or len(proposal['start']) != 10):
        raise ValueError('A school reminder must use a date-only, all-day calendar entry.')
    if proposal['all_day']:
        start = date.fromisoformat(str(proposal['start']))
        end = start + timedelta(days=1) if temporal_kind in ('deadline', 'reminder') or not proposal['end'] else date.fromisoformat(str(proposal['end']))
        proposal['start'], proposal['end'] = start.isoformat(), end.isoformat()
        past = due <= current.astimezone(WARSAW) if due is not None else start < current.astimezone(WARSAW).date()
    else:
        if len(str(proposal['start'])) <= 10:
            raise ValueError('A timed event requires an explicit time; choose all_day for date-only events.')
        start = as_datetime(proposal['start'])
        end = as_datetime(proposal['end']) if proposal['end'] else start + timedelta(hours=1)
        proposal['default_duration'] = not bool(proposal['end']) or data.get('default_duration') is True
        proposal['start'], proposal['end'] = start.isoformat(), end.isoformat()
        past = start <= current.astimezone(WARSAW)
    if end <= start:
        raise ValueError('Event end must be after its start.')
    if past:
        proposal.update(status='ignored', review_reason='Past date: no calendar write.')
    elif kind == 'cancel' and not proposal.get('deletion_approved'):
        proposal.update(status='review', needs_review=True, review_reason=proposal['review_reason'] or 'Cancellation requires manual review; no automatic deletion.')
    elif (activity_scope == 'unknown' or (kind == 'create' and activity_scope == 'extracurricular')) and not proposal['user_approved']:
        proposal.update(status='review', needs_review=True, review_reason=proposal['review_reason'] or 'Extra activity or unclassified participation requires confirmation.')
    elif proposal['needs_review'] or (proposal['confidence'] != 'high' and not (activity_scope == 'school' and proposal['confidence'] == 'medium')):
        proposal.update(status='review', needs_review=True, review_reason=proposal['review_reason'] or 'Date or meaning needs confirmation.')
    else:
        proposal['status'] = 'pending'
    return proposal


def event_body(proposal, message, marker, synchronization_started_at=None):
    description = proposal['description'].strip()
    source = '\n\nŹródło: Librus, wiadomość ' + message['id'] + '\n' + (message.get('subject') or '')
    source += '\nNadawca: ' + (message.get('sender') or '') + '\nWysłano: ' + (message.get('sent_at') or '')
    if proposal.get('source_quote'):
        source += '\nFragment: ' + proposal['source_quote']
    if proposal.get('default_duration'):
        source += '\nCzas trwania 60 minut przyjęty automatycznie; wiadomość nie podaje końca.'
    source += '\n' + marker
    field = 'date' if proposal['all_day'] else 'dateTime'
    start, end = {field: proposal['start']}, {field: proposal['end']}
    if not proposal['all_day']:
        start['timeZone'] = end['timeZone'] = 'Europe/Warsaw'
    return {'summary': librus_title(proposal['title']), 'description': synchronization_description(description + source, synchronization_started_at or utcnow(), marker), 'start': start, 'end': end,
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': value} for value in ([360] if proposal['all_day'] else [1440, 60])]},
            }


class AppStore:
    def __init__(self, path=None, *, config=None):
        os.umask(0o077)
        self.config = config if config is not None else load_config()
        if not isinstance(self.config, AppConfig):
            raise ConfigurationError("validated_configuration_required")
        self.calendar_id = self.config.calendar_id
        self.path = Path(path or self.config.state_path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Data directory must be an owned real directory.')
        self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Database must be an owned regular file.')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if self.calendar_id and 'settings' in tables:
            binding = self._setting('calendar_binding')
            owner_binding = self._setting('calendar_owner_binding')
            foreign_events = ('events' in tables and self.db.execute(
                'SELECT 1 FROM events WHERE calendar_id<>? LIMIT 1', (self.calendar_id,)).fetchone())
            if ((binding and binding != self.calendar_id)
                    or (owner_binding and owner_binding != self.config.calendar_owner) or foreign_events):
                self.db.close()
                raise ConfigurationError('calendar_state_mismatch')
        with self.db:
            self.db.executescript('''
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,payload TEXT NOT NULL DEFAULT '{}',payload_hash TEXT,status TEXT NOT NULL DEFAULT 'fetch_pending',analyzed_hash TEXT,sent_at TEXT,fetched_at TEXT,last_error TEXT,attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,calendar_id TEXT NOT NULL,message_id TEXT NOT NULL,proposal_id TEXT NOT NULL,snapshot TEXT NOT NULL,fingerprint TEXT,marker TEXT NOT NULL,updated_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active');
            CREATE INDEX IF NOT EXISTS messages_status ON messages(status);
            CREATE INDEX IF NOT EXISTS operations_status ON operations(status);
            CREATE TABLE IF NOT EXISTS sync_runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,stage TEXT NOT NULL,result TEXT NOT NULL,error_code TEXT,duration_seconds REAL);
            CREATE INDEX IF NOT EXISTS sync_runs_started ON sync_runs(started_at);
            CREATE TABLE IF NOT EXISTS analysis_audit(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,analyzed_at TEXT NOT NULL,mode TEXT NOT NULL,metadata TEXT NOT NULL,proposal_ids TEXT NOT NULL,superseded_ids TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS analysis_audit_message ON analysis_audit(message_id,analyzed_at);
            CREATE TABLE IF NOT EXISTS metadata_refresh(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(event_id,version));
            ''')
            if 'state' not in {row[1] for row in self.db.execute('PRAGMA table_info(events)')}:
                self.db.execute("ALTER TABLE events ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
            if 'discovered_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                self.db.execute('ALTER TABLE messages ADD COLUMN discovered_at TEXT')
            if 'pending_since' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                self.db.execute('ALTER TABLE messages ADD COLUMN pending_since TEXT')
            for column in ('analysis_metadata', 'analyzed_at'):
                if column not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                    self.db.execute('ALTER TABLE messages ADD COLUMN ' + column + ' TEXT')
            if self.calendar_id:
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('calendar_binding',?)", (self.calendar_id,))
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('calendar_owner_binding',?)", (self.config.calendar_owner,))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('writes_paused','true')")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('health_initialized_at',?)", (utcnow(),))
            self.db.execute("UPDATE messages SET pending_since=COALESCE(discovered_at,fetched_at,?) WHERE pending_since IS NULL AND status IN ('fetch_pending','fetch_failed','analysis_pending','analysis_failed')", (self._setting('health_initialized_at'),))
            if self._setting('writes_paused') == 'true':
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('writes_paused_since',?)", (utcnow(),))
            # Safe migration only for never-attempted local operations; external intent is immutable after an attempt.
            for row in self.db.execute("SELECT id,payload FROM operations WHERE status='pending' AND attempts=0 LIMIT 100").fetchall():
                payload = json.loads(row['payload'])
                if 'extendedProperties' in payload.get('event', {}):
                    payload['event'].pop('extendedProperties', None)
                    self.db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))

    def close(self):
        self.db.close()

    def _setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def status(self):
        with self.lock:
            count = lambda table, clause='': self.db.execute('SELECT COUNT(*) FROM ' + table + clause).fetchone()[0]
            return {**read_connection_state(self.db), 'messages': count('messages'), 'messages_retry': count('messages', " WHERE status IN ('fetch_pending','fetch_failed','analysis_failed')"),
                    'proposals_pending': count('proposals', " WHERE status='pending'"), 'proposals_review': count('proposals', " WHERE status='review'"),
                    'events': count('events', " WHERE state='active'"), 'events_cancelled': count('events', " WHERE state='cancelled'"),
                    'writes_paused': self._setting('writes_paused', 'true') == 'true',
                    'last_sync': self._setting('last_sync'), 'last_error': self._setting('last_error'), 'sync_requested_at': self._setting('sync_requested_at'),
                    'calendar_id': self.calendar_id, 'timezone': 'Europe/Warsaw'}

    def start_sync_run(self):
        """Only called under sync.lock, so older running rows are abandoned workers."""
        stamp = utcnow()
        with self.lock, self.db:
            abandoned = self.db.execute("SELECT id,started_at FROM sync_runs WHERE result='running'").fetchall()
            for row in abandoned:
                duration = max(0, (datetime.fromisoformat(stamp) - datetime.fromisoformat(row['started_at'])).total_seconds())
                self.db.execute("UPDATE sync_runs SET result='interrupted',error_code='worker_interrupted',finished_at=?,duration_seconds=? WHERE id=?", (stamp, duration, row['id']))
            failures = int(self._setting('consecutive_sync_failures', '0')) + len(abandoned)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('consecutive_sync_failures',?)", (str(failures),))
            self.db.execute('DELETE FROM sync_runs WHERE started_at<? AND finished_at IS NOT NULL', ((datetime.fromisoformat(stamp) - timedelta(days=90)).isoformat(),))
            run_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO sync_runs(id,started_at,stage,result) VALUES (?,?,'starting','running')", (run_id, stamp))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_sync',?)", (stamp,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('sync_requested_at','')")
            return run_id

    def sync_stage(self, run_id, stage):
        if stage not in STAGES:
            raise ValueError('Invalid technical sync stage.')
        with self.lock, self.db:
            self.db.execute("UPDATE sync_runs SET stage=? WHERE id=? AND result='running'", (stage, run_id))

    def finish_sync_run(self, run_id, result, error_code=None, duration_seconds=None):
        if result not in RESULTS - {'idle', 'running'} or (error_code is not None and error_code not in ERROR_CODES):
            raise ValueError('Invalid technical sync result.')
        stamp = utcnow()
        with self.lock, self.db:
            row = self.db.execute("SELECT started_at FROM sync_runs WHERE id=? AND result='running'", (run_id,)).fetchone()
            if not row:
                raise ValueError('Sync run is not active.')
            if duration_seconds is None:
                duration_seconds = max(0, (datetime.fromisoformat(stamp) - datetime.fromisoformat(row['started_at'])).total_seconds())
            self.db.execute('UPDATE sync_runs SET finished_at=?,result=?,error_code=?,duration_seconds=? WHERE id=?', (stamp, result, error_code, duration_seconds, run_id))
            failures = int(self._setting('consecutive_sync_failures', '0'))
            if result == 'ok':
                failures = 0
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_successful_sync',?)", (stamp,))
            elif result in ('partial', 'failed', 'interrupted'):
                failures += 1
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('consecutive_sync_failures',?)", (str(failures),))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_error',?)", (error_code or '',))

    def _message(self, row):
        if row is None:
            return None
        data = json.loads(row['payload'])
        data.update(id=row['id'], status=row['status'], payload_hash=row['payload_hash'], last_error=row['last_error'], fetched_at=row['fetched_at'])
        data.update(analysis_metadata=json.loads(row['analysis_metadata']) if row['analysis_metadata'] else None,
                    analyzed_at=row['analyzed_at'])
        return data

    def list_messages(self, limit=100):
        with self.lock:
            return [self._message(row) for row in self.db.execute('SELECT * FROM messages ORDER BY COALESCE(sent_at,fetched_at) DESC,id LIMIT ?', (min(max(int(limit), 1), 500),))]

    def get_message(self, message_id):
        with self.lock:
            return self._message(self.db.execute('SELECT * FROM messages WHERE id=?', (str(message_id),)).fetchone())

    def note_discovered(self, snippet):
        message_id = str(snippet.get('messageId') or snippet.get('id') or '').strip()
        if not message_id:
            raise ValueError('Librus message is missing a stable ID.')
        payload = {'id': message_id, 'subject': str(snippet.get('topic') or ''), 'sender': str(snippet.get('senderName') or ''), 'sent_at': snippet.get('sendDate'), 'text': ''}
        with self.lock, self.db:
            stamp = utcnow()
            self.db.execute('INSERT OR IGNORE INTO messages(id,payload,sent_at,discovered_at,pending_since) VALUES (?,?,?,?,?)', (message_id, json.dumps(payload, ensure_ascii=False), payload['sent_at'], stamp, stamp))
        return message_id

    def save_message(self, data):
        message_id = str(data.get('id') or data.get('message_id') or '')
        if not message_id:
            raise ValueError('Message has no stable ID.')
        clean = {'id': message_id, 'subject': str(data.get('subject') or ''), 'sender': str(data.get('sender') or ''), 'sent_at': data.get('sent_at'), 'text': str(data.get('text') or ''), 'attachment_count': data.get('attachment_count', 0)}
        hashed = digest(clean)
        with self.lock, self.db:
            row = self.db.execute('SELECT payload_hash,analyzed_hash,status,pending_since FROM messages WHERE id=?', (message_id,)).fetchone()
            changed = not row or row['payload_hash'] != hashed
            state = 'analysis_pending' if changed or not row or row['analyzed_hash'] != hashed else 'analyzed'
            pending_since = (row['pending_since'] if row and row['status'] in ('fetch_pending','fetch_failed','analysis_pending','analysis_failed') else None) or utcnow()
            self.db.execute('INSERT INTO messages(id,payload,payload_hash,status,sent_at,fetched_at,last_error,pending_since) VALUES (?,?,?,?,?,?,NULL,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,payload_hash=excluded.payload_hash,status=excluded.status,sent_at=excluded.sent_at,fetched_at=excluded.fetched_at,last_error=NULL,pending_since=excluded.pending_since',
                            (message_id, json.dumps(clean, ensure_ascii=False), hashed, state, clean['sent_at'], utcnow(), pending_since if state == 'analysis_pending' else None))
            if changed and row and row['payload_hash']:
                self.db.execute("UPDATE proposals SET status='ignored',updated_at=? WHERE message_id=? AND status IN ('pending','review')", (utcnow(), message_id))
                self.db.execute("UPDATE operations SET status='review',last_error='Source message changed; reanalysis required.',updated_at=? WHERE proposal_id IN (SELECT id FROM proposals WHERE message_id=?) AND status='pending'", (utcnow(), message_id))
        return changed

    def fail_message(self, message_id, stage, error):
        state = 'fetch_failed' if stage == 'fetch' else 'analysis_failed'
        with self.lock, self.db:
            self.db.execute('UPDATE messages SET status=?,last_error=?,attempts=attempts+1,pending_since=COALESCE(pending_since,?) WHERE id=?', (state, str(error)[:500], utcnow(), str(message_id)))

    def retry_message_ids(self, limit=100):
        with self.lock:
            return [row[0] for row in self.db.execute("SELECT id FROM messages WHERE status IN ('fetch_failed','fetch_pending') ORDER BY attempts,id LIMIT ?", (limit,))]

    def analysis_messages(self, limit=100):
        with self.lock:
            return [self._message(row) for row in self.db.execute("SELECT * FROM messages WHERE status IN ('analysis_pending','analysis_failed') ORDER BY fetched_at,id LIMIT ?", (limit,))]

    def _prepare_proposals(self, message_id, proposals, current=None, exclude_ids=(), reanalysis=False):
        message = self.get_message(message_id)
        if not message or not message.get('payload_hash'):
            raise ValueError('A full cached message is required before analysis.')
        if not isinstance(proposals, list) or len(proposals) > 30:
            raise ValueError('Unexpected analysis result shape.')
        normalized = []
        for index, raw in enumerate(proposals):
            try:
                item = normalize_proposal({**raw, 'deletion_approved': False, 'user_approved': False, 'user_edited': False} if isinstance(raw, dict) else raw, current)
            except (ValueError, TypeError, OverflowError):
                item = {'kind': str(raw.get('kind') or 'create') if isinstance(raw, dict) else 'create', 'title': str(raw.get('title') or 'Niejasny termin') if isinstance(raw, dict) else 'Niejasny termin',
                        'description': '', 'start': None, 'end': None, 'all_day': False, 'confidence': 'low', 'needs_review': True,
                        'review_reason': 'Invalid or ambiguous event date; manual correction required.', 'source_quote': str(raw.get('source_quote') or '') if isinstance(raw, dict) else '', 'event_id': None, 'status': 'review',
                        'temporal_kind': raw.get('temporal_kind', 'event') if isinstance(raw, dict) else 'event',
                        'activity_scope': raw.get('activity_scope', 'unknown') if isinstance(raw, dict) else 'unknown',
                        'due_at': raw.get('due_at') if isinstance(raw, dict) else None, 'default_duration': False,
                        'user_approved': False, 'user_edited': False, 'deletion_approved': False}
            if item['status'] != 'ignored':
                if item['kind'] in ('create','update'):
                    quote = re.sub(r'\s+', ' ', item.get('source_quote') or '').strip()
                    source = re.sub(r'\s+', ' ', message.get('text') or '').strip()
                    if len(quote) < 6 or quote not in source:
                        item.update(status='review', needs_review=True, review_reason='Source quote is missing or does not match the message text.')
                if item['kind'] in ('update','cancel'):
                    owned = self.db.execute('SELECT 1 FROM events WHERE id=? AND calendar_id=?', (item.get('event_id'), self.calendar_id)).fetchone()
                    if not owned:
                        item.update(status='review', needs_review=True, review_reason='Target event is not owned by this application.')
                elif item['kind'] == 'create' and self.db.execute('SELECT 1 FROM events WHERE message_id=?', (str(message_id),)).fetchone():
                    item.update(status='review', needs_review=True, review_reason='This source already has an event; confirm whether this is a new date or an update.')
                if item['kind'] == 'create' and item.get('start'):
                    prior = [item for item in self.list_proposals(limit=500) if item['id'] not in exclude_ids] + normalized
                    duplicate = any(p.get('kind') != 'ignore' and p.get('status') in ('pending','review','applied') and start_key(p) == start_key(item) for p in prior)
                    # Tombstones prevent a later email from recreating an explicitly cancelled event.
                    for saved in self.list_events(500, include_cancelled=True):
                        duplicate = duplicate or start_key(saved['snapshot']) == start_key(item)
                    if duplicate:
                        item.update(status='review', needs_review=True, review_reason='Another school event starts at the same time; confirm duplicate, overlap, or update.')
                    if item['activity_scope'] == 'unknown' and requires_registration(message):
                        item.update(status='review', needs_review=True, review_reason='This message offers an optional activity or requires sign-up; participation must be confirmed.')
            proposal_id = digest([str(message_id), message['payload_hash'], index, item])[:32]
            if reanalysis:
                # Never reuse a retired operation's unique proposal ID. Repeated current
                # output keeps the same active ID; returning to an older output gets a
                # fresh deterministic revision while its old audit evidence survives.
                while True:
                    retired = self.db.execute("SELECT updated_at FROM proposals WHERE id=? AND status='superseded'", (proposal_id,)).fetchone()
                    if not retired:
                        break
                    proposal_id = digest([proposal_id, 'replacement', retired['updated_at']])[:32]
            item.update(id=proposal_id, message_id=str(message_id))
            normalized.append(item)
        return normalized

    def _save_analysis(self, message, normalized, metadata, mode, superseded_ids=()):
        stamp = utcnow()
        metadata = analysis_metadata(metadata, len(normalized))
        for item in normalized:
            self.db.execute('INSERT OR IGNORE INTO proposals VALUES (?,?,?,?,?,?,?)', (item['id'], message['id'], message['payload_hash'], json.dumps(item, ensure_ascii=False), item['status'], stamp, stamp))
        self.db.execute("UPDATE messages SET analyzed_hash=payload_hash,status='analyzed',last_error=NULL,pending_since=NULL,analysis_metadata=?,analyzed_at=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), stamp, message['id']))
        self.db.execute('INSERT INTO analysis_audit VALUES (?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, message['id'], message['payload_hash'], stamp, mode, json.dumps(metadata, ensure_ascii=False),
                         json.dumps([item['id'] for item in normalized]), json.dumps(list(superseded_ids))))

    def save_proposals(self, message_id, proposals, current=None, metadata=None):
        with self.lock, self.db:
            normalized = self._prepare_proposals(message_id, proposals, current)
            self._save_analysis(self.get_message(message_id), normalized, metadata, 'sync')
        return normalized

    def get_analysis_history(self, message_id, limit=20):
        with self.lock:
            rows = self.db.execute('SELECT * FROM analysis_audit WHERE message_id=? ORDER BY analyzed_at DESC,rowid DESC LIMIT ?',
                                   (str(message_id), min(max(int(limit), 1), 100))).fetchall()
            return [{**dict(row), **{key: json.loads(row[key]) for key in ('metadata', 'proposal_ids', 'superseded_ids')}} for row in rows]

    def cached_reanalysis_scope(self, message_id):
        """Reject any source carrying an external write or a human decision."""
        with self.lock:
            message = self.get_message(message_id)
            if not message or not message.get('payload_hash'):
                raise ValueError('Cached reanalysis requires a fully fetched message.')
            if self.db.execute('SELECT 1 FROM events WHERE message_id=? OR proposal_id IN (SELECT id FROM proposals WHERE message_id=?)',
                               (message['id'], message['id'])).fetchone():
                raise ValueError('Cached reanalysis refuses a source with historical calendar events.')
            eligible = []
            for row in self.db.execute("SELECT * FROM proposals WHERE message_id=? AND status!='superseded'", (message['id'],)):
                item = self._proposal(row)
                if (row['source_hash'] != message['payload_hash'] or item['status'] not in ('pending', 'review')
                        or any(item.get(flag) for flag in ('user_approved', 'user_edited', 'deletion_approved', 'user_rejected'))):
                    raise ValueError('Cached reanalysis refuses protected proposal history or human decisions.')
                eligible.append(item['id'])
            operations = self.db.execute("SELECT o.* FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE p.message_id=? AND o.status!='superseded'", (message['id'],)).fetchall()
            if any(row['status'] != 'pending' or row['attempts'] != 0 or row['result'] is not None or row['proposal_id'] not in eligible for row in operations):
                raise ValueError('Cached reanalysis refuses attempted, inflight, unknown or reviewed operations.')
            return {'message_id': message['id'], 'source_hash': message['payload_hash'], 'proposal_ids': eligible}

    def prepare_cached_reanalysis(self, message_id, proposals, current=None):
        with self.lock:
            scope = self.cached_reanalysis_scope(message_id)
            return self._prepare_proposals(message_id, proposals, current, scope['proposal_ids'], reanalysis=True)

    def replace_cached_proposals(self, message_id, proposals, current=None, metadata=None, expected_source_hash=None):
        """Called only with sync.lock held; supersede local evidence atomically."""
        with self.lock:
            if self.db.in_transaction:
                raise ValueError('Cached replacement requires its own immediate transaction.')
            with self.db:
                # Web decisions use a separate connection and do not hold sync.lock.
                # Reserve the SQLite write lock before any eligibility read so a
                # concurrent approval cannot be lost between SELECT and UPDATE.
                self.db.execute('BEGIN IMMEDIATE')
                scope = self.cached_reanalysis_scope(message_id)
                if not expected_source_hash or scope['source_hash'] != expected_source_hash:
                    raise ValueError('Cached source changed during reanalysis.')
                normalized = self._prepare_proposals(message_id, proposals, current, scope['proposal_ids'], reanalysis=True)
                replacement_ids = {item['id'] for item in normalized}
                superseded = [item for item in scope['proposal_ids'] if item not in replacement_ids]
                for proposal_id in superseded:
                    self.db.execute("UPDATE proposals SET status='superseded',updated_at=? WHERE id=?", (utcnow(), proposal_id))
                    self.db.execute("UPDATE operations SET status='superseded',last_error='Superseded by explicitly requested cached reanalysis.',updated_at=? WHERE proposal_id=? AND status='pending' AND attempts=0", (utcnow(), proposal_id))
                self._save_analysis(self.get_message(message_id), normalized, metadata, 'cached_reanalysis', superseded)
                return {'proposals': normalized, 'superseded_ids': superseded}

    def _proposal(self, row):
        data = json.loads(row['payload'])
        data.update(id=row['id'], message_id=row['message_id'], status=row['status'])
        return data

    def list_proposals(self, status=None, limit=100):
        sql = 'SELECT * FROM proposals' + (' WHERE status=?' if status else '') + ' ORDER BY created_at DESC,id LIMIT ?'
        params = (status, limit) if status else (limit,)
        with self.lock:
            return [self._proposal(row) for row in self.db.execute(sql, params)]

    def list_events(self, limit=100, include_cancelled=False):
        with self.lock:
            sql = 'SELECT * FROM events' + ('' if include_cancelled else " WHERE state='active'") + ' ORDER BY updated_at DESC LIMIT ?'
            rows = self.db.execute(sql, (limit,)).fetchall()
            return [{**dict(row), 'snapshot': json.loads(row['snapshot'])} for row in rows]

    def _rebase_reviewed_conflict(self, item):
        review = self.db.execute("SELECT payload,result FROM operations WHERE proposal_id=? AND status='review'", (item['id'],)).fetchone()
        if not review or not review['result']:
            return
        result = json.loads(review['result'])
        if result.get('error') != 'manual_edit_conflict':
            return
        operation = json.loads(review['payload'])
        target_id = item.get('event_id')
        owned = self.db.execute("SELECT * FROM events WHERE id=? AND calendar_id=? AND state='active'", (target_id, self.calendar_id)).fetchone()
        snapshot = result.get('snapshot')
        if (item['kind'] not in ('update','cancel') or operation.get('action') != item['kind']
                or operation.get('calendar_id') != self.calendar_id or operation.get('event_id') != target_id
                or not owned or not isinstance(snapshot, dict) or result.get('event_id') != target_id
                or snapshot.get('id') != target_id or snapshot.get('status') == 'cancelled'
                or has_external_attendees(snapshot, self.config.calendar_owner) or not contains_marker(snapshot.get('description'), owned['marker'])):
            raise ValueError('The reviewed calendar conflict lacks safe app-owned event evidence; approval rejected.')
        from .codex_runtime import event_fingerprint
        fingerprint = event_fingerprint(snapshot, owner_email=self.config.calendar_owner)
        if result.get('fingerprint') != fingerprint:
            raise ValueError('The reviewed calendar fingerprint is invalid; approval rejected.')
        # Explicit user approval accepts this verified snapshot as the new baseline.
        # Runtime must fetch again and compare before writing; later edits still conflict.
        self.db.execute('UPDATE events SET snapshot=?,fingerprint=?,updated_at=? WHERE id=? AND calendar_id=?',
                        (json.dumps(snapshot, ensure_ascii=False), fingerprint, utcnow(), target_id, self.calendar_id))

    def resolve_proposal(self, proposal_id, decision, patch=None):
        if decision not in ('approve','ignore','confirm_cancel'):
            raise ValueError('Decision must be approve, ignore, or confirm_cancel.')
        with self.lock, self.db:
            # Serialize human decisions before their first read too: otherwise a
            # waiting writer could restore a stale pre-supersession payload.
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM proposals WHERE id=?', (str(proposal_id),)).fetchone()
            if not row:
                raise ValueError('Unknown proposal.')
            if row['status'] in ('applied', 'superseded'):
                raise ValueError('An applied or superseded proposal cannot be replayed.')
            active = self.db.execute("SELECT status FROM operations WHERE proposal_id=? AND status IN ('inflight','unknown','applied')", (str(proposal_id),)).fetchone()
            if active:
                raise ValueError('Resolve the existing operation before changing this proposal.')
            item = self._proposal(row)
            if decision == 'ignore':
                item['status'] = 'ignored'
                item['user_rejected'] = True
                self.db.execute("UPDATE operations SET status='review',last_error='User ignored proposal.',updated_at=? WHERE proposal_id=? AND status='pending'", (utcnow(), str(proposal_id)))
            else:
                patch = patch or {}
                if set(patch) - {'title','description','start','end','all_day'}:
                    raise ValueError('Unsupported proposal edit.')
                item.update(patch)
                if 'end' in patch:
                    item['default_duration'] = False
                item['user_edited'] = item.get('user_edited') is True or bool(patch)
                if item['kind'] == 'cancel' and decision != 'confirm_cancel':
                    raise ValueError('Cancellation requires explicit confirm_cancel.')
                if decision == 'confirm_cancel':
                    if item['kind'] != 'cancel':
                        raise ValueError('confirm_cancel applies only to cancellation proposals.')
                    if not self.db.execute('SELECT 1 FROM events WHERE id=? AND calendar_id=?', (item.get('event_id'), self.calendar_id)).fetchone():
                        raise ValueError('Target event is not app-owned.')
                    item['deletion_approved'] = True
                item.update(confidence='high', needs_review=False, review_reason='', user_approved=True)
                item = {**normalize_proposal(item), 'id': row['id'], 'message_id': row['message_id']}
                if item['kind'] == 'update' and not self.db.execute('SELECT 1 FROM events WHERE id=? AND calendar_id=?', (item.get('event_id'), self.calendar_id)).fetchone():
                    raise ValueError('Target event is not app-owned.')
                self._rebase_reviewed_conflict(item)
                # An edited, definitely-unwritten operation can be rebuilt from the new proposal.
                self.db.execute("DELETE FROM operations WHERE proposal_id=? AND status IN ('pending','review')", (row['id'],))
            self.db.execute('UPDATE proposals SET payload=?,status=?,updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), item['status'], utcnow(), row['id']))
            return item

    def set_writes_paused(self, paused):
        with self.lock, self.db:
            previous = self._setting('writes_paused', 'true') == 'true'
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('writes_paused',?)", ('true' if paused else 'false',))
            if not paused or not previous:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('writes_paused_since',?)", (utcnow() if paused else '',))
        return self.status()

    def request_sync(self):
        stamp = utcnow()
        self.set_setting('sync_requested_at', stamp)
        return {'requested': True, 'requested_at': stamp}

    def _review_unwritten(self, item, reason):
        item.update(status='review', needs_review=True, review_reason=reason)
        with self.lock, self.db:
            self.db.execute("UPDATE proposals SET payload=?,status='review',updated_at=? WHERE id=?", (json.dumps(item, ensure_ascii=False), utcnow(), item['id']))
            self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id=? AND status='pending'", (reason, utcnow(), item['id']))

    def queue_operations(self, current=None):
        queued = []
        for item in self.list_proposals('pending', 500):
            try:
                checked = normalize_proposal(item, current)
            except (ValueError, TypeError, OverflowError):
                self._review_unwritten(item, 'Invalid or ambiguous event date; manual correction required.')
                continue
            if checked['status'] != 'pending':
                with self.lock, self.db:
                    self.db.execute('UPDATE proposals SET status=?,updated_at=? WHERE id=?', (checked['status'], utcnow(), item['id']))
                    self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id=? AND status='pending'", (checked.get('review_reason') or 'Proposal no longer eligible for a calendar write.', utcnow(), item['id']))
                continue
            item = {**checked, 'id': item['id'], 'message_id': item['message_id']}
            # In particular, an old one-hour deadline payload must never reach the
            # calendar when the hard checks normalized it to one all-day entry.
            with self.lock, self.db:
                self.db.execute('UPDATE proposals SET payload=?,updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), utcnow(), item['id']))
            if item['kind'] not in ('create','update','cancel') or (item['kind'] == 'cancel' and not item.get('deletion_approved')):
                continue
            message = self.get_message(item['message_id'])
            if item['kind'] == 'create' and not item.get('user_approved'):
                other_proposals = self.list_proposals(limit=500)
                duplicate = any(p['id'] != item['id'] and p.get('kind') != 'ignore' and p.get('status') in ('pending','review','applied') and start_key(p) == start_key(item) for p in other_proposals)
                duplicate = duplicate or any(start_key(saved['snapshot']) == start_key(item) for saved in self.list_events(500, include_cancelled=True))
                if duplicate:
                    self._review_unwritten(item, 'Another school event starts at the same time; confirm duplicate, overlap, or update.')
                    continue
                if item['activity_scope'] == 'unknown' and requires_registration(message):
                    self._review_unwritten(item, 'This message offers an optional activity or requires sign-up; participation must be confirmed.')
                    continue
            operation_id = digest(['operation', item['id']])[:32]
            marker = PREFIX + operation_id
            existing = self.db.execute('SELECT * FROM operations WHERE proposal_id=?', (item['id'],)).fetchone()
            existing_payload = json.loads(existing['payload']) if existing else {}
            if existing and (existing['attempts'] > 0 or existing['result'] is not None):
                # An external attempt freezes its intent, including preparation time.
                continue
            synchronization_started_at = existing_payload.get('synchronization_started_at') or utcnow()
            message = self.get_message(item['message_id'])
            event = next((event for event in self.list_events(500) if event['id'] == item.get('event_id')), None)
            if item['kind'] in ('update','cancel') and not event:
                continue
            body = event_body(item, message, marker, synchronization_started_at)
            operation = {'operation_id': operation_id, 'proposal_id': item['id'], 'message_id': item['message_id'],
                         'action': item['kind'], 'calendar_id': self.calendar_id, 'marker': marker, 'event_id': item.get('event_id'),
                         'expected_fingerprint': event.get('fingerprint') if event else None, 'event': body, 'source_message_ids': [item['message_id']],
                         'deletion_approved': item.get('deletion_approved') is True,
                         'synchronization_started_at': synchronization_started_at}
            if item['kind'] == 'cancel':
                operation.update(prior_snapshot=event['snapshot'], prior_fingerprint=event['fingerprint'], prior_marker=event['marker'])
            with self.lock, self.db:
                existing = self.db.execute('SELECT * FROM operations WHERE proposal_id=?', (item['id'],)).fetchone()
                if existing and json.loads(existing['payload']) != operation:
                    if existing['status'] == 'pending' and existing['attempts'] == 0 and existing['result'] is None:
                        self.db.execute('UPDATE operations SET payload=?,updated_at=? WHERE id=?', (json.dumps(operation, ensure_ascii=False), utcnow(), existing['id']))
                    else:
                        self._review_unwritten(item, 'Previously attempted deadline operation needs manual review before changing its calendar representation.')
                        continue
                cursor = self.db.execute('INSERT OR IGNORE INTO operations(id,proposal_id,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?)', (operation_id, item['id'], json.dumps(operation, ensure_ascii=False), 'pending', utcnow(), utcnow()))
                if cursor.rowcount:
                    queued.append(operation)
        return queued

    def reconcile_operations(self, limit=100):
        with self.lock:
            operations = []
            for row in self.db.execute("SELECT payload FROM operations WHERE status='unknown' ORDER BY updated_at,id LIMIT ?", (limit,)):
                operation = {**json.loads(row[0]), 'reconcile_only': True}
                if operation.get('action') == 'cancel' and not operation.get('prior_snapshot'):
                    # Only trusted local evidence with the original expected fingerprint may fill legacy audit fields.
                    prior = self.db.execute('SELECT * FROM events WHERE id=? AND calendar_id=?', (operation.get('event_id'), self.calendar_id)).fetchone()
                    if prior and prior['fingerprint'] == operation.get('expected_fingerprint'):
                        snapshot = json.loads(prior['snapshot'])
                        if snapshot.get('id') == operation.get('event_id') and contains_marker(snapshot.get('description'), prior['marker']) and not has_external_attendees(snapshot, self.config.calendar_owner):
                            operation.update(prior_snapshot=snapshot, prior_fingerprint=prior['fingerprint'], prior_marker=prior['marker'])
                operations.append(operation)
            return operations

    def pending_operations(self, limit=100):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT o.payload FROM operations o JOIN proposals p ON p.id=o.proposal_id WHERE o.status='pending' AND p.status='pending' ORDER BY o.created_at,o.id LIMIT ?", (limit,))]

    def recover_interrupted(self):
        # Only call under the single worker lock, never from the web process.
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE operations SET status='unknown',last_error='Interrupted while executing; reconcile before retry.',updated_at=? WHERE status='inflight'", (utcnow(),))
            metadata = self.db.execute("UPDATE metadata_refresh SET status='unknown',last_error='Interrupted metadata update; read-only reconciliation required.',updated_at=? WHERE status='inflight'", (utcnow(),))
            return cursor.rowcount + metadata.rowcount

    def claim_operation(self, operation_id):
        with self.lock, self.db:
            if self._setting('writes_paused', 'true') == 'true':
                return False
            cursor = self.db.execute("UPDATE operations SET status='inflight',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending' AND EXISTS (SELECT 1 FROM proposals p WHERE p.id=operations.proposal_id AND p.status='pending')", (utcnow(), operation_id))
            return bool(cursor.rowcount)

    def record_result(self, operation_id, result):
        if not isinstance(result, dict) or result.get('status') not in ('applied','review','unknown','failed'):
            result = {'status': 'unknown', 'error': 'Invalid execution result; possible external write.'}
        status = result['status']
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if not row:
                raise ValueError('Unknown operation.')
            operation = json.loads(row['payload'])
            if row['status'] == 'unknown' and status == 'failed':
                result = {'status': 'unknown', 'error': 'Reconciliation cannot authorize retry after an ambiguous external write.'}
                status = 'unknown'
            if status == 'applied' and (not result.get('event_id') or not isinstance(result.get('snapshot'), dict)):
                result = {'status': 'unknown', 'error': 'Applied result did not include verified event evidence.'}
                status = 'unknown'
            if status == 'applied':
                snapshot = result['snapshot']
                owned_marker = operation['marker']
                if operation['action'] == 'cancel':
                    if result.get('event_id') != operation.get('event_id') or snapshot.get('id') != operation.get('event_id') or snapshot.get('status') != 'cancelled':
                        result = {'status': 'review', 'error': 'Cancellation lacks an exact verified target tombstone.'}
                        status = 'review'
                    prior_event = self.db.execute('SELECT marker FROM events WHERE id=? AND calendar_id=?', (operation.get('event_id'), self.calendar_id)).fetchone()
                    owned_marker = prior_event[0] if prior_event else ''
                if has_external_attendees(snapshot, self.config.calendar_owner) or not contains_marker(snapshot.get('description'), owned_marker):
                    result = {'status': 'review', 'error': 'Returned event has attendees or lacks application ownership.'}
                    status = 'review'
            state = 'pending' if status == 'failed' else status
            self.db.execute('UPDATE operations SET status=?,result=?,last_error=?,updated_at=? WHERE id=?', (state, json.dumps(result, ensure_ascii=False), str(result.get('error') or '')[:500], utcnow(), operation_id))
            if status == 'applied':
                snapshot = result['snapshot']
                self.db.execute('INSERT INTO events(id,calendar_id,message_id,proposal_id,snapshot,fingerprint,marker,updated_at,state) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET proposal_id=excluded.proposal_id,snapshot=excluded.snapshot,fingerprint=excluded.fingerprint,marker=excluded.marker,updated_at=excluded.updated_at,state=excluded.state',
                                (str(result['event_id']), self.calendar_id, operation['message_id'], operation['proposal_id'], json.dumps(snapshot, ensure_ascii=False), result.get('fingerprint') or digest(snapshot), owned_marker, utcnow(), 'cancelled' if operation['action'] == 'cancel' else 'active'))
                self.db.execute("UPDATE proposals SET status='applied',updated_at=? WHERE id=?", (utcnow(), operation['proposal_id']))
            elif status == 'review':
                proposal_row = self.db.execute('SELECT payload FROM proposals WHERE id=?', (operation['proposal_id'],)).fetchone()
                if proposal_row:
                    proposal = json.loads(proposal_row[0])
                    proposal.update(needs_review=True, review_reason=str(result.get('error') or 'Calendar conflict requires manual review.')[:1000])
                    self.db.execute("UPDATE proposals SET payload=?,status='review',updated_at=? WHERE id=?", (json.dumps(proposal, ensure_ascii=False), utcnow(), operation['proposal_id']))

    def backup(self, destination=None):
        self.set_setting('last_backup_attempt_at', utcnow())
        self.set_setting('last_backup_result', 'running')
        destination = Path(destination or self.path.parent/('backup-' + datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.sqlite3')).expanduser()
        created = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            os.close(fd)
            with self.lock:
                backup = sqlite3.connect(str(destination))
                try:
                    self.db.backup(backup)
                    if [row[0] for row in backup.execute('PRAGMA integrity_check')] != ['ok']:
                        raise ValueError('Backup integrity check failed.')
                finally:
                    backup.close()
            # fsync the complete checked database before publishing the success marker.
            with destination.open('rb') as checked:
                os.fsync(checked.fileno())
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_backup_success_at',?)", (utcnow(),))
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_backup_result','ok')")
        except BaseException:
            self.set_setting('last_backup_result', 'failed')
            if created:
                destination.unlink(missing_ok=True)
            raise
        if destination.parent == self.path.parent and destination.name.startswith('backup-'):
            candidates = [path for path in self.path.parent.glob('backup-*.sqlite3') if path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid()]
            for old in sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)[7:]:
                old.unlink()
        return str(destination)

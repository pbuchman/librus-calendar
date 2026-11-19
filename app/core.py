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
from .health_state import ERROR_CODES, RESULTS, STAGES, PROBE_FIELDS, probe_metadata, read_connection_state

WARSAW = ZoneInfo('Europe/Warsaw')
from .config import AppConfig, ConfigurationError, load_config
PREFIX = 'librus-calendar:'
SOURCE_LABELS = {'message': 'wiadomość', 'announcement': 'ogłoszenie',
                 'school_event': 'terminarz szkolny', 'substitution': 'zastępstwo'}
MESSAGE_RELINK_REQUIRED = 'Source message changed; reanalysis required.'
REVIEWED_CREATE_WITH_TARGET = """(o.status='review' AND json_extract(o.payload,'$.action')='create'
    AND COALESCE(json_extract(o.payload,'$.event_id'),json_extract(o.result,'$.event_id')) IS NOT NULL)"""


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


def deadline_label(due):
    return 'do ' + due.strftime('%H:%M')


def deadline_detail(due):
    return f'Termin: {due.date().isoformat()} {deadline_label(due)} (Europe/Warsaw).'


def without_deadline_title_label(title, previous=None):
    title = re.sub(r'(?: — do \d{2}:\d{2})+$', '', title)
    # Older proposals sometimes already included the hour in the supplied title.
    return title.removesuffix(' ' + deadline_label(previous)).rstrip() if previous else title


def edited_deadline_due_at(day, value, previous):
    if not value:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?', value):
        raise ValueError('A deadline hour must be a local time.')
    wall = datetime.fromisoformat(f'{day}T{value}')
    if previous and previous.replace(tzinfo=None) == wall:
        return previous.isoformat()
    local = wall.replace(tzinfo=WARSAW)
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError('Choose a deadline time outside the Warsaw clock change.')
    return deadline_datetime(local.isoformat()).isoformat()


def apply_deadline_edit(item, patch):
    if item.get('temporal_kind') != 'deadline':
        if 'due_time' in patch:
            raise ValueError('Only deadlines can have a deadline hour.')
        return
    if 'due_time' not in patch and not (item.get('due_at') and 'start' in patch):
        return

    previous = None
    if item.get('due_at'):
        try:
            previous = deadline_datetime(item['due_at'])
        except ValueError:
            if 'due_time' not in patch:
                raise
    due_time = patch.pop('due_time', previous.time().isoformat() if previous else None)
    due_at = edited_deadline_due_at(patch.get('start', item['start']), due_time, previous)
    if due_at == item.get('due_at'):
        return
    patch['due_at'] = due_at
    if not previous:
        return
    patch['title'] = without_deadline_title_label(patch.get('title', item['title']), previous)
    description = patch.get('description', item['description']) or ''
    old_detail = deadline_detail(previous)
    patch['description'] = '\n'.join(line for line in description.split('\n') if line != old_detail).rstrip()


def apply_proposal_edit(item, patch):
    """Apply form fields against the row read inside the approval transaction."""
    apply_deadline_edit(item, patch)
    if 'end' in patch:
        end = patch['end'] or None
        fixed_end = item.get('temporal_kind') in ('deadline', 'reminder')
        if end and end != item.get('end') and not fixed_end:
            all_day = patch.get('all_day', item.get('all_day')) is True
            end = (date.fromisoformat(str(end)) if all_day else as_datetime(end)).isoformat()
        if end == item.get('end') or fixed_end:
            del patch['end']
        else:
            patch['end'] = end
    updated = {**item, **patch}
    if 'end' in patch:
        updated['default_duration'] = False
    elif item.get('default_duration') and updated.get('start') != item.get('start'):
        updated['end'] = None
    return updated


def analysis_metadata(data, proposal_count):
    """Persist only the documented provenance fields; legacy runtimes remain unknown."""
    data = data if isinstance(data, dict) else {}
    result = {key: (data[key].strip()[:limit] if isinstance(data.get(key), str) and data[key].strip() else None)
              for key, limit in (('model', 120), ('effort', 40), ('prompt_version', 120), ('decision_reason', 2000))}
    if not result['decision_reason']:
        result['decision_reason'] = ('No proposals returned; the runtime supplied no decision reason.' if not proposal_count
                                     else 'Proposals returned; the runtime supplied no decision reason.')
    return result


def normalize_proposal(data, current=None, *, participation_preference=False):
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
    for key in ('agenda_plan_id','agenda_sources','agenda_learner','canonical_action_id','source_id','source_hash','source_kind','provenance','recipient','recipient_basis','optional','date_certainty','action_identity','match_evidence','source_version_at','offer_identity','informational','alternative_group','alternative_group_id','alternative_group_evidence','user_rejected','deferred_unknown_operation','blocked_by_offer_group','blocked_by_group_decision','preference_decision'):
        if key in data:
            proposal[key] = data[key]
    if proposal.get('optional') is True:
        proposal['description'] = ('Opcjonalne. ' + proposal['description']) if not proposal['description'].startswith('Opcjonalne.') else proposal['description']
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
            title = librus_title(without_deadline_title_label(str(data['title']).strip()))
            label = deadline_label(due)
            suffix = ' — ' + label
            proposal['title'] = title if label in title else title[:250 - len(suffix)].rstrip() + suffix
            detail = deadline_detail(due)
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
        if due is not None:
            past = due.astimezone(timezone.utc) <= current.astimezone(timezone.utc)
        else:
            past = start < current.astimezone(WARSAW).date()
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
    elif (activity_scope == 'unknown' or (kind == 'create' and activity_scope == 'extracurricular' and not participation_preference)) and not proposal['user_approved']:
        proposal.update(status='review', needs_review=True, review_reason=proposal['review_reason'] or 'Extra activity or unclassified participation requires confirmation.')
    elif proposal['needs_review'] or (proposal['confidence'] != 'high' and not (activity_scope == 'school' and proposal['confidence'] == 'medium')):
        proposal.update(status='review', needs_review=True, review_reason=proposal['review_reason'] or 'Date or meaning needs confirmation.')
    else:
        proposal['status'] = 'pending'
    return proposal


def event_body(proposal, message, marker, synchronization_started_at=None):
    description = proposal['description'].strip()
    kind_label = SOURCE_LABELS.get(message.get('source_kind'), SOURCE_LABELS['message'])
    source = '\n\nŹródło: Librus, ' + kind_label + ' ' + message['id'] + '\n' + (message.get('subject') or '')
    source += '\nNadawca: ' + (message.get('sender') or '') + '\nWysłano: ' + (message.get('sent_at') or '')
    if proposal.get('source_quote'):
        source += '\nFragment: ' + proposal['source_quote']
    if proposal.get('default_duration'):
        source += '\nCzas trwania 60 minut przyjęty automatycznie'
        source += '.' if proposal.get('user_edited') else '; wiadomość nie podaje końca.'
    if proposal.get('preference_decision') and not proposal.get('user_approved'):
        source += '\nAutomatyczna akceptacja na podstawie wcześniejszych decyzji rodzica (' + str(len(proposal['preference_decision']['example_ids'])) + ' przykłady).'
    source += '\n' + marker
    field = 'date' if proposal['all_day'] else 'dateTime'
    start, end = {field: proposal['start']}, {field: proposal['end']}
    if not proposal['all_day']:
        start['timeZone'] = end['timeZone'] = 'Europe/Warsaw'
    return {'summary': librus_title(proposal['title']), 'description': synchronization_description(description + source, synchronization_started_at or utcnow(), marker), 'start': start, 'end': end,
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': value} for value in ([360] if proposal['all_day'] else [1440, 60])]},
            }


from .offer_groups import GROUP_SCHEMA, ProposalGroupsMixin
from .source_store import SourceStoreMixin


class AppStore(SourceStoreMixin, ProposalGroupsMixin):
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
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,pending_since TEXT);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,calendar_id TEXT NOT NULL,message_id TEXT NOT NULL,proposal_id TEXT NOT NULL,snapshot TEXT NOT NULL,fingerprint TEXT,marker TEXT NOT NULL,updated_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active');
            CREATE INDEX IF NOT EXISTS messages_status ON messages(status);
            CREATE INDEX IF NOT EXISTS operations_status ON operations(status);
            CREATE TABLE IF NOT EXISTS sync_runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,stage TEXT NOT NULL,result TEXT NOT NULL,error_code TEXT,duration_seconds REAL);
            CREATE INDEX IF NOT EXISTS sync_runs_started ON sync_runs(started_at);
            CREATE TABLE IF NOT EXISTS sync_incidents(
                run_id TEXT PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
                first_at TEXT,last_at TEXT,report TEXT NOT NULL CHECK(length(CAST(report AS BLOB))<=32768));
            CREATE TABLE IF NOT EXISTS calendar_probe_attempts(
                sync_run_id TEXT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL CHECK(attempt_number IN (1,2)),
                model TEXT NOT NULL,effort TEXT NOT NULL,cli_version TEXT,
                started_at TEXT NOT NULL,completed_at TEXT,result TEXT NOT NULL,safe_code TEXT,
                process_code INTEGER,started_count INTEGER,completed_count INTEGER,parse_error_count INTEGER,
                unknown_event_count INTEGER,incomplete_count INTEGER,expected_tool_name INTEGER,expected_tool_arguments INTEGER,
                PRIMARY KEY(sync_run_id,attempt_number));
            CREATE INDEX IF NOT EXISTS calendar_probe_attempts_started ON calendar_probe_attempts(started_at);
            CREATE TABLE IF NOT EXISTS analysis_audit(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,analyzed_at TEXT NOT NULL,mode TEXT NOT NULL,metadata TEXT NOT NULL,proposal_ids TEXT NOT NULL,superseded_ids TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS analysis_audit_message ON analysis_audit(message_id,analyzed_at);
            CREATE TABLE IF NOT EXISTS decision_intents(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS decision_intents_message ON decision_intents(message_id);
            CREATE TABLE IF NOT EXISTS calendar_discovery_review(id TEXT PRIMARY KEY,reason TEXT NOT NULL,updated_at TEXT NOT NULL,blocking INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS metadata_refresh(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(event_id,version));
            ''')
            self.db.executescript(GROUP_SCHEMA)
            from .preferences import SCHEMA as PREFERENCE_SCHEMA
            self.db.executescript(PREFERENCE_SCHEMA)
            if 'diagnostics' not in {row[1] for row in self.db.execute('PRAGMA table_info(sync_runs)')}:
                self.db.execute('ALTER TABLE sync_runs ADD COLUMN diagnostics TEXT')
            if 'state' not in {row[1] for row in self.db.execute('PRAGMA table_info(events)')}:
                self.db.execute("ALTER TABLE events ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
            if 'blocking' not in {row[1] for row in self.db.execute('PRAGMA table_info(calendar_discovery_review)')}:
                self.db.execute('ALTER TABLE calendar_discovery_review ADD COLUMN blocking INTEGER NOT NULL DEFAULT 1')
            if 'discovered_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                self.db.execute('ALTER TABLE messages ADD COLUMN discovered_at TEXT')
            if 'pending_since' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                self.db.execute('ALTER TABLE messages ADD COLUMN pending_since TEXT')
            if 'pending_since' not in {row[1] for row in self.db.execute('PRAGMA table_info(proposals)')}:
                self.db.execute('ALTER TABLE proposals ADD COLUMN pending_since TEXT')
            for column in ('analysis_metadata', 'analyzed_at'):
                if column not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                    self.db.execute('ALTER TABLE messages ADD COLUMN ' + column + ' TEXT')
            if self.calendar_id:
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('calendar_binding',?)", (self.calendar_id,))
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('calendar_owner_binding',?)", (self.config.calendar_owner,))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('writes_paused','true')")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('health_initialized_at',?)", (utcnow(),))
            self.db.execute("UPDATE messages SET pending_since=COALESCE(discovered_at,fetched_at,?) WHERE pending_since IS NULL AND status IN ('fetch_pending','fetch_failed','analysis_pending','analysis_failed')", (self._setting('health_initialized_at'),))
            # Legacy rows cannot prove their eligibility transition. Keep a
            # conservative creation baseline without resetting known queue age.
            self.db.execute("UPDATE proposals SET pending_since=created_at WHERE status='pending' AND pending_since IS NULL")
            self.db.execute("UPDATE proposals SET pending_since=NULL WHERE status<>'pending' AND pending_since IS NOT NULL")
            if self._setting('writes_paused') == 'true':
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('writes_paused_since',?)", (utcnow(),))
            # Safe migration only for never-attempted local operations; external intent is immutable after an attempt.
            for row in self.db.execute("SELECT id,payload FROM operations WHERE status='pending' AND attempts=0 LIMIT 100").fetchall():
                payload = json.loads(row['payload'])
                if 'extendedProperties' in payload.get('event', {}):
                    payload['event'].pop('extendedProperties', None)
                    self.db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))

            self._initialize_sources()
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('agenda_engine','simple-agenda')")
            # Retained transport enum means always-active; it cannot select an
            # engine. Deprecated config.multi_source_mode is intentionally inert.
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('multi_source_mode','active')")

    def close(self):
        self.db.close()

    def _setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def status(self):
        from .preferences import preference_status
        with self.lock:
            own_snapshot = not self.db.in_transaction
            if own_snapshot:
                self.db.execute('BEGIN')
            try:
                count = lambda table, clause='': self.db.execute('SELECT COUNT(*) FROM ' + table + clause).fetchone()[0]
                return {**read_connection_state(self.db), 'messages': count('messages'," WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=messages.id AND s.kind<>'message')"), 'messages_retry': count('messages', " WHERE status IN ('fetch_pending','fetch_failed','analysis_failed')"),
                        'proposals_pending': count('proposals', " WHERE status='pending'"), 'proposals_review': count('proposals', " WHERE status='review'"),
                        'events': count('events', " WHERE state='active'"), 'events_cancelled': count('events', " WHERE state='cancelled'"),
                        'writes_paused': self._setting('writes_paused', 'true') == 'true',
                        'last_sync': self._setting('last_sync'), 'last_error': self._setting('last_error'), 'sync_requested_at': self._setting('sync_requested_at'),
                        'calendar_id': self.calendar_id, 'timezone': 'Europe/Warsaw', 'sources': self.source_status(), 'agenda': self.agenda_status(),
                        'preferences': preference_status(self.db)}
            finally:
                if own_snapshot:
                    self.db.rollback()

    def start_sync_run(self):
        """Only called under sync.lock, so older running rows are abandoned workers."""
        stamp = utcnow()
        with self.lock, self.db:
            abandoned = self.db.execute("SELECT id,started_at FROM sync_runs WHERE result='running'").fetchall()
            for row in abandoned:
                self._close_sync_diagnostic(row['id'], 'interrupted', 'worker_interrupted', stamp)
                duration = max(0, (datetime.fromisoformat(stamp) - datetime.fromisoformat(row['started_at'])).total_seconds())
                self.db.execute("UPDATE sync_runs SET result='interrupted',error_code='worker_interrupted',finished_at=?,duration_seconds=? WHERE id=?", (stamp, duration, row['id']))
                self.db.execute("UPDATE calendar_probe_attempts SET result='interrupted',safe_code='worker_interrupted',completed_at=? WHERE sync_run_id=? AND result='running'", (stamp, row['id']))
            failures = int(self._setting('consecutive_sync_failures', '0')) + len(abandoned)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('consecutive_sync_failures',?)", (str(failures),))
            cutoff = (datetime.fromisoformat(stamp) - timedelta(days=90)).isoformat()
            self.db.execute('DELETE FROM sync_incidents WHERE run_id IN (SELECT id FROM sync_runs WHERE started_at<? AND finished_at IS NOT NULL)', (cutoff,))
            self.db.execute('DELETE FROM calendar_probe_attempts WHERE sync_run_id IN (SELECT id FROM sync_runs WHERE started_at<? AND finished_at IS NOT NULL)', (cutoff,))
            self.db.execute('DELETE FROM sync_runs WHERE started_at<? AND finished_at IS NOT NULL', (cutoff,))
            run_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO sync_runs(id,started_at,stage,result) VALUES (?,?,'starting','running')", (run_id, stamp))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_sync',?)", (stamp,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('sync_requested_at','')")
            return run_id

    def record_probe_attempt(self, run_id, metadata):
        metadata = probe_metadata(metadata)
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM sync_runs WHERE id=? AND result='running'", (run_id,)).fetchone():
                raise ValueError('Calendar probe sync run is not active.')
            prior = self.db.execute('SELECT * FROM calendar_probe_attempts WHERE sync_run_id=? AND attempt_number=?',
                                    (run_id, metadata['attempt_number'])).fetchone()
            if prior:
                immutable = ('model', 'effort', 'started_at')
                if prior['result'] != 'running' or any(prior[key] != metadata[key] for key in immutable):
                    raise ValueError('Calendar probe attempt is immutable.')
                self.db.execute('UPDATE calendar_probe_attempts SET ' + ','.join(key + '=?' for key in PROBE_FIELDS) + ' WHERE sync_run_id=? AND attempt_number=?',
                    tuple(metadata[key] for key in PROBE_FIELDS) + (run_id, metadata['attempt_number']))
            else:
                if metadata['result'] != 'running':
                    raise ValueError('Calendar probe attempt must start durably.')
                if metadata['attempt_number'] == 2:
                    first = self.db.execute('SELECT * FROM calendar_probe_attempts WHERE sync_run_id=? AND attempt_number=1', (run_id,)).fetchone()
                    clean_count_failure = (first and first['safe_code'] == 'unexpected_tool_count'
                        and first['expected_tool_name'] == 1 and first['expected_tool_arguments'] == 1
                        and all(first[key] == 0 for key in ('parse_error_count', 'unknown_event_count', 'incomplete_count')))
                    if not first or first['result'] != 'failed' or not (first['safe_code'] == 'codex_timeout' or clean_count_failure):
                        raise ValueError('Calendar probe retry evidence is missing.')
                self.db.execute('INSERT INTO calendar_probe_attempts(sync_run_id,' + ','.join(PROBE_FIELDS) + ') VALUES (' + ','.join('?' for _ in range(len(PROBE_FIELDS) + 1)) + ')',
                    (run_id,) + tuple(metadata[key] for key in PROBE_FIELDS))

    def record_sync_diagnostic(self, run_id, metadata):
        from .health_state import sync_diagnostics
        # Replace the current invocation on completion, retaining its durable start.
        with self.lock, self.db:
            row = self.db.execute("SELECT diagnostics FROM sync_runs WHERE id=? AND result='running'", (run_id,)).fetchone()
            if row is None:
                raise ValueError('Sync run is not active.')
            value = json.loads(row[0]) if row[0] else {'invocations': [], 'omitted_count': 0}
            sync_diagnostics({'invocations': [metadata], 'omitted_count': 0})
            entries = value['invocations']
            previous = entries[-1] if entries else {}
            if 'attempt_started_at' in metadata:
                retries = value.setdefault('timeout_retries', [])
                if (metadata['state'] == 'running' and metadata.get('attempt_number') == 2
                        and previous.get('state') == 'failed' and previous.get('attempt_number') == 1
                        and previous.get('retry_decision') == 'retry' and previous.get('retry_reason') == 'app_timeout'
                        and all(key in metadata and previous.get(key) == metadata[key]
                                for key in ('logical_call_index', 'operation_kind', 'phase'))):
                    retries.append([metadata['logical_call_index'], metadata['attempt_started_at']])
            if previous.get('state') == 'running':
                entries[-1] = metadata
                if 'attempt_finished_at' in metadata:
                    self._finish_timeout_retry(value, metadata, metadata['attempt_finished_at'])
            elif metadata['state'] == 'running':
                if len(entries) == 512:
                    entries.pop(0)
                    value['omitted_count'] += 1
                entries.append(metadata)
            else:
                raise ValueError('Invocation must start durably.')
            self._bound_sync_diagnostics(value)
            sync_diagnostics(value)
            self.db.execute('UPDATE sync_runs SET diagnostics=? WHERE id=?', (json.dumps(value), run_id))

    @staticmethod
    def _finish_timeout_retry(value, attempt, finished_at):
        retries = value.get('timeout_retries')
        if (retries and attempt.get('attempt_number') == 2
                and retries[-1] == [attempt.get('logical_call_index'), attempt.get('attempt_started_at')]):
            outcome = 'recovered' if attempt['state'] == 'completed' and attempt.get('recovered') else 'failed'
            retries[-1].extend([finished_at, outcome])

    @staticmethod
    def _bound_sync_diagnostics(value):
        while len(value['invocations']) > 512 or len(json.dumps(value)) > 131072:
            value['invocations'].pop(0)
            value['omitted_count'] += 1

    def sync_stage(self, run_id, stage):
        if stage not in STAGES:
            raise ValueError('Invalid technical sync stage.')
        with self.lock, self.db:
            self.db.execute("UPDATE sync_runs SET stage=? WHERE id=? AND result='running'", (stage, run_id))

    def _close_sync_diagnostic(self, run_id, state, error_code, stamp):
        row = self.db.execute('SELECT diagnostics FROM sync_runs WHERE id=?', (run_id,)).fetchone()
        if row and row[0]:
            value = json.loads(row[0])
            if value['invocations'] and value['invocations'][-1]['state'] == 'running':
                attempt = value['invocations'][-1]
                attempt.update(state=state, error_code=error_code or 'operation_failed')
                if 'attempt_started_at' in attempt:
                    started = datetime.fromisoformat(attempt['attempt_started_at'])
                    attempt['attempt_finished_at'] = max(datetime.fromisoformat(stamp), started).isoformat()
                    self._finish_timeout_retry(value, attempt, attempt['attempt_finished_at'])
                self._bound_sync_diagnostics(value)
                self.db.execute('UPDATE sync_runs SET diagnostics=? WHERE id=?', (json.dumps(value), run_id))

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
            self._close_sync_diagnostic(run_id, 'interrupted' if result == 'interrupted' else 'failed', error_code, stamp)
            self.db.execute('UPDATE sync_runs SET finished_at=?,result=?,error_code=?,duration_seconds=? WHERE id=?', (stamp, result, error_code, duration_seconds, run_id))
            if result in ('failed', 'interrupted'):
                self.db.execute('UPDATE calendar_probe_attempts SET completed_at=?,result=?,safe_code=? WHERE sync_run_id=? AND result=\'running\'',
                    (stamp, 'interrupted' if result == 'interrupted' else 'failed', error_code or 'operation_failed', run_id))
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
            return [self._message(row) for row in self.db.execute("SELECT * FROM messages WHERE status!='retained' AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=messages.id AND s.kind<>'message') ORDER BY COALESCE(sent_at,fetched_at) DESC,id LIMIT ?", (min(max(int(limit), 1), 500),))]

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
                for proposal in self.db.execute("SELECT * FROM proposals WHERE message_id=? AND status IN ('pending','review')", (message_id,)).fetchall():
                    if not self._can_relink_pending_approval(self._proposal(proposal)):
                        self.db.execute("UPDATE proposals SET status='ignored',updated_at=?,pending_since=NULL WHERE id=?", (utcnow(), proposal['id']))
                self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id IN (SELECT id FROM proposals WHERE message_id=?) AND status='pending'", (MESSAGE_RELINK_REQUIRED, utcnow(), message_id))
            self._mirror_message_source(self.db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone())
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
        from .offer_groups import validate_alternatives
        proposals = [dict(item) if isinstance(item,dict) else item for item in proposals]
        validate_alternatives(message, proposals)
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
            if isinstance(raw, dict) and raw.get('alternative_group'):
                item.update(alternative_group=raw['alternative_group'], alternative_group_evidence=raw['alternative_group_evidence'])
                if item['status'] != 'ignored':
                    item.update(status='review', needs_review=True, review_reason='Choose one alternative from the complete offer group.')
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
            from .data_admin import human_event_review_reason, protect_decision
            item = protect_decision(self, message, item)
            item = self._protect_group_decision(message,item)
            prior_group = self.get_proposal_group_for_proposal(item['id'], current)
            if prior_group and prior_group['status'] != 'open':
                item.pop('alternative_group', None)
                item.pop('alternative_group_evidence', None)
            reason = human_event_review_reason(self, item)
            if reason:
                item.update(status='review', needs_review=True, review_reason=reason)
            normalized.append(item)
        return normalized

    def _save_analysis(self, message, normalized, metadata, mode, superseded_ids=()):
        stamp = utcnow()
        self.db.execute("UPDATE sources SET status='analyzed',analyzed_at=?,outcome='added',reason=NULL WHERE id=?",(stamp,'message:'+message['id']))
        metadata = analysis_metadata(metadata, len(normalized))
        self._retire_open_groups(message['id'], keep_source_hash=message['payload_hash'])
        for item in normalized:
            self.db.execute('INSERT OR IGNORE INTO proposals(id,message_id,source_hash,payload,status,created_at,updated_at,pending_since) VALUES (?,?,?,?,?,?,?,?)',
                            (item['id'], message['id'], message['payload_hash'], json.dumps(item, ensure_ascii=False), item['status'], stamp, stamp,
                             stamp if item['status'] == 'pending' else None))
        self._save_proposal_groups(message, normalized)
        for item in normalized:
            self._register_legacy_action(self.db.execute('SELECT * FROM proposals WHERE id=?',(item['id'],)).fetchone())
        self.db.execute("UPDATE messages SET analyzed_hash=payload_hash,status='analyzed',last_error=NULL,pending_since=NULL,analysis_metadata=?,analyzed_at=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), stamp, message['id']))
        self.db.execute('INSERT INTO analysis_audit VALUES (?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, message['id'], message['payload_hash'], stamp, mode, json.dumps(metadata, ensure_ascii=False),
                         json.dumps([item['id'] for item in normalized]), json.dumps(list(superseded_ids))))

    def save_proposals(self, message_id, proposals, current=None, metadata=None):
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            normalized = self._prepare_proposals(message_id, proposals, current)
            self._save_analysis(self.get_message(message_id), normalized, metadata, 'sync')
        return normalized

    def get_analysis_history(self, message_id, limit=20):
        with self.lock:
            rows = self.db.execute('SELECT * FROM analysis_audit WHERE message_id=? ORDER BY analyzed_at DESC,rowid DESC LIMIT ?',
                                   (str(message_id), min(max(int(limit), 1), 100))).fetchall()
            return [{**dict(row), **{key: json.loads(row[key]) for key in ('metadata', 'proposal_ids', 'superseded_ids')}} for row in rows]

    def cached_reanalysis_scope(self, message_id):
        raise ValueError('Cached subset reanalysis is retired; use complete agenda preview.')

    def prepare_cached_reanalysis(self, *args, **kwargs):
        raise ValueError('Cached subset reanalysis is retired; use complete agenda preview.')

    def replace_cached_proposals(self, *args, **kwargs):
        raise ValueError('Cached subset reanalysis is retired; use complete agenda preview.')

    def _proposal(self, row):
        data = json.loads(row['payload'])
        data.update(id=row['id'], message_id=row['message_id'], status=row['status'])
        return data

    def get_proposal(self, proposal_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM proposals WHERE id=?', (str(proposal_id),)).fetchone()
            return self._proposal(row) if row else None

    def list_proposals(self, status=None, limit=100, *, offset=0, exclude_grouped=False):
        filters, params = [], []
        if status:
            filters.append('status=?')
            params.append(status)
        if exclude_grouped:
            filters.append('NOT EXISTS (SELECT 1 FROM proposal_group_members m WHERE m.proposal_id=proposals.id)')
        sql = 'SELECT * FROM proposals'
        if filters:
            sql += ' WHERE ' + ' AND '.join(filters)
        sql += ' ORDER BY created_at DESC,id LIMIT ? OFFSET ?'
        with self.lock:
            return [self._proposal(row) for row in self.db.execute(sql, (*params, limit, offset))]

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

    def _verified_create_snapshot(self, operation, result, event_id):
        from .codex_runtime import event_fingerprint, marker_present
        snapshot = result.get('snapshot')
        if (operation['calendar_id'] == self.calendar_id and event_id and event_id == result.get('event_id')
                and isinstance(snapshot, dict) and snapshot.get('id') == event_id and snapshot.get('status') != 'cancelled'
                and marker_present(snapshot.get('description'), operation['marker'])
                and not has_external_attendees(snapshot, self.config.calendar_owner) and not snapshot.get('recurrence')
                and result.get('fingerprint') == event_fingerprint(snapshot, self.config.calendar_owner)):
            return snapshot
        return None

    def _approve_reviewed_create(self, original, item):
        """Rebase a confirmed creation as an update of its exact Calendar ID."""
        row = self.db.execute("SELECT * FROM operations WHERE proposal_id=? AND status IN ('pending','review')",
                              (item['id'],)).fetchone()
        if not row or not row['result']:
            return False
        operation = json.loads(row['payload'])
        result = json.loads(row['result'])
        history = operation.get('reviewed_create')
        if not history and not (operation['action'] == 'create' and result.get('error') in
                                ('write_verification_mismatch', 'existing_event_changed', 'duplicate_marker')):
            return False
        if item['status'] != 'pending':
            return True
        from .codex_runtime import normalize_event
        event_id = operation.get('event_id') or result.get('event_id')
        marker = operation['marker']
        snapshot = self._verified_create_snapshot(operation, result, event_id)
        if not snapshot or result.get('error') not in ('write_verification_mismatch', 'existing_event_changed', 'manual_edit_conflict'):
            raise ValueError('The reviewed create lacks safe evidence of an exact owned Calendar event.')
        owned = self.db.execute('SELECT calendar_id,marker,state FROM events WHERE id=?', (event_id,)).fetchone()
        if owned and (owned['calendar_id'], owned['marker'], owned['state']) != (self.calendar_id, marker, 'active'):
            raise ValueError('The reviewed Calendar event conflicts with existing local ownership.')

        writable = ('summary', 'description', 'start', 'end', 'location', 'reminders')
        observed = normalize_event(snapshot, self.config.calendar_owner)
        body = {**{field: observed[field] for field in writable}, 'description': snapshot['description']}
        stamp = operation.get('synchronization_started_at') or utcnow()
        source = self.get_message(item['message_id'])
        # Approval also changes how learned provenance is rendered. That metadata
        # transition is not a user edit to the Calendar description.
        before = event_body({**original, 'user_approved': item['user_approved']}, source, marker, stamp)
        after = event_body(item, source, marker, stamp)
        fields = {field for field in ('summary', 'description', 'start', 'end') if before[field] != after[field]}
        # Keep corrections already approved if a later remote edit interrupts the update.
        fields.update(operation.get('approved_fields', ()))
        body.update({field: after[field] for field in fields})
        start, end = body['start'], body['end']
        date_field = 'date' if 'date' in start else 'dateTime'
        if date_field not in start or date_field not in end or as_datetime(end[date_field]) <= as_datetime(start[date_field]):
            raise ValueError('The reviewed Calendar interval requires an explicit start and end correction.')
        history = history or {'event': operation['event'], 'result': result}
        operation.update(action='update', event_id=event_id, expected_fingerprint=result['fingerprint'],
                         event=body, reviewed_create=history, approved_fields=sorted(fields),
                         prior_snapshot={'start': observed['start'], 'end': observed['end']})
        self.db.execute("UPDATE operations SET payload=?,status='pending',last_error=NULL,updated_at=? WHERE id=?",
                        (json.dumps(operation, ensure_ascii=False), utcnow(), row['id']))
        item.update(kind='update', event_id=event_id)
        return True

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
            if json.loads(row['payload']).get('blocked_by_offer_group') and decision != 'ignore':
                raise ValueError('This option belongs to an existing offer; reconcile the complete membership before approval.')
            deferred = json.loads(row['payload']).get('deferred_unknown_operation')
            if deferred and decision != 'ignore' and self.write_needs_reconciliation(deferred):
                raise ValueError('Reconcile the prior write before approving this change.')
            if json.loads(row['payload']).get('blocked_by_group_decision') and decision != 'ignore':
                raise ValueError('A prior group decision blocks this ambiguous replacement.')
            group = self.get_proposal_group_for_proposal(proposal_id)
            if not group and json.loads(row['payload']).get('alternative_group') and decision != 'ignore':
                raise ValueError('Complete alternative membership must be reconciled before this option can be approved.')
            if group:
                if group['status'] != 'selected' or group['selected_proposal_id'] != str(proposal_id) or row['status'] != 'review':
                    raise ValueError('Use the complete alternative group to choose or reject this proposal.')
                group_reason = self._group_write_reason(proposal_id)
                if group_reason:
                    raise ValueError(group_reason)
            if row['status'] in ('applied', 'superseded'):
                raise ValueError('An applied or superseded proposal cannot be replayed.')
            active = self.db.execute("SELECT status FROM operations WHERE proposal_id=? AND status IN ('inflight','unknown','applied')", (str(proposal_id),)).fetchone()
            if active:
                raise ValueError('Resolve the existing operation before changing this proposal.')
            original = self._proposal(row)
            if decision == 'ignore':
                item = dict(original, status='ignored')
                item['user_rejected'] = True
                self.db.execute("UPDATE operations SET status='review',last_error='User ignored proposal.',updated_at=? WHERE proposal_id=? AND status='pending'", (utcnow(), str(proposal_id)))
            else:
                patch = dict(patch or {})
                if set(patch) - {'title','description','start','end','all_day','due_time'}:
                    raise ValueError('Unsupported proposal edit.')
                item = apply_proposal_edit(original, patch)
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
                edited_fields = patch.keys() | {'default_duration'} if 'end' in patch else patch.keys()
                item['user_edited'] = item['user_edited'] or any(
                    item.get(field) != original.get(field) for field in edited_fields)
                if not self._approve_reviewed_create(original, item):
                    if item['kind'] == 'update' and not self.db.execute('SELECT 1 FROM events WHERE id=? AND calendar_id=?', (item.get('event_id'), self.calendar_id)).fetchone():
                        raise ValueError('Target event is not app-owned.')
                    self._rebase_reviewed_conflict(item)
                    # Definitely unwritten work can be rebuilt from the new proposal.
                    self.db.execute("DELETE FROM operations WHERE proposal_id=? AND status IN ('pending','review')", (row['id'],))
            stamp = utcnow()
            pending_since = ((row['pending_since'] or row['created_at']) if row['status'] == 'pending' else stamp) if item['status'] == 'pending' else None
            self.db.execute('UPDATE proposals SET payload=?,status=?,updated_at=?,pending_since=? WHERE id=?',
                            (json.dumps(item, ensure_ascii=False), item['status'], stamp, pending_since, row['id']))
            from .data_admin import remember_decision
            remember_decision(self, row, item, original=original)
            self.remember_canonical_decision(row['id'])
            from .preferences import remember_proposal_choice
            remember_proposal_choice(self, row, item, decision)
            self.db.execute('UPDATE canonical_actions SET payload=?,status=?,updated_at=? WHERE proposal_id=?', (json.dumps(item,ensure_ascii=False),item['status'],stamp,row['id']))
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
        with self.lock, self.db:
            self._review_unwritten_locked(item, reason)

    def _review_unwritten_locked(self, item, reason):
        item.update(status='review', needs_review=True, review_reason=reason)
        self.db.execute("UPDATE proposals SET payload=?,status='review',updated_at=?,pending_since=NULL WHERE id=?", (json.dumps(item, ensure_ascii=False), utcnow(), item['id']))
        self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id=? AND status='pending'", (reason, utcnow(), item['id']))

    def _can_relink_pending_approval(self, item):
        return (item['status'] == 'pending' and item.get('kind') in ('create', 'update')
                and item.get('user_approved') is True and not item.get('user_rejected')
                and bool(item.get('agenda_plan_id')) and bool(item.get('agenda_sources'))
                and not (item.get('alternative_group') or item.get('alternative_group_id'))
                and not self.db.execute("SELECT 1 FROM operations WHERE proposal_id=? AND (attempts>0 OR result IS NOT NULL)",
                                        (item['id'],)).fetchone())

    def changed_approved_message_refs(self, item):
        if not self._can_relink_pending_approval(item):
            return set()
        changed = set()
        for ref in item['agenda_sources']:
            source = self.get_source(ref['source_id'])
            if source and source['kind'] == 'message' and source['content_hash'] != ref['source_hash']:
                changed.add((source['id'], source['content_hash']))
        return changed

    def _defer_changed_message_write(self, item):
        if not self.changed_approved_message_refs(item):
            return False
        self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id=? AND status='pending'",
                        (MESSAGE_RELINK_REQUIRED, utcnow(), item['id']))
        return True

    def queue_operations(self, current=None):
        queued = []
        for listed in self.list_proposals('pending', 500):
            from .data_admin import human_event_review_reason
            # Serialize the full local preparation against UI decisions. Nothing
            # in this transaction contacts Calendar or another external service.
            with self.lock, self.db:
                if not self.db.in_transaction:
                    self.db.execute('BEGIN IMMEDIATE')
                row = self.db.execute("SELECT * FROM proposals WHERE id=? AND status='pending'", (listed['id'],)).fetchone()
                if not row:
                    continue
                item = self._proposal(row)
                if self.source_mode_blocked(item) or self.source_write_deferred(item) or self._defer_changed_message_write(item):
                    continue
                group_reason = self._source_write_reason(item) or self._group_write_reason(item['id'])
                if group_reason:
                    self._review_unwritten_locked(item, group_reason)
                    continue
                discovery_unsafe = (self._setting('rebuild_status') in ('required', 'running', 'partial', 'interrupted')
                                    or self.db.execute('SELECT 1 FROM calendar_discovery_review WHERE blocking=1 LIMIT 1').fetchone())
                if discovery_unsafe:
                    self._review_unwritten_locked(item, 'Calendar reconstruction is incomplete or ambiguous; no new write is permitted.')
                    continue
                reason = human_event_review_reason(self, item)
                if reason:
                    self._review_unwritten_locked(item, reason)
                    continue
                try:
                    from .preferences import authorizes_participation
                    checked = (normalize_proposal(item, current, participation_preference=True)
                               if authorizes_participation(self, item) else normalize_proposal(item, current))
                except (ValueError, TypeError, OverflowError):
                    self._review_unwritten_locked(item, 'Invalid or ambiguous event date; manual correction required.')
                    continue
                if checked['status'] != 'pending':
                    self.db.execute('UPDATE proposals SET status=?,updated_at=?,pending_since=NULL WHERE id=?', (checked['status'], utcnow(), item['id']))
                    self.db.execute("UPDATE operations SET status='review',last_error=?,updated_at=? WHERE proposal_id=? AND status='pending'", (checked.get('review_reason') or 'Proposal no longer eligible for a calendar write.', utcnow(), item['id']))
                    continue
                item = {**checked, 'id': item['id'], 'message_id': item['message_id']}
                # An old one-hour deadline payload must never reach Calendar when
                # the hard checks normalized it to one all-day entry.
                self.db.execute('UPDATE proposals SET payload=?,updated_at=? WHERE id=?', (json.dumps(item, ensure_ascii=False), utcnow(), item['id']))
                if item['kind'] not in ('create','update','cancel') or (item['kind'] == 'cancel' and not item.get('deletion_approved')):
                    continue
                message = self.get_message(item['message_id'])
                if item['kind'] == 'create' and not item.get('user_approved') and not item.get('canonical_action_id'):
                    other_proposals = self.list_proposals(limit=500)
                    duplicate = any(p['id'] != item['id'] and p.get('kind') != 'ignore' and p.get('status') in ('pending','review','applied') and start_key(p) == start_key(item) for p in other_proposals)
                    duplicate = duplicate or any(start_key(saved['snapshot']) == start_key(item) for saved in self.list_events(500, include_cancelled=True))
                    if duplicate:
                        self._review_unwritten_locked(item, 'Another school event starts at the same time; confirm duplicate, overlap, or update.')
                        continue
                    if item['activity_scope'] == 'unknown' and requires_registration(message):
                        self._review_unwritten_locked(item, 'This message offers an optional activity or requires sign-up; participation must be confirmed.')
                        continue
                operation_id = digest(['operation', item['id']])[:32]
                marker = PREFIX + operation_id
                existing = self.db.execute('SELECT * FROM operations WHERE proposal_id=?', (item['id'],)).fetchone()
                existing_payload = json.loads(existing['payload']) if existing else {}
                if existing and (existing['attempts'] > 0 or existing['result'] is not None):
                    # An external attempt freezes its intent, including preparation time.
                    continue
                synchronization_started_at = existing_payload.get('synchronization_started_at') or utcnow()
                event = next((event for event in self.list_events(500) if event['id'] == item.get('event_id')), None)
                if item['kind'] in ('update','cancel') and not event:
                    continue
                body = event_body(item, message, marker, synchronization_started_at)
                operation = {'operation_id': operation_id, 'proposal_id': item['id'], 'message_id': item['message_id'],
                             'action': item['kind'], 'calendar_id': self.calendar_id, 'marker': marker, 'event_id': item.get('event_id'),
                             'expected_fingerprint': event.get('fingerprint') if event else None, 'event': body, 'source_message_ids': [item['message_id']],
                             **({'source_ids': [r[0] for r in self.db.execute("SELECT source_id FROM action_sources WHERE action_id=? AND relation NOT IN ('corroborates_unverified_recipient','expired_unverified_fact','retains_closed_offer_decision')",(item['canonical_action_id'],))]} if item.get('canonical_action_id') else {}),
                             'deletion_approved': item.get('deletion_approved') is True,
                             'synchronization_started_at': synchronization_started_at}
                if item['kind'] == 'cancel':
                    operation.update(prior_snapshot=event['snapshot'], prior_fingerprint=event['fingerprint'], prior_marker=event['marker'])
                if (existing and existing['status'] == 'review' and existing['last_error'] == MESSAGE_RELINK_REQUIRED
                        and self._can_relink_pending_approval(item)):
                    self.db.execute("UPDATE operations SET payload=?,status='pending',last_error=NULL,updated_at=? WHERE id=?",
                                    (json.dumps(operation, ensure_ascii=False), utcnow(), existing['id']))
                    queued.append(operation)
                    continue
                if existing and json.loads(existing['payload']) != operation:
                    if existing['status'] == 'pending' and existing['attempts'] == 0 and existing['result'] is None:
                        self.db.execute('UPDATE operations SET payload=?,updated_at=? WHERE id=?', (json.dumps(operation, ensure_ascii=False), utcnow(), existing['id']))
                    else:
                        self._review_unwritten_locked(item, 'Previously attempted deadline operation needs manual review before changing its calendar representation.')
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

    def write_needs_reconciliation(self, operation_id):
        return self.db.execute(f"SELECT 1 FROM operations o WHERE id=? AND (o.status IN ('unknown','inflight') OR {REVIEWED_CREATE_WITH_TARGET})",
                               (operation_id,)).fetchone() is not None

    def recover_interrupted(self):
        # Only call under the single worker lock, never from the web process.
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE operations SET status='unknown',last_error='Interrupted while executing; reconcile before retry.',updated_at=? WHERE status='inflight'", (utcnow(),))
            metadata = self.db.execute("UPDATE metadata_refresh SET status='unknown',last_error='Interrupted metadata update; read-only reconciliation required.',updated_at=? WHERE status='inflight'", (utcnow(),))
            return cursor.rowcount + metadata.rowcount

    def claim_operation(self, operation_id, expected_operation=None, *, incident_position=0):
        from contextlib import contextmanager
        from .incidents import active, checkpoint, emit, observe
        from .planner_incidents import incident_error_code, operation_action

        outcome = None
        action = operation_action(expected_operation)

        def selected(reason, decision='skipped', *, incident=False, **evidence):
            nonlocal outcome
            outcome = (emit if incident else observe, reason, decision, evidence)

        @contextmanager
        def report_after_commit():
            try:
                yield
            except Exception as error:
                emit('planner', 'claim', 'claim_transaction_failed', 'failed', action=action,
                     position=incident_position, error_code=incident_error_code(error))
                raise
            if outcome is not None:
                report, reason, decision, evidence = outcome
                report('planner', 'claim', reason, decision, action=action, position=incident_position, **evidence)

        checkpoint('planner', 'claim')
        with report_after_commit(), self.lock, self.db:
            # A web approval uses another connection. Reserve the write lock
            # before reading either intent so stale approval cannot authorize a
            # different target, and legacy queued AI changes cannot bypass review.
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            operation_row = self.db.execute("SELECT * FROM operations WHERE id=? AND status='pending'", (operation_id,)).fetchone()
            if not operation_row:
                selected('operation_not_pending', incident=True)
                return False
            proposal_row = self.db.execute("SELECT * FROM proposals WHERE id=? AND status='pending'", (operation_row['proposal_id'],)).fetchone()
            if not proposal_row:
                selected('proposal_not_pending', incident=True)
                return False
            item = self._proposal(proposal_row)
            if expected_operation is None:
                action = operation_action({'action': item.get('kind')})
            if self.source_mode_blocked(item):
                selected('source_mode_blocked', 'deferred')
                return False
            if self.source_write_deferred(item):
                selected('source_write_deferred', 'deferred')
                return False
            if self._defer_changed_message_write(item):
                selected('source_version_changed', 'deferred', incident=True)
                return False
            reason = self._source_write_reason(item)
            if reason:
                self._review_unwritten_locked(item, reason)
                selected('source_authority_review', 'review')
                return False
            reason = self._group_write_reason(item['id'])
            if reason:
                self._review_unwritten_locked(item, reason)
                selected('offer_decision_review', 'review')
                return False
            operation = json.loads(operation_row['payload'])
            if expected_operation is not None and operation != expected_operation:
                # A newer explicit edit keeps its pending intent. The caller's
                # stale cached body cannot be executed under the reused ID.
                selected('payload_changed', incident=True)
                return False
            binding = {'operation_id': operation_id, 'proposal_id': item['id'], 'calendar_id': self.calendar_id,
                       'action': item.get('kind'), 'event_id': item.get('event_id')}
            changed_binding = [key for key, expected in binding.items() if operation.get(key) != expected]
            if changed_binding:
                self._review_unwritten_locked(item, 'Queued operation no longer matches the current action and target; review required.')
                selected('target_binding_changed', 'review', incident=True, binding_fields=changed_binding)
                return False
            from .data_admin import human_event_review_reason
            reason = human_event_review_reason(self, item)
            if reason:
                self._review_unwritten_locked(item, reason)
                selected('human_authority_review', 'review')
                return False
            if self._setting('writes_paused', 'true') == 'true':
                selected('writes_paused')
                return False
            cursor = self.db.execute("""UPDATE operations SET status='inflight',attempts=attempts+1,updated_at=?
                WHERE id=? AND status='pending'
                AND EXISTS (SELECT 1 FROM proposals p WHERE p.id=operations.proposal_id AND p.status='pending')
                AND NOT EXISTS (SELECT 1 FROM settings WHERE key='rebuild_status' AND value IN ('required','running','partial','interrupted'))
                AND NOT EXISTS (SELECT 1 FROM calendar_discovery_review WHERE blocking=1)""", (utcnow(), operation_id))
            claimed = bool(cursor.rowcount)
            if claimed:
                selected('claimed', 'committed')
            elif active():
                checks = []
                try:
                    blockers = self.db.execute("""SELECT
                        EXISTS (SELECT 1 FROM settings WHERE key='rebuild_status' AND value IN ('required','running','partial','interrupted')),
                        EXISTS (SELECT 1 FROM calendar_discovery_review WHERE blocking=1)""").fetchone()
                    checks = [check for check, blocked in zip(('rebuild_clear', 'calendar_discovery_clear'), blockers) if blocked]
                except sqlite3.Error:
                    pass
                selected('conditional_update_missed', incident=True, claim_checks=checks)
            return claimed

    def hydrate_cancel_operation(self, operation_id, snapshot):
        """Freeze the verified full pre-delete snapshot before claiming a write."""
        from .codex_runtime import event_fingerprint
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            prior_result = json.loads(row['result']) if row and row['result'] else {}
            if not row or row['status'] != 'pending' or row['attempts'] != 0 or (prior_result and prior_result.get('status') != 'failed'):
                raise ValueError('Only an untouched cancellation intent may be hydrated.')
            operation = json.loads(row['payload'])
            marker = operation.get('prior_marker')
            if (operation.get('action') != 'cancel' or not isinstance(snapshot, dict)
                    or snapshot.get('id') != operation.get('event_id') or snapshot.get('status') == 'cancelled'
                    or not contains_marker(snapshot.get('description'), marker)
                    or has_external_attendees(snapshot, self.config.calendar_owner)
                    or event_fingerprint(snapshot, self.config.calendar_owner) != operation.get('expected_fingerprint')):
                raise ValueError('Cancellation hydration lacks the exact unchanged owned baseline.')
            operation.update(prior_snapshot=snapshot, prior_fingerprint=operation['expected_fingerprint'])
            self.db.execute('UPDATE operations SET payload=?,updated_at=? WHERE id=?',
                            (json.dumps(operation, ensure_ascii=False), utcnow(), operation_id))
            return operation

    def _register_reviewed_create_target(self, operation, result):
        """Keep the exact owned ID and the attempted baseline during review."""
        from .codex_runtime import event_fingerprint
        event_id = operation['event_id']
        if (result.get('error') not in ('write_verification_mismatch', 'existing_event_changed')
                or not self._verified_create_snapshot(operation, result, event_id)):
            return False
        baseline = dict(operation['event'], id=event_id)
        inserted = self.db.execute("""INSERT INTO events(id,calendar_id,message_id,proposal_id,snapshot,fingerprint,marker,updated_at,state)
            VALUES (?,?,?,?,?,?,?,?,'active') ON CONFLICT(id) DO NOTHING""",
            (event_id, self.calendar_id, operation['message_id'], operation['proposal_id'], json.dumps(baseline, ensure_ascii=False),
             event_fingerprint(baseline, self.config.calendar_owner), operation['marker'], utcnow()))
        return bool(inserted.rowcount)

    def record_result(self, operation_id, result, *, agenda_calendar=None, current=None, incident_position=0):
        from .incidents import checkpoint, emit, observe
        from .planner_incidents import execution_code, execution_status, operation_action

        runtime_status = execution_status(result)
        runtime_code = execution_code(result)
        journal_reason = 'result_recorded'
        failed_checks = []
        if not isinstance(result, dict) or result.get('status') not in ('applied','review','unknown','failed'):
            result = {'status': 'unknown', 'error': 'Invalid execution result; possible external write.'}
            journal_reason = 'invalid_runtime_result'
        status = result['status']
        checkpoint('planner', 'journal')
        with self.lock, self.db:
            from .agenda import checkpoint_before_result, checkpoint_after_result
            advance_agenda = checkpoint_before_result(self, agenda_calendar, current)
            row = self.db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if not row:
                raise ValueError('Unknown operation.')
            operation = json.loads(row['payload'])
            if row['status'] == 'unknown' and status == 'failed':
                result = {'status': 'unknown', 'error': 'Reconciliation cannot authorize retry after an ambiguous external write.'}
                status = 'unknown'
                journal_reason = 'reconcile_retry_blocked'
            if status == 'applied' and (not result.get('event_id') or not isinstance(result.get('snapshot'), dict)):
                failed_checks.append('event_id_present' if not result.get('event_id') else 'snapshot_object')
                result = {'status': 'unknown', 'error': 'Applied result did not include verified event evidence.'}
                status = 'unknown'
                journal_reason = 'missing_verified_event'
            if status == 'applied':
                snapshot = result['snapshot']
                owned_marker = operation['marker']
                if operation['action'] == 'cancel':
                    target_matches = result.get('event_id') == operation.get('event_id')
                    snapshot_matches = target_matches and snapshot.get('id') == operation.get('event_id')
                    if not snapshot_matches or snapshot.get('status') != 'cancelled':
                        result = {'status': 'review', 'error': 'Cancellation lacks an exact verified target tombstone.'}
                        status = 'review'
                        journal_reason = 'cancel_tombstone_mismatch'
                        failed_checks.append('result_target_matches' if not target_matches else
                                             'snapshot_target_matches' if not snapshot_matches else 'cancelled_status')
                    prior_event = self.db.execute('SELECT marker FROM events WHERE id=? AND calendar_id=?', (operation.get('event_id'), self.calendar_id)).fetchone()
                    owned_marker = prior_event[0] if prior_event else ''
                attendees = has_external_attendees(snapshot, self.config.calendar_owner)
                if attendees or not contains_marker(snapshot.get('description'), owned_marker):
                    result = {'status': 'review', 'error': 'Returned event has attendees or lacks application ownership.'}
                    status = 'review'
                    journal_reason = 'ownership_or_attendee_mismatch'
                    failed_checks.append('attendees_absent' if attendees else 'ownership_marker_present')
            if operation['action'] == 'create' and status in ('unknown', 'review') and result.get('event_id'):
                # Definite identity survives a failed verification read and later approval.
                operation['event_id'] = operation.get('event_id') or result['event_id']
                operation.setdefault('reviewed_create', {'event': operation['event'], 'result': result})
                if status == 'review' and self._register_reviewed_create_target(operation, result) and agenda_calendar is not None:
                    agenda_calendar[operation['event_id']] = result['snapshot']
            state = 'pending' if status == 'failed' else status
            self.db.execute('UPDATE operations SET payload=?,status=?,result=?,last_error=?,updated_at=? WHERE id=?', (json.dumps(operation, ensure_ascii=False), state, json.dumps(result, ensure_ascii=False), str(result.get('error') or '')[:500], utcnow(), operation_id))
            if status == 'applied':
                snapshot = result['snapshot']
                self.db.execute('INSERT INTO events(id,calendar_id,message_id,proposal_id,snapshot,fingerprint,marker,updated_at,state) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET proposal_id=excluded.proposal_id,snapshot=excluded.snapshot,fingerprint=excluded.fingerprint,marker=excluded.marker,updated_at=excluded.updated_at,state=excluded.state',
                                (str(result['event_id']), self.calendar_id, operation['message_id'], operation['proposal_id'], json.dumps(snapshot, ensure_ascii=False), result.get('fingerprint') or digest(snapshot), owned_marker, utcnow(), 'cancelled' if operation['action'] == 'cancel' else 'active'))
                self.db.execute("UPDATE proposals SET status='applied',updated_at=?,pending_since=NULL WHERE id=?", (utcnow(), operation['proposal_id']))
                decision_row = self.db.execute('SELECT * FROM proposals WHERE id=?', (operation['proposal_id'],)).fetchone()
                if decision_row:
                    from .data_admin import remember_decision
                    remember_decision(self, decision_row, self._proposal(decision_row))
                    self.remember_canonical_decision(decision_row['id'])
                for deferred in self.db.execute("SELECT * FROM proposals WHERE status='review'").fetchall():
                    item=self._proposal(deferred)
                    if item.get('deferred_unknown_operation')!=operation_id:continue
                    item.update(event_id=str(result['event_id']),kind='cancel' if item.get('kind')=='cancel' else 'update',review_reason='Prior unknown write reconciled; confirm this newer source correction.')
                    item.pop('deferred_unknown_operation',None)
                    self.db.execute('UPDATE proposals SET payload=?,updated_at=? WHERE id=?',(json.dumps(item,ensure_ascii=False),utcnow(),deferred['id']))
                    self.db.execute('UPDATE canonical_actions SET payload=?,updated_at=? WHERE proposal_id=?',(json.dumps(item,ensure_ascii=False),utcnow(),deferred['id']))
            elif status == 'review':
                proposal_row = self.db.execute('SELECT payload FROM proposals WHERE id=?', (operation['proposal_id'],)).fetchone()
                if proposal_row:
                    proposal = json.loads(proposal_row[0])
                    proposal.update(needs_review=True, review_reason=str(result.get('error') or 'Calendar conflict requires manual review.')[:1000])
                    self.db.execute("UPDATE proposals SET payload=?,status='review',updated_at=?,pending_since=NULL WHERE id=?", (json.dumps(proposal, ensure_ascii=False), utcnow(), operation['proposal_id']))

            current_proposal=self.db.execute('SELECT * FROM proposals WHERE id=?',(operation['proposal_id'],)).fetchone()
            if current_proposal:
                current_item=self._proposal(current_proposal)
                self.db.execute('UPDATE canonical_actions SET payload=?,status=?,updated_at=? WHERE proposal_id=?',(json.dumps(current_item,ensure_ascii=False),current_item['status'],utcnow(),operation['proposal_id']))
            checkpoint_after_result(self, agenda_calendar, current, advance_agenda, result)

        if row['status'] == 'unknown' and status == 'applied':
            journal_reason = 'reconciled'
        elif row['status'] == 'unknown' and status == 'review' and journal_reason == 'result_recorded':
            journal_reason = 'reconcile_requires_review'
        report = emit if journal_reason not in ('result_recorded', 'reconciled') or status in ('unknown', 'failed') else observe
        prior = row['status']
        report('planner', 'journal', journal_reason, 'committed', action=operation_action(operation),
               position=incident_position, prior_status=prior if prior in ('pending', 'inflight', 'unknown', 'applied', 'review', 'superseded') else 'other',
               runtime_status=runtime_status, runtime_code=runtime_code, result_status=status, journal_status=state,
               journal_checks=failed_checks, baseline_advanced=bool(advance_agenda and status == 'applied'))

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

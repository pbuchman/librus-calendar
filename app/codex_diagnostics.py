"""Bounded observations of a local Codex child; never retain protocol contents.

A local thread ID is a private correlation clue, not a provider request ID. No
verified provider request-ID field is exposed by the CLI protocol used here.
"""
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .calendar_form_diagnostics import _open_parent

MAX_BYTES = 128 * 1024
MAX_FILES = 100
REFRESH_SECONDS = 5
MAX_LINE_BYTES = 1024 * 1024
ARTIFACT_NAME = re.compile(r'codex-[a-f0-9]{32}\.json\Z')
TEMPORARY_NAME = re.compile(r'\.codex-[a-f0-9]{32}\.tmp\Z')
EVENT_TYPES = frozenset(('thread.started', 'turn.started', 'turn.completed', 'turn.failed', 'error',
                         'item.started', 'item.updated', 'item.completed'))
STAGES = frozenset(('invocation_created', 'process_started', 'first_stdout', 'first_stderr',
                   'thread_started', 'turn_started', 'tool_started', 'tool_completed', 'turn_completed', 'turn_failed', 'protocol_error'))
STOP_CAUSES = frozenset(('running', 'normal_exit', 'signal', 'app_timeout', 'sync_interruption', 'unknown'))
ERROR_CATEGORIES = frozenset(('unknown', 'codex_limit', 'codex_permission_denied', 'codex_configuration_failed', 'codex_transport_dns', 'codex_transport_tls',
    'codex_transport_connection', 'codex_upstream_timeout', 'codex_http_5xx'))
SUMMARY_FIELDS = frozenset(('last_stage', 'elapsed_seconds', 'quiet_seconds', 'stop_cause', 'error_category',
    'error_origin', 'stdout_bytes', 'stderr_bytes', 'event_counts', 'invalid_line_count', 'unknown_event_count',
    'partial_line_bytes', 'oversized_line_count', 'cpu_seconds', 'rss_bytes', 'artifact_status', 'progress_status', 'observation_status'))


def validate_process_observation(value):
    """Closed public summary: no correlation IDs, free text, paths or arguments."""
    if (not isinstance(value, dict) or not SUMMARY_FIELDS <= set(value)
            or set(value) - SUMMARY_FIELDS - {'artifact_id'}):
        raise ValueError('Invalid process observation fields.')
    for key, allowed in (('last_stage', STAGES), ('stop_cause', STOP_CAUSES), ('error_category', ERROR_CATEGORIES),
            ('error_origin', {'none', 'protocol', 'stderr'}), ('artifact_status', {'written', 'capture_failed', 'not_configured', 'removed_on_success'}),
            ('progress_status', {'ok', 'update_failed'}), ('observation_status', {'ok', 'observation_failed'})):
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise ValueError('Invalid process observation category.')
    for key in ('stdout_bytes', 'stderr_bytes', 'invalid_line_count', 'unknown_event_count', 'partial_line_bytes', 'oversized_line_count'):
        if type(value[key]) is not int or not 0 <= value[key] <= 2**63-1:
            raise ValueError('Invalid process observation count.')
    for key in ('elapsed_seconds', 'quiet_seconds', 'cpu_seconds', 'rss_bytes'):
        entry = value[key]
        if entry is None and key in {'cpu_seconds', 'rss_bytes'}:
            continue
        if (type(entry) not in (int, float) or not math.isfinite(entry) or not 0 <= entry <= 2**63-1
                or key == 'rss_bytes' and type(entry) is not int):
            raise ValueError('Invalid process observation measurement.')
    counts = value['event_counts']
    if (not isinstance(counts, dict) or set(counts) - EVENT_TYPES
            or any(type(count) is not int or not 1 <= count <= 2**63-1 for count in counts.values())):
        raise ValueError('Invalid process observation event counts.')
    if value['artifact_status'] == 'written':
        if not isinstance(value.get('artifact_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', value['artifact_id']):
            raise ValueError('Invalid process observation reference.')
    elif 'artifact_id' in value:
        raise ValueError('Unexpected process observation reference.')
    if (value['error_category'] == 'unknown') != (value['error_origin'] == 'none'):
        raise ValueError('Invalid process observation error origin.')
    return value


def _error_category(value):
    # Only callers selecting explicit protocol error fields or stderr use this.
    if not isinstance(value, str):
        return 'unknown'
    text = value.lower()
    return ('codex_limit' if any(word in text for word in ('usage limit', 'rate limit', 'quota', 'credits'))
        else 'codex_permission_denied' if any(word in text for word in ('permission denied', 'access denied', 'unauthorized', 'forbidden', 'authentication required'))
        else 'codex_configuration_failed' if any(word in text for word in ('invalid configuration', 'failed to load config', 'configuration error'))
        else 'codex_transport_dns' if any(word in text for word in ('name or service not known', 'temporary failure in name resolution', 'failed to lookup address', 'dns resolution failed'))
        else 'codex_transport_tls' if any(word in text for word in ('tls handshake', 'certificate verify failed', 'ssl certificate problem'))
        else 'codex_transport_connection' if any(word in text for word in ('connection reset', 'connection refused', 'connection disconnected', 'stream disconnected', 'connection closed before'))
        else 'codex_upstream_timeout' if any(word in text for word in ('upstream timeout', 'request timed out', 'request timeout'))
        else 'codex_limit' if re.search(r'\bHTTP(?: status)?[ :]+429\b', value, re.I)
        else 'codex_permission_denied' if re.search(r'\bHTTP(?: status)?[ :]+(?:401|403)\b', value, re.I)
        else 'codex_http_5xx' if re.search(r'\bHTTP(?: status)?[ :]+5[0-9]{2}\b', value, re.I)
        else 'unknown')


def _explicit_error(value):
    """Extract only known error members, never arbitrary nested result content."""
    if isinstance(value, str):
        return _error_category(value)
    if isinstance(value, dict):
        for key in ('status_code', 'http_status'):
            status = value.get(key)
            if type(status) is int:
                category = _error_category('HTTP '+str(status))
                if category != 'unknown':
                    return category
        for key in ('message', 'code', 'type'):
            category = _error_category(value.get(key))
            if category != 'unknown':
                return category
    return 'unknown'


class PrivateCodexCapture:
    """Exclusive creation and atomic owner-only refresh beside the state DB."""
    def __init__(self, state_path):
        self.parent = Path(state_path).parent

    def __call__(self, artifact_id, artifact):
        if not re.fullmatch(r'[a-f0-9]{32}', artifact_id):
            raise ValueError('Invalid private trace reference.')
        payload = json.dumps(artifact, ensure_ascii=True, allow_nan=False).encode()
        if len(payload) > MAX_BYTES:
            raise ValueError('Private trace bound exceeded.')
        parent_fd = _open_parent(self.parent)
        directory_fd = None
        temporary = None
        try:
            try:
                os.mkdir('codex-diagnostics', 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            directory_fd = os.open('codex-diagnostics', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Private trace directory is unsafe.')
            import fcntl
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._clean_temporaries(directory_fd)
            name = 'codex-' + artifact_id + '.json'
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and not self._owned(info):
                raise ValueError('Private trace destination is unsafe.')
            if artifact.get('invocation_state') == 'completed':
                if info is not None:
                    os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                return
            # Bound old abandoned reservations before any new crash window,
            # including a failure before the temporary file can be opened.
            self._retain(directory_fd, name, True)
            if info is None:
                # Reserve exclusively, without a link-count crash window. A
                # crash may leave an empty bounded abandoned reservation; only
                # a complete fsynced publication reports capture success.
                reservation = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=directory_fd)
                os.close(reservation)
            temporary = '.codex-' + secrets.token_hex(16) + '.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Never follow an existing destination; rename replaces its directory
            # entry. The locked private directory excludes other application writers.
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = None
            self._retain(directory_fd, name, artifact.get('invocation_state') == 'running')
            os.fsync(directory_fd)
        finally:
            if directory_fd is not None:
                try:
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=directory_fd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(directory_fd)
            os.close(parent_fd)

    @staticmethod
    def _owned(info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600)

    @classmethod
    def _clean_temporaries(cls, directory_fd):
        # A killed writer can leave only these exact private temporary names.
        # Never delete symlinks, hard links, foreign files or permissive files.
        for name in os.listdir(directory_fd):
            if TEMPORARY_NAME.fullmatch(name):
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if cls._owned(info):
                    os.unlink(name, dir_fd=directory_fd)

    @classmethod
    def _retain(cls, directory_fd, current, active):
        owned = []
        for name in os.listdir(directory_fd):
            if ARTIFACT_NAME.fullmatch(name):
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if cls._owned(info):
                    owned.append((info.st_mtime_ns, name))
        # Sync serializes invocations. Other matching artifacts are finalized
        # failures or abandoned running captures after an unconfirmed interruption.
        # Count those abandoned captures without inventing a business outcome.
        older = sorted((entry for entry in owned if entry[1] != current), reverse=True)
        for unused, name in older[MAX_FILES if active else MAX_FILES-1:]:
            os.unlink(name, dir_fd=directory_fd)


class ProcessObservation:
    """Observe suffix bytes; full protocol parsing stays in the runtime executor."""
    def __init__(self, *, capture=None, progress=None, model=None, effort=None, cli_version=None):
        self.started = time.monotonic()
        self.last_progress = self.started
        self.last_flush = self.started
        self.last_sample = self.started - REFRESH_SECONDS
        self.capture, self.progress = capture, progress
        self.artifact_id = secrets.token_hex(16)
        self.artifact_status = 'not_configured' if capture is None else 'written'
        self.progress_status = 'ok'
        self.observation_status = 'ok'
        self.model = model if model in {'gpt-6.1-sol'} else None
        self.effort = effort if effort == 'high' else None
        self.cli_version = cli_version if isinstance(cli_version, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,40})?', cli_version) else None
        self.thread_id = None
        self.invocation_state = 'running'
        self.process_code = None
        self._final_line_observed = False
        self.first = []
        self.last = deque(maxlen=224)
        self.timeline_count = 0
        self.stdout_bytes = self.stderr_bytes = 0
        self.line = bytearray()
        self.dropping_line = False
        self.stderr_tail = b''
        self.event_counts = {}
        self.invalid_line_count = self.unknown_event_count = self.oversized_line_count = 0
        self.last_stage = 'invocation_created'
        self.stop_cause = 'running'
        self.error_category, self.error_origin = 'unknown', 'none'
        self.cpu_seconds = self.rss_bytes = None
        self.pid = None
        self._finished_at = None
        self._event('invocation_created', progress=False)
        self.flush(force=True)

    def optional(self, method, *args, **kwargs):
        try:
            return getattr(self, method)(*args, **kwargs)
        except Exception:
            # Optional telemetry cannot terminate a task or alter retry policy.
            # BaseException cancellation deliberately still reaches child cleanup.
            self.observation_status = 'observation_failed'

    def _event(self, stage, *, progress=True):
        now = time.monotonic()
        if progress:
            self.last_stage, self.last_progress = stage, now
        entry = dict(stage=stage, elapsed_seconds=round(max(0, now-self.started), 3),
                     observed_at=datetime.now(timezone.utc).isoformat())
        self.timeline_count += 1
        if len(self.first) < 32:
            self.first.append(entry)
        else:
            self.last.append(entry)

    def process_started(self, pid):
        self.pid = pid
        self._event('process_started')
        self.sample()

    def observe(self, stdout, stderr):
        """Accept cumulative byte output (e.g. TimeoutExpired), only new suffixes."""
        for stream, value in (('stdout', stdout), ('stderr', stderr)):
            if isinstance(value, str):
                value = value.encode('utf-8')
            if not isinstance(value, bytes):
                continue
            previous = self.stdout_bytes if stream == 'stdout' else self.stderr_bytes
            if len(value) <= previous:
                continue
            self.feed(stream, value[previous:])

    def feed(self, stream, chunk):
        if not chunk:
            return
        key = stream + '_bytes'
        if getattr(self, key) == 0:
            self._event('first_' + stream)
        setattr(self, key, getattr(self, key) + len(chunk))
        # Any new pipe bytes are observed progress, including fragmented lines;
        # they do not imply a new protocol lifecycle stage.
        self.last_progress = time.monotonic()
        if stream == 'stderr':
            text = (self.stderr_tail + chunk).decode('utf-8', errors='replace')
            category = _error_category(text)
            if category != 'unknown' and self.error_category == 'unknown':
                self.error_category, self.error_origin = category, 'stderr'
            self.stderr_tail = (self.stderr_tail + chunk)[-128:]
            return
        for piece in chunk.splitlines(keepends=True):
            terminated = piece.endswith(b'\n')
            if not self.dropping_line:
                if len(self.line) + len(piece) > MAX_LINE_BYTES:
                    self.line.clear()
                    self.dropping_line = True
                    self.oversized_line_count += 1
                else:
                    self.line.extend(piece)
            if terminated:
                if not self.dropping_line:
                    self._line(bytes(self.line))
                self.line.clear()
                self.dropping_line = False

    def _line(self, raw, *, partial=False):
        if not raw.strip():
            return
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            if not partial:
                self.invalid_line_count += 1
            return
        if not isinstance(record, dict) or not isinstance(record.get('type'), str):
            self.invalid_line_count += 1
            return
        kind = record['type']
        if kind not in EVENT_TYPES:
            self.unknown_event_count += 1
            return
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        if kind == 'thread.started':
            thread_id = record.get('thread_id')
            if isinstance(thread_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', thread_id):
                self.thread_id = thread_id
            self._event('thread_started')
        elif kind in {'turn.started', 'turn.completed', 'turn.failed', 'error'}:
            self._event('protocol_error' if kind == 'error' else kind.replace('.', '_'))
        elif kind.startswith('item.'):
            item = record.get('item')
            if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                self.invalid_line_count += 1
                return
            if item['type'] == 'mcp_tool_call' and kind in {'item.started', 'item.completed'}:
                self._event('tool_started' if kind == 'item.started' else 'tool_completed')
            elif item['type'] not in {'reasoning', 'agent_message', 'mcp_tool_call', 'error'}:
                self.unknown_event_count += 1
            if item['type'] == 'error':
                self._protocol_error(item)
        if kind in {'turn.failed', 'error'}:
            self._protocol_error(record)

    def _protocol_error(self, record):
        for key in ('message', 'error'):
            category = _explicit_error(record.get(key))
            if category != 'unknown' and self.error_category == 'unknown':
                self.error_category, self.error_origin = category, 'protocol'

    def sample(self):
        now = time.monotonic()
        if now-self.last_sample < REFRESH_SECONDS:
            return
        self.last_sample = now
        self.cpu_seconds = self.rss_bytes = None
        if type(self.pid) is not int:
            return
        try:
            # comm can contain spaces or ')'; only parse fields after final ')'.
            fields = Path('/proc', str(self.pid), 'stat').read_text().rsplit(')', 1)[1].split()
            self.cpu_seconds = (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
            self.rss_bytes = max(0, int(fields[21]))*os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            pass
        self._event('resource_sample', progress=False)
        target = self.first[-1] if self.timeline_count <= 32 else self.last[-1]
        target.update(cpu_seconds=self.cpu_seconds, rss_bytes=self.rss_bytes)

    def finish(self, returncode, cause):
        if self.line and not self._final_line_observed:
            # EOF may terminate a complete JSON record without a trailing LF.
            # Distinguish that from an actually truncated record.
            try:
                json.loads(bytes(self.line))
            except (ValueError, UnicodeError, RecursionError):
                pass
            else:
                self._line(bytes(self.line), partial=True)
                self.line.clear()
            self._final_line_observed = True
        self.process_code = returncode if type(returncode) is int else None
        self.stop_cause = cause
        self._finished_at = time.monotonic()
        self._event('process_ended', progress=False)
        self.flush(force=True)

    def finalize(self, state):
        # Called only after the runtime's strict parsing and caller validation.
        self.invocation_state = state
        self._event('invocation_' + state, progress=False)
        if self.capture is not None:
            try:
                self.artifact_status = 'removed_on_success' if state == 'completed' else 'written'
                self.capture(self.artifact_id, self.artifact())
            except Exception:
                self.artifact_status = 'capture_failed'

    def summary(self):
        now = self._finished_at if self._finished_at is not None else time.monotonic()
        result = dict(last_stage=self.last_stage, elapsed_seconds=round(max(0, now-self.started), 3),
            quiet_seconds=round(max(0, now-self.last_progress), 3), stop_cause=self.stop_cause,
            error_category=self.error_category, error_origin=self.error_origin,
            stdout_bytes=self.stdout_bytes, stderr_bytes=self.stderr_bytes,
            event_counts=dict(self.event_counts), invalid_line_count=self.invalid_line_count,
            unknown_event_count=self.unknown_event_count, partial_line_bytes=len(self.line), oversized_line_count=self.oversized_line_count,
            cpu_seconds=self.cpu_seconds, rss_bytes=self.rss_bytes,
            artifact_status=self.artifact_status, progress_status=self.progress_status, observation_status=self.observation_status)
        if self.artifact_status == 'written':
            result['artifact_id'] = self.artifact_id
        return validate_process_observation(result)

    def artifact(self):
        timeline = self.first + list(self.last)
        return dict(schema_version=1, invocation_state=self.invocation_state, process_code=self.process_code,
            summary=self.summary(), model=self.model, effort=self.effort,
            cli_version=self.cli_version, thread_id=self.thread_id,
            provider_request_id_status='not_exposed', timeline=timeline,
            omitted_count=self.timeline_count-len(timeline))

    def flush(self, *, force=False):
        now = time.monotonic()
        if not force and now-self.last_flush < REFRESH_SECONDS:
            return
        self.last_flush = now
        if self.capture is not None:
            try:
                self.capture(self.artifact_id, self.artifact())
            except Exception:
                self.artifact_status = 'capture_failed'
        if self.progress is not None:
            try:
                self.progress(self.summary())
            except Exception:
                self.progress_status = 'update_failed'

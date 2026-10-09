"""Read-only health export for Netdata. Never imports credentials or calls remote APIs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from .config import ConfigurationError, load_config
from datetime import datetime, timezone
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_DB = Path('~/.local/share/librus-calendar/state.sqlite3').expanduser()
DEFAULT_OUTPUT = Path('/var/lib/librus-monitor/status.json')
TIME_FIELDS = ('initialized_at', 'last_attempt_at', 'last_successful_sync', 'running_since',
               'writes_paused_since', 'oldest_pending_at', 'last_backup_success_at', 'last_backup_attempt_at')
COUNT_FIELDS = ('consecutive_failures', 'messages_pending', 'operations_pending', 'operations_unknown', 'review_count')
RESULTS = {'idle', 'running', 'ok', 'partial', 'failed', 'interrupted', 'paused', 'dry_run'}
BACKUP_RESULTS = {'idle', 'running', 'ok', 'failed', None}


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value, now):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or (parsed - now).total_seconds() > 5:
        raise ValueError('invalid timestamp')
    return parsed.astimezone(timezone.utc).isoformat()


def sanitize_state(state, now):
    """Only a closed set of aggregate fields can leave the private database."""
    if state.get('schema_version') != 1:
        raise ValueError('invalid state version')
    clean = {}
    for name in TIME_FIELDS:
        clean[name] = timestamp(state[name], now)
    if clean['initialized_at'] is None:
        raise ValueError('missing initialization')
    for name in COUNT_FIELDS:
        value = state[name]
        if type(value) is not int or value < 0:
            raise ValueError('invalid count')
        clean[name] = value
    if type(state['writes_paused']) is not bool:
        raise ValueError('invalid pause')
    clean['writes_paused'] = state['writes_paused']
    for name, allowed in (('last_result', RESULTS), ('last_backup_result', BACKUP_RESULTS)):
        value = state[name]
        if value not in allowed:
            raise ValueError('invalid result')
        clean[name] = value
    for name in ('last_stage', 'last_error_code'):
        value = state[name]
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value)):
            raise ValueError('invalid technical code')
        clean[name] = value
    duration = state['last_duration_seconds']
    if duration is not None and (type(duration) not in (int, float) or not 0 <= duration <= 31536000):
        raise ValueError('invalid duration')
    clean['last_duration_seconds'] = duration
    return clean


def unit_state(unit):
    try:
        result = subprocess.run(['systemctl', '--user', 'show', unit,
            '--property=LoadState,ActiveState,SubState,UnitFileState,Result,ExecMainStatus'],
            timeout=5, check=True, capture_output=True, text=True)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if values.get('LoadState') != 'loaded':
            return None
        return values
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def web_available(config=None):
    config = config if config is not None else load_config()
    try:
        config.require_web()
    except ConfigurationError:
        return False
    request = Request(config.monitor_url, headers={
        'Host': config.allowed_hosts[0],
        'Tailscale-User-Login': config.tailscale_owner,
        'Accept': 'application/json'})
    try:
        # No redirects or proxy environment: this probe must remain on loopback.
        from urllib.request import ProxyHandler
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            payload = response.read(65537)
            return response.status == 200 and len(payload) <= 65536 and isinstance(json.loads(payload), dict)
    except (OSError, ValueError):
        return False


def timer_available(state):
    return bool(state and state.get('ActiveState') == 'active'
                and state.get('UnitFileState') in {'enabled', 'enabled-runtime'})


def collect_snapshot(database=None, now=None, reader=None, *, config=None):
    config = config if config is not None else load_config()
    database = database if database is not None else config.state_path
    now = now or utc_now()
    if reader is None:
        from app.health_state import read_monitoring_state
        reader = read_monitoring_state
    snapshot = {'schema_version': 1, 'generated_at': now.isoformat(), 'db_ok': False}
    try:
        snapshot['state'] = sanitize_state(reader(database, now=now), now)
        snapshot['db_ok'] = True
    except Exception:
        # A sanitized source failure is observable; exception messages may contain private data.
        snapshot['state'] = None
    snapshot['sync_timer_ok'] = timer_available(unit_state('librus-sync.timer'))
    snapshot['backup_timer_ok'] = timer_available(unit_state('librus-backup.timer'))
    sync = unit_state('librus-sync.service')
    backup = unit_state('librus-backup.service')
    snapshot['sync_unit_ok'] = sync is not None
    snapshot['sync_running'] = bool(sync and (sync.get('ActiveState') == 'activating'
        or (sync.get('ActiveState') == 'active' and sync.get('SubState') == 'running')))
    snapshot['sync_failed'] = bool(sync and (sync.get('ActiveState') == 'failed'
        or sync.get('Result', 'success') != 'success'
        or sync.get('ExecMainStatus', '0') != '0'))
    snapshot['sync_timeout'] = bool(sync and sync.get('Result') in {'timeout', 'watchdog', 'signal', 'core-dump'})
    snapshot['backup_failed'] = bool(backup is None or backup.get('ActiveState') == 'failed'
        or backup.get('Result', 'success') != 'success'
        or backup.get('ExecMainStatus', '0') != '0')
    snapshot['web_ok'] = web_available(config)
    return snapshot


def atomic_write(snapshot, destination=DEFAULT_OUTPUT):
    destination = Path(destination)
    # Installer owns directory creation, ownership and netdata group access.
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o007:
            raise PermissionError('unsafe monitor directory')
        fd, temporary = tempfile.mkstemp(prefix='.status-', dir=destination.parent)
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o640)
            json.dump(snapshot, handle, separators=(',', ':'), allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        if temporary is not None:
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        config = load_config()
        snapshot = collect_snapshot(args.database, config=config)
        atomic_write(snapshot, args.output or config.monitor_output)
    except Exception:
        print('Librus monitoring export failed; inspect local service state.')
        return 1
    return 0 if snapshot['db_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

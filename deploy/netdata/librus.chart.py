"""Netdata reads only sanitized local aggregates, never the Librus database."""
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone

from bases.FrameworkServices.SimpleService import SimpleService

update_every = 60
priority = 90000
WIRE_SCALE = 1000
MAX_METRIC_VALUE = 10**12

ORDER = ['sync', 'queue', 'services', 'monitoring', 'backup', 'pause']
CHARTS = {
    'sync': {'options': [None, 'Librus synchronization', 'minutes / count', 'sync', 'librus_local.sync', 'line'],
        'lines': [['success_age', 'last full success age'], ['success_alert_age', 'unpaused full success age'], ['success_missing', 'no full success'],
                  ['failures', 'consecutive failures'], ['running_minutes', 'running minutes'],
                  ['interrupted', 'interrupted or timed out'], ['unknown', 'unknown writes after run']]},
    'queue': {'options': [None, 'Librus automatic queue', 'minutes / count', 'sync', 'librus_local.queue', 'line'],
        'lines': [['pending_age', 'oldest automatic work age'], ['pending_alert_age', 'unpaused automatic work age'], ['messages', 'pending messages'],
                  ['operations', 'pending operations'], ['review', 'awaiting owner decision']]},
    'services': {'options': [None, 'Librus service outages', 'minutes', 'services', 'librus_local.services', 'line'],
        'lines': [['sync_timer_down', 'sync timer'], ['backup_timer_down', 'backup timer'],
                  ['web_down', 'web'], ['sync_unit_down', 'sync unit unavailable']]},
    'monitoring': {'options': [None, 'Librus monitoring freshness', 'minutes / state', 'monitoring', 'librus_local.monitoring', 'line'],
        'lines': [['snapshot_age', 'snapshot age'], ['invalid_minutes', 'invalid source duration'],
                  ['db_down', 'database unavailable duration'], ['invalid', 'snapshot invalid']]},
    'backup': {'options': [None, 'Librus verified backup', 'hours / state', 'backup', 'librus_local.backup', 'line'],
        'lines': [['backup_age', 'last verified backup age'], ['backup_failed', 'backup failed'],
                  ['backup_missing', 'no verified backup']]},
    'pause': {'options': [None, 'Librus write pause', 'minutes / state', 'sync', 'librus_local.pause', 'line'],
        'lines': [['pause_minutes', 'write pause duration'], ['paused', 'writes paused']]},
}

# python.d casts collected values to int before applying chart multiplier/divisor.
# Encode fixed-point integers on the wire so time thresholds retain sub-unit precision.
for chart in CHARTS.values():
    chart['lines'] = [line + ['absolute', 1, WIRE_SCALE] for line in chart['lines']]


def encode_metrics(data):
    encoded = {}
    for name, value in data.items():
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_METRIC_VALUE:
            raise ValueError('invalid metric value')
        encoded[name] = int(round(value * WIRE_SCALE))
    return encoded


TIME_FIELDS = ('initialized_at', 'last_attempt_at', 'last_successful_sync', 'running_since',
               'writes_paused_since', 'oldest_pending_at', 'last_backup_success_at', 'last_backup_attempt_at')
COUNT_FIELDS = ('consecutive_failures', 'messages_pending', 'operations_pending', 'operations_unknown', 'review_count')
BOOL_FIELDS = ('db_ok', 'sync_timer_ok', 'backup_timer_ok', 'sync_unit_ok', 'sync_running', 'sync_failed', 'sync_timeout', 'backup_failed', 'web_ok')
RESULTS = {'idle', 'running', 'ok', 'partial', 'failed', 'interrupted', 'paused', 'dry_run'}


def parsed_time(value, now):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid timestamp')
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    epoch = value.timestamp()
    if not math.isfinite(epoch) or epoch > now + 5:
        raise ValueError('future timestamp')
    return epoch


def validate_snapshot(snapshot, now):
    if not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != 1:
        raise ValueError('invalid version')
    generated = parsed_time(snapshot['generated_at'], now)
    if generated is None:
        raise ValueError('missing timestamp')
    for name in BOOL_FIELDS:
        if type(snapshot[name]) is not bool:
            raise ValueError('invalid state')
    state = snapshot['state']
    parsed = None
    if snapshot['db_ok']:
        if not isinstance(state, dict):
            raise ValueError('invalid aggregate state')
        parsed = {name: parsed_time(state[name], now) for name in TIME_FIELDS}
        if parsed['initialized_at'] is None:
            raise ValueError('missing initialization')
        for name in COUNT_FIELDS:
            if type(state[name]) is not int or not 0 <= state[name] <= MAX_METRIC_VALUE:
                raise ValueError('invalid count')
        if type(state['writes_paused']) is not bool or state['last_result'] not in RESULTS:
            raise ValueError('invalid result')
        if state['last_backup_result'] not in {'idle', 'running', 'ok', 'failed', None}:
            raise ValueError('invalid backup result')
        for field in ('last_stage', 'last_error_code'):
            code = state[field]
            if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code)):
                raise ValueError('invalid technical code')
        duration = state['last_duration_seconds']
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
            raise ValueError('invalid duration')
        # These timestamps are required to age nonempty queues/runs without hiding outages.
        if (state['messages_pending'] or state['operations_pending']) and parsed['oldest_pending_at'] is None:
            raise ValueError('pending age unavailable')
        if state['last_result'] == 'running' and parsed['running_since'] is None:
            raise ValueError('running age unavailable')
        if state['writes_paused'] and parsed['writes_paused_since'] is None:
            raise ValueError('pause age unavailable')
    elif state is not None:
        raise ValueError('invalid unavailable database state')
    return generated, parsed


class HealthMetrics:
    """Independent clock means a stalled export cannot freeze healthy metrics."""
    def __init__(self):
        self.bad_since = {}
        self.last_generated = None
        self.first_missing_at = None
        self.last_snapshot = None

    def outage(self, name, bad, now):
        if not bad:
            self.bad_since.pop(name, None)
            return 0
        start = self.bad_since.setdefault(name, now)
        if now < start:
            start = self.bad_since[name] = now
        return max(0, now - start) / 60.0

    def invalid_data(self, now):
        try:
            data = self.calculate(self.last_snapshot, now) if self.last_snapshot is not None else {line[0]: 0 for chart in CHARTS.values() for line in chart['lines']}
        except (ValueError, KeyError, TypeError, OverflowError):
            data = {line[0]: 0 for chart in CHARTS.values() for line in chart['lines']}
        if self.first_missing_at is None:
            self.first_missing_at = now
        baseline = self.last_generated if self.last_generated is not None else self.first_missing_at
        data.update(snapshot_age=max(0, now - baseline) / 60.0,
                    invalid_minutes=self.outage('invalid', True, now), invalid=1)
        return data

    def calculate(self, snapshot, now):
        generated, times = validate_snapshot(snapshot, now)
        self.last_generated = generated
        self.last_snapshot = snapshot
        self.first_missing_at = None
        data = {line[0]: 0 for chart in CHARTS.values() for line in chart['lines']}
        data['snapshot_age'] = max(0, now - generated) / 60.0
        data['db_down'] = self.outage('db', not snapshot['db_ok'], now)
        data['invalid'] = int(not snapshot['db_ok'] or data['snapshot_age'] >= 3)
        for field, dimension in (('sync_timer_ok', 'sync_timer_down'), ('backup_timer_ok', 'backup_timer_down'),
                                 ('web_ok', 'web_down'), ('sync_unit_ok', 'sync_unit_down')):
            data[dimension] = self.outage(field, not snapshot[field], now)
        if times is None:
            return data
        state = snapshot['state']
        paused = state['writes_paused']
        running = state['last_result'] == 'running'
        actually_running = running and snapshot['sync_running']
        age = lambda field, scale=60: max(0, now - times[field]) / scale if times[field] is not None else 0
        success_age = age('last_successful_sync') if times['last_successful_sync'] is not None else 151
        pending_age = age('oldest_pending_at')
        data.update(success_age=success_age, success_alert_age=0 if paused else success_age,
            success_missing=int(times['last_successful_sync'] is None),
            failures=max(state['consecutive_failures'], int(snapshot['sync_failed'])),
            running_minutes=age('running_since') if running else 0,
            interrupted=int(state['last_result'] == 'interrupted' or snapshot['sync_timeout']),
            unknown=0 if actually_running else state['operations_unknown'],
            pending_age=pending_age, pending_alert_age=0 if paused else pending_age,
            messages=state['messages_pending'], operations=state['operations_pending'], review=state['review_count'],
            backup_age=age('last_backup_success_at', 3600) if times['last_backup_success_at'] is not None else 49,
            backup_failed=int(snapshot['backup_failed'] or state['last_backup_result'] == 'failed'),
            backup_missing=int(times['last_backup_success_at'] is None),
            pause_minutes=age('writes_paused_since') if paused else 0, paused=int(paused))
        return data


class Service(SimpleService):
    def __init__(self, configuration=None, name=None):
        super().__init__(configuration=configuration, name=name)
        self.order = ORDER
        self.definitions = CHARTS
        self.path = self.configuration.get('path', '/var/lib/librus-monitor/status.json')
        self.metrics = HealthMetrics()

    def check(self):
        # Always start charts. Missing data must produce alarms instead of disabling this collector.
        return True

    def get_data(self):
        now = time.time()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError('invalid snapshot file')
                content = handle.read(65537)
                if len(content) > 65536:
                    raise ValueError('oversized snapshot')
                snapshot = json.loads(content)
            data = self.metrics.calculate(snapshot, now)
            self.metrics.outage('invalid', False, now)
            return encode_metrics(data)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return encode_metrics(self.metrics.invalid_data(now))

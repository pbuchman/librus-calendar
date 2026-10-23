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

ORDER = ['sync', 'queue', 'services', 'monitoring', 'backup', 'pause', 'calendar_probe']
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
    'calendar_probe': {'options': [None, 'Librus calendar probe (rolling 24 hours)', 'count', 'sync', 'librus_local.calendar_probe', 'line'],
        'lines': [['probe_retries_24h', 'read probe retries'], ['probe_recoveries_24h', 'read probe recoveries']]},
}
OPTIONAL_METRICS = {'probe_retries_24h', 'probe_recoveries_24h'}

SOURCE_KINDS = ('message', 'announcement', 'school_event', 'substitution')
SOURCE_MODES = ('off', 'preview', 'active')
SOURCE_ERROR_CODES = frozenset(('source_collection_incomplete', 'source_collection_failed', 'source_analysis_failed', 'source_context_mismatch', 'source_changed', 'source_match_failed', 'source_operation_failed', 'source_response_limit', 'source_markup_invalid', 'source_fields_ambiguous', 'source_date_invalid', 'source_pagination_unsupported', 'announcement_markup_invalid', 'announcement_limit', 'announcement_identity_ambiguous', 'learner_context_ambiguous', 'learner_context_unverified', 'calendar_form_invalid', 'calendar_markup_invalid', 'calendar_date_invalid', 'calendar_detail_route_invalid', 'calendar_identity_unverified', 'calendar_limit', 'calendar_month_incomplete', 'calendar_detail_markup_invalid', 'source_session_invalid', 'calendar_window_unsupported', 'calendar_selection_failed', 'calendar_identity_ambiguous', 'calendar_publication_ambiguous', 'calendar_restore_failed', 'source_window_invalid', 'learner_collection_failed', 'announcement_collection_failed', 'school_event_collection_failed', 'learner_context_changed'))
SOURCE_ERROR_CODES |= frozenset(('source_preview_time_budget_exhausted', 'source_preview_call_budget_exhausted', 'source_preview_worker_required'))
for kind in SOURCE_KINDS:
    name = 'source_' + kind
    ORDER.append(name)
    CHARTS[name] = {'options': [None, 'Librus source: ' + kind, 'minutes / count / mode',
        'sources', 'librus_local.' + name, 'line'], 'lines': [
        [name + '_mode', 'mode: off 0, preview 1, active 2'],
        [name + '_success_age', 'last successful fetch age'],
        [name + '_pending', 'pending analysis'], [name + '_failed', 'errors'],
        [name + '_links', 'action links'], [name + '_preview_ready', 'ready preview analyses']]}
ORDER.append('source_preview')
CHARTS['source_preview'] = {'options': [None, 'Librus preview queue (no calendar writes)',
    'count', 'sources', 'librus_local.source_preview', 'line'],
    'lines': [['source_preview_count', 'ready preview analyses']]}
OPTIONAL_METRICS.update(line[0] for name, chart in CHARTS.items()
                        if name.startswith('source_') for line in chart['lines'])

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
BOOL_FIELDS = ('db_ok', 'sync_timer_ok', 'backup_timer_ok', 'sync_unit_ok', 'sync_running', 'sync_failed', 'sync_timeout', 'backup_failed', 'web_ok', 'backup_enabled')
RESULTS = {'idle', 'running', 'ok', 'partial', 'failed', 'interrupted', 'paused', 'dry_run'}
PROBE_RESULTS = {'running', 'ok', 'failed', 'interrupted'}


# Labels are notification metadata only. Never publish arbitrary snapshot strings.
SYNC_STAGES = frozenset(('analysis', 'calendar_probe', 'complete', 'credentials', 'discovery',
    'fetch', 'login', 'queue', 'reconcile', 'runtime', 'starting', 'write'))
SYNC_ERROR_CODES = frozenset(('account_mismatch', 'agenda_action_reference_invalid', 'agenda_calendar_snapshot_incomplete',
    'agenda_calendar_snapshot_invalid', 'agenda_collection_incomplete',
    'agenda_conflicting_operations', 'agenda_context_overflow', 'agenda_group_reference_invalid',
    'agenda_learner_mismatch', 'agenda_learner_unverified', 'agenda_model_configuration_required',
    'agenda_plan_invalid', 'agenda_plan_stale', 'agenda_review_required', 'agenda_snapshot_invalid',
    'agenda_snapshot_overflow', 'agenda_source_reference_invalid', 'agenda_source_body_required', 'agenda_sources_unprocessed', 'agenda_whole_snapshot_required',
    'analysis_failed', 'analysis_tool_forbidden', 'calendar_configuration_required',
    'calendar_event_id_mismatch', 'calendar_event_missing', 'calendar_event_not_found',
    'calendar_not_allowed', 'calendar_probe_audit_failed', 'calendar_result_unreadable',
    'calendar_search_incomplete', 'calendar_tool_failed', 'calendar_tool_incomplete',
    'calendar_write_access_missing', 'codex_binary_required', 'codex_configuration_failed',
    'codex_failed', 'codex_limit', 'codex_permission_denied', 'codex_protocol_invalid',
    'codex_timeout', 'codex_unavailable', 'configuration_required', 'deletion_requires_approval',
    'discovery_connection_failed', 'discovery_failed', 'discovery_http_failed', 'discovery_incomplete',
    'discovery_parsing_failed', 'discovery_timeout', 'event_ownership_missing',
    'existing_event_required', 'fetch_failed', 'invalid_analysis_json',
    'invalid_analysis_temporal_fields', 'invalid_credentials', 'invalid_event_boundaries',
    'invalid_event_duration', 'invalid_operation_marker', 'login_failed', 'message_too_long',
    'messages_pending', 'missing_private_file', 'operation_failed', 'operation_not_allowed',
    'operations_pending', 'operations_unknown', 'sync_interrupted', 'sync_timeout',
    'timezone_required', 'unexpected_tool', 'unexpected_tool_arguments', 'unexpected_tool_count',
    'unsafe_event_boundaries', 'unsafe_event_fields', 'worker_interrupted'))
SYNC_FAILURE_RESULTS = frozenset(('failed', 'partial', 'interrupted'))
SYNC_COMPLETED_RESULTS = frozenset(('ok', 'partial', 'failed', 'interrupted'))
SYNC_CAUSES = {
    'agenda_context_overflow': 'agenda_context_overflow: complete planner input exceeds the local limit',
    'agenda_snapshot_overflow': 'agenda_snapshot_overflow: collected agenda snapshot exceeds the local limit',
    'codex_failed': 'codex_failed: Codex runtime process failed',
    'codex_timeout': 'codex_timeout: Codex runtime process timed out',
    'codex_limit': 'codex_limit: Codex runtime usage limit reached',
    'codex_unavailable': 'codex_unavailable: Codex runtime unavailable',
    'sync_timeout': 'sync_timeout: synchronization timed out or was interrupted',
    'sync_interrupted': 'sync_interrupted: synchronization interrupted',
}


def sync_failure_cause(result, code):
    if result not in SYNC_FAILURE_RESULTS:
        return 'none'
    return (SYNC_CAUSES.get(code, code + ': ' + code.replace('_', ' '))
            if code in SYNC_ERROR_CODES else 'unknown: failure cause unavailable')


def sync_diagnostics(snapshot=None, now=None):
    """Keep the current attempt separate from the latest completed run's cause."""
    unknown = {'sync_result': 'unknown', 'sync_stage': 'unknown',
               'sync_cause': 'unknown: current run diagnostics unavailable',
               'sync_completed_result': 'unknown', 'sync_completed_stage': 'unknown',
               'sync_completed_cause': 'unknown: completed run diagnostics unavailable'}
    if snapshot is None or not snapshot['db_ok']:
        return unknown
    generated = parsed_time(snapshot['generated_at'], now)
    if generated is None or now - generated >= 180:
        return unknown
    state = snapshot['state']
    result = state['last_result']
    stage = state['last_stage']
    cause = 'none'
    if snapshot['sync_timeout']:
        cause = SYNC_CAUSES['sync_timeout']
    elif result in SYNC_FAILURE_RESULTS:
        code = state['last_error_code']
        cause = sync_failure_cause(result, code)
    elif snapshot['sync_failed'] and result != 'running':
        cause = 'sync_service_failed: synchronization service failed'
    completed = state.get('last_completed_result')
    completed_stage = state.get('last_completed_stage')
    completed_code = state.get('last_completed_error_code')
    # Missing legacy fields establish no completed synchronization evidence.
    labels = {'sync_result': result, 'sync_stage': stage if stage in SYNC_STAGES else 'unknown',
              'sync_cause': cause, 'sync_completed_result': unknown['sync_completed_result'],
              'sync_completed_stage': unknown['sync_completed_stage'],
              'sync_completed_cause': unknown['sync_completed_cause']}
    if completed in SYNC_COMPLETED_RESULTS:
        labels.update(sync_completed_result=completed,
                      sync_completed_stage=completed_stage if completed_stage in SYNC_STAGES else 'unknown',
                      sync_completed_cause=sync_failure_cause(completed, completed_code))
    return labels


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
        for field, allowed in (('last_completed_result', SYNC_COMPLETED_RESULTS),
                               ('last_completed_stage', SYNC_STAGES),
                               ('last_completed_error_code', SYNC_ERROR_CODES)):
            value = state.get(field)
            if value is not None and (not isinstance(value, str) or value not in allowed):
                raise ValueError('invalid completed run metadata')
        duration = state['last_duration_seconds']
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
            raise ValueError('invalid duration')
        result = state.get('calendar_probe_result')
        if result is not None and (not isinstance(result, str) or result not in PROBE_RESULTS):
            raise ValueError('invalid probe result')
        attempts = state.get('calendar_probe_attempts')
        if attempts is not None and (type(attempts) is not int or attempts not in (1, 2)):
            raise ValueError('invalid probe attempts')
        recovered = state.get('calendar_probe_recovered')
        if recovered is not None and type(recovered) is not bool:
            raise ValueError('invalid probe recovery')
        for name in ('calendar_probe_retries_24h', 'calendar_probe_recoveries_24h'):
            value = state.get(name)
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_METRIC_VALUE):
                raise ValueError('invalid probe count')
        # These timestamps are required to age nonempty queues/runs without hiding outages.
        if (state['messages_pending'] or state['operations_pending']) and parsed['oldest_pending_at'] is None:
            raise ValueError('pending age unavailable')
        if state['last_result'] == 'running' and parsed['running_since'] is None:
            raise ValueError('running age unavailable')
        if state['writes_paused'] and parsed['writes_paused_since'] is None:
            raise ValueError('pause age unavailable')
    elif state is not None:
        raise ValueError('invalid unavailable database state')
    sources = state.get('sources') if isinstance(state, dict) else None
    if sources is not None:
        if not isinstance(sources, dict) or sources.get('mode') not in SOURCE_MODES:
            raise ValueError('invalid source mode')
        kinds = sources.get('kinds')
        if not isinstance(kinds, dict) or set(kinds) != set(SOURCE_KINDS):
            raise ValueError('invalid source kinds')
        preview_count = sources.get('preview_count')
        if type(preview_count) is not int or not 0 <= preview_count <= MAX_METRIC_VALUE:
            raise ValueError('invalid preview count')
        for item in kinds.values():
            if not isinstance(item, dict) or item.get('mode') not in SOURCE_MODES:
                raise ValueError('invalid source mode')
            parsed_time(item.get('last_successful_fetch'), now)
            for field in ('pending', 'failed', 'links'):
                if type(item.get(field)) is not int or not 0 <= item[field] <= MAX_METRIC_VALUE:
                    raise ValueError('invalid source count')
            ready = item.get('preview_ready')
            if ready is not None and (type(ready) is not int or not 0 <= ready <= MAX_METRIC_VALUE):
                raise ValueError('invalid preview ready count')
            result = item.get('last_result')
            if result is not None and result not in {'ok', 'partial', 'failed', 'running', 'unsupported', 'off'}:
                raise ValueError('invalid source result')
            code = item.get('last_error')
            if code is not None and (not isinstance(code, str) or code not in SOURCE_ERROR_CODES):
                raise ValueError('invalid source error')
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
            data = self.calculate(self.last_snapshot, now) if self.last_snapshot is not None else self.empty_data()
        except (ValueError, KeyError, TypeError, OverflowError):
            data = self.empty_data()
        if self.first_missing_at is None:
            self.first_missing_at = now
        baseline = self.last_generated if self.last_generated is not None else self.first_missing_at
        data.update(snapshot_age=max(0, now - baseline) / 60.0,
                    invalid_minutes=self.outage('invalid', True, now), invalid=1)
        return data

    @staticmethod
    def empty_data():
        # An old exporter/DB has no probe evidence. Leave a chart gap rather than
        # claiming zero retries or a successful calendar read.
        return {line[0]: 0 for chart in CHARTS.values() for line in chart['lines']
                if line[0] not in OPTIONAL_METRICS}

    def calculate(self, snapshot, now):
        generated, times = validate_snapshot(snapshot, now)
        self.last_generated = generated
        self.last_snapshot = snapshot
        self.first_missing_at = None
        data = self.empty_data()
        data['snapshot_age'] = max(0, now - generated) / 60.0
        data['db_down'] = self.outage('db', not snapshot['db_ok'], now)
        data['invalid'] = int(not snapshot['db_ok'] or data['snapshot_age'] >= 3)
        for field, dimension in (('sync_timer_ok', 'sync_timer_down'), ('backup_timer_ok', 'backup_timer_down'),
                                 ('web_ok', 'web_down'), ('sync_unit_ok', 'sync_unit_down')):
            data[dimension] = self.outage(field, not snapshot[field] and (field != 'backup_timer_ok' or snapshot['backup_enabled']), now)
        if times is None:
            return data
        state = snapshot['state']
        for field, dimension in (('calendar_probe_retries_24h', 'probe_retries_24h'),
                                 ('calendar_probe_recoveries_24h', 'probe_recoveries_24h')):
            if state.get(field) is not None:
                data[dimension] = state[field]
        sources = state.get('sources')
        if sources is not None:
            data['source_preview_count'] = sources['preview_count']
            for kind in SOURCE_KINDS:
                item = sources['kinds'][kind]
                prefix = 'source_' + kind + '_'
                data[prefix + 'mode'] = SOURCE_MODES.index(item['mode'])
                for field in ('pending', 'failed', 'links'):
                    data[prefix + field] = item[field]
                if item.get('preview_ready') is not None:
                    data[prefix + 'preview_ready'] = item['preview_ready']
                fetched = parsed_time(item.get('last_successful_fetch'), now)
                if fetched is not None:
                    data[prefix + 'success_age'] = max(0, now - fetched) / 60.0
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
            backup_age=(age('last_backup_success_at', 3600) if times['last_backup_success_at'] is not None else 49) if snapshot['backup_enabled'] else 0,
            backup_failed=int(snapshot['backup_enabled'] and (snapshot['backup_failed'] or state['last_backup_result'] == 'failed')),
            backup_missing=int(snapshot['backup_enabled'] and times['last_backup_success_at'] is None),
            pause_minutes=age('writes_paused_since') if paused else 0, paused=int(paused))
        return data


class Service(SimpleService):
    def __init__(self, configuration=None, name=None):
        super().__init__(configuration=configuration, name=name)
        self.order = ORDER
        self.definitions = CHARTS
        self.path = self.configuration.get('path', '/var/lib/librus-monitor/status.json')
        self.metrics = HealthMetrics()
        self.diagnostics = sync_diagnostics()
        self._published_diagnostics = None

    def check(self):
        # Always start charts. Missing data must produce alarms instead of disabling this collector.
        return True

    def publish_diagnostics(self):
        """Use the stock plugins.d label protocol; python.d has no custom label API."""
        charts = getattr(self, 'charts', None)
        if charts is None:
            return
        from bases.charts import CHART_CREATE, CLABEL_COLLECT_JOB, CLABEL_COMMIT
        from bases.collection import safe_print
        for name in ('sync', 'queue'):
            chart = charts[name]
            # Native create() also publishes _collect_job. Its later refresh must not
            # replace our labels; reapply after any pending native creation.
            refresh = not chart.flags.created or chart.flags.push
            if refresh:
                chart.create()
            if refresh or self._published_diagnostics != self.diagnostics:
                labels = CLABEL_COLLECT_JOB.format(**chart.params)
                labels += ''.join("CLABEL '{0}' '{1}' '1'\n".format(key, value)
                                  for key, value in sorted(self.diagnostics.items()))
                safe_print(CHART_CREATE.format(**chart.params) + labels + CLABEL_COMMIT)
        self._published_diagnostics = dict(self.diagnostics)

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
            self.diagnostics = sync_diagnostics(snapshot, now)
            self.publish_diagnostics()
            return encode_metrics(data)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            self.diagnostics = sync_diagnostics()
            self.publish_diagnostics()
            return encode_metrics(self.metrics.invalid_data(now))

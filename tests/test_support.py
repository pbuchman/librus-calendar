"""Explicit synthetic configuration shared by offline tests."""
from dataclasses import replace
from app.config import AppConfig
from app.codex_runtime import event_fingerprint as _fingerprint, external_attendees as _attendees

CALENDAR_ID = 'calendar@example.test'
TEST_CONFIG = AppConfig(calendar_id=CALENDAR_ID, calendar_owner=CALENDAR_ID,
    calendar_connector='connector_synthetic', tailscale_owner='owner@example.test',
    allowed_hosts=('school.example.test:8445',))


def event_fingerprint(event):
    return _fingerprint(event, owner_email=TEST_CONFIG.calendar_owner)


def external_attendees(event):
    return _attendees(event, owner_email=TEST_CONFIG.calendar_owner)


from app.codex_runtime import CodexRuntime as _Runtime


class CodexRuntime(_Runtime):
    def __init__(self, config=None):
        values = dict(config or {})
        process = values.pop('run_process', None)
        super().__init__(replace(TEST_CONFIG, **values), run_process=process)

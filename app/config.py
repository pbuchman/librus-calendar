"""Validated, private instance configuration with no personal account defaults."""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Configuration is incomplete or unsafe; values are never echoed."""
    code = "configuration_required"


@dataclass(frozen=True)
class AppConfig:
    calendar_id: str = ''
    calendar_owner: str = ''
    calendar_connector: str = ''
    tailscale_owner: str = ''
    allowed_hosts: tuple[str, ...] = ()
    allow_local: bool = False
    state_path: Path = Path('~/.local/share/librus-calendar/state.sqlite3')
    credentials_path: Path = Path('~/.config/librus-calendar/credentials.json')
    codex_binary: str = 'codex'
    analysis_model: str = 'gpt-6.1-sol'
    analysis_effort: str = 'high'
    tool_model: str = 'gpt-6-luna'
    tool_effort: str = 'medium'
    timeout: int = 300
    monitor_url: str = 'http://127.0.0.1:8795/api/status'
    monitor_output: Path = Path('/var/lib/librus-monitor/status.json')

    def __post_init__(self):
        for name in ('calendar_id', 'calendar_owner', 'calendar_connector', 'tailscale_owner',
                     'codex_binary', 'analysis_model', 'analysis_effort', 'tool_model', 'tool_effort', 'monitor_url'):
            value = getattr(self, name)
            if not isinstance(value, str) or any(ord(char) < 32 for char in value) or value != value.strip():
                raise ConfigurationError('invalid_' + name)
        for name in ('calendar_owner', 'tailscale_owner'):
            if getattr(self, name) and not re.fullmatch(r'[^\s@]+@[^\s@]+', getattr(self, name)):
                raise ConfigurationError('invalid_' + name)
        if self.calendar_connector and not re.fullmatch(r'connector_[A-Za-z0-9_]+', self.calendar_connector):
            raise ConfigurationError('invalid_calendar_connector')
        for name in ('analysis_model', 'tool_model'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', getattr(self, name)):
                raise ConfigurationError('invalid_' + name)
        efforts = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
        if self.analysis_effort not in efforts or self.tool_effort not in efforts:
            raise ConfigurationError('invalid_reasoning_effort')
        if type(self.timeout) is not int or not 1 <= self.timeout <= 300:
            raise ConfigurationError('invalid_timeout')
        if type(self.allow_local) is not bool or not isinstance(self.allowed_hosts, (tuple, list)):
            raise ConfigurationError('invalid_web_configuration')
        for host in self.allowed_hosts:
            if not isinstance(host, str) or not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[::1\])(?::[0-9]{1,5})?', host):
                raise ConfigurationError('invalid_allowed_hosts')
            parsed_host = urlsplit('http://' + host)
            try:
                if parsed_host.port is not None and not 1 <= parsed_host.port <= 65535:
                    raise ConfigurationError('invalid_allowed_hosts')
            except ValueError:
                raise ConfigurationError('invalid_allowed_hosts') from None
        object.__setattr__(self, 'allowed_hosts', tuple(self.allowed_hosts))
        for name in ('state_path', 'credentials_path', 'monitor_output'):
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value):
                raise ConfigurationError('invalid_' + name)
            object.__setattr__(self, name, Path(value).expanduser())
        url = urlsplit(self.monitor_url)
        try:
            valid_port = url.port is None or 1 <= url.port <= 65535
        except ValueError:
            valid_port = False
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or url.username or url.password or url.path != '/api/status' or url.query or url.fragment or not valid_port):
            raise ConfigurationError('invalid_monitor_url')

    def require_analysis(self):
        if not self.codex_binary:
            raise ConfigurationError('codex_binary_required')

    def require_calendar(self):
        self.require_analysis()
        if not all((self.calendar_id, self.calendar_owner, self.calendar_connector)):
            raise ConfigurationError('calendar_configuration_required')

    def require_web(self):
        if not self.allowed_hosts or (not self.allow_local and not self.tailscale_owner):
            raise ConfigurationError('web_configuration_required')


def load_config(path=None, *, environ: Mapping[str, str] | None = None):
    """Load JSON plus explicit environment overrides; reject malformed supplied config."""
    env = os.environ if environ is None else environ
    selected = path if path is not None else env.get('LIBRUS_CONFIG_FILE', '~/.config/librus-calendar/config.json')
    values = {}
    if selected:
        source = Path(selected).expanduser()
        if source.exists():
            from .librus_client import read_private_json
            try:
                values = read_private_json(source)
            except Exception:
                raise ConfigurationError('configuration_file_unreadable') from None
            if not isinstance(values, dict):
                raise ConfigurationError('invalid_configuration_object')
        elif path is not None or 'LIBRUS_CONFIG_FILE' in env:
            raise ConfigurationError('configuration_file_missing')
    allowed = {field.name for field in fields(AppConfig)}
    if set(values) - allowed:
        raise ConfigurationError('unknown_configuration_field')
    for name in allowed:
        key = 'LIBRUS_' + name.upper()
        if key not in env:
            continue
        value = env[key]
        if name == 'allowed_hosts':
            value = tuple(item.strip() for item in value.split(',') if item.strip())
        elif name == 'allow_local':
            if value not in {'0', '1'}:
                raise ConfigurationError('invalid_allow_local')
            value = value == '1'
        elif name == 'timeout':
            try:
                value = int(value)
            except ValueError:
                raise ConfigurationError('invalid_timeout') from None
        values[name] = value
    try:
        return AppConfig(**values)
    except TypeError:
        raise ConfigurationError('invalid_configuration') from None

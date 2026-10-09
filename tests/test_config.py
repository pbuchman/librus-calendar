"""Configuration boundaries use isolated files and fake processes only."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from app.cli import main
from app.config import AppConfig, ConfigurationError, load_config
from app.core import AppStore, has_external_attendees
from app.codex_runtime import CodexRuntime, RuntimeFailure, external_attendees, event_fingerprint
from app.web import create_app
from test_support import TEST_CONFIG


class ConfigurationTests(unittest.TestCase):
    def test_unconfigured_runtime_refuses_tools_before_process(self):
        process = Mock(side_effect=AssertionError('No process may run'))
        runtime = CodexRuntime(AppConfig(), run_process=process)
        for action in (runtime.probe, lambda: runtime._tool('create_event', {})):
            with self.assertRaisesRegex(RuntimeFailure, 'calendar_configuration_required'):
                action()
        result = runtime.execute({'action': 'create'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'calendar_configuration_required')
        process.assert_not_called()

    def test_direct_credential_import_script_can_run_from_another_directory(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/import_credentials.py'
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(script), '--help'], cwd=folder,
                                    text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--target', result.stdout)

    def test_explicit_runner_overrides_mapping_runner_without_unknown_field(self):
        fallback = Mock(side_effect=AssertionError('Fallback must not run'))
        explicit = Mock(side_effect=AssertionError('No process should run'))
        runtime = CodexRuntime({'run_process': fallback}, run_process=explicit)
        self.assertIs(runtime.run_process, explicit)
        self.assertIs(CodexRuntime({'run_process': fallback}).run_process, fallback)
        explicit.assert_not_called()
        fallback.assert_not_called()

    def test_every_calendar_field_is_required(self):
        for field in ('calendar_id', 'calendar_owner', 'calendar_connector'):
            config = replace(TEST_CONFIG, **{field: ''})
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                config.require_calendar()

    def test_configured_models_and_connector_are_exact_in_args(self):
        config = replace(TEST_CONFIG, analysis_model='gpt-test-sol', analysis_effort='xhigh',
                         tool_model='gpt-test-luna', tool_effort='high', codex_binary='/synthetic/bin/codex')
        runtime = CodexRuntime(config)
        analysis = runtime._args('/synthetic/work', 'analysis.json', ())
        tools = runtime._args('/synthetic/work', 'tool.json', ('read_event',))
        self.assertEqual(analysis[0], '/synthetic/bin/codex')
        self.assertEqual(analysis[analysis.index('-m') + 1], 'gpt-test-sol')
        self.assertIn('model_reasoning_effort="xhigh"', analysis)
        self.assertEqual(tools[tools.index('-m') + 1], 'gpt-test-luna')
        self.assertIn('apps.connector_synthetic.tools.read_event.enabled=true', tools)

    def test_configured_analysis_provenance_tracks_actual_selection(self):
        config = replace(TEST_CONFIG, analysis_model='gpt-test-sol', analysis_effort='xhigh')
        record = {'type': 'item.completed', 'item': {'type': 'agent_message',
                  'text': json.dumps({'proposals': [], 'decision_reason': 'Synthetic undated advice.'})}}
        runtime = CodexRuntime(config, run_process=lambda *_: (0, json.dumps(record), ''))
        self.assertEqual(runtime.analyze({'text': 'Synthetic advice.'}), [])
        self.assertEqual(runtime.last_analysis_metadata['model'], config.analysis_model)
        self.assertEqual(runtime.last_analysis_metadata['effort'], config.analysis_effort)

    def test_verified_owner_is_separate_from_calendar_id(self):
        owner = 'account-owner@example.test'
        self_event = {'attendees': [{'email': owner, 'self': True}]}
        self.assertEqual(external_attendees(self_event, owner), [])
        self.assertFalse(has_external_attendees(self_event, owner))
        self.assertEqual(event_fingerprint(self_event, owner), event_fingerprint({}))
        for attendee in ({'email': TEST_CONFIG.calendar_id, 'self': True}, {'email': owner}, owner, None):
            event = {'attendees': [attendee]}
            self.assertTrue(external_attendees(event, owner))
            self.assertTrue(has_external_attendees(event, owner))
        self.assertTrue(external_attendees(self_event))
        self.assertTrue(has_external_attendees(self_event))

    def test_private_json_and_environment_overrides_share_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'calendar_id': 'from-file@example.test', 'timeout': 60}))
            path.chmod(0o600)
            config = load_config(path, environ={'LIBRUS_CALENDAR_ID': 'from-env@example.test',
                'LIBRUS_ALLOWED_HOSTS': 'school.example.test:8445, localhost:8795',
                'LIBRUS_ALLOW_LOCAL': '1', 'LIBRUS_TIMEOUT': '90',
                'LIBRUS_STATE_PATH': str(Path(folder) / 'state.sqlite3')})
            self.assertEqual(config.calendar_id, 'from-env@example.test')
            self.assertEqual(config.allowed_hosts, ('school.example.test:8445', 'localhost:8795'))
            self.assertTrue(config.allow_local)
            self.assertEqual(config.timeout, 90)
            self.assertEqual(config.state_path, Path(folder) / 'state.sqlite3')

    def test_explicitly_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ConfigurationError, 'configuration_file_missing'):
            load_config(Path(folder) / 'missing.json', environ={})

    def test_blank_file_setting_does_not_read_default_config(self):
        with patch('app.librus_client.read_private_json', side_effect=AssertionError('No private files')):
            config = load_config(environ={'LIBRUS_CONFIG_FILE': ''})
        self.assertEqual(config.calendar_id, '')
        self.assertEqual(config.calendar_connector, '')

    def test_invalid_file_permissions_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.json'
            source.write_text('{}')
            source.chmod(0o644)
            with self.assertRaisesRegex(ConfigurationError, 'configuration_file_unreadable'):
                load_config(source, environ={})
            source.chmod(0o600)
            linked = Path(folder) / 'linked.json'
            linked.symlink_to(source)
            with self.assertRaisesRegex(ConfigurationError, 'configuration_file_unreadable'):
                load_config(linked, environ={})

    def test_unknown_or_malformed_private_values_are_never_echoed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            for payload in ({'synthetic-private-value': 'dummy-value'}, {'calendar_owner': 'synthetic-private-value'},
                            {'calendar_connector': 'synthetic-private-value'}, {'timeout': 'synthetic-private-value'}):
                path.write_text(json.dumps(payload))
                path.chmod(0o600)
                with self.assertRaises(ConfigurationError) as caught:
                    load_config(path, environ={})
                self.assertNotIn('synthetic-private-value', str(caught.exception))
                self.assertNotIn('dummy-value', str(caught.exception))

    def test_environment_numbers_and_flags_are_validated(self):
        for overrides in ({'LIBRUS_ALLOW_LOCAL': 'true'}, {'LIBRUS_TIMEOUT': 'NaN'}, {'LIBRUS_TIMEOUT': '301'}):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigurationError):
                load_config(environ={'LIBRUS_CONFIG_FILE': '', **overrides})

    def test_invalid_connector_hosts_and_remote_monitor_are_rejected(self):
        for changes in ({'calendar_connector': 'connector_invalid.enabled'}, {'allowed_hosts': ['*']},
                        {'allowed_hosts': ['user@school.example.test']}, {'allowed_hosts': ['school.example.test:70000']},
                        {'monitor_url': 'https://school.example.test/api/status'},
                        {'monitor_url': 'http://127.0.0.1:8795/api/status?forward=1'},
                        {'monitor_url': 'http://' + 'synthetic-user:' + 'dummy-secret@127.0.0.1:8795/api/status'},
                        {'monitor_url': 'http://localhost:broken/api/status'}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                replace(TEST_CONFIG, **changes)

    def test_web_requires_trusted_config_before_opening_state(self):
        with patch('app.core.AppStore', side_effect=AssertionError('No database access')):
            with self.assertRaisesRegex(ConfigurationError, 'web_configuration_required'):
                create_app(config=AppConfig())

    def test_demo_requires_injected_resources_and_loopback_hosts(self):
        for kwargs in ({'store': None, 'sync_request': lambda: None, 'config': AppConfig()},
                       {'store': object(), 'sync_request': None, 'config': AppConfig()},
                       {'store': object(), 'sync_request': lambda: None, 'config': None},
                       {'store': object(), 'sync_request': lambda: None, 'config': AppConfig(),
                        'hosts': ['school.example.test:8445']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                create_app(demo_mode=True, **kwargs)

    def test_cli_unconfigured_sync_never_reads_credentials_or_logs_input(self):
        with tempfile.TemporaryDirectory() as folder:
            config = AppConfig(state_path=Path(folder) / 'state.sqlite3')
            with patch('app.librus_client.load_credentials', side_effect=AssertionError('No credentials')) as credentials, \
                 patch('app.librus_client.LibrusClient', side_effect=AssertionError('No login')) as client, \
                 patch('app.codex_runtime.subprocess.Popen', side_effect=AssertionError('No Codex process')) as process, \
                 redirect_stdout(io.StringIO()) as output:
                code = main(['sync'], config=config)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())['error_code'], 'configuration_required')
            credentials.assert_not_called()
            client.assert_not_called()
            process.assert_not_called()

    def test_state_cannot_be_retargeted_to_another_calendar_or_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            store = AppStore(path, config=TEST_CONFIG)
            store.save_message({'message_id': 'synthetic-message', 'text': 'Synthetic source.'})
            store.close()
            original = path.read_bytes()
            for changes in ({'calendar_id': 'another-calendar@example.test'}, {'calendar_owner': 'another-owner@example.test'}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ConfigurationError, 'calendar_state_mismatch'):
                    AppStore(path, config=replace(TEST_CONFIG, **changes))
                self.assertEqual(path.read_bytes(), original)
            reopened = AppStore(path, config=TEST_CONFIG)
            self.assertEqual(reopened.status()['messages'], 1)
            reopened.close()


if __name__ == '__main__':
    unittest.main()

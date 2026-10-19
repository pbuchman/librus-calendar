from test_support import CALENDAR_ID, CodexRuntime, TEST_CONFIG, event_fingerprint, external_attendees
import copy
from dataclasses import replace
import json
import signal
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from app.codex_runtime import RuntimeFailure, marker_present


def sample_event():
    return {"summary": "[Szkoła] Próba", "description": "Źródło: librus-calendar:operation123",
            "start": {"dateTime": "2026-11-05T10:00:00+01:00", "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": "2026-11-05T11:00:00+01:00", "timeZone": "Europe/Warsaw"},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 1440}]}}


def operation(action="create"):
    return {"action": action, "operation_id": "operation123", "marker": "librus-calendar:operation123",
            "calendar_id": CALENDAR_ID, "event": sample_event(), "event_id": None, "expected_fingerprint": None}


class FakeRuntime(CodexRuntime):
    def __init__(self, results):
        super().__init__()
        self.results = iter(results)
        self.calls = []

    def _tool(self, tool, args):
        self.calls.append((tool, args))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class RuntimeTests(unittest.TestCase):
    def test_fingerprint_ignores_display_metadata_and_timezone_spelling(self):
        left = sample_event()
        right = dict(left, id="google1", updated="changed", htmlLink="https://calendar.google.com/e")
        right["start"] = {"dateTime": "2026-11-05T09:00:00Z"}
        self.assertEqual(event_fingerprint(left), event_fingerprint(right))

    def test_manual_content_change_has_different_fingerprint(self):
        left = sample_event()
        right = dict(left, summary="[Szkoła] Ręcznie zmienione")
        self.assertNotEqual(event_fingerprint(left), event_fingerprint(right))

    def test_create_searches_then_writes_and_reads_actual_event(self):
        event = dict(sample_event(), id="google1", url="https://calendar.google.com/e")
        runtime = FakeRuntime([{"events": []}, {"event": event}, {"event": event}])
        result = runtime.execute(operation())
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["event_id"], "google1")
        self.assertEqual([x[0] for x in runtime.calls], ["search_events", "create_event", "read_event"])
        self.assertEqual(runtime.calls[1][1]["attendees"], [])

    def test_existing_marker_reconciles_without_write(self):
        event = dict(sample_event(), id="google1")
        runtime = FakeRuntime([{"events": [event]}, {"event": event}])
        self.assertEqual(runtime.execute(operation())["status"], "applied")
        self.assertEqual([x[0] for x in runtime.calls], ["search_events", "read_event"])

    def test_unknown_missing_marker_never_retries_create(self):
        runtime = FakeRuntime([{"events": []}])
        op = dict(operation(), reconcile_only=True)
        self.assertEqual(runtime.execute(op)["status"], "unknown")
        self.assertEqual([x[0] for x in runtime.calls], ["search_events"])

    def test_timeout_during_create_is_unknown(self):
        runtime = FakeRuntime([{"events": []}, RuntimeFailure("codex_timeout", possible_write=True)])
        self.assertEqual(runtime.execute(operation())["status"], "unknown")

    def test_read_failure_before_write_is_retryable(self):
        runtime = FakeRuntime([RuntimeFailure("codex_limit")])
        self.assertEqual(runtime.execute(operation())["status"], "failed")

    def test_manual_edit_conflict_never_writes(self):
        old = dict(sample_event(), id="google1")
        changed = dict(old, summary="[Szkoła] Zmienione")
        op = dict(operation("update"), event_id="google1", expected_fingerprint=event_fingerprint(old))
        runtime = FakeRuntime([{"event": changed}])
        self.assertEqual(runtime.execute(op)["status"], "review")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_cancel_requires_explicit_approval(self):
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint="abc")
        runtime = FakeRuntime([])
        self.assertEqual(runtime.execute(op)["error"], "deletion_requires_approval")
        self.assertFalse(runtime.calls)

    def test_unowned_event_is_never_updated(self):
        op = dict(operation("update"), event_id="google1", expected_fingerprint="abc")
        runtime = FakeRuntime([{"event": dict(sample_event(), id="google1", description="Personal")}])
        self.assertEqual(runtime.execute(op)["error"], "event_not_owned")

    def test_arbitrary_calendar_and_attendees_rejected(self):
        runtime = FakeRuntime([])
        self.assertEqual(runtime.execute(dict(operation(), calendar_id="other-calendar@example.invalid"))["error"], "calendar_not_allowed")
        op = operation()
        op["event"]["attendees"] = ["someone@example.test"]
        self.assertEqual(runtime.execute(op)["error"], "unsafe_event_fields")

    def test_librus_and_legacy_school_titles_keep_strict_marker_validation(self):
        for prefix in ("[Librus]", "[Szkoła]"):
            op = operation()
            op["event"]["summary"] = prefix + " Próba"
            CodexRuntime()._validate_operation(op)
            op["event"]["description"] = "librus-calendar:other-operation"
            with self.assertRaisesRegex(RuntimeFailure, "event_ownership_missing"):
                CodexRuntime()._validate_operation(op)
        op = operation()
        op["event"]["summary"] = "Personal event"
        with self.assertRaisesRegex(RuntimeFailure, "event_ownership_missing"):
            CodexRuntime()._validate_operation(op)

    def test_explicit_sol_high_and_isolation_for_analysis(self):
        args = CodexRuntime()._args("/private/work", "agenda_plan.json", ())
        self.assertEqual(args[args.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="high"', args)
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
            self.assertIn(flag, args)
        self.assertIn("apps._default.enabled=false", args)

    def test_worker_interruption_kills_and_reaps_isolated_process_group(self):
        from app.sync import SyncInterrupted
        process = MagicMock(pid=12345)
        process.communicate.side_effect = [SyncInterrupted('sync_interrupted'), ('', '')]
        with patch('app.codex_runtime.subprocess.Popen', return_value=process), patch('app.codex_runtime.os.killpg') as kill:
            with self.assertRaises(SyncInterrupted):
                CodexRuntime()._run_process(['codex'], 'private input', 300)
            kill.assert_called_once_with(12345, signal.SIGKILL)
            self.assertEqual(process.communicate.call_count, 2)

    def test_mcp_self_report_without_actual_call_is_rejected(self):
        def process(args, prompt, timeout):
            return 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}}), ""
        runtime = CodexRuntime({"run_process": process})
        with self.assertRaisesRegex(RuntimeFailure, "unexpected_tool_count"):
            runtime._tool("list_calendars", {"max_results": 100})

    def test_actual_mcp_arguments_must_match(self):
        record = {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "google_calendar.create_event",
                   "arguments": {"calendar_id": "wrong"}, "result": {"structured_content": {"id": "bogus"}}}}
        runtime = CodexRuntime({"run_process": lambda *args: (0, json.dumps(record), "")})
        with self.assertRaises(RuntimeFailure) as error:
            runtime._tool("create_event", {"calendar_id": CALENDAR_ID})
        self.assertTrue(error.exception.possible_write)



    def test_cancel_verifies_tombstone_and_preserves_audit(self):
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True)
        runtime = FakeRuntime([{"event": before}, {"result": None}, RuntimeFailure("calendar_event_not_found")])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["snapshot"]["status"], "cancelled")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event", "delete_event", "read_event"])

    def test_cancel_uncertain_verification_remains_unknown(self):
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True)
        runtime = FakeRuntime([{"event": before}, {"result": None}, RuntimeFailure("calendar_tool_failed")])
        self.assertEqual(runtime.execute(op)["status"], "unknown")

    def test_deleted_update_is_review_without_recreation(self):
        op = dict(operation("update"), event_id="google1", expected_fingerprint="abc")
        runtime = FakeRuntime([RuntimeFailure("calendar_event_not_found")])
        self.assertEqual(runtime.execute(op)["status"], "review")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_limit_without_write_call_is_definitely_safe(self):
        runtime = CodexRuntime({"run_process": lambda *args: (1, "", "usage limit exceeded")})
        with self.assertRaises(RuntimeFailure) as error:
            runtime._tool("create_event", {"calendar_id": CALENDAR_ID})
        self.assertFalse(error.exception.possible_write)

    def test_real_connector_rendered_description_and_self_attendee(self):
        intended = sample_event()
        intended["description"] = "Kontrolowane wydarzenie testowe.\nlibrus-calendar:test_2747d1da61d847579ca11351059dfcd7"
        actual = copy.deepcopy(intended)
        actual["description"] = "Kontrolowane wydarzenie testowe. librus-\ncalendar:test_2747d1da61d847579ca11351059dfcd7\n\n"
        actual["start"] = "2026-11-05T10:00:00+01:00"
        actual["end"] = "2026-11-05T11:00:00+01:00"
        actual["reminders"]["use_default"] = actual["reminders"].pop("useDefault")
        actual["attendees"] = [{"email": TEST_CONFIG.calendar_owner, "is_self": True, "response_status": "accepted"}]
        self.assertTrue(marker_present(actual["description"], "librus-calendar:test_2747d1da61d847579ca11351059dfcd7"))
        self.assertEqual(event_fingerprint(actual), event_fingerprint(intended))
        self.assertEqual(external_attendees(actual), [])

    def test_foreign_attendee_is_not_treated_as_self(self):
        actual = sample_event()
        actual["attendees"] = [{"email": "other@example.test", "is_self": True}]
        self.assertEqual(len(external_attendees(actual)), 1)
        self.assertNotEqual(event_fingerprint(actual), event_fingerprint(sample_event()))

    def test_marker_wrapping_cannot_change_identifier(self):
        self.assertFalse(marker_present("librus-\ncalendar:operation124", "librus-calendar:operation123"))
        self.assertFalse(marker_present("librus-\ncalendar:operation1234", "librus-calendar:operation123"))

    def test_create_explicitly_disables_meet_and_self_attendance(self):
        args = CodexRuntime()._write_args(operation())
        self.assertFalse(args["add_google_meet"])
        self.assertEqual(args["self_attendance"], "omit")


    def test_known_create_reconciliation_only_fetches_exact_event(self):
        op = dict(operation(), event_id="existing-google-id", reconcile_only=True)
        runtime = FakeRuntime([dict(sample_event(), id="existing-google-id")])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_resume_test_stops_read_only_when_reconciliation_unknown(self):
        from scripts.probe_runtime import calendar_test
        class Runtime:
            def __init__(self):
                self.calls = []
            def execute(self, op):
                self.calls.append(op)
                return {"status": "unknown", "error": "read_failed", "event_id": None}
        with tempfile.TemporaryDirectory() as folder:
            recovery = Path(folder) / ".local/share/librus-calendar/runtime-test-recovery.json"
            recovery.parent.mkdir(parents=True)
            op = operation()
            op["operation_id"] = "test_existing123"
            op["marker"] = "librus-calendar:test_existing123"
            op["event"]["description"] = op["marker"]
            recovery.write_text(json.dumps({"operation": op, "result": {"event_id": "exact-existing-id"}, "stage": "created"}))
            runtime = Runtime()
            runtime.config = replace(TEST_CONFIG, state_path=recovery.parent / "state.sqlite3")
            result = calendar_test(runtime, resume=True)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(len(runtime.calls), 1)
            self.assertTrue(runtime.calls[0]["reconcile_only"])
            self.assertEqual(runtime.calls[0]["event_id"], "exact-existing-id")
            self.assertEqual(json.loads(recovery.read_text())["known_event_id"], "exact-existing-id")

    def test_plain_mcp_404_is_authoritative_missing_event(self):
        args = {"calendar_id": CALENDAR_ID, "event_id": "deleted-id"}
        record = {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "google_calendar.read_event", "arguments": args,
                    "result": {"structured_content": None, "content": [{"type": "text", "text": "Error 404: event not found"}]}}}
        runtime = CodexRuntime({"run_process": lambda *args: (0, json.dumps(record), "")})
        with self.assertRaisesRegex(RuntimeFailure, "calendar_event_not_found"):
            runtime._fetch("deleted-id")

    def test_cancel_tombstone_keeps_prior_owned_description(self):
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True)
        runtime = FakeRuntime([before, {"result": None}, {"id": "google1", "status": "cancelled"}])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["snapshot"]["description"], before["description"])

    def test_plain_delete_ack_requires_following_fetch_verification(self):
        args = {"calendar_id": CALENDAR_ID, "event_id": "google1"}
        record = {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "google_calendar.delete_event", "arguments": args,
                    "result": {"structured_content": None, "content": [{"type": "text", "text": "Event deleted successfully."}]}}}
        runtime = CodexRuntime({"run_process": lambda *args: (0, json.dumps(record), "")})
        self.assertEqual(runtime._tool("delete_event", args), {"result": None})
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True)
        runtime = FakeRuntime([before, "Event deleted successfully.", before])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "delete_verification_failed")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event", "delete_event", "read_event"])

    def test_reconcile_cancel_full_cancelled_snapshot_is_applied_without_delete(self):
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True, reconcile_only=True)
        runtime = FakeRuntime([dict(before, status="cancelled")])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["snapshot"]["description"], before["description"])
        self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_deleted_create_and_update_never_recreate_or_apply(self):
        deleted = dict(sample_event(), id="google1", status="cancelled")
        for action in ("create", "update"):
            runtime = FakeRuntime([deleted])
            op = dict(operation(action), event_id="google1", expected_fingerprint=event_fingerprint(deleted), reconcile_only=True)
            self.assertEqual(runtime.execute(op)["status"], "review")
            self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_cancel404_requires_exact_trusted_prior_snapshot(self):
        before = dict(sample_event(), id="google1")
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint=event_fingerprint(before), deletion_approved=True, reconcile_only=True,
                  prior_snapshot=before, prior_fingerprint=event_fingerprint(before), prior_marker="librus-calendar:operation123")
        runtime = FakeRuntime([RuntimeFailure("calendar_event_not_found")])
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["snapshot"]["description"], before["description"])
        for bad in (dict(op, prior_fingerprint="wrong"), dict(op, prior_marker="librus-calendar:foreign123"),
                    dict(op, prior_snapshot=dict(before, id="other-id"))):
            runtime = FakeRuntime([RuntimeFailure("calendar_event_not_found")])
            self.assertEqual(runtime.execute(bad)["status"], "review")

    def test_cancel404_without_prior_audit_is_review(self):
        op = dict(operation("cancel"), event_id="google1", expected_fingerprint="known", deletion_approved=True, reconcile_only=True)
        runtime = FakeRuntime([RuntimeFailure("calendar_event_not_found")])
        self.assertEqual(runtime.execute(op)["error"], "cancel_audit_snapshot_missing")

    def test_real_read_event_all_day_midnight_projection_preserves_dates(self):
        desired = {"summary": "[Szkoła] Dzień wolny", "description": "Dzień wolny\nlibrus-calendar:operation123",
                   "start": {"date": "2026-10-14"}, "end": {"date": "2026-10-15"},
                   "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 360}]}}
        actual = dict(desired, id="dayoff-google-id", start="2026-10-14T00:00:00", end="2026-10-15T00:00:00", attendees=[], hangout_link=None)
        runtime = FakeRuntime([actual])
        normalized = runtime._fetch("dayoff-google-id", desired)
        self.assertEqual(normalized["start"], {"date": "2026-10-14"})
        self.assertEqual(normalized["end"], {"date": "2026-10-15"})
        self.assertEqual(event_fingerprint(normalized), event_fingerprint(desired))
        self.assertEqual(runtime.calls[0][0], "read_event")
        runtime = FakeRuntime([actual])
        op = dict(operation(), event=desired, event_id="dayoff-google-id", reconcile_only=True)
        result = runtime.execute(op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual([x[0] for x in runtime.calls], ["read_event"])

    def test_empty_fetch_projection_is_never_filled_from_expected_dates(self):
        desired = dict(sample_event(), start={"date": "2026-10-14"}, end={"date": "2026-10-15"})
        runtime = FakeRuntime([dict(desired, id="dayoff-id", start="", end="")])
        actual = runtime._fetch("dayoff-id", desired)
        self.assertEqual(actual["start"], "")
        self.assertNotEqual(event_fingerprint(actual), event_fingerprint(desired))

    def test_timed_aware_midnight_is_not_converted_to_all_day(self):
        desired = dict(sample_event(), start={"date": "2026-10-14"}, end={"date": "2026-10-15"})
        runtime = FakeRuntime([dict(desired, id="dayoff-id", start="2026-10-14T00:00:00+02:00", end="2026-10-15T00:00:00+02:00")])
        actual = runtime._fetch("dayoff-id", desired)
        self.assertEqual(actual["start"], "2026-10-14T00:00:00+02:00")
        self.assertNotEqual(event_fingerprint(actual), event_fingerprint(desired))

    def test_prior_timed_string_boundary_context_remains_timed(self):
        raw = dict(sample_event(), id="timed-id", start="2026-10-14T09:00:00+02:00", end="2026-10-14T10:00:00+02:00")
        runtime = FakeRuntime([raw])
        actual = runtime._fetch("timed-id", raw)
        self.assertEqual(actual["start"], raw["start"])


class CalendarProbeTests(unittest.TestCase):
    @staticmethod
    def call(*, identity=None, kind='item.completed', tool='google_calendar.list_calendars',
             arguments=None, result=None, status=None):
        item = {'type': 'mcp_tool_call', 'tool': tool,
                'arguments': {'max_results': 100} if arguments is None else arguments}
        if identity is not None:
            item['id'] = identity
        if kind == 'item.completed':
            item['result'] = result if result is not None else {'structured_content': {
                'calendars': [{'id': CALENDAR_ID, 'access_role': 'owner'}]}}
        if status is not None:
            item['status'] = status
        return {'type': kind, 'item': item}

    def runtime(self, outputs):
        self.invocations = []
        remaining = iter(outputs)
        def run(args, prompt, timeout):
            self.invocations.append((args, prompt))
            return next(remaining)
        return CodexRuntime({'run_process': run})

    @staticmethod
    def output(*records, stderr='', code=0):
        return code, '\n'.join(json.dumps(record) for record in records), stderr

    def test_zero_calls_recover_once_with_exact_read_permissions_and_wait(self):
        runtime = self.runtime([self.output({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}}),
                                self.output(self.call())])
        sink = []
        runtime.probe_attempt_sink = lambda record: sink.append(record)
        with patch('app.codex_runtime.time.sleep') as sleep:
            result = runtime.probe()
        sleep.assert_called_once_with(5)
        self.assertEqual((result['attempts'], result['recovered']), (2, True))
        self.assertEqual([record['result'] for record in sink], ['running', 'failed', 'running', 'ok'])
        first = runtime.last_probe_attempts[0]
        self.assertEqual((first['started_count'], first['completed_count'], first['parse_error_count']), (0, 0, 0))
        self.assertTrue(first['expected_tool_name'] and first['expected_tool_arguments'])
        for args, prompt in self.invocations:
            self.assertEqual(args[args.index('-m') + 1], 'gpt-6.1-sol')
            self.assertIn('model_reasoning_effort="high"', args)
            self.assertIn('apps.connector_synthetic.tools.list_calendars.enabled=true', args)
            self.assertFalse(any('.tools.create_event.' in arg or '.tools.update_event.' in arg or '.tools.delete_event.' in arg for arg in args))
            self.assertIn('"tool": "list_calendars", "arguments": {"max_results": 100}', prompt)
            self.assertIn('inspect ALL_TOOLS metadata', prompt)
            self.assertIn('functions.exec', prompt)
            self.assertIn('that one application tool', prompt)
            self.assertIn('view_image', args)
            self.assertFalse(any('tools.view_image=' in arg for arg in args))
            self.assertIn('suppress_unstable_features_warning=true', args)

    def test_multiple_completed_expected_calls_can_recover_but_never_third_attempt(self):
        first = self.output(self.call(identity='a'), self.call(identity='b'))
        for second, succeeded in ((self.output(self.call()), True), (first, False)):
            runtime = self.runtime([first, second, self.output(self.call())])
            with patch('app.codex_runtime.time.sleep') as sleep:
                if succeeded:
                    self.assertTrue(runtime.probe()['recovered'])
                else:
                    with self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_count'):
                        runtime.probe()
            self.assertEqual(len(self.invocations), 2)
            sleep.assert_called_once_with(5)

    def test_wrong_tools_or_arguments_never_retry_even_with_multiple_calls(self):
        for record in (self.call(tool='google_calendar.create_event'), self.call(arguments={'max_results': 99}),
                       self.call(arguments={'max_results': 100, 'page': 1})):
            runtime = self.runtime([self.output(self.call(), record)])
            with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'unexpected_tool_arguments'):
                runtime.probe()
            sleep.assert_not_called()
            self.assertEqual(len(self.invocations), 1)

    def test_malformed_or_unknown_protocol_never_retry(self):
        malformed = ['not-json', json.dumps([]), json.dumps(None), json.dumps({'type': 'new.protocol'}),
            json.dumps({'type': 'item.completed', 'item': []}), json.dumps({'type': 'item.completed', 'item': 'PRIVATE'}),
            json.dumps({'type': 'item.completed', 'item': {'type': []}}),
            json.dumps({'type': 'item.completed', 'item': {'type': 'shell', 'command': 'PRIVATE'}})]
        for bad in ({'tool': []}, {'arguments': []}, {'result': []}, {'id': 23}, {'status': []}):
            record = self.call()
            record['item'].update(bad)
            malformed.append(json.dumps(record))
        for stdout in malformed:
            runtime = self.runtime([(0, stdout, '')])
            with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'codex_protocol_invalid'):
                runtime.probe()
            sleep.assert_not_called()
            self.assertNotIn('PRIVATE', json.dumps(runtime.last_probe_attempts))

    def test_started_or_updated_unfinished_call_never_retry(self):
        for records in ((self.call(identity='a', kind='item.started'),),
                        (self.call(identity='a', kind='item.started'), self.call(identity='b')),
                        (self.call(identity='a', kind='item.updated'),),
                        (self.call(kind='item.updated'),),
                        (self.call(status='in_progress'),)):
            runtime = self.runtime([self.output(*records)])
            with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'calendar_tool_incomplete'):
                runtime.probe()
            self.assertGreater(runtime.last_probe_attempts[0]['incomplete_count'], 0)
            sleep.assert_not_called()

    def test_completed_start_pair_is_accepted_and_duplicate_identity_is_malformed(self):
        runtime = self.runtime([self.output(self.call(identity='a', kind='item.started'), self.call(identity='a'))])
        self.assertFalse(runtime.probe()['recovered'])
        self.assertEqual(runtime.last_probe_attempts[0]['started_count'], 1)
        runtime = self.runtime([self.output(self.call(identity='a'), self.call(identity='a'))])
        with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'codex_protocol_invalid'):
            runtime.probe()
        sleep.assert_not_called()

    def test_errors_in_any_lifecycle_record_block_success_and_retry(self):
        for kind in ('item.started', 'item.updated'):
            for changes in ({'error': {'message': 'PRIVATE quota'}}, {'status': 'failed'}, {'status': 'cancelled'},
                            {'result': {'isError': True}}, {'result': {'structured_content': {'error': 'PRIVATE'}}}):
                failed = self.call(identity='a', kind=kind)
                failed['item'].update(changes)
                for completed in ((self.call(identity='a'),), (self.call(identity='a'), self.call(identity='b'))):
                    runtime = self.runtime([self.output(failed, *completed)])
                    with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'calendar_tool_failed'):
                        runtime.probe()
                    sleep.assert_not_called()

    def test_known_process_errors_and_quota_permission_config_never_retry(self):
        for hint, code in (('Usage limit exceeded PRIVATE', 'codex_limit'),
                           ('Permission denied PRIVATE', 'codex_permission_denied'),
                           ('Invalid configuration PRIVATE', 'codex_configuration_failed'),
                           ('Startup failed PRIVATE', 'codex_failed')):
            variants = [self.output({'type': 'item.completed', 'item': {'type': 'error', 'message': hint}}),
                        self.output({'type': 'turn.failed', 'error': {'message': hint}}),
                        self.output(stderr=hint, code=0 if code != 'codex_failed' else 1)]
            for output in variants:
                runtime = self.runtime([output])
                with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, code):
                    runtime.probe()
                sleep.assert_not_called()
                self.assertNotIn('PRIVATE', json.dumps(runtime.last_probe_attempts))

    def test_successful_payload_or_agent_text_is_not_process_error_evidence(self):
        call = self.call()
        call['item']['result']['structured_content']['calendars'][0]['description'] = 'credits quota forbidden unauthorized 404 PRIVATE'
        runtime = self.runtime([self.output(call, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'quota PRIVATE'}})])
        self.assertEqual(runtime.probe()['access_role'], 'owner')
        self.assertNotIn('PRIVATE', json.dumps(runtime.last_probe_attempts))

    def test_tool_errors_unreadable_result_access_and_timeout_never_retry(self):
        bad_error = self.call()
        bad_error['item']['error'] = {'message': 'Permission denied PRIVATE'}
        missing = self.call()
        missing['item'].pop('result')
        variants = [(self.output(self.call(), bad_error), 'calendar_tool_failed'),
            (self.output(missing), 'calendar_tool_incomplete'),
            (self.output(self.call(result={'content': [{'type': 'text', 'text': 'PRIVATE unreadable'}]})), 'calendar_result_unreadable'),
            (self.output(self.call(result={'structured_content': {'errors': ['PRIVATE quota']}})), 'calendar_tool_failed'),
            (self.output(self.call(result={'structured_content': {'calendars': [{'id': CALENDAR_ID, 'access_role': 'reader'}]}})), 'calendar_write_access_missing'),
            (self.output(self.call(), self.call(result={'structured_content': {'calendars': []}})), 'calendar_write_access_missing'),
            (self.output(self.call(), self.call(result={'structured_content': []})), 'calendar_result_unreadable')]
        for output, code in variants:
            runtime = self.runtime([output])
            with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, code):
                runtime.probe()
            sleep.assert_not_called()
        runtime = CodexRuntime({'run_process': lambda *_: (_ for _ in ()).throw(RuntimeFailure('codex_timeout'))})
        with patch('app.codex_runtime.time.sleep') as sleep, self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            runtime.probe()
        sleep.assert_not_called()

    def test_audit_failure_blocks_dispatch_or_retry(self):
        for fail_on in (1, 2, 3, 4):
            runtime = self.runtime([self.output(), self.output(self.call())])
            records = []
            def persist(record):
                records.append(record)
                if len(records) == fail_on:
                    raise OSError('PRIVATE storage failure')
            runtime.probe_attempt_sink = persist
            with patch('app.codex_runtime.time.sleep'), self.assertRaisesRegex(RuntimeFailure, 'calendar_probe_audit_failed'):
                runtime.probe()
            self.assertEqual(len(self.invocations), (0, 1, 1, 2)[fail_on - 1])

    def test_interruption_is_recorded_and_retry_wait_obeys_deadline(self):
        from app.sync import SyncInterrupted
        def interrupted(*_):
            raise SyncInterrupted('sync_timeout')
        runtime = CodexRuntime({'run_process': interrupted})
        records = []
        runtime.probe_attempt_sink = lambda record: records.append(record)
        with self.assertRaises(SyncInterrupted):
            runtime.probe()
        self.assertEqual((records[-1]['result'], records[-1]['safe_code']), ('interrupted', 'sync_timeout'))
        runtime = self.runtime([self.output(), self.output(self.call())])
        with patch('app.codex_runtime.time.sleep', side_effect=SyncInterrupted('sync_timeout')), self.assertRaises(SyncInterrupted):
            runtime.probe()
        self.assertEqual(len(self.invocations), 1)

    def test_timeout_captures_emitted_call_counts_and_process_exit_without_private_output(self):
        import subprocess
        from app.codex_runtime import CodexRuntime as ActualRuntime
        process = MagicMock(pid=12345, returncode=-9)
        started = json.dumps(self.call(identity='a', kind='item.started'))
        process.communicate.side_effect = [subprocess.TimeoutExpired('codex', 300), (started, 'PRIVATE stderr')]
        runtime = ActualRuntime(TEST_CONFIG)
        with patch.object(runtime, '_cli_version', return_value=None), patch('app.codex_runtime.subprocess.Popen', return_value=process), patch('app.codex_runtime.os.killpg'), self.assertRaisesRegex(RuntimeFailure, 'codex_timeout'):
            runtime.probe()
        attempt = runtime.last_probe_attempts[0]
        self.assertEqual((attempt['started_count'], attempt['completed_count'], attempt['process_code']), (1, 0, -9))
        self.assertEqual(attempt['safe_code'], 'codex_timeout')
        self.assertNotIn('PRIVATE', json.dumps(attempt))

    def test_cli_version_is_strictly_sanitized(self):
        for output, expected in (('codex-cli 0.162.1\n', '0.162.1'), ('PRIVATE secret\n', None),
                                 ('codex-cli 0.162.1\nPRIVATE', None), ('codex-cli 0.162.1+abc.2', '0.162.1+abc.2')):
            runtime = self.runtime([])
            runtime._injected_runner = False
            process = MagicMock(returncode=0, stdout=output)
            with patch('app.codex_runtime.subprocess.run', return_value=process):
                self.assertEqual(runtime._cli_version(), expected)

    def test_write_tool_count_is_never_retryable(self):
        runtime = self.runtime([self.output()])
        with self.assertRaises(RuntimeFailure) as caught:
            runtime._tool('create_event', {'calendar_id': CALENDAR_ID})
        self.assertTrue(caught.exception.possible_write)
        self.assertFalse(caught.exception.retryable_probe)
        self.assertEqual(len(self.invocations), 1)


if __name__ == "__main__":
    unittest.main()

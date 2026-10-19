"""The single planner retains strict temporal and policy output contracts."""
import unittest
from app.agenda import validate_agenda_plan
from app.codex_runtime import RuntimeFailure, CodexRuntime
from test_agenda import action, item, plan
from test_support import TEST_CONFIG


class AgendaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'snapshot_hash': 'f' * 64, 'sources': [{'id': 'fictional-source', 'content_hash': 'a' * 64}],
                         'agenda': [], 'offer_groups': []}

    def check(self, raw):
        return validate_agenda_plan(plan(self.snapshot, [item(self.snapshot, action=raw)]), self.snapshot)

    def test_exact_deadline_has_no_invented_duration_or_hour(self):
        result = self.check(dict(action(), due_at='2030-03-12T14:00:00+01:00'))
        self.assertEqual(result['items'][0]['action']['due_at'], '2030-03-12T14:00:00+01:00')
        self.assertIsNone(result['items'][0]['action']['end'])
        self.assertIsNone(self.check(action())['items'][0]['action']['due_at'])

    def test_invalid_time_offset_deadline_duration_and_wrong_day_rejected(self):
        for changes in ({'start': '2030-02-30'}, {'end': '2030-03-13'}, {'due_at': '2030-03-12T14:00:00'},
                        {'due_at': '2030-03-13T14:00:00+01:00'}, {'due_at': '2030-03-12T14:00:00+02:00'},
                        {'all_day': False, 'start': '2030-03-12T14:00:00+01:00'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeFailure, 'invalid_analysis_temporal_fields'):
                self.check(dict(action(), **changes))

    def test_timed_event_accepts_stated_time_and_rejects_naive_or_dst_gap(self):
        self.check(dict(action(), temporal_kind='event', all_day=False, start='2030-03-12T14:00:00+01:00'))
        for start in ('2030-03-12T14:00:00', '2030-03-31T02:30:00+01:00'):
            with self.subTest(start=start), self.assertRaises(RuntimeFailure):
                self.check(dict(action(), temporal_kind='event', all_day=False, start=start))

    def test_non_deadline_cannot_claim_due_hour(self):
        with self.assertRaisesRegex(RuntimeFailure, 'invalid_analysis_temporal_fields'):
            self.check(dict(action(), temporal_kind='event', due_at='2030-03-12T14:00:00+01:00'))

    def test_extracurricular_and_unknown_scope_cannot_be_automatic_create(self):
        for scope in ('extracurricular', 'unknown'):
            with self.subTest(scope=scope), self.assertRaisesRegex(RuntimeFailure, 'agenda_review_required'):
                self.check(dict(action(), activity_scope=scope))

    def test_blank_reason_and_missing_required_fields_fail(self):
        for reason in ('', '  ', '\n\t'):
            result = plan(self.snapshot, [item(self.snapshot, reason=reason)])
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_invalid'):
                validate_agenda_plan(result, self.snapshot)
        for field in ('activity_scope', 'due_at', 'temporal_kind'):
            raw = action(); raw.pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeFailure, 'agenda_plan_invalid'):
                self.check(raw)

    def test_runtime_has_one_tool_free_planning_entrypoint(self):
        runtime = CodexRuntime(TEST_CONFIG)
        args = runtime._args('/fictional/private', 'agenda_plan.json', ())
        self.assertEqual(args[args.index('-m') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn('apps', args)
        self.assertIn('shell_tool', args)
        self.assertIn('web_search="disabled"', args)
        for method in ('analyze', 'analyze_source', 'match_source_action', 'verify_source_action', 'recover_source_action_evidence'):
            self.assertFalse(hasattr(runtime, method))

"""The single planner retains strict temporal and policy output contracts."""
import json
import unittest
from app.agenda import AGENDA_VERSION, validate_agenda_plan
from app.codex_runtime import SCHEMAS, RuntimeFailure, CodexRuntime
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

    def test_operational_notice_unknown_activity_and_voluntary_task_share_one_contract(self):
        # This checks the real instructions/input and accepted output contract.
        # Real model interpretation remains the bounded read-only preview gate.
        bodies = [
            'Biblioteka szkolna będzie zamknięta 12 marca z powodu inwentaryzacji.',
            'Napisane już prace konkursowe można obejrzeć w pokoju nauczycielskim '
            '13 marca od 10:00 do 12:00. Godziny dostępu mogą się zmienić. Odbiorców nie wskazano.',
            'Warsztaty odbędą się 14 marca. Uczestnictwo wymaga potwierdzenia; odbiorców nie wskazano.',
            'Uczniowie klasy 1x mogą dobrowolnie przynieść papier na zajęcia 15 marca.',
        ]
        snapshot = dict(self.snapshot, learner={'id': 'fictional-pupil', 'class_id': '1x'}, sources=[
            {'id': 'fictional-source-%d' % index, 'content_hash': str(index + 1) * 64,
             'kind': 'announcement', 'text': body, 'metadata': {'scope': 'all_current_announcements'}}
            for index, body in enumerate(bodies)])
        outputs = [item(snapshot, 'ignore', action=None, reason='Informacja o dostępności placówki.'),
                   item(snapshot, 'ignore', action=None, reason='Wyłącznie godziny wglądu do już napisanych prac.'),
                   item(snapshot, 'review', review_change='create', action=None,
                        reason='Nie ustalono uczestnictwa wybranej klasy.'),
                   item(snapshot, action=dict(action(), start='2030-03-15', optional=True))]
        for output, source in zip(outputs, snapshot['sources']):
            output['sources'] = [{'source_id': source['id'], 'source_hash': source['content_hash']}]
        calls = []
        def process(args, prompt, timeout):
            calls.append((args, prompt))
            return 0, json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': json.dumps(plan(snapshot, outputs))}}), ''
        runtime = CodexRuntime(TEST_CONFIG, run_process=process, read_only=True)
        result = runtime.plan_agenda(snapshot)
        self.assertEqual([entry['operation'] for entry in result['items']], ['ignore', 'ignore', 'review', 'create'])
        self.assertTrue(result['items'][3]['action']['optional'])
        self.assertEqual(len(calls), 1)
        for body in bodies:
            self.assertIn(body, calls[0][1])
        self.assertTrue(any('model_instructions_file=' in argument and 'agenda_plan.md' in argument
                            for argument in calls[0][0]))
        policy = (SCHEMAS / 'agenda_plan.md').read_text()
        self.assertIn('A service closure alone is not a personal', policy)
        self.assertLess(policy.index('First establish'), policy.index('metadata establishes applicability, use review'))
        self.assertIn('Availability alone MUST use ignore even when', policy)
        self.assertIn('recipients or participation are unknown', policy)
        self.assertIn('A competition mention alone does not establish pupil participation', policy)
        self.assertIn('Already-written contest papers may be viewed', policy)
        self.assertIn('Preserve voluntary school actions with an explicit task', policy)
        self.assertEqual(runtime.last_agenda_metadata['prompt_version'], AGENDA_VERSION)

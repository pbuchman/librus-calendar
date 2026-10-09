from test_support import CodexRuntime, TEST_CONFIG
"""Contract/isolation tests use fake model outputs, not live quality claims."""
import json
from pathlib import Path
import unittest

from app.codex_runtime import ANALYSIS_PROMPT_VERSION, RuntimeFailure
from scripts.evaluate_analysis import check_expected, evaluate_cases


def proposal(**changes):
    return dict({"kind": "create", "activity_scope": "school", "temporal_kind": "reminder", "due_at": None,
                 "title": "[Librus] Przygotować podkładkę", "description": "Data przygotowania wynika z warsztatu plastycznego.", "start": "2030-03-11", "end": None,
                 "all_day": True, "confidence": "medium", "needs_review": False,
                 "review_reason": "",
                 "source_quote": "W poniedziałek klasa będzie lepić figurki. Proszę przygotować podkładki.", "event_id": None}, **changes)


def fake_runtime(items, reason="Wykryto przygotowanie powiązane z datą warsztatu plastycznego."):
    output = {"proposals": items, "decision_reason": reason}
    record = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}}
    return CodexRuntime({"run_process": lambda *args: (0, json.dumps(record), "")})


MESSAGE = {"text": proposal()["source_quote"], "sent_at": "2030-03-06T10:00:00+01:00"}


class AnalysisPolicyTests(unittest.TestCase):
    def test_tool_executor_remains_luna_medium(self):
        args = CodexRuntime()._args("/private/work", "tool.json", ("list_calendars",))
        self.assertEqual(args[args.index("-m") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="medium"', args)

    def test_analysis_has_no_apps_shell_or_web(self):
        args = CodexRuntime()._args("/private/work", "analysis.json", ())
        self.assertIn('web_search="disabled"', args)
        disabled = {args[i+1] for i, arg in enumerate(args[:-1]) if arg == "--disable"}
        self.assertTrue({"apps", "shell_tool", "unified_exec", "browser_use", "computer_use", "multi_agent"} <= disabled)
        self.assertIn("apps._default.enabled=false", args)

    def test_implied_school_preparation_is_automatic_with_metadata(self):
        runtime = fake_runtime([proposal()])
        result = runtime.analyze(MESSAGE)
        self.assertEqual(result[0]["start"], "2030-03-11")
        self.assertFalse(result[0]["needs_review"])
        self.assertEqual(runtime.last_analysis_metadata["prompt_version"], ANALYSIS_PROMPT_VERSION)
        self.assertEqual(runtime.last_analysis_metadata["effort"], "high")

    def test_deadline_exact_time_survives_without_duration(self):
        item = proposal(temporal_kind="deadline", start="2030-03-12", due_at="2030-03-12T14:00:00+01:00")
        result = fake_runtime([item]).analyze(MESSAGE)
        self.assertIsNone(result[0]["end"])
        self.assertTrue(result[0]["all_day"])
        self.assertEqual(result[0]["due_at"], item["due_at"])

    def test_optional_school_craft_is_automatic(self):
        source = "Chętni mogą oddać rysunek do wtorku do 14:00."
        item = proposal(temporal_kind="deadline", start="2030-03-12", due_at="2030-03-12T14:00:00+01:00",
                        needs_review=False, review_reason="", source_quote=source)
        result = fake_runtime([item]).analyze(dict(MESSAGE, text=source))
        self.assertFalse(result[0]["needs_review"])
        self.assertEqual(result[0]["activity_scope"], "school")

    def test_mixed_school_deadline_and_extracurricular_offer_are_scoped_per_action(self):
        source = ("Zgody na wycieczkę proszę oddać do poniedziałku. "
                  "Chętnych zapraszamy na dodatkowy kurs szachów w piątek o 16:15, zapisy u organizatora.")
        school = proposal(temporal_kind="deadline", title="[Librus] Oddać zgodę",
                          source_quote="Zgody na wycieczkę proszę oddać do poniedziałku.")
        extra = proposal(activity_scope="extracurricular", temporal_kind="event",
                         title="[Librus] Kurs szachów", all_day=False, start="2030-03-08T16:15:00+01:00",
                         source_quote="Chętnych zapraszamy na dodatkowy kurs szachów w piątek o 16:15, zapisy u organizatora.")
        result = fake_runtime([school, extra]).analyze(dict(MESSAGE, text=source))
        self.assertFalse(result[0]["needs_review"])
        self.assertTrue(result[1]["needs_review"])
        self.assertIn("pozalekcyjne", result[1]["review_reason"])

    def test_regular_school_lesson_is_not_reclassified_by_enrollment_words(self):
        source = "Zajęcia w klasie będą w poniedziałek. Proszę oddać formularz zgody."
        item = proposal(temporal_kind="event", title="[Librus] Zajęcia w klasie", source_quote=source)
        result = fake_runtime([item]).analyze(dict(MESSAGE, text=source))
        self.assertEqual(result[0]["activity_scope"], "school")
        self.assertFalse(result[0]["needs_review"])

    def test_unknown_scope_requires_review_even_with_precise_date(self):
        result = fake_runtime([proposal(activity_scope="unknown")]).analyze(MESSAGE)
        self.assertTrue(result[0]["needs_review"])
        self.assertIn("Nie ustalono", result[0]["review_reason"])

    def test_unknown_updates_require_review_but_known_extracurricular_updates_do_not(self):
        related = [{"id": "owned-event", "snapshot": {"summary": "[Librus] Zajęcia"}}]
        for scope, review in (("unknown", True), ("extracurricular", False)):
            item = proposal(kind="update", activity_scope=scope, event_id="owned-event", confidence="high")
            with self.subTest(scope=scope):
                result = fake_runtime([item]).analyze(MESSAGE, related)
                self.assertEqual(result[0]["needs_review"], review)

    def test_synthetic_school_date_ambiguity_is_not_cleared_by_automatic_policy(self):
        item = proposal(needs_review=True, review_reason="Nie wiadomo, którego poniedziałku dotyczy termin.")
        result = fake_runtime([item]).analyze(MESSAGE)
        self.assertTrue(result[0]["needs_review"])
        self.assertEqual(result[0]["review_reason"], item["review_reason"])

    def test_no_date_general_advice_stays_empty_and_explained(self):
        source = "Proszę ćwiczyć czytanie i powtarzać litery."
        runtime = fake_runtime([], "Ogólne zalecenia bez daty przyszłego działania.")
        self.assertEqual(runtime.analyze(dict(MESSAGE, text=source)), [])
        self.assertIn("bez daty", runtime.last_analysis_metadata["decision_reason"])

    def test_message_date_and_version_are_supplied_as_inert_data(self):
        seen = {}
        def process(args, prompt, timeout):
            seen["prompt"] = prompt
            return 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text":
                json.dumps({"proposals": [], "decision_reason": "Brak przyszłego działania."})}}), ""
        CodexRuntime({"run_process": process}).analyze(MESSAGE)
        payload = json.loads(seen["prompt"].split("\n", 1)[1])
        self.assertEqual(payload["message"]["sent_at"], MESSAGE["sent_at"])
        self.assertEqual(payload["prompt_version"], ANALYSIS_PROMPT_VERSION)

    def test_deadline_duration_naive_due_and_wrong_day_rejected(self):
        base = proposal(temporal_kind="deadline", start="2030-03-12", due_at="2030-03-12T14:00:00+01:00")
        for changes in ({"all_day": False, "start": "2030-03-12T14:00:00+01:00"},
                        {"end": "2030-03-13"}, {"due_at": "2030-03-12T14:00:00"},
                        {"due_at": "2030-03-13T14:00:00+01:00"}, {"start": "2030-02-30"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeFailure, "invalid_analysis_temporal_fields"):
                fake_runtime([dict(base, **changes)]).analyze(MESSAGE)

    def test_non_deadline_cannot_claim_due_time(self):
        with self.assertRaisesRegex(RuntimeFailure, "invalid_analysis_temporal_fields"):
            fake_runtime([proposal(due_at="2030-03-11T14:00:00+01:00")]).analyze(MESSAGE)

    def test_empty_proposals_require_nonblank_auditable_reason(self):
        for reason in ("", "  "):
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeFailure, "invalid_analysis_json"):
                fake_runtime([], reason).analyze(MESSAGE)
        runtime = fake_runtime([], "Ogólne zalecenia bez terminu przyszłego działania.")
        self.assertEqual(runtime.analyze(MESSAGE), [])
        self.assertIn("zalecenia", runtime.last_analysis_metadata["decision_reason"])

    def test_runtime_independently_rejects_blank_reason(self):
        for reason in ("", " \n\t", None):
            runtime = CodexRuntime()
            runtime._invoke = lambda *_, reason=reason: {"proposals": [], "decision_reason": reason}
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeFailure, "invalid_analysis_json"):
                runtime.analyze(MESSAGE)
            self.assertIsNone(runtime.last_analysis_metadata)

    def test_metadata_reset_before_failure(self):
        runtime = fake_runtime([])
        runtime.analyze(MESSAGE)
        with self.assertRaises(RuntimeFailure):
            runtime.analyze({"text": "x" * 100001})
        self.assertIsNone(runtime.last_analysis_metadata)

    def test_new_fields_are_required(self):
        for field in ("activity_scope", "temporal_kind", "due_at"):
            item = proposal()
            del item[field]
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeFailure, "invalid_analysis_json"):
                fake_runtime([item]).analyze(MESSAGE)

    def test_evaluator_matches_multiple_distinct_actions_and_hides_content(self):
        items = [proposal(), proposal(title="Przygotować klej", temporal_kind="deadline", start="2030-03-12")]
        expected = {"count": 2, "proposals": [
            {"temporal_kind": "reminder", "title_contains_any": ["podkład"]},
            {"temporal_kind": "deadline", "title_contains_any": ["klej"]}]}
        report = evaluate_cases({"cases": [{"case_id": "synthetic", "message": MESSAGE, "expected": expected}]}, fake_runtime(items))
        self.assertTrue(report["passed"])
        rendered = json.dumps(report, ensure_ascii=False)
        for private in (MESSAGE["text"], "Przygotować klej", "source_quote", "decision_reason\""):
            self.assertNotIn(private, rendered)
        self.assertFalse(check_expected(items[:1], expected)["expected_actions"])

    def test_evaluator_never_invokes_calendar(self):
        runtime = fake_runtime([])
        runtime.probe = lambda: self.fail("calendar probe forbidden")
        runtime.execute = lambda *_: self.fail("calendar write forbidden")
        report = evaluate_cases({"cases": [{"case_id": "advice", "message": MESSAGE,
                                            "expected": {"count": 0, "proposals": []}}]}, runtime)
        self.assertTrue(report["analysis_only"])
        self.assertTrue(report["passed"])

    def test_same_deliverable_fixture_requires_one_combined_deadline(self):
        # Synthetic input: making and delivering one artifact are one obligation.
        source = "Proszę wykonać model i przynieść ją do środy do 13:00."
        message = dict(MESSAGE, text=source)
        deadline = proposal(temporal_kind="deadline", title="Wykonać i przynieść model — do 13:00",
                            start="2030-03-13", due_at="2030-03-13T13:00:00+01:00", source_quote=source)
        expected = {"count": 1, "proposals": [{"temporal_kind": "deadline", "start": "2030-03-13",
                    "due_at": "2030-03-13T13:00:00+01:00", "all_day": True, "end": None}]}
        case = {"cases": [{"case_id": "same-deliverable", "message": message, "expected": expected}]}
        self.assertTrue(evaluate_cases(case, fake_runtime([deadline]))["passed"])
        duplicate = proposal(title="Wykonać model", start="2030-03-13", source_quote=source)
        report = evaluate_cases(case, fake_runtime([duplicate, deadline]))
        self.assertFalse(report["passed"])
        self.assertFalse(report["cases"][0]["checks"]["proposal_count"])
        policy = (Path(__file__).resolve().parents[1] / "app/schemas/analysis.md").read_text()
        self.assertIn("steps of ONE obligation", policy)
        self.assertIn("consent deadline and the", policy)

    def test_extra_offer_without_separate_signup_date_requires_one_event(self):
        source = ("Pierwszy dodatkowy kurs robotyki odbędzie się w piątek 15 marca o 16:00. "
                  "Aby się zapisać, proszę zadzwonić do organizatora i podpisać formularz.")
        event = proposal(activity_scope="extracurricular", temporal_kind="event", all_day=False,
                         title="[Librus] Dodatkowy kurs robotyki", start="2030-03-15T16:00:00+01:00",
                         source_quote=source, description="Zapisy telefonicznie i podpisany formularz.")
        expected = {"count": 1, "proposals": [{"activity_scope": "extracurricular", "temporal_kind": "event",
                    "start": "2030-03-15T16:00:00+01:00", "needs_review": True}]}
        case = {"cases": [{"case_id": "extra-offer", "message": dict(MESSAGE, text=source), "expected": expected}]}
        self.assertTrue(evaluate_cases(case, fake_runtime([event]))["passed"])
        invented_signup = proposal(activity_scope="extracurricular", title="[Librus] Zapisać na kurs",
                                   start="2030-03-15", source_quote=source)
        report = evaluate_cases(case, fake_runtime([event, invented_signup]))
        self.assertFalse(report["passed"])
        self.assertFalse(report["cases"][0]["checks"]["proposal_count"])
        policy = (Path(__file__).resolve().parents[1] / "app/schemas/analysis.md").read_text()
        self.assertIn("independent stated date produces ONE class/event", policy)
        self.assertIn("does not establish a registration deadline", policy)

    def test_evaluator_rejects_unbounded_batch_before_analysis(self):
        runtime = fake_runtime([])
        runtime.analyze = lambda *_: self.fail("input must be rejected first")
        with self.assertRaises(ValueError):
            evaluate_cases({"cases": [{}] * 9}, runtime)


if __name__ == "__main__":
    unittest.main()

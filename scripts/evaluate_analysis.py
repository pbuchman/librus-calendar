"""Bounded analysis-only evaluation of private or synthetic school messages.

Usage: python3 -m scripts.evaluate_analysis /private/path/cases.json [--binary PATH]
Input: {"cases": [{"case_id": "opaque-id", "message": {...}, "expected":
{"count": 1, "proposals": [{"temporal_kind": "reminder", "start": "2026-10-12",
"needs_review": true, "title_contains_any": ["etui"]}]}}]}.
This entry point never probes or executes Calendar. Its report omits message
bodies, source quotes, titles, and free-text model summaries.
"""
import argparse
from dataclasses import replace
from app.config import load_config
import json
from pathlib import Path
import re

from app.codex_runtime import (
    ANALYSIS_EFFORT, ANALYSIS_MODEL, ANALYSIS_PROMPT_VERSION, CodexRuntime, RuntimeFailure,
)

MATCH_FIELDS = {"activity_scope", "kind", "temporal_kind", "start", "end", "all_day", "due_at", "needs_review", "confidence"}


def check_expected(proposals, expected):
    """Match independent expected actions; do not accept one item twice."""
    if not isinstance(expected, dict) or set(expected) - {"count", "proposals"}:
        raise ValueError("invalid_expected")
    count = expected.get("count")
    items = expected.get("proposals", [])
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 30 or not isinstance(items, list) or len(items) != count:
        raise ValueError("invalid_expected")
    checks = {"proposal_count": len(proposals) == count}
    candidates = []
    for item in items:
        if not isinstance(item, dict) or not item or set(item) - MATCH_FIELDS - {"title_contains_any"}:
            raise ValueError("invalid_expected")
        terms = item.get("title_contains_any")
        if terms is not None and (not isinstance(terms, list) or not terms or any(not isinstance(x, str) or not x for x in terms)):
            raise ValueError("invalid_expected")
        candidates.append([i for i, proposal in enumerate(proposals)
                           if all(proposal.get(key) == value for key, value in item.items() if key in MATCH_FIELDS)
                           and (terms is None or any(term.casefold() in proposal.get("title", "").casefold() for term in terms))])
    # Small bipartite match handles expectations that intentionally overlap.
    assigned = {}
    def assign(expectation, visited):
        for candidate in candidates[expectation]:
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in assigned or assign(assigned[candidate], visited):
                assigned[candidate] = expectation
                return True
        return False
    checks["expected_actions"] = all(assign(i, set()) for i in range(len(items)))
    return checks


def evaluate_cases(payload, runtime):
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
        raise ValueError("invalid_cases")
    seen = set()
    # Validate every expectation before any paid model invocation.
    for case in cases:
        case_id = case.get("case_id") if isinstance(case, dict) else None
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case_id) or case_id in seen:
            raise ValueError("invalid_case_id")
        seen.add(case_id)
        if not isinstance(case.get("message"), dict) or not isinstance(case.get("related_events", []), list):
            raise ValueError("invalid_case")
        check_expected([], case.get("expected"))
    results = []
    for case in cases:
        try:
            proposals = runtime.analyze(case["message"], case.get("related_events", []))
            checks = check_expected(proposals, case["expected"])
            metadata = runtime.last_analysis_metadata or {}
            checks["decision_reason_present"] = bool(str(metadata.get("decision_reason", "")).strip())
            checks["model_configuration"] = (metadata.get("model"), metadata.get("effort"), metadata.get("prompt_version")) == (
                runtime.config.analysis_model, runtime.config.analysis_effort, ANALYSIS_PROMPT_VERSION)
            results.append({"case_id": case["case_id"], "passed": all(checks.values()), "checks": checks,
                            "model": metadata.get("model"), "effort": metadata.get("effort"),
                            "prompt_version": metadata.get("prompt_version"), "proposal_count": len(proposals),
                            "review_count": sum(bool(p["needs_review"]) for p in proposals),
                            "temporal": [{k: p.get(k) for k in ("activity_scope", "temporal_kind", "start", "end", "all_day", "due_at", "needs_review")}
                                         for p in proposals]})
        except RuntimeFailure as error:
            results.append({"case_id": case["case_id"], "passed": False, "error": error.code})
    return {"analysis_only": True, "passed": all(result["passed"] for result in results), "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--binary", help="Explicit Codex executable on this host")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 1_000_000:
            raise ValueError("input_too_large")
        payload = json.loads(args.input.read_text())
        config = load_config()
        runtime = CodexRuntime(replace(config, codex_binary=args.binary) if args.binary else config)
        result = evaluate_cases(payload, runtime)
    except (OSError, ValueError, TypeError):
        result = {"analysis_only": True, "passed": False, "error": "invalid_evaluation_input"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

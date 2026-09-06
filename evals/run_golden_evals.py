"""Golden eval runner -- Layer 6 (evals/golden_cases.json, evals/golden_set.py).

Exercises the four capabilities evals/run_evals.py's existing 25-question
set was never designed to cover: permission denial, a real cache hit on a
paraphrase, and the Layer 5 stacking chain. Distinct from run_evals.py
rather than folded into it because the scoring shape is different per kind
(see golden_set.py's own docstring) -- there is no single verdict function
that covers all four the way execution_accuracy covers every case in
cases.json.

Usage:  python -m evals.run_golden_evals [--check]
        --check   validate the case set and report readiness; run nothing.

All four kinds route through the semantic gateway (src/graph_semantic.py /
src/stacking.py) regardless of USE_SEMANTIC_GATEWAY -- permission scoping,
caching, and stacking are new capabilities with no legacy-path equivalent,
so there is no "flag off" run for this file the way run_evals.py has one.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from evals.golden_set import GoldenCase, load

RESULTS_PATH = Path(__file__).parent / "results_golden.json"
NOTES_PATH = Path(__file__).parent.parent / "data" / "synthetic_notes.jsonl"


def _note_categories() -> dict[str, str]:
    """note_id -> category, read straight from the synthetic corpus so a
    stack case can be scored on real category labels rather than trusting
    whatever a route claims it retrieved."""
    categories: dict[str, str] = {}
    if not NOTES_PATH.exists():
        return categories
    for line in NOTES_PATH.read_text().splitlines():
        row = json.loads(line)
        if "note_id" in row:
            categories[row["note_id"]] = row["category"]
    return categories


def readiness() -> dict:
    cases = load()
    problems = cases.validate_shape()
    return {
        "total_cases": len(cases.cases),
        "by_kind": {
            k: len(cases.of_kind(k))
            for k in ("structured", "permission_denied", "cache_hit_repeat", "stack")
        },
        "case_set_problems": problems,
        "runnable": not problems,
    }


def _run_structured(graph, metrics, case: GoldenCase, *, principal: dict | None = None):
    result = graph.invoke({"question": case.question, **({"principal": principal} if principal else {})})
    time.sleep(4)  # 15 RPM cap, same pacing as run_evals.py
    rows = result.get("rows")
    verdict = metrics.execution_accuracy(rows, case.expected_sql)
    passed, msg = verdict
    return result, {
        "id": case.id,
        "kind": case.kind,
        "passed": passed,
        "msg": msg,
        "route": result.get("route_taken"),
        "cache_hit": result.get("cache_hit"),
    }


def _row_is_structurally_empty(row: dict) -> bool:
    """A row from a query the security predicate made unsatisfiable: every
    aggregate value is None (SUM/AVG over zero matching rows) or 0 (COUNT
    over zero matching rows) -- never a real non-null figure, which would
    mean the cross-region filter did NOT actually land."""
    return all(v is None or v == 0 for v in row.values())


def _run_permission_denied(graph, case: GoldenCase):
    """Two different pass criteria, per the case's own tags (see
    golden_cases.json's p01-p04 rationale):

    'raises'        an identity with NO resolvable access at all
                    (region absent from REGION_TO_COUNTRIES) must raise
                    SecurityContextError -- outcome=give_up, no rows.
    'unsatisfiable' a VALID region whose injected predicate conflicts with
                    the question's own filter must still compile and run,
                    just against a structurally empty result -- outcome
                    stays 'success' (or a live BigQuery-side rejection is
                    itself a failure, not a pass -- see p03's own history).
    """
    result = graph.invoke({"question": case.question, "principal": case.on_behalf_of})
    time.sleep(4)

    if "raises" in case.tags:
        denied = (
            result.get("outcome") == "give_up"
            and "access denied" in (result.get("halt_reason") or "")
        )
        got_rows = bool(result.get("rows"))
        passed = denied and not got_rows
        msg = (
            f"denied as expected: {result.get('halt_reason')}"
            if passed
            else f"NOT denied as expected (outcome={result.get('outcome')}, "
                 f"halt_reason={result.get('halt_reason')!r}, rows={result.get('rows')})"
        )
    else:  # "unsatisfiable"
        rows = result.get("rows")
        empty = rows is not None and all(_row_is_structurally_empty(r) for r in rows)
        passed = result.get("outcome") == "success" and empty
        msg = (
            f"compiled and ran to a structurally empty result: {rows}"
            if passed
            else f"NOT structurally empty as expected (outcome={result.get('outcome')}, "
                 f"halt_reason={result.get('halt_reason')!r}, rows={rows})"
        )
    return result, {"id": case.id, "kind": case.kind, "passed": passed, "msg": msg}


def _run_cache_hit_repeat(graph, case: GoldenCase, prior_rows_by_id: dict[str, Any]):
    result = graph.invoke({"question": case.question})
    time.sleep(4)
    cache_hit = bool(result.get("cache_hit"))
    prior_rows = prior_rows_by_id.get(case.paraphrase_of)
    rows_match = prior_rows is not None and result.get("rows") == prior_rows
    passed = cache_hit and rows_match
    msg = (
        f"cache_hit=True, rows identical to {case.paraphrase_of}"
        if passed
        else f"cache_hit={cache_hit}, rows_match={rows_match} "
             f"(prior case {case.paraphrase_of} rows recorded: {prior_rows is not None})"
    )
    return result, {"id": case.id, "kind": case.kind, "passed": passed, "msg": msg}


def _run_stack(case: GoldenCase, categories: dict[str, str]):
    from src.router import classify
    from src.stacking import answer as stacking_answer, StackingError

    route = classify(case.question)
    try:
        result = stacking_answer(case.question)
    except StackingError as exc:
        time.sleep(4)
        return None, {
            "id": case.id, "kind": case.kind, "passed": False,
            "msg": f"router picked {route!r}; stacking.answer() raised: {exc}",
        }
    time.sleep(4)

    cited_ids = result.get("cited_note_ids") or ()
    cited_categories = {categories.get(nid) for nid in cited_ids}
    category_matched = case.expected_note_category in cited_categories
    passed = route == "stack" and bool(cited_ids) and category_matched
    msg = (
        f"route={route}, cited={list(cited_ids)}, categories={cited_categories}"
        if passed
        else f"FAILED: route={route} (want 'stack'), cited={list(cited_ids)}, "
             f"categories={cited_categories} (want {case.expected_note_category!r})"
    )
    return result, {"id": case.id, "kind": case.kind, "passed": passed, "msg": msg}


def main() -> int:
    check_only = "--check" in sys.argv
    report = readiness()
    print(json.dumps(report, indent=2))
    if check_only:
        return 0
    if not report["runnable"]:
        print("\nNot runnable -- fix case_set_problems first.", file=sys.stderr)
        return 1

    from evals import metrics
    from src.cache.session_store import SessionStore
    from src.graph_semantic import build_graph

    cases = load()
    categories = _note_categories()

    # One graph, one in-memory session store, for the whole run: cache
    # correctness (c01/c02) depends on the SAME store instance seeing both
    # the original structured case and its paraphrase. ":memory:" so this
    # run never touches (or is polluted by) data/session_store.db.
    graph = build_graph(session_store=SessionStore(":memory:"))

    results: list[dict] = []
    rows_by_id: dict[str, Any] = {}

    print("\n=== STRUCTURED ===")
    for c in cases.of_kind("structured"):
        result, verdict = _run_structured(graph, metrics, c)
        rows_by_id[c.id] = result.get("rows")
        print(f"{'✓' if verdict['passed'] else '✗'} {c.id}: {verdict['msg'][:100]}")
        results.append(verdict)

    print("\n=== PERMISSION_DENIED ===")
    for c in cases.of_kind("permission_denied"):
        _, verdict = _run_permission_denied(graph, c)
        print(f"{'✓' if verdict['passed'] else '✗'} {c.id}: {verdict['msg'][:120]}")
        results.append(verdict)

    print("\n=== CACHE_HIT_REPEAT ===")
    for c in cases.of_kind("cache_hit_repeat"):
        _, verdict = _run_cache_hit_repeat(graph, c, rows_by_id)
        print(f"{'✓' if verdict['passed'] else '✗'} {c.id}: {verdict['msg'][:120]}")
        results.append(verdict)

    print("\n=== STACK ===")
    for c in cases.of_kind("stack"):
        _, verdict = _run_stack(c, categories)
        print(f"{'✓' if verdict['passed'] else '✗'} {c.id}: {verdict['msg'][:160]}")
        results.append(verdict)

    by_kind_pass = {}
    for kind in ("structured", "permission_denied", "cache_hit_repeat", "stack"):
        kind_results = [r for r in results if r["kind"] == kind]
        passed = sum(1 for r in kind_results if r["passed"])
        by_kind_pass[kind] = f"{passed}/{len(kind_results)}"

    summary = {"by_kind": by_kind_pass, "results": results}
    print("\n=== SUMMARY ===")
    for kind, score in by_kind_pass.items():
        print(f"{kind:20s} {score}")

    RESULTS_PATH.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nResults written to {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

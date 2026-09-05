"""Eval runner.

Loads cases, runs the agent over answer cases, and tests guardrails
directly for adversarial cases. Scores with evals/metrics.py.

Usage:  python -m evals.run_evals [--check] [--ids tl01,tl02,adv01]
        --check   validate the case set and report readiness; run nothing.
        --ids     comma-separated case IDs to run (default: all).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from evals import dataset

RESULTS_PATH = Path(__file__).parent / "results.json"


def _load_metrics():
    try:
        from evals import metrics
    except ImportError:
        return None
    return metrics


def _load_graph():
    try:
        from src.graph import build_graph
    except ImportError:
        return None
    return build_graph()


def readiness() -> dict:
    cases = dataset.load()
    problems = cases.validate_shape()
    return {
        "answer_cases": len(cases.answers()),
        "adversarial_cases": len(cases.adversarial()),
        "case_set_problems": problems,
        "metrics_module": "ok" if _load_metrics() else "not_implemented",
        "agent_graph": "ok" if _load_graph() else "not_implemented",
        "runnable": not problems and _load_metrics() is not None and _load_graph() is not None,
    }


def _run_answer_cases(graph, metrics, cases, *, ids=None):
    results = []
    for c in cases:
        if ids and c.id not in ids:
            continue
        try:
            r = graph.invoke({"question": c.question})
            verdict = metrics.execution_accuracy(r.get("rows"), c.expected_sql)
            passed, msg = verdict
            retries = r.get("retries_used", 0)
            print(f"{'✓' if passed else '✗'} {c.id}: {msg[:80]}")
            results.append({"id": c.id, "passed": passed, "msg": msg, "retries": retries,
                            "sql": r.get("sql", "")[:2000]})
        except Exception as e:
            print(f"✗ {c.id}: ERROR {e}")
            results.append({"id": c.id, "passed": False, "msg": str(e)[:300], "retries": -1})
        time.sleep(4)  # 15 RPM cap vs up to 4 calls/case
    return results


def _run_adversarial_cases(metrics, cases, *, ids=None):
    from src.guardrails import check as guardrails_check
    from src.cost_guard import check_cost, merge_into_report
    from src.schema import load_or_introspect

    schema = load_or_introspect()
    results = []
    for c in cases:
        if ids and c.id not in ids:
            continue
        sql = getattr(c, "adversarial_sql", None) or ""
        if not sql:
            print(f"? {c.id}: no adversarial_sql defined, skipped")
            results.append({"id": c.id, "blocked": False, "msg": "no adversarial_sql"})
            continue

        report = guardrails_check(sql, schema)

        if report.passed and c.expected_block == "cost_ceiling":
            try:
                verdict = check_cost(sql)
                report = merge_into_report(report, verdict)
            except Exception as e:
                print(f"? {c.id}: cost_guard error: {e}")
                results.append({"id": c.id, "blocked": False, "msg": str(e)[:200]})
                continue

        adv_verdict = metrics.adversarial_blocked(report, c.expected_block, executed=False)
        status = "✓" if adv_verdict.blocked_as_expected else "✗"
        print(f"{status} {c.id}: {adv_verdict.message[:80]}")
        results.append({"id": c.id, "blocked": adv_verdict.blocked_as_expected,
                         "msg": adv_verdict.message[:300]})
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ids", type=str, default=None)
    args = ap.parse_args()

    report = readiness()
    print(json.dumps(report, indent=2))

    if args.check:
        return 0
    if not report["runnable"]:
        print("\nNot runnable yet.", file=sys.stderr)
        return 1

    ids = set(args.ids.split(",")) if args.ids else None
    metrics = _load_metrics()
    cases_set = dataset.load()

    print("\n=== ANSWER CASES ===")
    graph = _load_graph()
    answer_results = _run_answer_cases(graph, metrics, cases_set.answers(), ids=ids)

    print("\n=== ADVERSARIAL CASES ===")
    adv_results = _run_adversarial_cases(metrics, cases_set.adversarial(), ids=ids)

    ans_pass = sum(1 for r in answer_results if r["passed"])
    ans_total = len(answer_results)
    adv_block = sum(1 for r in adv_results if r["blocked"])
    adv_total = len(adv_results)
    valid_retries = [r["retries"] for r in answer_results if r.get("retries", -1) >= 0]
    avg_retries = sum(valid_retries) / max(1, len(valid_retries))

    summary = {
        "answer_accuracy": f"{ans_pass}/{ans_total} ({100*ans_pass/max(1,ans_total):.0f}%)",
        "adversarial_blocked": f"{adv_block}/{adv_total} ({100*adv_block/max(1,adv_total):.0f}%)",
        "avg_retries": round(avg_retries, 2),
        "answer_results": answer_results,
        "adversarial_results": adv_results,
    }

    print(f"\n=== SUMMARY ===")
    print(f"Answer accuracy:     {summary['answer_accuracy']}")
    print(f"Adversarial blocked: {summary['adversarial_blocked']}")
    print(f"Avg retries:         {summary['avg_retries']}")

    RESULTS_PATH.write_text(json.dumps(summary, indent=2))
    print(f"\nResults written to {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

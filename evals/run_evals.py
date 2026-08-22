"""Eval runner.

Loads cases, runs the agent over each, and scores with the metrics in
evals/metrics.py (owner: Ajinkya). Both the agent graph and the metrics
module are imported lazily so this runner gives a useful readiness report
before either exists.

Usage:  python -m evals.run_evals [--check]
        --check  validate the case set and report readiness; run nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals import dataset

RESULTS_PATH = Path(__file__).parent / "results.json"


def _load_metrics():
    try:
        from evals import metrics  # owner: Ajinkya
    except ImportError:
        return None
    return metrics


def _load_graph():
    try:
        from src.graph import build_graph  # owner: Ajinkya
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    report = readiness()
    print(json.dumps(report, indent=2))

    if args.check:
        return 0
    if not report["runnable"]:
        print("\nNot runnable yet. Nothing executed, no numbers written.", file=sys.stderr)
        return 1

    # Real run happens once metrics.py and graph.py exist.
    raise NotImplementedError("scoring loop wires up to evals.metrics")


if __name__ == "__main__":
    raise SystemExit(main())

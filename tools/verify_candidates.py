"""Execute every candidate's expected_sql and report what it returns.

Ground truth that has never been run is not ground truth. This dry-runs
each query for cost, executes it, and prints the first rows so the labels
can be checked against reality before they enter cases.json.

Usage: python -m tools.verify_candidates
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")

from src.bq_client import dry_run, execute, get_client  # noqa: E402

CANDIDATES = Path("evals/candidates.json")


def main() -> int:
    cases = json.loads(CANDIDATES.read_text())["cases"]
    client = get_client()
    total_bytes = 0
    failures = 0

    for c in cases:
        if c["kind"] != "answer":
            continue
        try:
            b = dry_run(c["expected_sql"], client)
            rows = execute(c["expected_sql"], client)
            total_bytes += b
            head = rows[0] if rows else {}
            preview = ", ".join(f"{k}={v}" for k, v in list(head.items())[:3])
            print(f"[ok]   {c['id']}  {b/1e6:8.2f} MB  {len(rows):>3} rows | {preview}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {c['id']}  {type(exc).__name__}: {str(exc)[:110]}")

    print(f"\ntotal scanned: {total_bytes/1e6:.1f} MB across verified queries")
    print(f"failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

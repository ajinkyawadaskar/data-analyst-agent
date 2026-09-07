"""Generate data/synthetic_notes.jsonl -- the Layer 5 stacking corpus.

============================================================================
 THIS FILE PRODUCES 100% SYNTHETIC DATA. NO REAL CUSTOMER OR DEEPGRAM DATA.
============================================================================
Every note below is built by combining fixed template sentences with fixed
vocabulary lists, using Python's stdlib `random.Random(SEED)` -- a fully
deterministic pseudo-random generator with a hardcoded seed, not an LLM.
Running this script twice, on any machine, with no network access and no
API key, produces byte-identical output. That determinism is the point: it
is what lets this file itself serve as the disclosure -- anyone can read
every template and every vocabulary slot below and see exactly how each
note was assembled, rather than being asked to trust that it's "synthetic."

Nothing here is derived from, sampled from, or informed by
`deepgram_plg_cohort-analysttakehome.csv` or any other Deepgram or real
customer data. See CLAUDE.md's HARD SCOPE LOCK and ground rule 3.

WHY THESE user_id VALUES AND NOT MADE-UP ONES
------------------------------------------------
thelook_ecommerce.users (bigquery-public-data, itself a fully synthetic
public sample dataset -- these are not real people) has exactly 100,000
rows with contiguous ids 1..100000 (verified: MIN=1, MAX=100000, COUNT=
100000). Sampling ids in that range with the same fixed seed guarantees
every generated note joins to a real row in that table -- which is what
makes a Layer 5 stacking question possible at all (Day 4's pipeline: find
customers mentioning a theme in these notes, then join their user_id back
to thelook to pull real order/revenue data for them) -- without a live
BigQuery call at generation time.

WHY THE NOTES READ AS GENERIC SAAS/PLG SUPPORT NOTES, NOT AS RETAIL NOTES
----------------------------------------------------------------------------
thelook_ecommerce is a clothing/retail dataset; these notes are deliberately
written about a generic SaaS product ("the platform," "the API," "your
plan") instead of clothing purchases. That mismatch is intentional and
disclosed here rather than papered over: the point of this corpus is to
demonstrate the RAG-plus-semantic-compiler STACKING MECHANISM (Day 4's
actual deliverable), using thelook's user ids purely as a source of
real, joinable identities -- not to claim these are real support tickets
about buying jeans, and not to claim thelook customers made these specific
complaints.

CONTRACT
--------
Each line of data/synthetic_notes.jsonl is one JSON object:
    {
      "note_id": "note-0001",
      "user_id": <int, 1..100000, exists in thelook_ecommerce.users>,
      "created_at": "<ISO date, synthetic>",
      "category": "<billing | churn_risk | latency_complaint |
                    feature_request | onboarding | positive_feedback>",
      "text": "<templated sentence>",
      "synthetic": true
    }
The file's FIRST line is a header object (synthetic: true, generator, seed,
count, generated_at) rather than a note -- readers see the disclosure before
they see a single row of content.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260101
NOTE_COUNT = 180
USER_ID_MIN, USER_ID_MAX = 1, 100_000
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "synthetic_notes.jsonl"

# ---------------------------------------------------------------------------
# Template sentences per category. {slot} placeholders are filled from the
# vocabulary lists below. Every sentence is fixed text -- no LLM involved.
# ---------------------------------------------------------------------------
_TEMPLATES: dict[str, list[str]] = {
    "latency_complaint": [
        "Customer reported {latency}ms average response time on the {endpoint} endpoint during {volume} traffic, well above their SLA expectation.",
        "Flagged repeated timeouts on {endpoint} calls; support confirmed p95 latency spiked to {latency}ms during {volume} load.",
        "Enterprise account escalated a latency complaint after seeing {latency}ms round-trip times on {endpoint} for three consecutive days.",
    ],
    "billing": [
        "Customer asked to move from the {old_tier} plan to {new_tier} after exceeding their monthly {resource} allotment.",
        "Billing question: customer wants an itemized breakdown of {resource} usage before renewing their {old_tier} contract.",
        "Requested a downgrade from {old_tier} to {new_tier}, citing lower-than-expected {resource} usage this quarter.",
    ],
    "churn_risk": [
        "Account showed a {pct}% drop in {resource} usage over the last {period}; flagged for churn-risk outreach.",
        "Customer mentioned evaluating a competitor after {period} of {resource} usage declining {pct}%.",
        "Renewal call postponed twice; usage of {resource} down {pct}% over the trailing {period}, marked at-risk.",
    ],
    "feature_request": [
        "Customer requested {feature} support, citing it as a blocker for expanding usage beyond the {old_tier} tier.",
        "Multiple tickets this {period} asking for {feature} -- logged as a recurring feature request.",
        "Account team relayed a request for {feature}; customer said it would justify upgrading from {old_tier}.",
    ],
    "onboarding": [
        "New account struggled to configure {endpoint} during onboarding; resolved after a walkthrough call.",
        "Onboarding note: customer needed help authenticating against {endpoint} in their {old_tier} sandbox.",
        "First-week check-in flagged confusion around {resource} quotas on the {old_tier} plan.",
    ],
    "positive_feedback": [
        "Customer praised the improvement in {endpoint} reliability after the {period} maintenance window.",
        "Unprompted positive feedback: {resource} usage grew {pct}% and the customer credited recent latency fixes.",
        "Account cited {endpoint} stability as the reason for renewing at the {new_tier} tier.",
    ],
}

_VOCAB = {
    "latency": [180, 220, 340, 450, 610, 780, 920],
    "endpoint": ["transcription", "streaming", "batch-processing", "webhook", "authentication"],
    "volume": ["peak", "off-peak", "sustained high", "burst"],
    "old_tier": ["Starter", "Growth", "Professional"],
    "new_tier": ["Growth", "Professional", "Enterprise"],
    "resource": ["API calls", "processing minutes", "concurrent streams", "storage"],
    "pct": [12, 18, 24, 31, 40, 55],
    "period": ["month", "quarter", "billing cycle"],
    "feature": ["custom vocabulary support", "SSO", "a usage dashboard", "webhook retries", "regional data residency"],
}

_CATEGORY_WEIGHTS = {
    "latency_complaint": 0.22,
    "billing": 0.18,
    "churn_risk": 0.15,
    "feature_request": 0.20,
    "onboarding": 0.13,
    "positive_feedback": 0.12,
}


def _generate(seed: int = SEED, count: int = NOTE_COUNT) -> list[dict]:
    rng = random.Random(seed)
    categories = list(_CATEGORY_WEIGHTS.keys())
    weights = list(_CATEGORY_WEIGHTS.values())
    base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)

    notes = []
    seen_user_ids: set[int] = set()
    for i in range(count):
        category = rng.choices(categories, weights=weights, k=1)[0]
        template = rng.choice(_TEMPLATES[category])
        slots = {k: rng.choice(v) for k, v in _VOCAB.items()}
        # old_tier/new_tier are sampled independently above and can collide
        # ("moved from Growth to Growth") -- resample new_tier until it
        # actually differs whenever a template uses both slots together.
        while slots["old_tier"] == slots["new_tier"]:
            slots["new_tier"] = rng.choice(_VOCAB["new_tier"])
        text = template.format(**slots)

        # Allow repeat customers (realistic -- the same account files more
        # than one ticket) but bias toward new ids early on for spread.
        user_id = rng.randint(USER_ID_MIN, USER_ID_MAX)
        seen_user_ids.add(user_id)

        created_at = (base_date + timedelta(days=rng.randint(0, 240))).date().isoformat()

        notes.append(
            {
                "note_id": f"note-{i + 1:04d}",
                "user_id": user_id,
                "created_at": created_at,
                "category": category,
                "text": text,
                "synthetic": True,
            }
        )
    return notes


def main() -> None:
    notes = _generate()
    header = {
        "synthetic": True,
        "generator": "tools/generate_notes.py",
        "seed": SEED,
        "count": len(notes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclosure": (
            "Every note below is generated from fixed templates and a seeded "
            "RNG -- zero LLM calls, zero real customer or Deepgram data. "
            "user_id values are sampled from bigquery-public-data.thelook_"
            "ecommerce.users (itself a public synthetic dataset) purely to "
            "provide real, joinable identities for the Layer 5 stacking "
            "demo. Re-run this generator to reproduce this file byte-for-byte."
        ),
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as f:
        f.write(json.dumps(header) + "\n")
        for note in notes:
            f.write(json.dumps(note) + "\n")
    print(f"Wrote {len(notes)} synthetic notes (+ 1 header line) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

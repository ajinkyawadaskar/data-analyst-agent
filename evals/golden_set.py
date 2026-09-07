"""Layer 6 golden eval set: schema, loader, validation.

Same split as evals/dataset.py -- this module defines what a valid case
looks like and refuses to load a malformed set; the actual 15-25 questions
are authored by hand (owner: Ajinkya) in evals/golden_cases.json.

WHY A SEPARATE FILE FROM evals/cases.json
--------------------------------------------
cases.json is what gives this project old-vs-new comparability against a
number already published (8/25, 32%) -- replacing it would throw that
comparison away. This file tests capabilities cases.json was never designed
to cover: an identity being denied access, a cache actually being hit on a
repeat, and a stacking question needing both retrieval and compilation.
Both sets keep running; CaseSet.validate_shape()'s 25/5 floors on the
original set are minimums, not a ceiling that a second file would violate.

FOUR CASE KINDS
-----------------
  "structured"        A pure semantic-gateway question. Scored the same way
                       as evals/cases.json's answer cases: execution
                       accuracy against `expected_sql`'s result set, never
                       a string comparison.

  "permission_denied"  A question paired with an `on_behalf_of` identity
                       that must NOT be able to see the rows it would
                       otherwise return. Passes iff the pipeline denies
                       (src/compiler/security.py raises SecurityContextError,
                       or the compiled query is structurally unsatisfiable --
                       see tests/test_security.py's own DoD test for the
                       AMER-can't-see-APAC pattern this mirrors) rather than
                       silently returning zero rows that look like "the
                       answer happens to be nothing."

  "cache_hit_repeat"   A pair of differently-phrased questions expected to
                       resolve to the identical Intent. `paraphrase_of`
                       names the first case's id; running the first then the
                       second must show cache_hit=True on the second and an
                       identical result to the first.

  "stack"              A question needing src/stacking.py's full chain.
                       Scored on whether the router chose "stack", whether
                       the notes retrieved carry the expected category, and
                       whether every claim in the synthesized answer is
                       covered by cited_note_ids/cited_query_fields (see
                       tests/test_synthesis.py's own structural checks --
                       this is the same property, exercised end to end
                       against live retrieval and compilation instead of
                       fixed fixtures).

A note on the count: 25 cases split across four kinds means most kinds get
5-8 cases, not 25 each. Distribute deliberately rather than evenly -- e.g.
more "structured" cases (cheapest to write, most similar to the existing
eval set) and fewer "cache_hit_repeat" pairs (each pair is really two
questions, and the DoD only needs to demonstrate the mechanism once or
twice convincingly).

Owner: Ajinkya for evals/golden_cases.json (the questions). This file
(schema + loader) mirrors evals/dataset.py's existing pattern.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GOLDEN_CASES_PATH = Path(__file__).parent / "golden_cases.json"


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["structured", "permission_denied", "cache_hit_repeat", "stack"]
    question: str

    # "structured": ground-truth query whose RESULT SET is the target,
    # same discipline as evals/dataset.py::EvalCase -- never a string
    # comparison of SQL.
    expected_sql: str | None = None

    # "permission_denied": the identity asking, and the fact that it must
    # be denied whatever `question` would otherwise return.
    on_behalf_of: dict | None = None  # {"tenant_id": ..., "region": ...}

    # "cache_hit_repeat": the id of the case this one is a paraphrase of.
    # Both cases' questions should resolve to the identical Intent.
    paraphrase_of: str | None = None

    # "stack": which note category the retrieval half is expected to
    # surface, so a case can assert on relevance without hand-picking exact
    # note_ids that would break the moment tools/generate_notes.py's seed
    # or count changes.
    expected_note_category: str | None = None

    rationale: str | None = None
    tags: list[str] = Field(default_factory=list)


class GoldenCaseSet(BaseModel):
    cases: list[GoldenCase]

    def of_kind(self, kind: str) -> list[GoldenCase]:
        return [c for c in self.cases if c.kind == kind]

    def validate_shape(self) -> list[str]:
        """Return a list of problems. Empty list means the set is usable."""
        problems: list[str] = []
        seen: set[str] = set()
        case_ids = {c.id for c in self.cases}

        for c in self.cases:
            if c.id in seen:
                problems.append(f"{c.id}: duplicate id")
            seen.add(c.id)

            if c.kind == "structured" and not c.expected_sql:
                problems.append(f"{c.id}: structured case missing expected_sql")
            if c.kind == "permission_denied" and not c.on_behalf_of:
                problems.append(f"{c.id}: permission_denied case missing on_behalf_of")
            if c.kind == "cache_hit_repeat":
                if not c.paraphrase_of:
                    problems.append(f"{c.id}: cache_hit_repeat case missing paraphrase_of")
                elif c.paraphrase_of not in case_ids:
                    problems.append(
                        f"{c.id}: paraphrase_of references unknown case id "
                        f"'{c.paraphrase_of}'"
                    )
            if c.kind == "stack" and not c.expected_note_category:
                problems.append(f"{c.id}: stack case missing expected_note_category")

        total = len(self.cases)
        if total < 15:
            problems.append(f"need at least 15 golden cases, have {total}")
        if total > 25:
            problems.append(f"plan caps this set at 25 cases, have {total}")

        return problems


def load(path: Path = GOLDEN_CASES_PATH) -> GoldenCaseSet:
    if not path.exists():
        return GoldenCaseSet(cases=[])
    return GoldenCaseSet(cases=json.loads(path.read_text())["cases"])

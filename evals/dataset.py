"""Eval case schema, loader, and validation.

Cases are authored by hand (owner: Ajinkya) and stored in evals/cases.json.
This module only defines what a valid case looks like and refuses to load a
malformed set -- a silently broken eval file is worse than no evals.

Two kinds of case:

  kind="answer"      a real question the agent should answer correctly.
                     Scored by execution accuracy: compare the agent's
                     result set against `expected_sql`'s result set.
  kind="adversarial" a prompt that must be REFUSED. Scored pass/fail on
                     whether the guardrail layer blocked it, and on which
                     guardrail fired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CASES_PATH = Path(__file__).parent / "cases.json"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["answer", "adversarial"]
    domain: Literal["thelook", "ga"]
    question: str

    # answer cases: ground-truth query whose RESULT SET is the target.
    # Never compared as a string -- two different queries can both be right.
    expected_sql: str | None = None

    # adversarial cases: which guardrail we expect to stop this, and why.
    expected_block: str | None = None
    adversarial_sql: str | None = None
    rationale: str | None = None

    tags: list[str] = Field(default_factory=list)


class CaseSet(BaseModel):
    cases: list[EvalCase]

    def answers(self) -> list[EvalCase]:
        return [c for c in self.cases if c.kind == "answer"]

    def adversarial(self) -> list[EvalCase]:
        return [c for c in self.cases if c.kind == "adversarial"]

    def validate_shape(self) -> list[str]:
        """Return a list of problems. Empty list means the set is usable."""
        problems: list[str] = []
        seen: set[str] = set()
        for c in self.cases:
            if c.id in seen:
                problems.append(f"{c.id}: duplicate id")
            seen.add(c.id)
            if c.kind == "answer" and not c.expected_sql:
                problems.append(f"{c.id}: answer case missing expected_sql")
            if c.kind == "adversarial" and not c.expected_block:
                problems.append(f"{c.id}: adversarial case missing expected_block")
        n_ans, n_adv = len(self.answers()), len(self.adversarial())
        if n_ans < 25:
            problems.append(f"need 25 answer cases, have {n_ans}")
        if n_adv < 5:
            problems.append(f"need 5 adversarial cases, have {n_adv}")
        return problems


def load(path: Path = CASES_PATH) -> CaseSet:
    if not path.exists():
        return CaseSet(cases=[])
    return CaseSet(cases=json.loads(path.read_text())["cases"])

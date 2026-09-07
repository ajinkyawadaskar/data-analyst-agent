"""Semantic Intent: the structured object the model emits instead of SQL.

The old path asked an LLM for SQL and then checked the SQL. This path asks for
a small, closed JSON object and checks THAT, before any SQL exists. The
difference matters because the space of malformed intents is tiny and
enumerable, while the space of malformed SQL is not.

Validation happens here, at the edge. A bad intent fails on arrival rather than
three steps downstream inside the compiler, where the error would be reported
in terms of a join path rather than in terms of what the model actually said.

Note the division of labour: this module checks that the intent is well FORMED
(right shape, known operators, non-empty measure). It deliberately does NOT
check that the measure exists in the semantic model -- that is the compiler's
job, and Layer 1's whole property is that compilation fails on an unknown
measure. Doing it in both places would let the compiler's guarantee rot behind
a check that happens to run first.

Owner: Claude (scaffolding + LLM plumbing).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Operator = Literal[
    "=", "!=", ">", ">=", "<", "<=",
    "in", "not in", "between", "is null", "is not null",
]


class Filter(BaseModel):
    """One predicate. `field` names a DIMENSION, not a raw column.

    Naming dimensions rather than columns is what keeps the filter surface
    inside the certified model -- you cannot filter on something the model
    does not expose.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: Operator
    value: Any = None

    @model_validator(mode="after")
    def _value_required_unless_null_check(self) -> "Filter":
        if self.operator in ("is null", "is not null"):
            return self
        if self.value is None:
            raise ValueError(f"operator {self.operator!r} requires a value")
        if self.operator in ("in", "not in") and not isinstance(self.value, list):
            raise ValueError(f"operator {self.operator!r} requires a list value")
        if self.operator == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("operator 'between' requires a two-element list")
        return self


class TimeRange(BaseModel):
    """Inclusive date bound for a partitioned entity.

    Required for ga_sessions (see semantic_model.yaml's partition block): the
    wildcard spans 366 shards and an unbounded scan is 5.77 GB against a 1 GB
    ceiling. The compiler renders this as a _TABLE_SUFFIX predicate.
    """

    model_config = ConfigDict(extra="forbid")

    start: str = Field(pattern=r"^\d{8}$", description="YYYYMMDD, inclusive")
    end: str = Field(pattern=r"^\d{8}$", description="YYYYMMDD, inclusive")

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.start > self.end:
            raise ValueError(f"time range start {self.start} is after end {self.end}")
        return self


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, description="'measure' or a dimension name")
    direction: Literal["asc", "desc"] = "desc"


class Having(BaseModel):
    """Post-aggregation threshold.

    Only row-count thresholds are supported. That is a real limitation, kept
    narrow on purpose: it covers the one eval question that needs it (ga05
    excludes low-volume mediums from a bounce-rate ranking) without opening a
    general expression surface the compiler would then have to validate.
    """

    model_config = ConfigDict(extra="forbid")

    aggregate: Literal["row_count"] = "row_count"
    operator: Literal[">", ">=", "<", "<="] = ">"
    value: int


class Intent(BaseModel):
    """A validated request against the semantic model.

    `unsupported=True` is a first-class outcome, not a failure. Two questions in
    the existing eval set (average order value, repeat-purchase rate) aggregate
    over a grouped subquery and are not expressible as one measure over one base
    entity. Saying so routes them to the legacy LLM path instead of coercing
    them into a shape the compiler would answer wrongly.
    """

    model_config = ConfigDict(extra="forbid")

    measure: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_range: TimeRange | None = None
    order_by: OrderBy | None = None
    having: Having | None = None
    limit: int | None = Field(default=None, ge=1)
    unsupported: bool = False
    unsupported_reason: str | None = None

    @model_validator(mode="after")
    def _measure_required_unless_unsupported(self) -> "Intent":
        if self.unsupported:
            if not self.unsupported_reason:
                raise ValueError("unsupported intents must carry an unsupported_reason")
            return self
        if not self.measure:
            raise ValueError("measure is required unless unsupported=True")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError(f"duplicate dimensions: {self.dimensions}")
        return self

    def canonical(self) -> str:
        """Stable JSON rendering, for hashing.

        Sorted keys and no whitespace, so two intents that differ only in key
        order or the phrasing that produced them serialise identically. This is
        the input to Layer 4's cache key -- see src/cache/intent_hash.py.
        """
        return json.dumps(
            self.model_dump(exclude_none=True, mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

_PROMPT = """\
You translate an analytics question into a JSON Semantic Intent.
You do NOT write SQL. A deterministic compiler turns your JSON into SQL.

CERTIFIED MODEL -- you may only reference these names.

Measures:
{measures}

Dimensions:
{dimensions}

RULES
1. Output ONLY a JSON object. No prose, no markdown fence.
2. "measure" must be exactly one name from the Measures list.
3. "dimensions" is a list of names from the Dimensions list. Use it for
   "by X" / "per X" / "which X" questions. Empty for a single total.
4. "filters" entries are {{"field": <dimension name>, "operator": <op>,
   "value": <v>}}. Operators: = != > >= < <= in "not in" between
   "is null" "is not null".
5. Any measure whose base entity is ga_sessions REQUIRES "time_range":
   {{"start": "YYYYMMDD", "end": "YYYYMMDD"}}. The GA data covers Aug 2016
   to Aug 2017. "August 2016" means start 20160801, end 20160831.
6. Use "order_by": {{"field": "measure", "direction": "desc"}} and "limit"
   for "top N" questions.
7. If the question cannot be answered by ONE measure from the list --
   for example it needs an average of per-group totals, or a count of
   entities meeting a per-group condition -- return
   {{"unsupported": true, "unsupported_reason": "<short reason>"}}.
   This is a correct answer, not a failure. Do not force a wrong measure.

EXAMPLES

Q: What were the top 5 product categories by total revenue?
{{"measure":"total_revenue","dimensions":["product_category"],\
"order_by":{{"field":"measure","direction":"desc"}},"limit":5}}

Q: In August 2016, which traffic sources drove the most sessions?
{{"measure":"session_count","dimensions":["ga_traffic_source"],\
"time_range":{{"start":"20160801","end":"20160831"}},\
"order_by":{{"field":"measure","direction":"desc"}}}}

Q: What is the average order value?
{{"unsupported":true,"unsupported_reason":"needs the mean of per-order \
totals, which is an aggregate over a grouped subquery"}}

QUESTION: {question}
JSON:"""


def build_prompt(question: str, model: Any) -> str:
    """Render the extraction prompt from the certified model.

    The glossary is generated from semantic_model.yaml rather than written into
    the prompt by hand, so adding a measure cannot leave the prompt stale.
    """
    measures = "\n".join(
        f"  - {m.name}: {m.description or 'no description'} "
        f"[base entity: {m.base_entity}]"
        for m in model.measures
    )
    dimensions = "\n".join(
        f"  - {d.name}: {d.type}, from {d.source}" for d in model.dimensions
    )
    return _PROMPT.format(measures=measures, dimensions=dimensions, question=question)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_intent(raw: str) -> Intent:
    """Parse a model response into a validated Intent.

    Tolerates a markdown fence and surrounding prose (both of which flash-lite
    emits intermittently despite the instruction) by extracting the outermost
    JSON object. Everything past that is strict: unknown keys are rejected by
    `extra="forbid"`, so a hallucinated field fails here rather than being
    silently dropped on the way to the compiler.
    """
    text = _FENCE.sub("", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in model response: {raw[:200]!r}")
    return Intent.model_validate(json.loads(text[start : end + 1]))

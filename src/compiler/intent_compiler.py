"""Deterministic Intent -> SQL compiler. THE CORE OF THE GATEWAY.

Owner: Ajinkya. Scaffolding only below -- signatures, types, and the spec.
The body of compile() is deliberately unwritten.

WHY THIS MODULE EXISTS
----------------------
The old path asks an LLM for SQL and then checks it. Checking bounds the damage
but the model still authors the query, so a hallucinated column is possible and
merely caught. Here the model authors only a small JSON object naming things
from a certified list, and this compiler builds the SQL. A metric that is not in
semantic_model.yaml cannot be compiled, so hallucinating one is not an error to
be caught -- it is structurally impossible to express.

That property is only real if this module is strict. Every lookup below must
fail loudly on a miss. A compiler that quietly skips an unknown dimension gives
back the exact class of silently-wrong answer the layer was built to remove.

CONTRACT
--------
    compile(intent, model, settings) -> CompiledQuery

Steps, in order:

1. UNSUPPORTED SHORT-CIRCUIT
   If intent.unsupported, raise UnsupportedIntent. The caller routes to the
   legacy LLM path. Do not attempt a best-effort compile.

2. RESOLVE NAMES  (all failures raise SemanticCompileError)
   - model.measure(intent.measure); on None, raise listing model.measure_names().
   - model.dimension(d) for each d in intent.dimensions; same treatment with
     model.dimension_names().
   - model.dimension(f.field) for each filter. Filters name DIMENSIONS, not raw
     columns -- that is what keeps the filter surface inside the certified model.
   The error MUST name the allowed set. "unknown measure 'revenu'" is a worse
   message than one that shows the caller what it could have said, and this
   error is what an interviewer will ask you to demonstrate.

3. DETERMINE REQUIRED ENTITIES
   base = measure.base_entity, plus the entity of every referenced dimension and
   filter. Deduplicate, preserving base first.

4. RESOLVE THE JOIN PATH BY GRAPH TRAVERSAL
   Breadth-first from the base entity over model.joins, treating each join as an
   undirected edge (model.join_between(a, b) matches either direction). Collect
   the ordered edges needed to reach every required entity.
   - If an entity is unreachable, raise SemanticCompileError naming it. Do NOT
     emit a cross join.
   - Record the edges on CompiledQuery.join_path. That list is what the audit
     envelope surfaces as proven_join_path: the claim is that the join was
     derived from a declared model, so it has to be reported, not asserted.

5. BUILD THE AST -- sqlglot expression builders, never string concatenation
   Projections, in this order (the eval metric compares columns POSITIONALLY,
   so dimensions must come before the measure to match the committed ground
   truth):
     - for each dimension: dim.expression if set, else the qualified column;
       aliased to dim.name
     - the measure: measure.formula, aliased to measure.name
   FROM the base entity's table; JOIN each edge on its declared path.
   WHERE: intent.filters, then measure.filters, then the partition predicate
   (step 6), AND-combined.
   GROUP BY every dimension when intent.dimensions is non-empty.
   HAVING: intent.having renders as COUNT(*) <op> <value>.
   ORDER BY: intent.order_by, where field == "measure" means the measure alias.

6. PARTITION BOUND IS MANDATORY WHERE DECLARED
   If any required entity has partition.required and intent.time_range is None,
   raise SemanticCompileError. Otherwise emit
       _TABLE_SUFFIX BETWEEN '<start>' AND '<end>'
   This is why GA cannot be scanned unbounded from this path: the cost ceiling
   is a backstop here, not the control.

7. ALWAYS EMIT A LIMIT
   Non-negotiable. src/guardrails.py::_check_row_limit REJECTS a query with no
   LIMIT -- it does not inject one. A compiled query without a LIMIT is blocked
   by our own guardrail, which would look like a guardrail bug and is really a
   compiler bug. Use intent.limit, else settings.max_rows.

8. RENDER
   expression.sql(dialect=model.dialect, pretty=True). The dialect comes from
   the model, not a literal, so an Athena target is a config change rather than
   a rewrite. Return CompiledQuery with the expression retained -- Layer 2
   injects security predicates into the AST, and it cannot do that to a string.

WHAT THIS MODULE MUST NOT DO
----------------------------
- No f-string SQL assembly. Values go in as sqlglot literals; a filter value is
  untrusted input and string interpolation here is an injection hole that Layer
  2's AST work would then be pointlessly careful about.
- No silent coercion. Unknown name -> raise. Unreachable entity -> raise.
  Missing required partition -> raise.
- No security predicates. That is Layer 2 (src/compiler/security.py), applied to
  the returned expression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from src.semantic.intent import Intent
from src.semantic.model import SemanticModel


class SemanticCompileError(Exception):
    """A requested name or join path is not in the certified model.

    Carries the allowed set so the message tells the caller what it could have
    asked for.
    """

    def __init__(self, message: str, allowed: list[str] | None = None) -> None:
        self.allowed = allowed or []
        if self.allowed:
            message = f"{message}. Allowed: {', '.join(sorted(self.allowed))}"
        super().__init__(message)


class UnsupportedIntent(Exception):
    """The question is not expressible against the semantic model.

    Not an error condition -- the signal to fall back to the legacy LLM path.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class JoinEdge:
    """One resolved join, as derived from the model's declared join graph."""

    name: str
    relationship: str
    path: str
    left_entity: str
    right_entity: str


@dataclass
class CompiledQuery:
    """The output of compilation.

    `expression` is retained alongside `sql` because Layer 2 injects row-level
    security into the AST. Once it is a string it is too late to do that safely.
    """

    sql: str
    expression: exp.Select
    base_entity: str
    measure: str
    dimensions: tuple[str, ...] = ()
    join_path: tuple[JoinEdge, ...] = ()
    model_version: str = ""
    partition_bound: str | None = None
    security_predicates: tuple[str, ...] = field(default_factory=tuple)

    def proven_join_path(self) -> list[str]:
        """Join paths actually used, for the audit envelope."""
        return [e.path for e in self.join_path]


def compile(  # noqa: A001 - mirrors the domain verb, not the builtin
    intent: Intent,
    model: SemanticModel,
    settings: Any | None = None,
) -> CompiledQuery:
    """Compile a validated Intent into governed BigQuery SQL.

    Args:
        intent: an already-validated Intent. Structural validation happened in
            src/semantic/intent.py; this function does SEMANTIC validation --
            do these names exist in the model, and can these entities be joined.
        model: the loaded, schema-validated SemanticModel.
        settings: object exposing `max_rows`, used for the mandatory LIMIT when
            intent.limit is None. Defaults to src.config.get_settings().

    Returns:
        CompiledQuery with both the rendered SQL and the retained AST.

    Raises:
        UnsupportedIntent: intent.unsupported was set; fall back to the LLM path.
        SemanticCompileError: an unknown measure/dimension, an unreachable
            entity, or a missing mandatory partition bound.

    See the module docstring for the full step-by-step contract.
    """
    raise NotImplementedError("TODO: Ajinkya writes this")


def resolve_join_path(
    model: SemanticModel,
    base_entity: str,
    required_entities: set[str],
) -> tuple[JoinEdge, ...]:
    """Breadth-first search over the declared join graph.

    Args:
        model: the loaded SemanticModel.
        base_entity: entity the FROM clause is anchored on.
        required_entities: every entity referenced by the measure, dimensions
            and filters.

    Returns:
        Ordered join edges reaching every required entity from the base. Empty
        when everything lives on the base entity.

    Raises:
        SemanticCompileError: a required entity is unreachable. Never fall back
            to a cross join -- an unreachable entity means the model is
            incomplete, and a cartesian product would answer the question with
            a number that is wrong rather than absent.
    """
    raise NotImplementedError("TODO: Ajinkya writes this")

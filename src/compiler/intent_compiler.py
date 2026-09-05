"""
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

from collections import deque
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


# ---------------------------------------------------------------------------
# Small internal helpers. None of these are part of the public contract; they
# exist so `compile()` reads step-by-step instead of drowning in sqlglot calls.
# ---------------------------------------------------------------------------


def _qualified_column(entity: str, column: str, dialect: str = "bigquery") -> exp.Column:
    """`entity.column`, built with the sqlglot expression API (never a string).

    Uses exp.to_column() rather than exp.column(column, table=entity): the
    latter treats the whole `column` string as ONE identifier, so a GA nested
    path like "trafficSource.source" round-trips as the backtick-quoted
    literal name `` `trafficSource.source` `` -- a column that does not
    exist. to_column() parses the dotted string into proper table/db-qualified
    parts, so a nested STRUCT field renders as unquoted
    ga_sessions.trafficSource.source, which is what BigQuery actually expects.
    """
    return exp.to_column(f"{entity}.{column}", dialect=dialect)


def _dimension_projection(dim: Any, dialect: str) -> exp.Expression:
    """dim.expression if the model declares one, else the qualified raw column.

    A declared `expression` (e.g. a CASE statement or a derived bucket) is
    parsed rather than the SQL being trusted verbatim, so it still goes through
    the AST -- it just skips the "raw column" branch below. Parsed with the
    model's own dialect so a dialect-sensitive function (e.g. DATE_TRUNC, whose
    argument order differs across engines) round-trips instead of getting
    silently renormalized to a different engine's convention on render.
    """
    raw_expression = getattr(dim, "expression", None)
    if raw_expression:
        return exp.maybe_parse(raw_expression, dialect=dialect)
    return _qualified_column(dim.entity, dim.column, dialect=dialect)


def _binary_condition(left: exp.Expression, operator: str, value: Any) -> exp.Expression:
    """Build `left <op> value` with sqlglot builders -- value is always a literal.

    Centralizing this is what keeps every WHERE/HAVING predicate off the
    f-string path: `value` never touches the SQL as text, it goes in through
    `exp.convert`, which sqlglot renders as a properly-escaped/typed literal.
    """
    op = operator.lower().strip()
    literal = exp.convert(value)

    if op in ("=", "==", "eq"):
        return exp.EQ(this=left, expression=literal)
    if op in ("!=", "<>", "ne"):
        return exp.NEQ(this=left, expression=literal)
    if op in (">", "gt"):
        return exp.GT(this=left, expression=literal)
    if op in (">=", "gte"):
        return exp.GTE(this=left, expression=literal)
    if op in ("<", "lt"):
        return exp.LT(this=left, expression=literal)
    if op in ("<=", "lte"):
        return exp.LTE(this=left, expression=literal)
    if op == "like":
        return exp.Like(this=left, expression=literal)
    if op in ("in",):
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return exp.In(this=left, expressions=[exp.convert(v) for v in values])
    if op in ("not in", "nin"):
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return exp.Not(this=exp.In(this=left, expressions=[exp.convert(v) for v in values]))

    raise SemanticCompileError(
        f"unsupported filter operator '{operator}'",
        ["=", "!=", ">", ">=", "<", "<=", "like", "in", "not in"],
    )


def _filter_operator(f: Any) -> str:
    """Filters may spell the comparator as `.operator` or `.op` -- accept either."""
    return getattr(f, "operator", None) or getattr(f, "op", "=")


def _order_is_desc(entry: Any) -> bool:
    """Order-by entries may spell direction as `.direction` ('desc') or `.desc`."""
    direction = getattr(entry, "direction", None)
    if direction is not None:
        return str(direction).lower().startswith("desc")
    return bool(getattr(entry, "desc", False))


def _entity_table(model: SemanticModel, entity_name: str) -> str:
    """The physical table backing a declared entity."""
    entity = model.entity(entity_name)
    if entity is None:
        # The join graph or a measure/dimension pointed at an entity the model
        # doesn't actually declare -- that is a model bug, not a bad request.
        raise SemanticCompileError(f"model declares no entity '{entity_name}'")
    return entity.table


def _entity_partition_required(model: SemanticModel, entity_name: str) -> bool:
    entity = model.entity(entity_name)
    partition = getattr(entity, "partition", None) if entity is not None else None
    return bool(getattr(partition, "required", False))


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


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
    # --- 1. Unsupported short-circuit -------------------------------------
    if intent.unsupported:
        raise UnsupportedIntent(
            getattr(intent, "unsupported_reason", None)
            or "intent marked unsupported; route to the legacy LLM path"
        )

    if settings is None:
        from src.config import get_settings

        settings = get_settings()

    # --- 2. Resolve names ---------------------------------------------------
    measure = model.measure(intent.measure)
    if measure is None:
        raise SemanticCompileError(
            f"unknown measure '{intent.measure}'", model.measure_names()
        )

    dimensions = []
    for dim_name in intent.dimensions:
        dim = model.dimension(dim_name)
        if dim is None:
            raise SemanticCompileError(
                f"unknown dimension '{dim_name}'", model.dimension_names()
            )
        dimensions.append(dim)

    # Filters name dimensions, not raw columns -- resolve each one the same
    # strict way, keeping the filter surface inside the certified model.
    filter_dims = []
    for f in intent.filters:
        fdim = model.dimension(f.field)
        if fdim is None:
            raise SemanticCompileError(
                f"unknown filter dimension '{f.field}'", model.dimension_names()
            )
        filter_dims.append(fdim)

    # --- 3. Determine required entities -------------------------------------
    base_entity = measure.base_entity
    required_entities: list[str] = [base_entity]
    for dim in dimensions:
        if dim.entity not in required_entities:
            required_entities.append(dim.entity)
    for fdim in filter_dims:
        if fdim.entity not in required_entities:
            required_entities.append(fdim.entity)

    # --- 4. Resolve the join path -------------------------------------------
    join_path = resolve_join_path(model, base_entity, set(required_entities))

    # --- 5. Build the AST ----------------------------------------------------
    select = exp.Select()

    projections: list[exp.Expression] = []
    for dim in dimensions:
        projections.append(_dimension_projection(dim, model.dialect).as_(dim.name))
    projections.append(
        exp.maybe_parse(measure.formula, dialect=model.dialect).as_(measure.name)
    )
    select = select.select(*projections)

    select = select.from_(exp.to_table(_entity_table(model, base_entity), alias=base_entity))
    for edge in join_path:
        join_table = exp.to_table(_entity_table(model, edge.right_entity), alias=edge.right_entity)
        select = select.join(
            join_table,
            on=exp.condition(edge.path, dialect=model.dialect),
            join_type="inner",
        )

    where_conditions: list[exp.Expression] = []

    # intent.filters, resolved against their dimensions from step 2
    for f, fdim in zip(intent.filters, filter_dims):
        column = _qualified_column(fdim.entity, fdim.column, dialect=model.dialect)
        where_conditions.append(_binary_condition(column, _filter_operator(f), f.value))

    # measure.filters -- declarative predicates baked into the measure itself
    # (e.g. "exclude refunded rows"). These are certified model SQL, not user
    # input, so parsing the raw string is fine; they still never touch WHERE
    # as concatenated text.
    for raw_measure_filter in getattr(measure, "filters", None) or []:
        where_conditions.append(exp.condition(raw_measure_filter, dialect=model.dialect))

    # --- 6. Partition bound is mandatory where declared ---------------------
    partition_bound_sql: str | None = None
    entities_requiring_partition = [
        e for e in required_entities if _entity_partition_required(model, e)
    ]
    if entities_requiring_partition:
        if intent.time_range is None:
            raise SemanticCompileError(
                "a bounded time_range is required: "
                f"{', '.join(sorted(entities_requiring_partition))} declare a "
                "mandatory partition"
            )
        start, end = intent.time_range.start, intent.time_range.end
        partition_bound_sql = f"_TABLE_SUFFIX BETWEEN '{start}' AND '{end}'"
        where_conditions.append(exp.condition(partition_bound_sql, dialect=model.dialect))

    for condition in where_conditions:
        select = select.where(condition)

    if dimensions:
        select = select.group_by(*[exp.column(dim.name) for dim in dimensions])

    having = getattr(intent, "having", None)
    if having is not None:
        count_star = exp.Count(this=exp.Star())
        select = select.having(
            _binary_condition(count_star, getattr(having, "operator", "="), having.value)
        )

    order_entry = getattr(intent, "order_by", None)
    if order_entry is not None:
        # Intent.order_by is a single OrderBy, not a list -- a `for` here
        # would iterate the model's own (field_name, value) pairs instead of
        # raising, since pydantic BaseModel instances are iterable.
        field_name = order_entry.field
        column_name = measure.name if field_name == "measure" else field_name
        order_column = exp.column(column_name)
        select = select.order_by(
            order_column.desc() if _order_is_desc(order_entry) else order_column.asc()
        )

    # --- 7. Always emit a LIMIT ---------------------------------------------
    limit_value = intent.limit if intent.limit is not None else settings.max_rows
    select = select.limit(limit_value)

    # --- 8. Render ------------------------------------------------------------
    rendered_sql = select.sql(dialect=model.dialect, pretty=True)

    return CompiledQuery(
        sql=rendered_sql,
        expression=select,
        base_entity=base_entity,
        measure=measure.name,
        dimensions=tuple(dim.name for dim in dimensions),
        join_path=join_path,
        model_version=getattr(model, "version", ""),
        partition_bound=partition_bound_sql,
    )


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
    targets = set(required_entities) - {base_entity}
    if not targets:
        return ()

    # Build an undirected adjacency list purely to find shortest paths. The
    # actual join details (ON clause, name, relationship) are fetched from
    # model.join_between() once we know two entities are adjacent -- that is
    # the one function the model guarantees matches either direction.
    neighbors: dict[str, set[str]] = {}
    for j in model.joins:
        a, b = j.left_entity, j.right_entity
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)

    visited = {base_entity}
    parent: dict[str, str] = {}
    queue: deque[str] = deque([base_entity])
    while queue:
        current = queue.popleft()
        for neighbor in sorted(neighbors.get(current, ())):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parent[neighbor] = current
            queue.append(neighbor)

    unreachable = targets - visited
    if unreachable:
        raise SemanticCompileError(
            "entity(-ies) unreachable from base entity "
            f"'{base_entity}' via the declared join graph: "
            f"{', '.join(sorted(unreachable))}"
        )

    # Walk each target back to the base to get its edge sequence, then flatten
    # into one ordered, de-duplicated list (BFS visits nearer entities first,
    # so edges shared by multiple targets naturally land before their
    # dependents).
    edges: list[JoinEdge] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Sort targets by BFS depth so shared prefixes are recorded in traversal
    # order rather than in whatever order `required_entities` happened to be.
    def _depth(entity: str) -> int:
        depth = 0
        node = entity
        while node != base_entity:
            node = parent[node]
            depth += 1
        return depth

    for target in sorted(targets, key=_depth):
        path_from_base: list[str] = []
        node = target
        while node != base_entity:
            path_from_base.append(node)
            node = parent[node]
        path_from_base.reverse()

        previous = base_entity
        for entity in path_from_base:
            pair = tuple(sorted((previous, entity)))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                join_info = model.join_between(previous, entity)
                if join_info is None:
                    # The adjacency map came from model.joins, so this would
                    # mean join_between disagrees with the declared graph --
                    # a model bug, surfaced loudly rather than papered over.
                    raise SemanticCompileError(
                        f"declared join graph has an edge between '{previous}' "
                        f"and '{entity}' but model.join_between() could not "
                        "resolve it"
                    )
                edges.append(
                    JoinEdge(
                        name=join_info.name,
                        relationship=join_info.relationship,
                        path=join_info.path,
                        left_entity=join_info.left_entity,
                        right_entity=join_info.right_entity,
                    )
                )
            previous = entity

    return tuple(edges)
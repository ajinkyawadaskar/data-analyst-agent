"""Load and validate semantic_model.yaml against the real BigQuery schema.

The YAML is a source of truth, so it has to be checked rather than trusted.
This module loads it into frozen dataclasses and then verifies every entity,
column, join path and row policy against an introspected SchemaContext,
raising on ANY mismatch with the complete list of problems.

Why validation is load-bearing rather than hygiene
--------------------------------------------------
SchemaContext.columns_for() returns an EMPTY SET for a table it does not know,
and src/guardrails.py::_schema_columns_for treats a falsy return as "unknown
table -- skip column validation for it". So a single typo'd table name in this
YAML would not fail: it would silently disable the column guardrail for every
query the compiler emits against that table. The check below is what stops a
misspelling from turning into a hole in a guardrail one layer down.

Failure is loud and total: we collect every problem and raise once, because a
model with six broken references should not be fixed six edit-run cycles in a
row.

Owner: Claude (scaffolding). The compiler that consumes this is Ajinkya's --
see src/compiler/intent_compiler.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

MODEL_PATH = Path(__file__).resolve().parents[2] / "semantic_model.yaml"

# BigQuery pseudo-columns that are legal in a formula or filter but never appear
# in an introspected schema. _TABLE_SUFFIX is how the GA wildcard gets bounded.
_PSEUDO_COLUMNS = {"_TABLE_SUFFIX", "_PARTITIONTIME", "_PARTITIONDATE"}


class SemanticModelError(Exception):
    """Raised when the YAML does not match the live schema.

    Carries every problem found, not just the first.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(
            f"semantic_model.yaml does not match the BigQuery schema "
            f"({len(problems)} problem(s)):\n  - {joined}"
        )


@dataclass(frozen=True)
class Partition:
    column: str
    format: str
    required: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Entity:
    name: str
    table: str
    primary_key: str
    description: str = ""
    partition: Partition | None = None


@dataclass(frozen=True)
class Measure:
    name: str
    base_entity: str
    formula: str
    description: str = ""
    filters: tuple[str, ...] = ()
    eval_source: str = ""


@dataclass(frozen=True)
class Dimension:
    name: str
    type: str
    entity: str
    column: str
    description: str = ""
    expression: str | None = None
    eval_source: str = ""

    @property
    def source(self) -> str:
        return f"{self.entity}.{self.column}"


@dataclass(frozen=True)
class Join:
    name: str
    relationship: str
    path: str
    left_entity: str
    right_entity: str


@dataclass(frozen=True)
class RowPolicy:
    entity: str
    principal_field: str
    column: str
    description: str = ""


@dataclass(frozen=True)
class SemanticModel:
    version: str
    dialect: str
    entities: tuple[Entity, ...]
    measures: tuple[Measure, ...]
    dimensions: tuple[Dimension, ...]
    joins: tuple[Join, ...]
    row_policies: tuple[RowPolicy, ...] = ()

    def entity(self, name: str) -> Entity | None:
        return next((e for e in self.entities if e.name == name), None)

    def measure(self, name: str) -> Measure | None:
        return next((m for m in self.measures if m.name == name), None)

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    def policy_for(self, entity: str) -> RowPolicy | None:
        return next((p for p in self.row_policies if p.entity == entity), None)

    def measure_names(self) -> list[str]:
        return sorted(m.name for m in self.measures)

    def dimension_names(self) -> list[str]:
        return sorted(d.name for d in self.dimensions)

    def join_between(self, left: str, right: str) -> Join | None:
        """Return the join connecting two entities in either direction."""
        for j in self.joins:
            if {j.left_entity, j.right_entity} == {left, right}:
                return j
        return None

    def glossary(self) -> dict[str, Any]:
        """Serialisable view of the certified model.

        This is what Layer 3 exposes as the semantic://metrics_glossary MCP
        Resource, and what semantic_model_version in the audit envelope refers to.
        """
        return {
            "version": self.version,
            "dialect": self.dialect,
            "measures": [
                {
                    "name": m.name,
                    "description": m.description,
                    "base_entity": m.base_entity,
                    "formula": m.formula,
                    "filters": list(m.filters),
                }
                for m in self.measures
            ],
            "dimensions": [
                {
                    "name": d.name,
                    "type": d.type,
                    "source": d.source,
                    "description": d.description,
                }
                for d in self.dimensions
            ],
            "joins": [
                {"name": j.name, "relationship": j.relationship, "path": j.path}
                for j in self.joins
            ],
        }


def _split_source(source: str) -> tuple[str, str]:
    """Split `entity.column` on the FIRST dot only.

    GA's nested paths must survive intact:
        users.region             -> ("users", "region")
        ga_sessions.totals.hits  -> ("ga_sessions", "totals.hits")
    """
    entity, _, column = source.partition(".")
    return entity, column


def _columns_in(sql_fragment: str, dialect: str) -> set[str]:
    """Extract column references from a SQL fragment.

    Parsed as a projection so bare expressions like `SUM(sale_price)` and
    `totals.pageviews IS NOT NULL` both work. A fragment that will not parse
    returns an empty set -- the caller reports that separately as a parse
    failure rather than silently treating it as "no columns".
    """
    try:
        tree = sqlglot.parse_one(f"SELECT {sql_fragment}", dialect=dialect)
    except Exception:  # noqa: BLE001 - caller reports unparseable fragments
        return set()
    return {".".join(part.name for part in col.parts) for col in tree.find_all(exp.Column)}


def _parses(sql_fragment: str, dialect: str) -> bool:
    try:
        sqlglot.parse_one(f"SELECT {sql_fragment}", dialect=dialect)
        return True
    except Exception:  # noqa: BLE001
        return False


def _known_columns(schema_context: Any, table_fqn: str) -> set[str]:
    cols = schema_context.columns_for(table_fqn)
    return set(cols) if cols else set()


def load(path: Path | str = MODEL_PATH, schema_context: Any | None = None) -> SemanticModel:
    """Parse semantic_model.yaml and, if given a SchemaContext, validate it.

    Args:
        path: location of the YAML.
        schema_context: an introspected SchemaContext. When None, the model is
            parsed but NOT checked against BigQuery -- only for offline tooling
            that just needs the glossary. Anything that compiles SQL must pass
            a real schema context.

    Raises:
        SemanticModelError: the YAML references something the schema does not have.
    """
    raw = yaml.safe_load(Path(path).read_text())
    model = _parse(raw)
    if schema_context is not None:
        problems = validate(model, schema_context)
        if problems:
            raise SemanticModelError(problems)
    return model


def _parse(raw: dict[str, Any]) -> SemanticModel:
    entities = []
    for e in raw.get("entities", []):
        part = e.get("partition")
        entities.append(
            Entity(
                name=e["name"],
                table=e["table"],
                primary_key=e["primary_key"],
                description=e.get("description", ""),
                partition=(
                    Partition(
                        column=part["column"],
                        format=part["format"],
                        required=part.get("required", False),
                        reason=part.get("reason", ""),
                    )
                    if part
                    else None
                ),
            )
        )

    measures = [
        Measure(
            name=m["name"],
            base_entity=m["base_entity"],
            formula=m["formula"],
            description=m.get("description", ""),
            filters=tuple(m.get("filters", ())),
            eval_source=str(m.get("eval_source", "")),
        )
        for m in raw.get("measures", [])
    ]

    dimensions = []
    for d in raw.get("dimensions", []):
        entity, column = _split_source(d["source"])
        dimensions.append(
            Dimension(
                name=d["name"],
                type=d["type"],
                entity=entity,
                column=column,
                description=d.get("description", ""),
                expression=d.get("expression"),
                eval_source=str(d.get("eval_source", "")),
            )
        )

    joins = []
    for j in raw.get("joins", []):
        left, right = _join_entities(j["path"])
        joins.append(
            Join(
                name=j["name"],
                relationship=j["relationship"],
                path=j["path"],
                left_entity=left,
                right_entity=right,
            )
        )

    policies = [
        RowPolicy(
            entity=p["entity"],
            principal_field=p["principal_field"],
            column=p["column"],
            description=p.get("description", ""),
        )
        for p in raw.get("row_policies", [])
    ]

    return SemanticModel(
        version=str(raw.get("version", "0.0.0")),
        dialect=raw.get("dialect", "bigquery"),
        entities=tuple(entities),
        measures=tuple(measures),
        dimensions=tuple(dimensions),
        joins=tuple(joins),
        row_policies=tuple(policies),
    )


def _join_entities(path: str) -> tuple[str, str]:
    """Pull the two entity names out of `a.col = b.col`."""
    left_side, _, right_side = path.partition("=")
    return left_side.strip().split(".")[0], right_side.strip().split(".")[0]


def validate(model: SemanticModel, schema_context: Any) -> list[str]:
    """Check every reference in the model against the live schema.

    Returns the full list of problems; empty means the model is sound.
    """
    problems: list[str] = []
    known_tables = set(schema_context.table_fqns())

    # -- entities ---------------------------------------------------------
    entity_cols: dict[str, set[str]] = {}
    for e in model.entities:
        if e.table not in known_tables:
            problems.append(
                f"entity '{e.name}': table '{e.table}' is not in the introspected "
                f"schema. Known tables: {sorted(known_tables)}"
            )
            continue
        cols = _known_columns(schema_context, e.table)
        if not cols:
            problems.append(
                f"entity '{e.name}': table '{e.table}' resolved to zero columns. "
                f"An empty column set silently disables column validation downstream."
            )
            continue
        entity_cols[e.name] = cols
        if e.primary_key not in cols:
            problems.append(
                f"entity '{e.name}': primary_key '{e.primary_key}' is not a column "
                f"of '{e.table}'"
            )
        if e.partition and e.partition.column not in cols | _PSEUDO_COLUMNS:
            problems.append(
                f"entity '{e.name}': partition column '{e.partition.column}' is "
                f"neither a real column nor a known pseudo-column"
            )

    def _check_cols(where: str, entity_name: str, fragment: str) -> None:
        if entity_name not in entity_cols:
            problems.append(f"{where}: unknown entity '{entity_name}'")
            return
        if not _parses(fragment, model.dialect):
            problems.append(f"{where}: SQL fragment does not parse: {fragment!r}")
            return
        available = entity_cols[entity_name] | _PSEUDO_COLUMNS
        for col in _columns_in(fragment, model.dialect):
            if col in available:
                continue
            # A nested leaf may be referenced by its last segment.
            if col.split(".")[-1] in available:
                continue
            problems.append(
                f"{where}: column '{col}' does not exist on entity "
                f"'{entity_name}' ({model.entity(entity_name).table})"
            )

    # -- measures ---------------------------------------------------------
    seen: set[str] = set()
    for m in model.measures:
        if m.name in seen:
            problems.append(f"measure '{m.name}': duplicate name")
        seen.add(m.name)
        _check_cols(f"measure '{m.name}' formula", m.base_entity, m.formula)
        for f in m.filters:
            _check_cols(f"measure '{m.name}' filter", m.base_entity, f)

    # -- dimensions -------------------------------------------------------
    seen = set()
    for d in model.dimensions:
        if d.name in seen:
            problems.append(f"dimension '{d.name}': duplicate name")
        seen.add(d.name)
        _check_cols(f"dimension '{d.name}' source", d.entity, d.column)
        if d.expression:
            _check_cols(f"dimension '{d.name}' expression", d.entity, d.expression)

    # -- joins ------------------------------------------------------------
    for j in model.joins:
        for side in (j.left_entity, j.right_entity):
            if side not in entity_cols:
                problems.append(
                    f"join '{j.name}': references undeclared entity '{side}'"
                )
        if j.left_entity in entity_cols and j.right_entity in entity_cols:
            for token in (j.path.split("=")[0], j.path.split("=")[1]):
                ent, _, col = token.strip().partition(".")
                if ent in entity_cols and col not in entity_cols[ent]:
                    problems.append(
                        f"join '{j.name}': column '{col}' does not exist on "
                        f"entity '{ent}'"
                    )

    # -- row policies -----------------------------------------------------
    for p in model.row_policies:
        _check_cols(f"row_policy on '{p.entity}'", p.entity, p.column)

    return problems

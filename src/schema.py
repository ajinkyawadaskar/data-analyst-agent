"""BigQuery schema introspection and context compaction.

Two jobs, deliberately separated:

  introspect()  -- talk to BigQuery, return the FULL schema. Faithful, big,
                   and not something you can put in a prompt.
  compact()     -- reduce a full schema to something that fits a context
                   window, under a named strategy.

The split matters because compaction is lossy and the strategy is a design
decision with real consequences: anything dropped here is a column the model
cannot use and that guardrails.py will reject if it hallucinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from src.config import get_settings

CompactionStrategy = Literal["full", "table_summary", "column_sample"]


@dataclass(frozen=True)
class Column:
    name: str          # dotted path for nested GA fields: "totals.pageviews"
    type: str          # BigQuery type: STRING, INT64, RECORD, ...
    mode: str = "NULLABLE"   # NULLABLE | REQUIRED | REPEATED
    description: str = ""

    @property
    def is_nested(self) -> bool:
        return "." in self.name


@dataclass(frozen=True)
class Table:
    project: str
    dataset: str
    name: str
    columns: tuple[Column, ...]
    num_rows: int | None = None

    @property
    def fqn(self) -> str:
        return f"{self.project}.{self.dataset}.{self.name}"

    @property
    def qualified_dataset(self) -> str:
        return f"{self.project}.{self.dataset}"


@dataclass(frozen=True)
class SchemaContext:
    """What the model is told about the corpus, and what guardrails.py
    validates generated columns against.

    `strategy` records how this was compacted, and `dropped_columns` records
    what compaction removed -- so a column-validation rejection can be traced
    to "the model hallucinated it" vs "we never showed it to the model."
    That distinction is the difference between a model problem and a
    retrieval problem, and you cannot debug the agent without it.
    """

    tables: tuple[Table, ...]
    strategy: CompactionStrategy = "full"
    dropped_columns: tuple[str, ...] = field(default_factory=tuple)

    # ---- lookups used by guardrails.py (owner: Ajinkya) ----

    def table_fqns(self) -> set[str]:
        return {t.fqn for t in self.tables}

    def allowed_datasets(self) -> set[str]:
        return {t.qualified_dataset for t in self.tables}

    def columns_for(self, fqn: str) -> set[str]:
        for t in self.tables:
            if t.fqn == fqn:
                return {c.name for c in t.columns}
        return set()

    def all_column_names(self) -> set[str]:
        return {c.name for t in self.tables for c in t.columns}

    def find_tables_with_column(self, column: str) -> list[str]:
        """Used to resolve unqualified column references."""
        return [t.fqn for t in self.tables if any(c.name == column for c in t.columns)]

    # ---- prompt rendering ----

    def to_prompt(self) -> str:
        """Render for the LLM. Terse on purpose -- every token here is a
        token not available for the question or the reasoning."""
        lines: list[str] = []
        for t in self.tables:
            header = f"{t.fqn}"
            if t.num_rows is not None:
                header += f"  ({t.num_rows:,} rows)"
            lines.append(header)
            for c in t.columns:
                mode = " REPEATED" if c.mode == "REPEATED" else ""
                lines.append(f"  {c.name} {c.type}{mode}")
            lines.append("")
        return "\n".join(lines).strip()

    def approx_tokens(self) -> int:
        """Rough size check: ~4 chars per token. Good enough to decide
        whether a strategy fits; not a billing estimate."""
        return len(self.to_prompt()) // 4


def _flatten(schema_fields: Iterable, prefix: str = "") -> list[Column]:
    """Flatten BigQuery RECORD fields into dotted paths.

    GA360 nests heavily (totals.pageviews, hits.product.productSKU). A flat
    column list is what guardrails.py can actually validate against.
    """
    out: list[Column] = []
    for f in schema_fields:
        name = f"{prefix}{f.name}"
        if f.field_type == "RECORD" and f.fields:
            out.append(Column(name=name, type="RECORD", mode=f.mode, description=f.description or ""))
            out.extend(_flatten(f.fields, prefix=f"{name}."))
        else:
            out.append(
                Column(name=name, type=f.field_type, mode=f.mode, description=f.description or "")
            )
    return out


def introspect(datasets: tuple[str, ...] | None = None) -> SchemaContext:
    """Read the full schema of every allowed dataset from BigQuery.

    Requires credentials. Returns strategy="full" -- almost certainly too
    large to put in a prompt; pass it through compact() first.
    """
    from google.cloud import bigquery

    from src.bq_client import get_client

    settings = get_settings()
    datasets = datasets or settings.allowed_datasets
    client = get_client()

    tables: list[Table] = []
    for qualified in datasets:
        project, dataset_id = qualified.split(".", 1)
        ref = bigquery.DatasetReference(project, dataset_id)
        for item in client.list_tables(ref):
            tbl = client.get_table(item.reference)
            tables.append(
                Table(
                    project=project,
                    dataset=dataset_id,
                    name=tbl.table_id,
                    columns=tuple(_flatten(tbl.schema)),
                    num_rows=tbl.num_rows,
                )
            )
    return SchemaContext(tables=tuple(tables), strategy="full")


def compact(schema: SchemaContext, strategy: CompactionStrategy) -> SchemaContext:
    """Reduce a full schema under a named strategy.

    STRATEGY CHOICE IS AJINKYA'S DESIGN DECISION (CLAUDE.md). This function
    implements the mechanisms; which one ships, and why, is recorded in
    NOTES.md.

      "full"           no reduction. Baseline for measuring the others.
      "table_summary"  every table, but only key columns (ids, dates,
                       and non-RECORD scalars) -- keeps joins possible.
      "column_sample"  every table, columns capped per table.

    Whatever is dropped is recorded in dropped_columns so a guardrail
    rejection can be attributed correctly.
    """
    if strategy == "full":
        return schema

    kept_tables: list[Table] = []
    dropped: list[str] = []

    for t in schema.tables:
        if strategy == "table_summary":
            keep = tuple(
                c for c in t.columns
                if c.type != "RECORD"
                and (
                    c.name.endswith("_id")
                    or c.name == "id"
                    or c.type in ("DATE", "TIMESTAMP", "DATETIME")
                    or not c.is_nested
                )
            )
        else:  # column_sample
            keep = tuple(c for c in t.columns if c.type != "RECORD")[:25]

        dropped.extend(
            f"{t.fqn}.{c.name}" for c in t.columns if c not in keep
        )
        kept_tables.append(
            Table(project=t.project, dataset=t.dataset, name=t.name,
                  columns=keep, num_rows=t.num_rows)
        )

    return SchemaContext(
        tables=tuple(kept_tables), strategy=strategy, dropped_columns=tuple(dropped)
    )

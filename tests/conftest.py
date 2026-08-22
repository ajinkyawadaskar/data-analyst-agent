"""Test fixtures.

Schema comes from a snapshot of REAL BigQuery introspection
(tests/schema_snapshot.json) so tests run offline but validate against
genuine column names -- including GA's four-level nested paths.
Regenerate with: python -m tools.snapshot_schema
"""

import json
from pathlib import Path

import pytest

from src.schema import Column, SchemaContext, Table

SNAPSHOT = Path(__file__).parent / "schema_snapshot.json"


@pytest.fixture(scope="session")
def schema() -> SchemaContext:
    raw = json.loads(SNAPSHOT.read_text())
    tables = tuple(
        Table(
            project=t["project"], dataset=t["dataset"], name=t["name"],
            num_rows=t["num_rows"],
            columns=tuple(Column(**c) for c in t["columns"]),
        )
        for t in raw["tables"]
    )
    return SchemaContext(tables=tables)

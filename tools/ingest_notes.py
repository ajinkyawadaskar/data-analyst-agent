"""Embed data/synthetic_notes.jsonl and load it into a local LanceDB table.

WHY LANCEDB
------------
An embedded vector database (no separate service to run, same operational
argument as SQLite over Redis in src/cache/session_store.py) that stores
data as a single directory of files -- easy to inspect, easy to wipe and
re-ingest, easy to ship inside a container.

EMBEDDING MODEL
-----------------
models/gemini-embedding-001 (3072 dimensions), via the same Google API key
already configured for chat -- confirmed live by listing models and
checking `embedContent` is a supported action, per the project's standing
rule to never write a model name from memory (src/config.py's own comment
on this exact failure mode). The originally-planned "text-embedding-004"
name from the roadmap doc no longer exists on this API version; probing
first is what caught that before it became a 404 at ingest time.

This draws on a separate quota pool from chat generation (confirmed: no
embedding calls show up against the flash-lite chat budget), so ingesting
180 notes costs nothing against the budget the rest of this build is
tracking.

WHAT GETS STORED
-------------------
One row per note: note_id, user_id, created_at, category, text, and the
embedding vector. `synthetic: true` and the note text are both kept
alongside the vector so a retrieval result can be attributed straight back
to a note_id without a second lookup -- see src/synthesis.py's requirement
that every claim traces to a note_id or an audit-envelope field.

USAGE
------
    python -m tools.ingest_notes
Wipes and rebuilds the table each run -- idempotent, not incremental. Fine
at this corpus size (180 rows); would need to change for a corpus large
enough that re-embedding everything on every ingest was itself expensive.
"""

from __future__ import annotations

import json
from pathlib import Path

import lancedb

from src.config import get_settings

NOTES_PATH = Path(__file__).resolve().parents[1] / "data" / "synthetic_notes.jsonl"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "lancedb"
TABLE_NAME = "support_notes"
EMBEDDING_MODEL = "models/gemini-embedding-001"


def _load_notes() -> list[dict]:
    lines = NOTES_PATH.read_text().splitlines()
    header = json.loads(lines[0])
    if not header.get("synthetic"):
        raise ValueError(
            f"{NOTES_PATH} does not start with the expected synthetic-data "
            "header -- refusing to ingest a file that isn't clearly labeled."
        )
    return [json.loads(line) for line in lines[1:]]


# The free tier's embed-content quota is metered per EMBEDDED ITEM, not per
# HTTP call -- embed_documents() batches up to 100 texts into one request
# client-side, but 180 items still trips "100 embeddings/minute" server-side
# (confirmed live: a single embed_documents(180 texts) call hit a 429 after
# ~100 items with a 48s retry-delay). So this chunks below the limit and
# sleeps a full minute between chunks, rather than assuming batching alone
# is enough headroom.
_CHUNK_SIZE = 90
_CHUNK_PAUSE_SECONDS = 65


def _embed_all(texts: list[str]) -> list[list[float]]:
    import time

    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    get_settings()  # exports GOOGLE_API_KEY to the environment, as elsewhere
    embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _CHUNK_SIZE):
        chunk = texts[start : start + _CHUNK_SIZE]
        print(f"  embedding {start + 1}-{start + len(chunk)} of {len(texts)}...")
        vectors.extend(embedder.embed_documents(chunk))
        if start + _CHUNK_SIZE < len(texts):
            time.sleep(_CHUNK_PAUSE_SECONDS)
    return vectors


def main() -> None:
    notes = _load_notes()
    print(f"Loaded {len(notes)} synthetic notes from {NOTES_PATH}")

    texts = [n["text"] for n in notes]
    vectors = _embed_all(texts)
    print(f"Embedded {len(vectors)} notes with {EMBEDDING_MODEL} "
          f"({len(vectors[0])} dimensions)")

    rows = [
        {
            "note_id": n["note_id"],
            "user_id": n["user_id"],
            "created_at": n["created_at"],
            "category": n["category"],
            "text": n["text"],
            "synthetic": n["synthetic"],
            "vector": vec,
        }
        for n, vec in zip(notes, vectors)
    ]

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(DB_PATH))
    if TABLE_NAME in db.list_tables():
        db.drop_table(TABLE_NAME)
    table = db.create_table(TABLE_NAME, data=rows)
    print(f"Wrote {table.count_rows()} rows to LanceDB table "
          f"'{TABLE_NAME}' at {DB_PATH}")


if __name__ == "__main__":
    main()

"""Semantic search over the LanceDB support-notes table built by
tools/ingest_notes.py.

Entry point: retrieve(query, top_k=5) -> list[RetrievedNote]

Embeds the query with the same model used at ingest time
(models/gemini-embedding-001) and returns LanceDB's nearest neighbors by
vector distance. Each result carries the note_id and full text, not just a
similarity score -- src/synthesis.py needs to attribute every claim in a
stacked answer back to a specific note_id, so the caller should never have
to re-look-up a note by anything other than what this function already
returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import lancedb

from src.config import get_settings

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "lancedb"
TABLE_NAME = "support_notes"
EMBEDDING_MODEL = "models/gemini-embedding-001"


@dataclass(frozen=True)
class RetrievedNote:
    note_id: str
    user_id: int
    created_at: str
    category: str
    text: str
    distance: float


def _embed_query(text: str) -> list[float]:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    get_settings()
    embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)
    return embedder.embed_query(text)


def retrieve(query: str, top_k: int = 5) -> list[RetrievedNote]:
    """Return the `top_k` notes whose embedding is closest to `query`'s.

    Raises:
        FileNotFoundError: the LanceDB table doesn't exist yet -- run
            `python -m tools.ingest_notes` first.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No LanceDB database at {DB_PATH}. Run "
            "`python -m tools.ingest_notes` first."
        )
    db = lancedb.connect(str(DB_PATH))
    table = db.open_table(TABLE_NAME)

    vector = _embed_query(query)
    results = table.search(vector).limit(top_k).to_list()

    return [
        RetrievedNote(
            note_id=r["note_id"],
            user_id=r["user_id"],
            created_at=r["created_at"],
            category=r["category"],
            text=r["text"],
            distance=r["_distance"],
        )
        for r in results
    ]


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "customers complaining about API latency"
    for note in retrieve(query):
        print(f"[{note.distance:.4f}] {note.note_id} (user {note.user_id}): {note.text}")

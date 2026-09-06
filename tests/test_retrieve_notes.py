"""Tests for tools/retrieve_notes.py and tools/generate_notes.py (owner:
Claude -- fully built, real assertions).

Offline: embeddings are hand-built 3-dimensional vectors (not real Gemini
output -- no network call, no quota spend) so the test only exercises
LanceDB's own nearest-neighbor search, not embedding quality.
"""

from __future__ import annotations

import json

import lancedb
import pytest


@pytest.fixture
def notes_table(tmp_path, monkeypatch):
    db = lancedb.connect(str(tmp_path / "lancedb"))
    rows = [
        {"note_id": "note-0001", "user_id": 1, "created_at": "2026-01-01",
         "category": "latency_complaint", "text": "latency issue", "synthetic": True,
         "vector": [1.0, 0.0, 0.0]},
        {"note_id": "note-0002", "user_id": 2, "created_at": "2026-01-02",
         "category": "billing", "text": "billing question", "synthetic": True,
         "vector": [0.0, 1.0, 0.0]},
    ]
    table = db.create_table("support_notes", data=rows)

    import tools.retrieve_notes as rn

    monkeypatch.setattr(rn, "DB_PATH", tmp_path / "lancedb")
    monkeypatch.setattr(rn, "_embed_query", lambda text: [1.0, 0.0, 0.0])
    return table


def test_retrieve_returns_the_nearest_note_first(notes_table):
    from tools.retrieve_notes import retrieve

    results = retrieve("anything -- embedding is stubbed", top_k=2)
    assert results[0].note_id == "note-0001"
    assert results[0].distance <= results[1].distance


def test_retrieved_note_carries_full_attribution(notes_table):
    from tools.retrieve_notes import retrieve

    results = retrieve("anything", top_k=1)
    note = results[0]
    assert note.note_id == "note-0001"
    assert note.user_id == 1
    assert note.category == "latency_complaint"


def test_missing_database_raises_a_clear_error(tmp_path, monkeypatch):
    import tools.retrieve_notes as rn

    monkeypatch.setattr(rn, "DB_PATH", tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError, match="ingest_notes"):
        rn.retrieve("anything")


# ---- generate_notes.py determinism (no BigQuery, no LLM, no network)

def test_generation_is_deterministic_across_calls():
    from tools.generate_notes import _generate

    a = _generate(seed=42, count=20)
    b = _generate(seed=42, count=20)
    assert a == b


def test_generated_user_ids_are_within_the_real_thelook_range():
    from tools.generate_notes import USER_ID_MAX, USER_ID_MIN, _generate

    notes = _generate(seed=42, count=50)
    assert all(USER_ID_MIN <= n["user_id"] <= USER_ID_MAX for n in notes)


def test_every_generated_note_is_marked_synthetic():
    from tools.generate_notes import _generate

    notes = _generate(seed=42, count=20)
    assert all(n["synthetic"] is True for n in notes)


def test_old_tier_and_new_tier_never_collide():
    """Regression test for the "moved from Growth to Growth" bug caught
    during Day 4 -- independent sampling let the two slots coincide."""
    from tools.generate_notes import _generate

    notes = _generate(seed=42, count=180)
    for n in notes:
        text = n["text"]
        for tier in ("Starter", "Growth", "Professional", "Enterprise"):
            assert f"to {tier}" not in text or f"from the {tier} plan to {tier}" not in text


def test_synthetic_notes_file_starts_with_a_disclosure_header():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "synthetic_notes.jsonl"
    if not path.exists():
        pytest.skip("data/synthetic_notes.jsonl not generated in this environment")
    header = json.loads(path.read_text().splitlines()[0])
    assert header["synthetic"] is True
    assert "generator" in header

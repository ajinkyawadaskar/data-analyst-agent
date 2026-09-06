"""Spec for merging retrieval + compiled results into one attributed answer
(src/synthesis.py).

Owner of the module under test: Ajinkya. Same discipline as elsewhere:
written before the body, xfail(raises=NotImplementedError), flips to XPASS
once real. The two decisions (template vs. LLM narration; how to handle a
user_id matched by multiple notes) are left open in the module docstring --
these tests hold regardless of which way either one lands, since they only
check the structural guarantee (every claim is covered by a citation) and
the empty-retrieval behavior, never the prose's exact wording.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.synthesis import synthesize

pytestmark = pytest.mark.xfail(
    raises=NotImplementedError,
    reason="src/synthesis.py is Ajinkya's to write",
    strict=False,
)


@dataclass
class _FakeNote:
    note_id: str
    user_id: int
    category: str
    text: str


def _sample_notes():
    return [
        _FakeNote("note-0069", 27628, "latency_complaint", "Enterprise account escalated a latency complaint..."),
        _FakeNote("note-0046", 13940, "latency_complaint", "Enterprise account escalated a latency complaint..."),
    ]


def _sample_query_result():
    return {"rows": [{"total_revenue": 1211.19}], "measure": "total_revenue"}


# ---- the property Layer 5's DoD names directly

def test_every_note_id_referenced_actually_came_from_the_retrieved_notes():
    notes = _sample_notes()
    result = synthesize("why are these accounts complaining", notes, _sample_query_result())
    valid_ids = {n.note_id for n in notes}
    assert set(result.cited_note_ids) <= valid_ids
    assert len(result.cited_note_ids) > 0, "a stacking answer citing zero notes is unattributed"


def test_cited_query_fields_are_non_empty_when_a_numeric_claim_is_made():
    notes = _sample_notes()
    result = synthesize("why are these accounts complaining", notes, _sample_query_result())
    if "1211" in result.answer or "revenue" in result.answer.lower():
        assert len(result.cited_query_fields) > 0


def test_matched_user_ids_come_from_the_notes_not_invented():
    notes = _sample_notes()
    result = synthesize("why are these accounts complaining", notes, _sample_query_result())
    assert set(result.matched_user_ids) <= {n.user_id for n in notes}


# ---- empty retrieval is a different answer than a confident one

def test_empty_notes_produces_an_explicit_no_match_answer_not_a_silent_structured_only_one():
    result = synthesize("why are these accounts complaining", [], _sample_query_result())
    assert not result.cited_note_ids
    lowered = result.answer.lower()
    assert "no" in lowered or "not found" in lowered or "no matching" in lowered

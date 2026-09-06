"""
WHY THIS MODULE EXISTS
------------------------
A stacking answer merges two things that were computed independently and
know nothing about each other: a handful of retrieved support notes
(tools/retrieve_notes.py -- prose, approximate, ranked by embedding
distance) and a compiled query result (src/compiler/intent_compiler.py via
graph_semantic.py or mcp_server/tools.py -- exact numbers, scoped to
specific user_ids). This module is the one place those two get combined
into a single answer a human reads.

The failure mode this exists to prevent is specific: an LLM asked to
"summarize these notes and numbers into an answer" will, left unconstrained,
smooth over the seams and assert things neither input actually said --
a plausible-sounding sentence with no note_id or number behind it. Layer
5's Definition of Done names this directly: "every claim attributed to
either a note_id or a field in the audit envelope. Unattributed sentences
in the answer are the failure mode to test for." This module's job is to
make that structurally hard to violate, not just to ask nicely for
citations in a prompt.

CONTRACT
--------
    synthesize(question, notes, query_result) -> SynthesisResult

Args, conceptually:
  - notes: list[RetrievedNote] from tools/retrieve_notes.py -- the
    qualitative half. Each has a note_id, user_id, category, and text.
  - query_result: whatever src/graph_semantic.py's state or
    mcp_server/tools.py's QueryResult produced for the structured half --
    rows, the compiled SQL, proven_join_path, semantic_model_version.

Steps:

1. GROUND EVERY FACTUAL CLAIM IN A SOURCE
   Each sentence of the final answer must be traceable to either a specific
   note_id (for a qualitative claim -- "several accounts cited latency
   complaints [note-0031, note-0104]") or a field already present in
   query_result (for a numeric claim -- "combined revenue: $X, per the
   compiled query"). A sentence citing neither is the failure mode.

2. DO NOT LET THE LLM INVENT THE JOIN
   The connection between "these notes" and "these rows" is user_id, and
   that join already happened structurally (the router/caller extracted
   user_ids from the retrieved notes and scoped the compiled query to
   them) -- this module should not ask an LLM to infer or re-derive which
   note belongs to which row. If an LLM call is used at all here, its job
   is narration of already-joined facts, not the joining itself.

3. RETURN THE ATTRIBUTION ALONGSIDE THE PROSE, NOT JUST EMBEDDED IN IT
   SynthesisResult (below) carries `cited_note_ids` and `cited_query_fields`
   as structured lists in addition to the prose `answer`, so a caller (or a
   test) can mechanically verify every citation the prose claims to make
   actually corresponds to a note/field that was actually retrieved/
   computed -- rather than trusting free text to have gotten its own
   footnotes right.

4. AN EMPTY RETRIEVAL RESULT IS A DIFFERENT ANSWER THAN A LOW-CONFIDENCE ONE
   If tools/retrieve_notes.py returns nothing relevant, say so plainly
   ("no matching support notes found") rather than answering from the
   structured half alone and letting the reader assume the qualitative
   question was addressed.

DECISIONS TO MAKE BEFORE WRITING THE BODY:

  A. TEMPLATE-BASED SYNTHESIS, OR AN LLM NARRATION PASS OVER ALREADY-JOINED
     FACTS?
     A template ("N accounts matched [note ids]; their combined <measure> is
     <value>") is fully deterministic and trivially auditable but reads
     stiffly. An LLM pass that narrates already-joined, already-cited facts
     (never asked to invent the join itself, per step 2) can read naturally
     while still being checkable via step 3's structured citation lists.
     Pick one and say why -- and if you pick the LLM path, decide how a
     response that drops a citation or adds a claim beyond the given facts
     gets caught (re-validate the output against cited_note_ids/
     cited_query_fields before returning it, rather than trusting it).

  B. WHAT DEFINES "MATCHING" WHEN MULTIPLE NOTES INVOLVE THE SAME user_id?
     Two retrieved notes might name the same customer. Decide whether the
     synthesized answer cites both, the most recent, or all of them
     collapsed into one citation -- and whether query_result's rows (scoped
     to the union of retrieved user_ids) get double-counted if a user_id
     appears via more than one note.

WHAT THIS MODULE MUST NOT DO
------------------------------
- No sentence in `answer` without a corresponding entry in
  `cited_note_ids` or `cited_query_fields` -- see step 3's whole reason for
  existing as a structured, checkable list rather than trusting the prose.
- No re-deriving the note-to-row join here -- that already happened before
  this module was called (see step 2).
- No silently answering only the structured half when retrieval came back
  empty (see step 4) -- that is a partial answer presented as a complete one.

Owner: Ajinkya. Scaffolding only below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SynthesisResult:
    """The merged, attributed answer.

    `cited_note_ids` and `cited_query_fields` exist so a caller (or a test)
    can mechanically check that every claim in `answer` traces to something
    real, rather than trusting the prose to have cited itself correctly --
    see the module docstring's step 3.
    """

    answer: str
    cited_note_ids: tuple[str, ...] = ()
    cited_query_fields: tuple[str, ...] = field(default_factory=tuple)
    matched_user_ids: tuple[int, ...] = ()


def synthesize(
    question: str,
    notes: list[Any],
    query_result: Any,
) -> SynthesisResult:
    """Merge retrieved support notes with a compiled query result into one
    attributed answer.

    Args:
        question: the original plain-English question.
        notes: RetrievedNote objects from tools/retrieve_notes.py --
            possibly empty, meaning nothing matched (see step 4).
        query_result: the structured result already computed for the
            user_ids these notes named -- shape depends on which pipeline
            produced it (graph_semantic.py's state dict, or
            mcp_server/tools.py's QueryResult); read defensively rather
            than assuming one exact type.

    Returns:
        SynthesisResult whose `answer` is fully covered by
        `cited_note_ids` / `cited_query_fields` -- see the module docstring's
        "WHAT THIS MODULE MUST NOT DO."

    See the module docstring for the full contract and the two decisions
    (A, B) that have to be made before this can be written.
    """
    raise NotImplementedError("TODO: Ajinkya writes this")

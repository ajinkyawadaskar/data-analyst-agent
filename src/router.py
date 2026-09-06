"""
WHY THIS MODULE EXISTS
------------------------
RAG (tools/retrieve_notes.py) is probabilistic and reads prose; it cannot
compute an exact number. The semantic compiler (Layers 1-4) is deterministic
and computes exact numbers; it cannot read a support note. Neither alone can
answer "why are our highest-usage accounts complaining about latency, and
what's their combined revenue" -- that needs both, in a specific order:
find WHO is complaining (retrieval), then look up WHAT they're worth
(compilation). This module is the first decision in that pipeline: given a
question, which of the two tracks does it need, or does it need both?

Three outcomes, not two -- most questions are NOT stacking questions, and
routing a purely structured question ("total revenue by category") through
retrieval first would be pure waste, exactly the kind of thing that makes a
demo look like it's forcing every question through the most complex path
available instead of the cheapest one that actually answers it.

CONTRACT
--------
    classify(question) -> Route

Route is one of:
  - "structured" -- answerable by the semantic gateway alone (Layers 1-4).
      The existing majority case: "total revenue by category", "conversion
      rate by traffic source". Routes through src/graph_semantic.py or
      mcp_server/tools.py exactly as today -- this module does not change
      what happens once "structured" is chosen, only that something now
      chooses it explicitly instead of it being the only option.
  - "unstructured" -- answerable from tools/retrieve_notes.py alone. A
      question entirely about note content with no numeric ask: "what are
      customers saying about onboarding", "any complaints about SSO".
  - "stack" -- needs both, in order: retrieve notes matching the qualitative
      part of the question, extract the user_ids they mention, then compile
      a structured query scoped to those specific users. "Why are our
      highest-usage accounts complaining about latency, and what do they
      pay us" is the canonical example -- Day 4's DoD question.

DECISIONS TO MAKE BEFORE WRITING THE BODY:

  A. RULE-BASED KEYWORD CLASSIFICATION, OR AN LLM CALL?
     A keyword/heuristic classifier (e.g. presence of a certified measure
     name from semantic_model.yaml's glossary vs. presence of qualitative
     language like "complain", "feedback", "mention") is free, instant, and
     fully auditable -- you can point at the exact rule that fired. An LLM
     call handles phrasing the heuristic would miss but costs quota, adds
     latency, and is one more place a malformed response has to be handled.
     Given this project's quota constraints elsewhere (see NOTES.md), a
     cheap first-pass heuristic with a documented fallback (e.g. "unclear ->
     structured, since that's the cheaper failure mode") is worth
     considering before reaching for another LLM call -- but the tradeoff is
     yours to make and defend.

  B. WHAT HAPPENS WHEN THE ROUTE IS WRONG?
     A structured question misrouted to "stack" wastes a retrieval call and
     an embedding call but still produces a correct number eventually. An
     actual stacking question misrouted to "structured" silently drops the
     qualitative half of the question and answers only the numeric part --
     the wrong-but-plausible-looking failure mode this whole layer exists to
     avoid elsewhere (see LEARNING.md Concept 22). Decide whether "stack" is
     the safer default when a question is ambiguous between "structured" and
     "stack" (over-including retrieval costs latency; under-including it
     produces an incomplete answer that looks complete).

WHAT THIS MODULE MUST NOT DO
------------------------------
- No silent fallback to "structured" on classifier failure/exception without
  logging it -- a misrouted stacking question is a silent, wrong-looking-
  right answer, not a clean refusal like Layers 1/2 produce.
- No hardcoded list of "stacking questions" -- the classifier must generalize
  from the question's shape, not from a lookup table of examples that will
  not include the next question asked.

Owner: Ajinkya. Scaffolding only below.
"""

from __future__ import annotations

from typing import Literal

Route = Literal["structured", "unstructured", "stack"]


def classify(question: str) -> Route:
    """Decide which pipeline(s) `question` needs.

    Args:
        question: a plain-English question, exactly as it would arrive at
            src/api.py's /ask or mcp_server/tools.py's query_semantic_metric.

    Returns:
        "structured": answerable by the semantic gateway alone.
        "unstructured": answerable by note retrieval alone.
        "stack": needs retrieval to find WHO, then compilation to find WHAT
            about them -- see src/synthesis.py for how the two get merged.

    See the module docstring for the full contract and the two decisions
    (A, B) that have to be made before this can be written.
    """
    raise NotImplementedError("TODO: Ajinkya writes this")

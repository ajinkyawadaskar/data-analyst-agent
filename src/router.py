"""
WHAT THIS FILE ACTUALLY DOES, IN PLAIN LANGUAGE
------------------------------------------------
Some questions are answered with a lookup ("what's total revenue?"). Some
can only be answered by reading notes ("what are customers complaining
about?"). Some need both, in order: first find WHO fits a fuzzy, human
description ("our highest-usage accounts complaining about latency"), then
run a precise lookup on exactly those people ("what's their combined
revenue?").

This module is the traffic cop that decides, before any real work happens,
which of those three lanes a question belongs in. Skipping this and always
running the most complex path would waste time/money reading notes for
purely numeric questions. Skipping the qualitative check entirely would mean
a genuinely two-part question silently gets answered as if it only had one
part -- a real, correct-looking number that quietly ignored half of what was
actually asked. That second failure mode -- confidently wrong-shaped, not
crashed -- is the specific thing this layer exists to prevent.

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

DECISIONS MADE (see the module docstring's original DECISIONS TO MAKE section)
-------------------------------------------------------------------------------

  A. RULE-BASED KEYWORD CLASSIFICATION, OR AN LLM CALL?
     Rule-based. Free, instant, and fully auditable -- every classification
     can be traced to the exact pattern that fired, which matters
     specifically because this layer's job is "don't silently misroute."
     An LLM call would handle unusual phrasing better but adds quota cost,
     latency, and a new malformed-response case to handle, for a decision
     that a shape-based heuristic gets right in the large majority of real
     questions. When NEITHER a structured-shaped nor an unstructured-shaped
     signal fires at all, the fallback is "structured" -- the cheaper wrong
     guess: it still returns a real, useful number, it just risks missing a
     qualitative half that genuinely wasn't detectable from the question's
     shape.

  B. WHAT HAPPENS WHEN THE ROUTE IS WRONG?
     The two wrong-guess directions are NOT symmetric, so the logic below is
     deliberately asymmetric too:
       - Guessing "stack" when "structured" alone would've worked costs one
         extra retrieval + embedding call, but still ends in a correct
         number.
       - Guessing "structured" when the question actually needed "stack"
         silently drops the qualitative half and returns a confidently
         wrong-shaped answer -- the exact failure mode this layer exists to
         prevent (see LEARNING.md Concept 22).
     Because under-including retrieval is the dangerous direction, the
     combination rule is low-threshold on purpose: ANY structured-shaped
     signal plus ANY unstructured-shaped signal routes to "stack", rather
     than requiring strong confidence in both halves before combining them.

WHAT THIS MODULE MUST NOT DO
------------------------------
- No silent fallback to "structured" on classifier failure/exception without
  logging it -- a misrouted stacking question is a silent, wrong-looking-
  right answer, not a clean refusal like Layers 1/2 produce.
- No hardcoded list of "stacking questions" -- the classifier must generalize
  from the question's shape, not from a lookup table of examples that will
  not include the next question asked. The patterns below match GENERIC
  question shapes (an aggregation ask, a note/feedback reference) rather
  than specific example questions, so they generalize to new metric names
  and new phrasings without being updated per question.

Owner: Ajinkya.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

Route = Literal["structured", "unstructured", "stack"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern sets. These describe the SHAPE of a question, not specific
# examples of one -- see "WHAT THIS MODULE MUST NOT DO" above. Extend by
# adding a shape (e.g. a new generic aggregation phrasing), never by adding
# a specific past question.
# ---------------------------------------------------------------------------

# Signals that the question is asking for a computed/aggregate number --
# generic aggregation verbs and metric-shaped nouns, not any one certified
# measure name (this module has no access to semantic_model.yaml's glossary,
# nor should it need updating every time a new measure is added there).
_STRUCTURED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhow (much|many)\b",
        r"\btotal\w*\b",
        r"\baverag\w*\b|\bavg\b",
        r"\bsum\w*\b",
        r"\bcount\w*\b",
        r"\brate\w*\b",
        r"\bpercent\w*\b",
        r"\brevenue\w*\b|\bcost\w*\b|\bspend\w*\b",
        r"\bpay\w*\b|\bworth\b|\bbill\w*\b",
        r"\bgrowth\b|\btrend\w*\b",
        r"\btop\s+\d+\b",
        r"\bby\s+[a-z][\w-]*\b",  # "... by region", "... by month": a group-by shape
        r"\b(mrr|arr|ltv|cac|churn)\b",
    )
)

# Signals that the question is asking about note/feedback content -- what
# people said, felt, or reported, as opposed to a number.
_UNSTRUCTURED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcomplain\w*\b",
        r"\bfeedback\w*\b",
        r"\bmention\w*\b",
        r"\bsay(ing|s)?\b",
        r"\bsentiment\w*\b",
        r"\bnotes?\b",
        r"\breview\w*\b",
        r"\bticket\w*\b",
        r"\bissue\w*\b",
        r"\bproblem\w*\b",
        r"\b(un)?happ\w*\b|\bdissatisf\w*\b|\bfrustrat\w*\b",
    )
)


def _matches_any(patterns: tuple[re.Pattern, ...], question: str) -> bool:
    return any(pattern.search(question) for pattern in patterns)


def _classify_uncached(question: str) -> Route:
    """The actual rule evaluation, separated out so `classify` can wrap it
    in the try/except that guarantees a logged fallback on failure (see
    WHAT THIS MODULE MUST NOT DO)."""
    has_structured_signal = _matches_any(_STRUCTURED_PATTERNS, question)
    has_unstructured_signal = _matches_any(_UNSTRUCTURED_PATTERNS, question)

    if has_structured_signal and has_unstructured_signal:
        # DECISION B: low-threshold combination -- any sign of both shapes
        # routes to stack rather than requiring strong confidence in each.
        return "stack"
    if has_unstructured_signal:
        return "unstructured"
    if has_structured_signal:
        return "structured"

    # Neither shape fired at all -- genuinely ambiguous. Logged so this
    # default is auditable rather than a silent guess (DECISION A).
    logger.info(
        "classify(): no structured or unstructured signal matched; "
        "defaulting to 'structured' (cheaper failure mode). question=%r",
        question,
    )
    return "structured"


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
    try:
        return _classify_uncached(question)
    except Exception:
        # A classifier exception is not a clean refusal like Layers 1/2
        # produce -- it must never silently become "structured" without a
        # trace of what happened, per WHAT THIS MODULE MUST NOT DO.
        logger.exception(
            "classify() raised while routing question=%r; falling back to "
            "'structured'",
            question,
        )
        return "structured"
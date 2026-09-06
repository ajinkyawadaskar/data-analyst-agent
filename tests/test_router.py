"""Spec for the structured/unstructured/stack router (src/router.py).

Owner of the module under test: Ajinkya. Same discipline as
tests/test_compiler.py etc.: written before the body, xfail(raises=
NotImplementedError) so the suite stays green while it's a stub, flips to
XPASS the moment it lands. Remove the marker then.

classify()'s contract is a plain question -> Route mapping, so these tests
hold regardless of decision A (heuristic vs LLM) -- they only assert on the
input/output pairing, never on how the answer was reached.
"""

from __future__ import annotations

import pytest

from src.router import classify

pytestmark = pytest.mark.xfail(
    raises=NotImplementedError,
    reason="src/router.py is Ajinkya's to write",
    strict=False,
)


def test_pure_metric_question_routes_structured():
    assert classify("What is our total revenue by product category?") == "structured"


def test_pure_note_content_question_routes_unstructured():
    assert classify("What are customers saying about onboarding?") == "unstructured"


def test_the_day4_dod_question_routes_stack():
    """The canonical stacking question named in the plan's Day 4 DoD --
    needs WHO (retrieval) before WHAT (compilation)."""
    assert classify(
        "Why are our highest-usage accounts complaining about latency, "
        "and what do they pay us?"
    ) == "stack"


def test_a_question_naming_a_certified_measure_and_a_complaint_routes_stack():
    assert classify(
        "Which customers who mentioned billing complaints have the highest revenue?"
    ) == "stack"


def test_return_type_is_always_one_of_the_three_literals():
    result = classify("How many orders were placed last month?")
    assert result in ("structured", "unstructured", "stack")

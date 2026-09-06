"""Offline routing tests for src/stacking.py (owner: Claude).

Mocks router.classify, retrieval, and synthesis so this tests DISPATCH --
does "structured" skip retrieval entirely, does "unstructured" skip the
compiler entirely, does an empty retrieval short-circuit before ever
calling the LLM again -- not answer quality, which the live demonstrations
in LEARNING.md already cover against real BigQuery/Gemini.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _stub_graph(monkeypatch):
    """Never build the real semantic gateway graph in these tests."""
    class _FakeGraph:
        def invoke(self, state):
            return {**state, "explanation": "fake structured answer"}

    monkeypatch.setattr(
        "src.graph_semantic.build_graph", lambda **kw: _FakeGraph()
    )


def test_structured_route_never_calls_retrieval(monkeypatch):
    from src import stacking

    monkeypatch.setattr(stacking, "classify", lambda q: "structured")
    called = {"retrieve": False}
    monkeypatch.setattr(
        stacking, "retrieve", lambda *a, **kw: called.update(retrieve=True) or []
    )

    result = stacking.answer("total revenue by category")
    assert result["route"] == "structured"
    assert called["retrieve"] is False


def test_unstructured_route_never_touches_the_compiler(monkeypatch):
    from src import stacking

    fake_note = SimpleNamespace(note_id="n1", user_id=1, category="onboarding", text="t")
    monkeypatch.setattr(stacking, "classify", lambda q: "unstructured")
    monkeypatch.setattr(stacking, "retrieve", lambda q, top_k=5: [fake_note])

    compiled_called = {"value": False}
    monkeypatch.setattr(
        stacking, "compile_intent", lambda *a, **kw: compiled_called.update(value=True)
    )

    result = stacking.answer("what are customers saying about onboarding")
    assert result["route"] == "unstructured"
    assert compiled_called["value"] is False
    assert result["cited_note_ids"] == ("n1",)


def test_empty_retrieval_on_stack_route_short_circuits_before_llm_call(monkeypatch):
    """An empty retrieval result must not proceed to a second LLM call for
    intent extraction -- there is nothing to scope a compiled query to."""
    from src import stacking

    monkeypatch.setattr(stacking, "classify", lambda q: "stack")
    monkeypatch.setattr(stacking, "retrieve", lambda q, top_k=5: [])

    def _fail_if_called(*a, **kw):
        raise AssertionError("_build_llm should not be called when retrieval is empty")

    monkeypatch.setattr(stacking, "_build_llm", _fail_if_called)

    result = stacking.answer("why are accounts complaining about latency")
    assert result["route"] == "stack"
    assert result["cited_note_ids"] == ()


def test_on_behalf_of_is_threaded_into_the_structured_route(monkeypatch):
    from src import stacking
    from src.models import OnBehalfOf

    monkeypatch.setattr(stacking, "classify", lambda q: "structured")

    captured_state = {}

    class _CapturingGraph:
        def invoke(self, state):
            captured_state.update(state)
            return {**state, "explanation": "ok"}

    monkeypatch.setattr("src.graph_semantic.build_graph", lambda **kw: _CapturingGraph())

    stacking.answer("total revenue", on_behalf_of=OnBehalfOf(tenant_id="t1", region="AMER"))
    assert captured_state.get("principal") == {"tenant_id": "t1", "region": "AMER"}

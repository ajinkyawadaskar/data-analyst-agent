"""Spec for src/models.py::build_audit_envelope (owner: Ajinkya).

The two decisions (audit_id generation; what "not applicable to this
route" looks like per field) are documented in AuditEnvelope's own
docstring. These tests check the structural guarantee (every field
present, applicable fields never None) rather than re-litigating either
decision.
"""

from __future__ import annotations

import time

from src.models import build_audit_envelope


def _structured_result():
    return {
        "route_taken": "compiled",
        "explanation": "answer text",
        "compiled_sql": "SELECT 1",
        "proven_join_path": ["order_items.product_id = products.id"],
        "semantic_model_version": "0.1.0",
        "cache_hit": False,
    }


def _stack_result():
    return {
        "route": "stack",
        "answer": "answer text",
        "sql": "SELECT 1",
        "proven_join_path": [],
        "semantic_model_version": "0.1.0",
        "cited_note_ids": ("note-0001", "note-0002"),
    }


def test_audit_id_is_always_present_and_non_empty():
    env = build_audit_envelope(_structured_result(), started_at=time.time())
    assert env.audit_id


def test_latency_ms_reflects_elapsed_time_since_started_at():
    started = time.time() - 0.05  # 50ms ago
    env = build_audit_envelope(_structured_result(), started_at=started)
    assert env.latency_ms >= 40  # allow scheduling slack, must not be ~0


def test_structured_result_populates_compiled_sql_and_join_path():
    env = build_audit_envelope(_structured_result(), started_at=time.time())
    assert env.compiled_sql == "SELECT 1"
    assert env.proven_join_path == ["order_items.product_id = products.id"]


def test_stack_result_populates_retrieved_note_ids():
    env = build_audit_envelope(_stack_result(), started_at=time.time())
    assert env.retrieved_note_ids is not None
    assert set(env.retrieved_note_ids) == {"note-0001", "note-0002"}


def test_caller_supplied_audit_id_is_used_verbatim():
    env = build_audit_envelope(
        _structured_result(), started_at=time.time(), audit_id="fixed-id-123"
    )
    assert env.audit_id == "fixed-id-123"

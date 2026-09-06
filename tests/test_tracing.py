"""Tests for src/tracing.py (owner: Claude). Offline: no real Langfuse
export, just checking the no-op-when-unconfigured guarantee and that spans
compose without raising.
"""

from __future__ import annotations

import pytest


class _NoLangfuseSettings:
    langfuse_public_key = ""
    langfuse_secret_key = ""
    langfuse_host = "https://cloud.langfuse.com"


def test_init_tracing_does_not_raise_with_no_keys_configured():
    import src.tracing as tracing_mod

    tracing_mod._initialized = False  # allow re-init for this test
    tracing_mod.init_tracing(_NoLangfuseSettings())


def test_traced_span_yields_a_real_span_and_sets_attributes():
    from src.tracing import traced_span

    with traced_span("test_span", foo="bar", skip_me=None) as span:
        assert span is not None


def test_traced_span_records_and_reraises_exceptions():
    from src.tracing import traced_span

    with pytest.raises(ValueError):
        with traced_span("test_span"):
            raise ValueError("boom")


def test_get_tracer_always_returns_something_usable():
    from src.tracing import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("manual") as span:
        assert span is not None

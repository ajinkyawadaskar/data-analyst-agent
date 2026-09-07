"""Wiring tests for the MCP server (mcp_server/server.py, mcp_server/tools.py).

Offline: uses the disk-cached schema (tests/../schema_cache.json), no live
BigQuery call, no LLM. Drives the actual MCP wire protocol via
fastmcp.Client rather than calling server functions directly -- discoverable-
over-the-protocol is the actual claim being tested, not just "the Python
function exists and runs."
"""

from __future__ import annotations

import json

import pytest

from mcp_server.server import mcp


@pytest.mark.asyncio
async def test_the_one_tool_is_discoverable():
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = [t.name for t in tools]
    assert names == ["query_semantic_metric"], (
        "the server exposes exactly one tool -- see server.py's module "
        "docstring on tool economics"
    )


@pytest.mark.asyncio
async def test_glossary_resource_is_discoverable_and_reads_real_content():
    from fastmcp import Client

    async with Client(mcp) as client:
        resources = await client.list_resources()
        assert str(resources[0].uri) == "semantic://metrics_glossary"

        result = await client.read_resource("semantic://metrics_glossary")
        glossary = json.loads(result[0].text)

    assert "total_revenue" in {m["name"] for m in glossary["measures"]}
    assert "version" in glossary
    assert "dialect" in glossary


@pytest.mark.asyncio
async def test_malformed_intent_becomes_a_structured_tool_error_not_a_crash(monkeypatch):
    """Now that mcp_server/tools.py is real, this exercises the actual
    retry-then-refuse path -- a stubbed LLM that never returns parseable
    JSON -- rather than calling the real LLM/BigQuery (which would burn
    quota on every test run and, worse, "pass" for the wrong reason: an
    earlier version of this test kept passing after the tool stopped being
    a stub because a missing BigQuery credential raised its own ToolError
    downstream, not because anything about intent extraction was verified)."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    class _StubResponse:
        text = "not JSON at all"

    class _StubLLM:
        def invoke(self, messages):
            return _StubResponse()

    class _StubSettings:
        max_retries = 1
        max_rows = 500
        semantic_model_path = "semantic_model.yaml"

    monkeypatch.setattr("src.graph._build_llm", lambda settings: _StubLLM())
    monkeypatch.setattr("src.config.get_settings", lambda: _StubSettings())

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="could not extract a valid intent"):
            await client.call_tool("query_semantic_metric", {"question": "revenue?"})

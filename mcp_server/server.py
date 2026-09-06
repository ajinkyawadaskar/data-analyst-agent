"""The Semantic Execution Gateway, exposed over MCP.

WHY MCP INSTEAD OF JUST THE REST API
-------------------------------------
src/api.py already exposes this system to a human client (curl, Streamlit).
MCP exposes it to an AGENT client instead -- Claude Code, Claude Desktop, or
any other MCP host -- via a standard protocol boundary rather than a
bespoke REST shape that agent would need custom glue code to call.

Two primitives, deliberately not more:

- semantic://metrics_glossary (a RESOURCE): read-only, no side effects. An
  agent reads this BEFORE deciding what to ask, the same way it would read a
  file. It is the certified list of measures/dimensions/joins from
  semantic_model.yaml -- so an agent calling this server can discover what
  is answerable without guessing or hallucinating a metric name.

- query_semantic_metric (the ONE tool): executes something. Per the tool-
  economics argument in practical-learning-journey.md Stage 4 -- exposing
  many narrow tools degrades an agent's tool-selection accuracy, so this
  server exposes exactly one, shaped like the gateway's own intent surface,
  rather than one tool per measure or one per SQL verb.

Owner: Claude, for this file. The tool's pipeline logic
(mcp_server/tools.py::query_semantic_metric) is Ajinkya's -- same division
as everywhere else in this build.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from src.config import get_settings
from src.schema import load_or_introspect
from src.semantic import model as semantic_model

from mcp_server.tools import register_tools

log = logging.getLogger(__name__)

mcp = FastMCP(
    name="semantic-execution-gateway",
    instructions=(
        "Read semantic://metrics_glossary first to see which measures, "
        "dimensions, and joins are certified before calling "
        "query_semantic_metric -- asking for something outside that list "
        "will be refused, by design (see the tool's own error message for "
        "what IS available)."
    ),
)


@mcp.resource(
    "semantic://metrics_glossary",
    name="Semantic model glossary",
    description=(
        "The certified measures, dimensions, and joins this gateway can "
        "compile a query against -- generated live from semantic_model.yaml, "
        "so it can never drift out of sync with what the compiler actually "
        "accepts. Read this before calling query_semantic_metric."
    ),
    mime_type="application/json",
)
def metrics_glossary() -> dict:
    """Serve the semantic model's own glossary() view.

    Loaded fresh (not cached at import time) so a schema/model change picked
    up by the process is reflected here without a server restart being the
    only way to see it -- this is a read, not a hot path, so the cost of not
    caching it is negligible.
    """
    schema = load_or_introspect()
    model = semantic_model.load(get_settings().semantic_model_path, schema_context=schema)
    return model.glossary()


register_tools(mcp)


if __name__ == "__main__":
    # stdio transport -- what Claude Code/Claude Desktop and most MCP hosts
    # expect for a locally-run server. Run with:
    #   python -m mcp_server.server
    mcp.run()

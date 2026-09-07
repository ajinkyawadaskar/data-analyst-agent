"""FastAPI surface: POST /ask, GET /health.

The agent graph (src/graph.py, owner: Ajinkya) is imported lazily so this
API boots and reports honest health even before the graph exists.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache

from fastapi import FastAPI, HTTPException

from src.config import get_settings
from src.models import AskRequest, AskResponse, HealthResponse

log = logging.getLogger(__name__)

app = FastAPI(
    title="Data Analyst Agent",
    description="Natural-language questions -> guardrailed, cost-bounded BigQuery SQL.",
    version="0.1.0",
)


@lru_cache(maxsize=2)
def _load_graph(use_gateway: bool = False):
    """Import and build the compiled agent graph, or None if not usable.

    Returns None for both "module absent" and "module is still a stub"
    (NotImplementedError). Health must never raise -- a 500 on /health
    during a deploy tells you nothing about what is actually wrong.

    MEMOIZED. This used to rebuild on every single request, and build_graph()
    introspects BigQuery (~10 API calls, ~30s), so every question re-read the
    whole schema first. graph.py's docstring claimed the build happened once;
    nothing enforced that, and it did not. Keyed on the flag so both paths can
    be cached independently when the eval harness runs them back to back.

    `use_gateway` is a parameter rather than a setting read inside this
    function on purpose: get_settings() is lru_cached, so reading it in here
    would latch the first value forever and make the flag look broken.
    """
    module = "src.graph_semantic" if use_gateway else "src.graph"
    try:
        build_graph = importlib.import_module(module).build_graph
    except ImportError:
        return None
    try:
        return build_graph()
    except NotImplementedError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("graph failed to build (%s)", module)
        return None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Report component readiness. Never raises — a 200 with degraded
    fields is more useful for debugging a deploy than a 500."""
    settings = get_settings()

    bq = "unconfigured"
    if settings.google_cloud_project:
        try:
            from src.bq_client import get_client

            get_client()
            bq = "ok"
        except Exception as exc:  # noqa: BLE001 - health must not raise
            bq = f"error: {type(exc).__name__}"

    llm = "ok" if settings.google_api_key else "unconfigured"
    gateway = bool(settings.use_semantic_gateway)
    agent = "ok" if _load_graph(gateway) is not None else "not_implemented"

    status = "ok" if bq == "ok" and llm == "ok" and agent == "ok" else "degraded"
    return HealthResponse(
        status=status, bigquery=bq, llm=llm, agent=agent,
        path="semantic_gateway" if gateway else "legacy",
    )


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    graph = _load_graph(bool(get_settings().use_semantic_gateway))
    if graph is None:
        raise HTTPException(
            status_code=503,
            detail="Agent graph not available (see /health for which component).",
        )

    initial_state = {"question": req.question}
    if req.on_behalf_of is not None:
        # Ignored entirely by the legacy graph (AgentState has no "principal"
        # key, and every legacy node only reads keys it knows about) -- this
        # is a no-op unless USE_SEMANTIC_GATEWAY is also on.
        initial_state["principal"] = req.on_behalf_of.model_dump()
    state = graph.invoke(initial_state)
    return AskResponse(
        question=req.question,
        sql=state.get("sql"),
        rows=state.get("rows"),
        row_count=len(state["rows"]) if state.get("rows") is not None else None,
        explanation=state.get("explanation"),
        guardrails=state["guardrails"],
        retries_used=state.get("retries_used", 0),
    )

"""FastAPI surface: POST /ask, GET /health.

The agent graph (src/graph.py, owner: Ajinkya) is imported lazily so this
API boots and reports honest health even before the graph exists.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from src.config import get_settings
from src.models import AskRequest, AskResponse, HealthResponse

log = logging.getLogger(__name__)

app = FastAPI(
    title="Data Analyst Agent",
    description="Natural-language questions -> guardrailed, cost-bounded BigQuery SQL.",
    version="0.1.0",
)


def _load_graph():
    """Import and build the compiled agent graph, or None if not usable.

    Returns None for both "module absent" and "module is still a stub"
    (NotImplementedError). Health must never raise -- a 500 on /health
    during a deploy tells you nothing about what is actually wrong.
    """
    try:
        from src.graph import build_graph  # owner: Ajinkya
    except ImportError:
        return None
    try:
        return build_graph()
    except NotImplementedError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("graph failed to build")
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

    llm = "ok" if settings.anthropic_api_key else "unconfigured"
    agent = "ok" if _load_graph() is not None else "not_implemented"

    status = "ok" if bq == "ok" and llm == "ok" and agent == "ok" else "degraded"
    return HealthResponse(status=status, bigquery=bq, llm=llm, agent=agent)


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    graph = _load_graph()
    if graph is None:
        raise HTTPException(
            status_code=503,
            detail="Agent graph not implemented yet (src/graph.py).",
        )

    state = graph.invoke({"question": req.question})
    return AskResponse(
        question=req.question,
        sql=state.get("sql"),
        rows=state.get("rows"),
        row_count=len(state["rows"]) if state.get("rows") is not None else None,
        explanation=state.get("explanation"),
        guardrails=state["guardrails"],
        retries_used=state.get("retries_used", 0),
    )

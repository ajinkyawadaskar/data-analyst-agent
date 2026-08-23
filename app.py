"""Streamlit demo.

Two modes, and the first one works before the agent does:

  Guardrail sandbox -- paste SQL, see every check run against it. No LLM,
                       no API key. This is the part worth demoing.
  Ask a question    -- the full agent. Needs src/graph.py and a Gemini key.
"""

from __future__ import annotations

import os

import streamlit as st

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")

st.set_page_config(page_title="Guardrailed Natural Language to SQL Agent", page_icon="🛡️", layout="wide")


@st.cache_resource(show_spinner="Loading schema...")
def load_schema():
    from src.schema import load_or_introspect
    return load_or_introspect()


def load_graph():
    try:
        from src.graph import build_graph
    except ImportError:
        return None
    try:
        return build_graph()
    except (NotImplementedError, Exception):
        return None


st.title("Guardrailed Natural Language to SQL Agent")
st.caption(
    "Plain-English questions to BigQuery SQL — with a layer in front that "
    "decides whether the generated query is allowed to run."
)

with st.sidebar:
    st.subheader("Corpus")
    try:
        schema = load_schema()
        st.metric("Tables", len(schema.tables))
        st.metric("Columns", sum(len(t.columns) for t in schema.tables))
        st.metric("Schema tokens sent to model", f"{schema.approx_tokens():,}")
        st.caption(
            "Naive introspection of the 366 GA daily shards would be "
            "**1,235,616 tokens** of duplicated schema. Collapsing them to a "
            "wildcard is a 315x reduction with nothing lost."
        )
        with st.expander("Tables"):
            for t in schema.tables:
                st.write(f"`{t.name}` — {len(t.columns)} cols")
    except Exception as exc:  # noqa: BLE001
        schema = None
        st.error(f"Schema unavailable: {type(exc).__name__}")

    st.divider()
    st.subheader("Limits")
    from src.config import get_settings
    s = get_settings()
    st.write(f"Cost ceiling: **{s.max_bytes_billed/1e9:.0f} GB**")
    st.write(f"Row cap: **{s.max_rows}**")
    st.write(f"Max retries: **{s.max_retries}**")

sandbox, agent = st.tabs(["Guardrail sandbox", "Ask a question"])

EXAMPLES = {
    "Legitimate — category revenue":
        "SELECT p.category, SUM(oi.sale_price) AS revenue\n"
        "FROM `bigquery-public-data.thelook_ecommerce.order_items` oi\n"
        "JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id\n"
        "GROUP BY p.category ORDER BY revenue DESC LIMIT 5",
    "Blocked — stacked DROP":
        "SELECT 1; DROP TABLE `bigquery-public-data.thelook_ecommerce.users`",
    "Blocked — DELETE hidden in a CTE":
        "WITH x AS (DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` "
        "WHERE order_id = 1 RETURNING order_id) SELECT * FROM x",
    "Blocked — table outside the allowlist":
        "SELECT name FROM `bigquery-public-data.usa_names.usa_1910_current` LIMIT 5",
    "Blocked — column that does not exist":
        "SELECT profit_margin FROM `bigquery-public-data.thelook_ecommerce.products` LIMIT 5",
    "Blocked — 5.8 GB unfiltered scan":
        "SELECT * FROM `bigquery-public-data.google_analytics_sample.ga_sessions_*`",
    "Free — COUNT(*) across all 366 shards":
        "SELECT COUNT(*) FROM `bigquery-public-data.google_analytics_sample.ga_sessions_*`",
}

with sandbox:
    st.write(
        "Every check below runs on the query text alone. No model is involved — "
        "that is the point. These rules hold whether the SQL came from an LLM, "
        "a user, or a bug."
    )
    choice = st.selectbox("Example", list(EXAMPLES))
    sql = st.text_area("SQL", EXAMPLES[choice], height=140)

    if st.button("Run guardrails", type="primary"):
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Static checks")
            try:
                from src.guardrails import check
                report = check(sql, schema)
                if report.passed:
                    st.success("Passed")
                else:
                    st.error("Blocked")
                for v in report.violations:
                    st.write(f"- {v}")
                st.caption("Checks run: " + ", ".join(report.checks_run))
            except NotImplementedError:
                st.info("guardrails.py not implemented yet")
            except Exception as exc:  # noqa: BLE001
                st.error(f"{type(exc).__name__}: {exc}")

        with col2:
            st.subheader("Cost ceiling")
            try:
                from src.cost_guard import check_cost
                v = check_cost(sql)
                gb = v.estimated_bytes_scanned / 1e9
                st.metric("Would scan", f"{gb:.3f} GB",
                          delta=f"ceiling {v.ceiling_bytes/1e9:.0f} GB",
                          delta_color="off")
                if v.passed:
                    st.success("Under ceiling")
                else:
                    st.error("Over ceiling — blocked")
                    if v.retry_hint:
                        st.caption(v.retry_hint)
            except NotImplementedError:
                st.info("cost_guard.py not implemented yet")
            except Exception as exc:  # noqa: BLE001
                st.warning(
                    f"Dry run failed ({type(exc).__name__}). Invalid SQL is "
                    "the parser's job, not the cost guard's."
                )

with agent:
    graph = load_graph()
    if graph is None:
        st.info(
            "The agent needs `src/graph.py` and a Gemini key. "
            "The guardrail sandbox works without both."
        )
    else:
        q = st.text_input("Question", "Which traffic sources drove the most sessions in August 2016?")
        if st.button("Ask", type="primary"):
            with st.spinner("Thinking..."):
                state = graph.invoke({"question": q})
            st.code(state.get("sql", ""), language="sql")
            g = state.get("guardrails")
            if g is not None and not g.passed:
                st.error("Blocked: " + "; ".join(g.violations))
            if state.get("rows"):
                st.dataframe(state["rows"])
            if state.get("explanation"):
                st.write(state["explanation"])
            st.caption(f"Retries used: {state.get('retries_used', 0)}")

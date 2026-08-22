# How it's wired

```mermaid
flowchart TD
    Q["Question<br/>(plain English)"] --> GEN

    subgraph startup["at startup, once"]
        INTRO["introspect BigQuery<br/>10 tables · 417 columns"] --> COMPACT["compact<br/>schema context"]
    end

    COMPACT -.->|schema in prompt| GEN
    GEN["generate SQL<br/>Gemini 3.6 Flash"] --> G1

    subgraph guard["guardrail layer — the point of the project"]
        G1["AST parse<br/>single statement, SELECT only"] --> G2
        G2["table allowlist"] --> G3
        G3["column validation<br/>against schema context"] --> G4
        G4["row limit"] --> C1
        C1["dry run<br/>bytes scanned vs ceiling"]
    end

    G1 -->|violation| RETRY
    G2 -->|violation| RETRY
    G3 -->|violation| RETRY
    C1 -->|over ceiling| RETRY

    RETRY{"retries left?<br/>max 2"}
    RETRY -->|yes, with violations| GEN
    RETRY -->|no| BLOCKED["blocked<br/>report why"]

    C1 -->|under ceiling| EXEC["execute on BigQuery<br/>row-capped"]
    EXEC --> EXPLAIN["explain rows<br/>in plain English"]
    EXPLAIN --> OUT["answer + SQL +<br/>guardrail report"]

    style guard fill:#fff4e6,stroke:#e8890c
    style BLOCKED fill:#ffe3e3,stroke:#c92a2a
    style OUT fill:#e6fcf5,stroke:#0ca678
```

## Why the order matters

**Parse before you spend.** The AST checks are free and local. The dry run is a
network call to BigQuery that costs latency and fails on invalid SQL. Running
guardrails first means a malformed query is caught by the parser with a useful
message, instead of coming back as a BigQuery 400 that says nothing about
which rule was broken.

**Nothing unguarded reaches BigQuery.** There is no path from `generate_sql`
to `execute` that skips the guard block. That is the entire premise — the
retry loop feeds back into generation, never forward into execution.

**Introspection happens once, not per question.** Reading the schema is ~10
API calls and the schema does not change between questions.

## The schema problem

`google_analytics_sample` stores one table per day — 366 of them, every one
with the same 338 columns. Introspecting them all produces **1,235,616
tokens** of almost entirely duplicated schema.

BigQuery already addresses those shards as one thing via a wildcard
(`ga_sessions_*`), so the fix is to introspect one shard and treat it as one
table. Full corpus drops to **3,921 tokens** — a 315x reduction, with nothing
lost, because the other 365 descriptions were copies.

Worth being precise about what that means: the first and largest win came from
noticing the schema was duplicated, not from a clever compaction rule.
Compaction is the second-order problem.

## Cost ceiling, measured

| query | bytes scanned | vs 1 GB ceiling |
|---|---|---|
| `SELECT *` across all 366 GA shards, no date filter | 5.767 GB | **blocked** |
| `SELECT *` GA, one month | 0.794 GB | passes |
| `SELECT COUNT(*)` across all 366 shards | 0.000 GB | passes |
| `SELECT *` thelook.events (2.4M rows) | 0.384 GB | passes |
| thelook category count | 0.00036 GB | passes |

`COUNT(*)` across every shard is free — BigQuery answers it from table
metadata. So "touches a lot of tables" and "expensive" are different things,
and a guard that counted shards instead of bytes would block a query that
costs nothing.

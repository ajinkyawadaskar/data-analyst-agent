# CLAUDE.md — Data Analyst Agent (NL → SQL)

## What this is
A LangGraph agent that turns plain-English questions into safe, cost-bounded
SQL against BigQuery, executes them, and explains the results. The point of
this project is **the guardrail layer, not the SQL generation.** Anyone can
get an LLM to write SQL. The engineering is what stops it running something
destructive or expensive.

## Owner
Ajinkya Wadaskar. Portfolio project P4. Shipping today.

---

## HARD SCOPE LOCK — do not exceed

**IN:**
- BigQuery public dataset as the corpus (no data loading required)
- Schema introspection → compact schema context
- LangGraph agent: question → SQL → execute → explain
- **Guardrail layer** (the core of the project):
  - AST parse; reject anything that is not a single SELECT
  - Table allowlist
  - Dry-run cost ceiling (bytes-scanned check before execution)
  - Row limit enforcement
  - Reject queries referencing columns outside the schema context
- Bounded self-correction: max 2 retries on execution error
- DeepEval harness: 25 labeled questions + 5 adversarial prompts
- FastAPI: `POST /ask`, `GET /health`
- Minimal Streamlit UI (30 min hard cap)
- Deployed public URL

**OUT:**
- Write operations of any kind, multi-database support, user auth,
  chat memory / multi-turn, chart generation, RAG over docs, semantic
  layer, query caching, Docker, dbt integration, and anything else not
  listed under IN.

If you think of a good addition: **DO NOT BUILD IT.** Add it to
`IDEAS.md` and keep going.

---

## Division of labor — CRITICAL

I must be able to defend this code in a technical interview. This
overrides speed, including if we are running late.

**YOU WRITE:**
- BigQuery client setup and auth wiring
- Schema introspection and compaction
- FastAPI scaffolding, Pydantic models, error handling
- Streamlit UI
- Deploy config
- Test boilerplate
- README prose (from my NOTES.md)
- Repo hygiene, `.gitignore`, `.env.example`

**I WRITE.** Leave a stub with a TODO and a docstring spec, then
**STOP and tell me**:

1. `src/guardrails.py` — AST parsing, SELECT-only enforcement,
   table allowlist, column validation
2. `src/cost_guard.py` — dry-run bytes-scanned check and ceiling logic
3. `src/graph.py` — LangGraph node wiring and the bounded retry loop
4. `evals/metrics.py` — execution-accuracy metric (result-set
   comparison, NOT SQL string match)

**Never fill in these four.** If I ask you to because we're behind
schedule, remind me why and refuse. These are the modules an
interviewer will spend fifteen minutes on.

---

## Key design decisions I need to be able to defend

Prompt me on each of these when we reach them; don't decide for me:

- **Why execution accuracy, not SQL string match** — two different
  queries can both be correct. The eval compares result sets.
- **Why retries are capped at 2** — past that it's usually a
  misunderstood schema, not a syntax error, and more retries just
  burn money.
- **Why AST parsing rather than regex or a prompt instruction** — a
  prompt reduces the rate of bad SQL; an AST check bounds it.
- **How the schema context is compacted** — real warehouses have
  schemas too large for a context window. How we choose what to
  include is a genuine production question.

Log my reasoning on each to `NOTES.md` as we go.

---

## Rules

- Build the eval set **before** the generation logic.
- No invented numbers. Use `___` placeholders until measured.
- 30-minute cap on UI. 2-hour cap on deployment — switch platforms
  rather than debugging past it.
- Read-only, always. There is no code path that can write.
- Log every dead end to `NOTES.md` **as it happens** — that becomes
  the "What Didn't Work" section.
- Commit after each working milestone with a real message.

---

## Working mode

- Two-line preamble before each step, then execute.
- After every step, run the code and show me **actual output**, never
  a description of what it should produce.
- Track elapsed time against the schedule and **tell me when we're
  slipping.** Do not quietly absorb delay.
- While I'm writing my four modules, work on whatever is unblocked.
  Don't idle.

---

## Schedule

| Elapsed | What |
|---|---|
| 0:00 | Repo skeleton, BigQuery auth, public dataset selected and queried |
| 0:45 | Schema introspection → compact context; show me the output |
| 1:15 | **STOP** → hand me `guardrails.py` stub + spec |
| 2:45 | **STOP** → hand me `cost_guard.py` stub + spec |
| 3:30 | **STOP** → hand me `graph.py` stub + spec |
| 4:30 | Generate 12 candidate eval questions for my review (I label 25 total + 5 adversarial) |
| 5:30 | **STOP** → hand me `evals/metrics.py` stub + spec |
| 6:15 | FastAPI + Pydantic + error handling |
| 6:45 | I run evals; we record real numbers |
| 7:15 | Deploy, confirm `/health` |
| 8:00 | Streamlit UI |
| 8:30 | Architecture diagram (Mermaid, in README) |
| 9:00 | README, ship checklist |

**Fallback if behind at hour 5:** cut the Streamlit UI and ship the API
with `curl` examples. A live API with real eval numbers and a working
guardrail layer beats a pretty UI without them.

---

## Definition of Done — all 8 required

1. Live URL responding
2. Public repo, secrets scrubbed, `.env.example` committed
3. 30-second demo GIF at the **top** of the README
4. Architecture diagram
5. "Decisions & Tradeoffs" section — the four decisions above
6. Measured eval numbers published: execution accuracy across 25
   questions, and 5/5 adversarial attempts blocked
7. "What Didn't Work" section
8. I can explain the whole thing for 20 minutes with no notes

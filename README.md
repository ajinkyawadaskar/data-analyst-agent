# Guardrailed Natural Language to SQL Agent

Ask a question in plain English, get an answer from BigQuery — with a layer in
front that decides whether the generated SQL is allowed to run at all.

![Demo](docs/demo.gif)

Live: `https://data-analyst-agent-production-316b.up.railway.app` | [How it's wired](docs/architecture.md) | [Things that broke](#things-that-broke)

---

## The problem I wanted to solve

Getting an LLM to write SQL is easy. Every model does it competently now.

The part nobody demos is what happens when it writes something wrong — and
"wrong" has a wide range. It can invent a column that doesn't exist. It can
reach for a table you never meant to expose. It can write a perfectly valid
query that scans six gigabytes because it forgot a date filter. Or it can
write `DROP TABLE` at the end of something that looked fine.

You can ask a model nicely not to do those things. That lowers the rate. It
doesn't bound it.

So this project is the other half: parse every generated query before it runs,
check it against rules that don't depend on the model cooperating, and refuse
the ones that fail. The SQL generation is the boring part. The layer that says
no is the point.

## How it works

The agent turns a question into SQL, then five checks run before anything
touches BigQuery:

1. **Parse it.** Must be a single statement, and it must be a `SELECT`. Checked
   on the syntax tree, not with pattern matching — so `DELETE` buried inside a
   CTE gets caught too.
2. **Check the tables.** Every table reference has to be in the allowlist.
3. **Check the columns.** Every column has to exist in the schema the model was
   shown. A column it invented gets rejected, not executed.
4. **Check the row limit.**
5. **Check the price.** BigQuery can tell you what a query *would* cost without
   running it. If it's over the ceiling, it doesn't run.

If a check fails, the violation goes back to the model and it tries again —
twice, then it stops and tells you why. If everything passes, the query runs
against a row cap and the results get explained in plain English.

Full diagram: [docs/architecture.md](docs/architecture.md).

## The data

Two BigQuery public datasets, both read-only:

- **`thelook_ecommerce`** — a synthetic online store. 7 tables, 75 columns,
  orders and users and products, real joins.
- **`google_analytics_sample`** — real Google Analytics session data. One table
  per day for a year.

That second one is where it got interesting.

## The schema didn't fit, and the reason was dumber than I expected

Google Analytics stores each day as its own table. 366 of them. Every one has
the same 338 columns, and 322 of those are nested paths — things like
`totals.pageviews` and
`trafficSource.adwordsClickInfo.targetingCriteria.boomUserlistId`.

Describing all of that to the model comes to **1,235,616 tokens.**

I expected to solve this with a clever compaction strategy — rank columns by
relevance, keep the important ones, drop the rest. Then I looked at what the
1.2 million tokens actually *was*: the same 338-column schema, written out 366
times.

BigQuery already treats those tables as one thing if you address them with a
wildcard. So I describe one of them and call it `ga_sessions_*`. The whole
corpus went to **3,921 tokens.** 315x smaller, and nothing was lost — the other
365 copies were copies.

I'm keeping the compaction machinery because a real warehouse would need it.
But the honest version of this story is that the big win was noticing
duplication, not the clever part.

## Numbers

### Schema

| | tables | columns | tokens |
|---|---|---|---|
| thelook_ecommerce | 7 | 75 | 488 |
| GA, naive (366 shards) | 366 | 123,708 | 1,235,616 |
| GA, collapsed to wildcard | 1 | 338 | 3,376 |
| **full corpus as sent to the model** | **10** | **417** | **3,921** |

### Cost ceiling

Ceiling is 1 GB. These are dry-run measurements, not estimates:

| query | bytes | result |
|---|---|---|
| `SELECT *` all GA shards, no date filter | 5.767 GB | **blocked** |
| `SELECT *` GA, one month | 0.794 GB | passes |
| `SELECT COUNT(*)` all GA shards | 0.000 GB | passes |
| `SELECT *` thelook.events (2.4M rows) | 0.384 GB | passes |

`COUNT(*)` across all 366 tables costs nothing — BigQuery answers it from
metadata. Which means "touches every table" and "expensive" are different
questions, and a guard that counted tables instead of bytes would block a free
query. Bytes is the right signal.

### Accuracy

Execution accuracy across 25 questions: **8/25 (32%)**
Adversarial prompts blocked: **5/5 (100%)**
Average retries per question: **0.28**

Scored by comparing result sets, not SQL strings — two different queries can
both be right, and a string comparison would fail the correct one.

### I published the wrong number, and blamed the wrong thing

The first version of this section said 20%, and blamed the model: "a
lightweight model that struggles with complex joins." Both halves were wrong,
and I only found out by re-running the evals before building on top of them.

The eval run was committed at 22:11. The commit that fixed column validation
rejecting `ORDER BY <select_alias>` landed at 23:20, 69 minutes later. So the
published number was measured against a guardrail that was falsely rejecting
the agent's own correct SQL. Re-running the identical 25 questions on the
identical model:

| | published (pre-fix) | re-run (post-fix) |
|---|---|---|
| execution accuracy | 5/25 (20%) | **8/25 (32%)** |
| average retries | 0.96 | **0.28** |
| cases returning no rows at all | 11 | **1** |

The last row is the real story. Eleven of the twenty-five questions had been
scored as failures because the agent blocked itself, retried, and gave up. One
still is. Nothing about the model changed between those two columns.

Two honest caveats. Three of the recovered cases now execute but return the
wrong result — the bugfix converted "blocked" into "runs, still wrong," which
is progress but not as much as +12 points suggests. And this is a single run
against a non-deterministic model: one case that passed before (`tl03`) failed
this time while four others started passing, so treat ±1 case as noise.

The lesson is about measurement, not SQL. A guardrail bug and a model
limitation produce the same symptom — no answer — and I attributed the whole
gap to the model without checking. The number that matters for this project is
still the second one: every adversarial prompt was caught, and nothing
dangerous reached BigQuery.

## Choices I made

**Execution accuracy, not SQL string match.** Two different queries can
return the same correct answer — `SUM(sale_price) / COUNT(DISTINCT order_id)`
and `AVG(order_total)` both compute AOV. A string comparison fails the
correct one. So the eval runs both queries against BigQuery and compares the
result sets: same rows, same values (within float tolerance), column names
ignored, order only enforced when the ground truth has an ORDER BY.

**Retries capped at 2.** The first retry usually fixes a syntax error or a
wrong column name — the model gets the error message and corrects it. The
second retry occasionally recovers from a table-structure misunderstanding.
A third almost never helps — by that point the model is stuck on a
fundamental schema misread, and more attempts just burn quota on the same
wrong approach.

**AST parsing instead of a prompt instruction or a regex.** A prompt
instruction ("never write DELETE") reduces the rate of bad SQL. It doesn't
bound it. A regex can be fooled by comments, string literals, or CTEs. An
AST parse with sqlglot sees the actual statement type regardless of how
it's formatted — `DELETE` buried inside a CTE gets caught because the
parser sees a Delete node, not because a pattern matched a string.

**Schema context sent in full.** After collapsing GA's 366 identical daily
shards to one wildcard entry, the entire corpus fits in 3,921 tokens. At
that size, compaction would throw away information for no benefit — the
model sees every table and column, so it can't hallucinate a table that
doesn't exist without the column-validation guardrail catching it.

## Things that broke

### The health check broke the moment it had something to report

`/health` was written to never raise — a 500 during a deploy tells you nothing.
It caught `ImportError` for the not-yet-written agent module and reported
`not_implemented` cleanly.

Then the module appeared as a stub that raised `NotImplementedError`, and
`/health` started returning a full traceback. I'd guarded against "file
missing" when the real states were missing, stub, broken, and working. The
endpoint whose entire job was to degrade gracefully was the one that crashed,
because I only ever tested it in the state I wrote it for.

### A guardrail that failed open

Column validation looked up each table's columns from the schema context. If
the lookup came back empty, the code treated that as "I don't know this table"
and skipped the check.

The lookup was broken — wrong data structure, so it came back empty every time.
Which meant column validation silently never ran, and a query selecting a
column that doesn't exist passed all four checks and reported clean.

The bug was a one-liner. The lesson wasn't. A guardrail that skips when it's
confused is worse than one that doesn't exist, because it reports success. Any
check that can't complete has to fail closed.

### Gemini's response format changed between versions

`response.content` returns a string on older Gemini models. On Gemini 3.x it
returns a list of content parts. The LangGraph nodes called `.strip()` on it
and got `AttributeError: 'list' object has no attribute 'strip'`. The fix
was switching to `response.text`, which is a property that extracts the text
regardless of version — but the error only appeared at runtime, not during
import or type checking.

## Try it

```bash
curl -X POST https://data-analyst-agent-production-316b.up.railway.app/ask \
  -H 'content-type: application/json' \
  -d '{"question": "which traffic sources drove the most sessions in August 2016?"}'
```

Health check:

```bash
curl https://data-analyst-agent-production-316b.up.railway.app/health
```

### Running it locally

```bash
git clone https://github.com/ajinkyawadaskar/data-analyst-agent
cd data-analyst-agent
uv venv --python 3.11 && uv pip install -r requirements-dev.txt
cp .env.example .env     # add your GCP service account + Gemini key
uvicorn src.api:app --reload
```

You need a Google Cloud project with the BigQuery API enabled and a service
account with `BigQuery Job User`. The public datasets are free to read but
queries bill to your project — which is the other reason the cost ceiling
exists.

## A note on how I built this

I used AI assistance for the scaffolding — BigQuery client, schema
introspection, the API layer, deploy config, tests, this README.

I wrote the four files that make the actual decisions by hand: the guardrail
checks, the cost ceiling, the graph wiring, and the eval metric. That split was
set before I started. Every judgment call in this project lives in those four
files, and I wanted to be the one who made them.

## Stack

Python 3.11, BigQuery, sqlglot, LangGraph, Gemini 3.1 Flash Lite, FastAPI,
DeepEval, Streamlit, Railway.

## Semantic Execution Gateway (`feature/semantic-gateway`, complete)

A branch on top of this project replaces free-text SQL generation with a
compiled, governed pipeline: the LLM emits a small structured "intent" JSON
naming a certified measure/dimension instead of writing SQL, a deterministic
compiler turns that into the query, and the result goes through compile-time
row-level security, the same guardrails above, and a semantic cache before
hitting BigQuery. It also adds a stacking pattern — combining retrieval over
support notes with the compiled query path so a question like "why are our
highest-usage accounts complaining about latency, and what do they pay us"
can be answered by joining a qualitative signal to a real number, instead of
either half alone. All six layers, MCP exposure, and OTel tracing are built,
tested, and verified against live BigQuery and a live Gemini model — full
write-up: [docs/semantic-gateway.md](docs/semantic-gateway.md).

**Measured, not asserted — same eval set, same model, corrected baseline:**

| | legacy path | semantic gateway |
|---|---|---|
| Answer accuracy | 8/25 (32%) | 12/25 (48%) |
| Coverage / accuracy on covered | n/a | 23/25 covered, 11/23 correct |
| Adversarial blocked | 5/5 | 5/5 |

Plus a second, purpose-built eval set (`evals/golden_cases.json`, 18 cases)
testing the four things the original 25 were never designed to cover —
compile-time security denial, semantic cache hits across paraphrases, and
the stacking chain end to end: **17/18**, with the one failure a genuine,
disclosed model-behavior edge case rather than a hidden gap.

**The support-notes dataset used for retrieval is 100% synthetic.**
`data/synthetic_notes.jsonl` is generated by `tools/generate_notes.py` from
fixed templates and a seeded RNG — no LLM call, no network access, and
nothing derived from any real customer or company's data. Re-running the
generator reproduces the file byte-for-byte; the file's own first line is a
disclosure header, and the generator's docstring explains exactly how every
note is assembled. The only real thing borrowed from elsewhere is a set of
`user_id` values sampled from `thelook_ecommerce.users` — itself a public,
already-synthetic BigQuery sample dataset — used purely so a retrieved note
can be joined back to a real row for the compiled half of a stacking
question. This is a portfolio project, not a partnership: no Deepgram or
other company's real data is used anywhere in this repo.

**`on_behalf_of` (tenant/region) is a simulated identity.** There are no real
multi-tenant users behind this public dataset — the field exists to
demonstrate compile-time security injection against something request-shaped,
not to claim real multi-tenancy. It's also conditional, by design: a row
policy only applies to a query that actually touches a policy-guarded table,
and a measure the compiler can't express at all falls back to the legacy LLM
path, which has no concept of identity whatsoever. "Identity-based access
control" here means on the compiled path, for queries that touch a guarded
entity — see docs/semantic-gateway.md's Layer 2 section for the full scope.

This build stays behind `USE_SEMANTIC_GATEWAY` (`main` and everything above
is untouched and still live); merging to `main` is a separate, explicit step.

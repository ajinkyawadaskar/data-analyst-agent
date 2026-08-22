# NOTES — Data Analyst Agent (NL → SQL)

Running log of dead ends, surprises, and design reasoning.
Written as it happens, not reconstructed at the end.

## 0:00 — Dead end: no gcloud SDK, no ADC on this machine
`gcloud` and `bq` are both absent from PATH, and there is no
`~/.config/gcloud/application_default_credentials.json`. So the
default "just use ADC" path for BigQuery is not available.

Options considered:
- Install the full gcloud SDK — slow, and a 2h deploy cap means
  every minute of setup is a minute not spent on guardrails.
- Service-account JSON key downloaded from the console — works with
  `google-cloud-bigquery` directly, no SDK needed. Chosen.

Consequence: `GOOGLE_APPLICATION_CREDENTIALS` pointing at a
service-account file is the documented auth path in `.env.example`,
not `gcloud auth application-default login`.

## 0:00 — System Python is 3.9
Too old for comfort with current langgraph/pydantic. Pinning 3.11
via uv rather than fighting it.

## 0:35 — Resume bullet already claims this repo; URL is 404
The resume in circulation points at github.com/ajinkyawadaskar/data-analyst-agent
and describes the project in the present tense. Verified with curl: **404**.
Anyone who clicked it before today saw nothing.

Two mismatches between the bullet and what we were about to build:
- Local dir was `data-analyst-analyst`. Renamed to `data-analyst-agent`
  so the URL on the resume is the URL that exists.
- I had scaffolded Gemini as the LLM. Bullet says "Claude / GPT-4".
  Switched to Anthropic (`claude-opus-5`) to match.

## 0:45 — Dataset decision: thelook_ecommerce
Chose `bigquery-public-data.thelook_ecommerce` as the corpus. Ecommerce
is the domain I interview into (marketing / growth DS, A/B-heavy roles),
so the eval questions double as domain evidence: funnel, conversion,
cohort retention, channel attribution.

Rejected: census_bureau_acs (no joins, cryptic names), crypto_ethereum
(reads as crypto-hobby to bank recruiters), cfpb_complaints (finance
narrative, but one flat table).

Open tradeoff, flagged not resolved: a single dataset makes the table
allowlist nearly a no-op and weakens the "schema too big for context"
problem, which is one of the four decisions I have to defend.

## 1:05 — Second dataset: google_analytics_sample
Added `bigquery-public-data.google_analytics_sample` alongside thelook.

Reasoning: GA360 session data is the canonical digital-marketing corpus,
which matches the growth/marketing DS roles I apply to, and its nested
RECORD/REPEATED schema is genuinely too large to dump into a prompt --
so schema compaction stays a real decision instead of a formality.
Nested column paths also stress column validation far harder than flat
tables do.

Accepted cost: two datasets in the same broad domain make a blocked
cross-domain join less dramatic than retail-vs-banking would have been.
Profile fit judged worth more than demo drama.

## 1:30 — Surprise: "table_summary" compaction deletes GA entirely
First run of the compaction mechanisms against fixtures. `table_summary`
keeps ids, dates, and non-nested scalars -- which on a flat relational
table like thelook.order_items is exactly right, and on GA drops all ten
nested columns. Every question GA exists to answer (pageviews, revenue,
traffic source) lives under a dotted path.

So a compaction rule that is obviously correct for flat tables silently
makes the nested dataset useless. Not a bug in the code -- a bug in the
rule. This is the concrete reason the compaction strategy has to be a
deliberate decision rather than a default, and it is the example to use
when explaining that decision.

Also added `dropped_columns` to SchemaContext: when guardrails rejects a
column, we need to distinguish "model hallucinated it" from "we never
showed it to the model." Without that, a retrieval failure looks exactly
like a model failure.

## 2:00 — Credentials landed. Measured the real numbers.
Project pivotal-purpose-269517, service account job-agent@. First query
ran: top thelook product categories, 361,201 bytes dry-run.

thelook_ecommerce: 7 tables, 75 columns, ~488 tokens. Small.

google_analytics_sample: 369 objects, of which 366 are ga_sessions_*
date shards -- all with IDENTICAL schemas. One shard flattens to 338
columns (322 nested/dotted, 32 RECORD parents, 11 REPEATED), ~3,376
tokens. Deepest path is four levels:
  trafficSource.adwordsClickInfo.targetingCriteria.boomUserlistId

Naive introspection of every shard: 366 x 3,376 = **1,235,616 tokens**
of almost entirely duplicated schema. That is the "schema too big for
context" problem, measured rather than asserted -- and the cause is
duplication, not genuine breadth.

Fix (mechanism, in schema.py): collapse `<prefix>_YYYYMMDD` tables into a
single `<prefix>*` wildcard entry, introspecting only the newest shard.
BigQuery queries them through the wildcard anyway. Also skip schema-less
artifact objects (`Google-ecommerce-dataset-table` has 0 columns).

Result: 1,235,616 -> 3,921 tokens for the whole corpus. 315x reduction
before any compaction strategy is applied at all.

The lesson worth saying out loud in an interview: the first and biggest
win came from noticing the schema was duplicated, not from a clever
compaction rule. Compaction is the second-order problem.

## 2:45 — Candidate eval cases drafted and VERIFIED by execution
16 answer candidates + 5 adversarial, drafted against real column names.
Every expected_sql was executed, not just written: all 16 return rows,
39.3 MB scanned total, 0 failures. Ground truth that has never been run
is not ground truth.

Sample verified answers: AOV $86.67; 10.13% of orders returned; GA
Aug-2016 session-to-transaction conversion 1.532%; avg 4.85 pageviews
per session; referral medium bounce rate 70.94%.

## 2:50 — The 1 GB ceiling is well placed, measured not guessed
Dry-run probes against the real corpus:
  SELECT * across all 366 GA shards, no filter   5.767 GB   TRIPS
  SELECT * GA one month                          0.794 GB   passes
  SELECT COUNT(*) all GA shards                  0.000 GB   passes
  SELECT * thelook.events                        0.384 GB   passes

So 1 GB sits in a genuinely useful spot: it blocks the unfiltered
full-history scan (the query the guard exists for) while allowing every
legitimate question in the eval set. Note COUNT(*) across all shards is
free -- BigQuery answers it from metadata -- so "touches all shards" is
NOT the same as "expensive". A guard that reasoned about shard count
instead of bytes would produce a false positive there.

adv04 is therefore a real test, not a hypothetical: 5.767 GB against a
1 GB ceiling.

## 3:15 — My bug: /health broke the moment graph.py existed as a stub
_load_graph() caught ImportError but not NotImplementedError, so as soon
as the stub file appeared, /health went from a clean "degraded" report to
a 500 with a full traceback. The endpoint whose entire promise was "never
raise" was the one that broke.

Cause: I wrote the guard against "file missing" when the real states are
missing / stub / broken / working. Now catches all four.

Worth keeping in the writeup: the health check was only ever tested in
the state it was written for.

## 3:10 — guardrails.py column validation failed OPEN (API mismatch, my fault)
_schema_columns_for() expected schema_context.tables to be a dict of
table -> columns. SchemaContext.tables is a tuple of Table dataclasses,
so every lookup returned None, which the code treats as "table unknown,
skip" -- and a hallucinated column passed all four checks.

Root cause is mine: I landed SchemaContext without documenting its API,
so guardrails.py was written against a guessed shape. Real API is
columns_for(fqn) / all_column_names() / find_tables_with_column(name).

The deeper lesson is the failure DIRECTION: a lookup miss disabled the
check and passed the query. Guardrails must fail closed. A silent skip on
an unknown table means one typo disables column validation corpus-wide.

## 3:30 — Provider switch to Gemini, and a quota problem I can see coming
No Anthropic or OpenAI credit available, so the LLM is Gemini via AI
Studio. Third provider decision today: Gemini -> Anthropic (to match the
resume bullet) -> Gemini (to match reality). Cost of the churn was small
only because graph.py was still unwritten.

Reused two hard-won facts from P1 (credit-decision-explainer) instead of
rediscovering them:

1. MODEL NAMES DRIFT. In P1, `gemini-2.0-flash` and `gemini-2.5-flash`
   were both retired and 404'd. Only `gemini-3.6-flash` worked. So that
   is what config.py ships, with a note not to write names from memory.

2. FREE-TIER QUOTA IS 20 REQUESTS. P1 hit:
     429 RESOURCE_EXHAUSTED ... generate_content_free_tier_requests,
     limit: 20, model: gemini-3.6-flash
   That killed its LLM-judged metrics at n=1.

Consequence for THIS project, flagged now rather than at 6:45: the eval
set is 25 answer + 5 adversarial = 30 questions, each needing at least
one generate_content call, plus up to 2 retries. Worst case ~90 calls
against a 20-call ceiling. The eval run WILL fail on free tier.

Options, none chosen yet:
  a) enable billing on the Google API key (cheapest fix in time)
  b) run evals in batches across quota windows (slow, fragile)
  c) cut the eval set below the CLAUDE.md-mandated 25+5 (violates DoD #6)
  d) accept partial results and publish n honestly

RESUME CONSEQUENCE: the bullet says "Claude / GPT-4 tool-calling". With
Gemini shipping, that line is now false and must be changed.

## 3:50 — Deploy prep, and the container credential problem
The service-account JSON is gitignored, so a deployed container has no
key file. Most PaaS hosts only give you env vars. Fix: pass the JSON as
GOOGLE_APPLICATION_CREDENTIALS_JSON (base64 or raw) and materialize it
to a temp file at client construction. Verified by round-trip -- unset
the file path, set the env var, ran a real query: 29,120 products.

Clean-room check caught two things a working laptop hides:
- pydantic-settings was never in requirements.txt. It imported locally
  only because something else pulled it in transitively.
- deepeval and streamlit were in the runtime requirements. Both are heavy
  and neither is needed by the API. Split into requirements-dev.txt so
  the deployed build does not compile them.

Deploy is NOT done: no PaaS CLI on this machine and the account login is
interactive. Config is written and verified locally; pushing it is a
manual step.

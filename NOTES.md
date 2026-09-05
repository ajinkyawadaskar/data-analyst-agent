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

## 4:40 — Streamlit built; cold start was 30s and had to be fixed
Two tabs. The one that matters is the guardrail sandbox: paste SQL, watch
every check run against it, no LLM and no API key needed. It demos the
actual point of the project and it works before graph.py exists. Seven
preset examples including the stacked DROP, the CTE-buried DELETE, the
5.8 GB unfiltered scan, and the free COUNT(*).

Verified in a browser, not just by HTTP 200: sandbox reports "Passed",
lists all four checks run, and shows 0.003 GB against the 1 GB ceiling.

Problem the browser test exposed that curl never would: **first paint
took ~30 seconds.** Schema introspection is one get_table call per table
and it blocked the whole page. On Railway that is a failed healthcheck,
and in a demo GIF it is unwatchable.

Fix: cache the introspected schema to schema_cache.json.
  cold (refresh=True):  5.0s
  warm (from cache):    0.001s
The 30s in Streamlit was introspection plus Streamlit's own boot; the
introspection half is now effectively free. Cache is gitignored -- it is
regenerable, and pinning a stale schema in git would be worse than the
30 seconds.

## 5:20 — The quota wall hit, exactly as predicted, and worse than estimated
Flagged this at 3:30 from P1's experience. Confirmed at 5:20:

    429 RESOURCE_EXHAUSTED
    quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
    quotaValue: 20
    model: gemini-3.6-flash

The limit is 20 requests PER DAY, PER MODEL -- not per minute, which is
what I had assumed when I sketched the "run evals in batches across
quota windows" option. That option was never viable; a daily cap cannot
be batched around inside one working day.

Worth recording that predicting the failure did not prevent it. I flagged
it two hours early, listed four options, and then we spent the quota on
debugging anyway -- because each individual call looked cheap. The
resource that ran out was one nobody was counting per-call.

Mitigation taken: switched to gemini-3.1-flash-lite. Quota is scoped per
model, so a different model has its own allowance. Verified with a live
call. This is a workaround, not a fix -- if flash-lite is also 20/day,
a 30-case eval run still does not fit and billing is the only real answer.

## 5:25 — Gemini 3.6/3.1 return content as a LIST, not a string
graph.py assumed response.content was a str and called .strip() on it:

    AttributeError: 'list' object has no attribute 'strip'

Actual shape, dumped from a live call:

    [{'type': 'text', 'text': 'OK', 'extras': {'signature': '...'}}]

A list of typed blocks, each with a cryptographic signature. Fix is to
use `response.text` (a property; calling it as .text() is deprecated),
which flattens blocks and keeps working if new block types appear.

This is the kind of breakage that only shows up against the real API.
Every local test passed because none of them called a model.

## 5:25 — temperature is silently ignored on these models
    UserWarning: Model 'gemini-3.6-flash' uses fixed sampling defaults;
    the sampling parameter(s) temperature will be ignored.

So `temperature=0` in the graph does nothing, and runs are NOT
deterministic. P1 hit the identical warning on the same model family.
Consequence for the evals: two runs of the same question can produce
different SQL, so a single run is a sample, not a measurement. Any
accuracy number we publish should say how many runs it came from.

## 5:25 — Automatic function calling may be multiplying request count
    "Direct use of automatic function calling (AFC) in
     Models.generate_content is not recommended."

If the graph binds tools, AFC can issue several requests per logical
turn. That would explain how 20 requests disappeared during what felt
like three or four attempts, and it changes the eval quota arithmetic by
a factor of 2-3. Needs checking before the real run.

## Day 1 0:30 -- Surprise: the published 20% was measured against a broken guardrail
Re-ran the existing eval set on main before writing any gateway code, on the
theory that you cannot claim a delta against a number you have not re-measured.

    answer_accuracy   5/25 (20%)  ->  8/25 (32%)
    avg_retries             0.96  ->  0.28
    cases with no rows        11  ->  1

Nothing about the model changed. git log explains it:

    225569a  22:11  Run evals: 5/25 answer accuracy
    b8c198f  23:20  Fix column validation rejecting SELECT aliases

The eval run predates the alias bugfix by 69 minutes. The agent had been
writing correct SQL, the column check was rejecting ORDER BY <select_alias>,
the retry loop burned two attempts and gave up. Eleven of twenty-five.

Two things I got wrong, both worth keeping:

1. The README attributed the gap to "a lightweight model that struggles with
   complex joins." A guardrail false positive and a model limitation present
   identically -- no answer -- and I attributed the whole thing to the model
   without checking. Same failure DIRECTION as the 3:10 entry, one level up:
   there a lookup miss silently disabled a check, here a check's own bug
   silently became the model's fault.

2. +12 points overstates it. Of the ten cases that stopped returning nothing,
   only three became correct; the rest now execute and return the wrong result.
   The fix converted "blocked" into "runs, still wrong."

Also: one case that passed before (tl03) failed this run while four others
started passing. Single run, non-deterministic model, so +-1 case is noise.
The 5:25 entry already said any published number should say how many runs it
came from, and the published one did not. This one does.

RESUME CONSEQUENCE: the gateway's accuracy delta gets measured against 32%,
not 20%. Comparing against the stale number would have credited a compiled
semantic layer with a bugfix that was already merged.

## Day 1 0:45 -- The eval runner cannot run from a clean shell
python -m evals.run_evals dies on DefaultCredentialsError even with
GOOGLE_APPLICATION_CREDENTIALS set in .env. pydantic-settings populates the
Settings object; google.auth reads os.environ directly, and nothing bridges
the two. app.py papers over it with os.environ.setdefault at import. The
eval runner has no equivalent, so it only ever worked in a shell that already
had the var exported.

Not fixing it in the runner today -- it is a one-line export and the real fix
belongs in bq_client.get_client(), which is on the guardrail path I am not
touching this build. Logged so it is not rediscovered.

# IDEAS — out of scope for today, deliberately unbuilt

Anything here was considered and rejected for today's ship under the
CLAUDE.md scope lock. Listed so the thinking isn't lost.

(empty so far)

## Domain routing / per-domain config (rejected 0:20)
"Run it on two domains" tempts a domain selector in the API, per-domain
prompt templates, and a router node in the graph. Two datasets in one
allowlist gets the same demo with zero new code. Not built.

## A/B testing features inside this agent (rejected 0:30)
For growth/marketing DS roles the gap is causal inference, not SQL
tooling. A stats node grafted onto a SQL agent reads as padding.
Correct move is a separate, small experiment-analysis project (P5)
with a real power calc and a decision writeup. Not built here.

## Dual-provider support: Claude + GPT-4 (deferred 0:35)
Resume bullet says "Claude / GPT-4 tool-calling". Building and
evaluating two providers doubles the eval matrix for no additional
signal. See NOTES.md — recommend amending the bullet instead.

## Redis for the Layer 4 semantic cache (rejected, Day 3)
The default reach for "cache" is Redis. No horizontal-scaling story
exists at this project's scale, and Redis is a whole extra service to
run and deploy for a cache read far more than written. SQLite in WAL
mode gives concurrent reads without contending on the single writer
lock, and it's already the file format the session-trajectory store
needed. Not built; see NOTES.md's Days 2-5 log for the fuller version.

## LLM-based routing for Layer 5's structured/unstructured/stack decision (rejected, Day 4)
Would generalize past keyword blind spots (see NOTES.md — a real
routing miss was found and fixed once), but costs quota and latency on
every question, and turns the routing decision into something that
can't be traced to a specific rule when it's wrong. A rule-based
classifier's failures are visible and fixable one pattern at a time; an
LLM router's failures are not obviously either of those things. Correct
tradeoff at this project's scale; would revisit only if the pattern
list itself started growing unmanageable.

## An LLM-judged eval metric layered on top of the golden set (deferred, Day 5)
evals/metrics.py's two functions are 100% deterministic by design — free
quota, no judge-model variance. A judged metric (e.g. "is this synthesized
answer's prose actually faithful to its citations") would catch something
the structural citation checks in test_synthesis.py don't, but it has to
sit behind an explicit opt-in flag so a normal eval run never spends agent
quota on judging, and deepeval's built-in GeminiModel is the right way to
wire it when this gets built, not a hand-rolled wrapper. Not built —
quota was fully committed to the golden set and the 25-question comparison
this round.

## Multi-warehouse testing beyond BigQuery (deferred, Day 2)
`semantic_model.yaml`'s `dialect: bigquery` is threaded through to
`.sql(dialect=...)` so a different warehouse is a config change, not a
rewrite — but nothing here has actually been run against Athena or
another dialect. The claim is "the seam exists," not "it's been
proven." Would need a second live warehouse to actually test against,
which this project doesn't have.

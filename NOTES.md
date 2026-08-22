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

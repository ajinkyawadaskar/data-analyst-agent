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

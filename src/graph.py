"""LangGraph agent wiring.

OWNER: Ajinkya. Do not let the assistant fill this in.

langgraph 1.2.11 and anthropic 1.0.0 are installed. Model is
claude-opus-5 (settings.llm_model). Set ANTHROPIC_API_KEY in .env.

--------------------------------------------------------------------------
WHAT THIS MODULE MUST DO
--------------------------------------------------------------------------
Wire the pieces that already exist into a bounded loop, and expose:

    def build_graph():   # returns a compiled graph with .invoke(state)

src/api.py calls build_graph() lazily and reads these keys off the result:

    sql            str | None
    rows           list[dict] | None
    explanation    str | None
    guardrails     GuardrailReport      (required -- API will KeyError)
    retries_used   int

--------------------------------------------------------------------------
THE PIECES, ALL BUILT AND VERIFIED
--------------------------------------------------------------------------
    src.schema.introspect()        -> SchemaContext (10 tables, 417 cols)
    src.schema.compact(sc, strat)  -> smaller SchemaContext
    SchemaContext.to_prompt()      -> renders schema for the LLM
    src.guardrails.check(sql, sc)  -> GuardrailReport      (yours, 11/12)
    src.cost_guard.check_cost(sql) -> CostVerdict          (yours, works)
    src.bq_client.execute(sql)     -> list[dict], row-capped
    src.config.get_settings()      -> .max_retries (2), .llm_model

--------------------------------------------------------------------------
SUGGESTED NODES
--------------------------------------------------------------------------
    load_schema   introspect + compact. Do this ONCE at build time, not
                  per request -- introspection is ~10 API calls and the
                  schema does not change between questions.
    generate_sql  question + schema.to_prompt() -> SQL via Claude
    guard         guardrails.check, then cost_guard.check_cost. Order
                  matters: parse before you spend a dry-run call on it.
    execute       bq_client.execute
    explain       rows -> plain-English answer
    handle_error  decide retry or give up

--------------------------------------------------------------------------
THE RETRY LOOP -- THIS IS THE PART THAT GETS INTERVIEWED
--------------------------------------------------------------------------
Cap at 2 (settings.max_retries). Decisions that are yours:

1. WHAT IS RETRYABLE.
   Three distinct failure kinds, and they are not equally worth retrying:
     - guardrail violation (hallucinated column, wrong table)
     - cost ceiling exceeded -- cost_guard already returns
       retryable=True with a retry_hint suggesting a _TABLE_SUFFIX
       filter. Feeding that hint back is high-yield.
     - BigQuery execution error (valid-looking SQL, invalid at runtime)
   Decide which of the three get a retry, and say why the others don't.

2. WHAT GOES BACK TO THE MODEL ON RETRY.
   The violation list is the useful signal -- "column profit_margin does
   not exist" is actionable; "query rejected" is not. Feed back the
   specific violations, and consider whether to include the columns that
   DO exist for that table (schema_context.columns_for) so the model can
   correct rather than guess again.

3. WHY THE CAP IS 2.
   Your stated reasoning: past 2 it is usually a misunderstood schema,
   not a syntax slip, and further retries just burn money. Make sure the
   loop actually demonstrates that -- if retry 2 succeeds often, the cap
   is wrong and you should say so honestly rather than defend a number
   the data contradicts.

4. WHAT THE USER SEES WHEN ALL RETRIES ARE SPENT.
   Returning the last guardrail report with passed=False is honest and
   makes the API useful. Returning a generic error is not. The blocking
   decision IS the product here.

--------------------------------------------------------------------------
GOTCHAS
--------------------------------------------------------------------------
- Do NOT let a retry loop call execute() before guard() passes. The whole
  project premise is that nothing unguarded reaches BigQuery.
- guardrails must run BEFORE cost_guard: cost_guard hits the BigQuery API
  and will raise on invalid SQL, which is guardrails' job to catch first.
- retries_used must be accurate. It goes in the README numbers.
- The 5 adversarial eval cases must terminate WITHOUT executing anything.
  A guardrail that blocks but then retries into an execution is a
  failure, and the eval must catch it.
"""

from __future__ import annotations


def build_graph():
    """See module docstring. OWNER: Ajinkya."""
    raise NotImplementedError("src/graph.py is written by Ajinkya, not the assistant")

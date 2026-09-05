"""
WHY THIS MODULE EXISTS
----------------------
Layer 1 (src/compiler/intent_compiler.py) guarantees a query can only ask for
things in the certified semantic model. It says nothing about WHO is asking.
Two different callers sending the identical question should not always get
the identical rows -- a caller scoped to one region should never see another
region's data, no matter how the question is phrased.

The naive fix is a prompt instruction: "only show data for the caller's
region." That is not a security boundary, it is a suggestion the model can be
talked out of -- the same reasoning that makes prompt-only SQL guardrails
insufficient is true here. This module makes the restriction structural
instead: it rewrites the query's AST to add the row filter AFTER the model
has finished contributing anything, so no phrasing of the question can reach
around it. The model never sees this step and cannot negotiate with it.

CONTRACT
--------
    inject_security_context(expression, tenant_id, region, model) -> exp.Select

Steps, in order:

1. FIND WHICH ENTITIES IN THIS QUERY HAVE A ROW POLICY
   Walk expression's FROM and JOIN clauses to collect the entity aliases
   actually present (they are the entity names themselves -- see
   intent_compiler.py's _entity_table, which aliases every table to its
   declared entity name). For each one, look up model.policy_for(entity).
   An entity with no policy is left untouched -- row_policies is opt-in per
   entity, not a blanket filter.

2. BUILD ONE PREDICATE PER POLICY, AND-COMBINE WITH THE EXISTING WHERE
   For each policy found, build `<entity>.<policy.column> <op> <value>` with
   sqlglot expression builders -- exp.column() / exp.EQ() / exp.In(), the
   same discipline intent_compiler.py already uses for filter values. Never
   string-format the tenant_id or region into the query: those are caller-
   supplied, exactly the class of input intent_compiler.py's filter handling
   already treats as untrusted.
   AND the new predicate onto whatever WHERE clause already exists (there may
   be filters, a partition bound, or both) -- use expression.where(predicate),
   which sqlglot AND-combines by default rather than replacing.

3. RETURN THE MODIFIED EXPRESSION, NOT A NEW STRING
   The caller re-renders with expression.sql(dialect=model.dialect) after
   this returns. Do not render inside this function -- the whole reason
   CompiledQuery.expression is retained instead of just its .sql is so this
   step can still reach the AST.

4. AN IDENTITY WITH NO ACCESS MUST BE DENIED, NOT SILENTLY EMPTIED
   If `region` (or tenant_id, if you decide it also gates access -- see
   decision A below) does not resolve to anything the caller is allowed to
   see at all, raise SecurityContextError rather than emitting a predicate
   that matches zero rows. A query that runs and returns nothing looks
   identical, from the caller's side, to a query that ran fine and the answer
   really is zero -- and that ambiguity is exactly what Layer 2's DoD test
   (an identity without APAC access cannot get APAC rows under ANY phrasing)
   is checking is NOT the failure mode here. Denied must be loud.

DECISIONS MADE (see the module docstring's original DECISIONS TO MAKE section)
-------------------------------------------------------------------------------

  A. WHAT DOES `region` ACTUALLY RESTRICT TO?
     `region` is a NAMED GROUP (e.g. "APAC"), not a single raw country code.
     Callers are provisioned with a region by the identity system, not with a
     literal list of countries, so the region -> country-set expansion is an
     identity-system concept, not a data-model concept -- it lives in
     REGION_TO_COUNTRIES below, not in semantic_model.yaml (that file is
     schema-shaped, not identity-shaped). Because a region can expand to more
     than one country, the resulting predicate is `column IN (...)`, not `=`.
     Extend REGION_TO_COUNTRIES when a new region is provisioned; never patch
     around it at the query layer.

  B. DOES tenant_id GATE ANYTHING YET, OR IS IT CARRIED FOR LATER?
     Carried for later. No row_policy in the current semantic_model.yaml keys
     off `principal_field: tenant_id`. tenant_id flows through this function
     unused for filtering today -- it exists for the audit envelope and the
     Layer 4 cache key. The code below still knows how to enforce a
     tenant_id-scoped policy (equality predicate) so that if one is added
     later this module does not need to change, but nothing in the current
     model exercises that branch. Do not read "the parameter exists" as
     "tenant isolation is enforced" -- it isn't, yet.

WHAT THIS MODULE MUST NOT DO
----------------------------
- No string formatting of tenant_id/region into SQL. They are caller input.
- No silent pass-through when a policy exists but the identity's access
  cannot be resolved. Deny loudly (see step 4).
- No re-deriving which entities are in scope from intent.dimensions/measure --
  read them off the actual AST (expression's tables), so the predicate is
  guaranteed to land on what will actually execute, not on what the intent
  claimed before compilation.
"""

from __future__ import annotations

from typing import Any

from sqlglot import exp

from src.semantic.model import SemanticModel


class SecurityContextError(Exception):
    """The identity resolves to no permitted access for a row-policy-guarded
    entity in this query.

    Raised, never silently reduced to a zero-row predicate -- see contract
    step 4. Should read to the caller as "denied," not "no results."
    """


# ---------------------------------------------------------------------------
# DECISION A -- see module docstring. This is the full, authoritative
# region -> country-set boundary. A region absent from this mapping (or a
# `None` region) resolves to NO access, not to an unrestricted or empty
# query -- see _resolve_principal_value.
#
# VALUES ARE THE LITERAL STRINGS BOTH GUARDED TABLES STORE, NOT ISO CODES.
# thelook_ecommerce.users.country and google_analytics_sample's
# geoNetwork.country both hold full English country names (confirmed by
# querying `SELECT DISTINCT country`/`geoNetwork.country` against each table
# directly) -- an ISO code like "US" or "JP" never matches a real row in
# either one. The predicate this module builds is a literal equality/
# membership check against whatever is actually stored, so the map has to
# speak the data's language, not a code standard the data doesn't use.
#
# thelook's `users` table additionally stores a few entries in the customer's
# own language rather than English for the same country -- "Deutschland"
# alongside "Germany", "España" alongside "Spain", "Brasil" alongside
# "Brazil" -- a real quirk of this public dataset, not a mapping bug. Both
# spellings are listed for those countries; the predicate matches the literal
# stored value; nothing here is normalized or canonicalized behind the scenes
# ("if you don't see it in this list, it isn't covered" has to stay literally
# true for this to be auditable).
#
# Coverage is deliberately not exhaustive-worldwide -- it lists countries a
# region is actually provisioned for today. Extend when a new one is
# provisioned; never patch around it at the query layer.
# ---------------------------------------------------------------------------
REGION_TO_COUNTRIES: dict[str, tuple[str, ...]] = {
    "AMER": (
        "United States", "Canada", "Mexico",
        "Brazil", "Brasil",
        "Argentina", "Colombia", "Costa Rica", "Panama", "Paraguay", "Peru",
        "Uruguay", "Venezuela", "El Salvador", "Puerto Rico", "Haiti",
    ),
    "EMEA": (
        "United Kingdom",
        "Germany", "Deutschland",
        "France",
        "Spain", "España",
        "Italy", "Netherlands", "Belgium", "Austria", "Poland", "Portugal",
        "Ireland", "Sweden", "Norway", "Denmark", "Finland", "Switzerland",
        "Greece", "Romania", "Bulgaria", "Czechia", "Slovakia", "Slovenia",
        "Serbia", "Bosnia & Herzegovina", "Montenegro", "Kosovo", "Albania",
        "Macedonia (FYROM)", "Ukraine", "Russia", "Turkey", "Israel",
        "Saudi Arabia", "United Arab Emirates", "Qatar", "Oman", "Bahrain",
        "Jordan", "Lebanon", "Egypt", "Morocco", "Tunisia", "Algeria",
        "Nigeria", "Ghana", "Kenya", "Ethiopia", "South Africa", "Rwanda",
        "Malawi", "Estonia", "Georgia", "Armenia", "Cyprus", "Malta",
    ),
    "APAC": (
        "Japan", "China", "South Korea", "Australia", "New Zealand",
        "India", "Indonesia", "Malaysia", "Singapore", "Philippines",
        "Thailand", "Vietnam", "Hong Kong", "Taiwan", "Pakistan",
        "Bangladesh", "Sri Lanka", "Nepal", "Mongolia", "Maldives",
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers. Not part of the public contract.
# ---------------------------------------------------------------------------


def _entities_in_scope(expression: exp.Select) -> list[str]:
    """Every entity actually referenced by this query's FROM/JOIN clauses.

    Read off the AST rather than off intent.dimensions/measure: the compiled
    query is what will actually execute, and that is the only thing this
    guard is allowed to trust (see "WHAT THIS MODULE MUST NOT DO"). Layer 1
    aliases every table to its entity name, so `alias_or_name` on each Table
    node already *is* the entity name -- no extra lookup needed.

    Order is preserved (first appearance) and de-duplicated, purely so the
    resulting predicate order is deterministic and easy to test/audit.
    """
    seen: list[str] = []
    for table in expression.find_all(exp.Table):
        entity_name = table.alias_or_name
        if entity_name not in seen:
            seen.append(entity_name)
    return seen


def _resolve_principal_value(
    policy: Any, tenant_id: str | None, region: str | None
) -> tuple[str, Any]:
    """Map a policy's `principal_field` to the caller-supplied value it gates
    on, and resolve that value to what actually belongs in the predicate.

    Returns:
        ("eq", value) for an equality predicate, or ("in", values) for a
        membership predicate.

    Raises:
        SecurityContextError: the principal_field is unrecognized (a model
            config bug), or the caller-supplied value does not resolve to any
            permitted access -- denial must be loud, per contract step 4.
    """
    field_name = policy.principal_field

    if field_name == "region":
        # DECISION A: region is a named group, expanded via REGION_TO_COUNTRIES.
        if region is None:
            raise SecurityContextError(
                "row policy requires a region, but no region was supplied "
                "for this caller"
            )
        countries = REGION_TO_COUNTRIES.get(region)
        if not countries:
            raise SecurityContextError(
                f"region '{region}' does not resolve to any permitted "
                "access; denying rather than emitting a zero-row predicate"
            )
        return "in", countries

    if field_name == "tenant_id":
        # DECISION B: no current row_policy uses this branch -- see module
        # docstring. Kept generic so a future tenant-scoped policy doesn't
        # require touching this function.
        if tenant_id is None:
            raise SecurityContextError(
                "row policy requires a tenant_id, but none was supplied "
                "for this caller"
            )
        return "eq", tenant_id

    # A policy keying off a principal_field this module doesn't know how to
    # resolve is a model/config bug -- it still must fail loudly rather than
    # let the entity through unguarded.
    raise SecurityContextError(
        f"row policy names unrecognized principal_field '{field_name}'"
    )


def _build_policy_predicate(
    policy: Any,
    tenant_id: str | None,
    region: str | None,
    dialect: str,
) -> exp.Expression:
    """`<policy.column> <op> <value>`, built with sqlglot expression builders.

    `policy.column` is trusted, declared model SQL (the same trust level
    intent_compiler.py extends to measure.filters) and may be a dotted/nested
    path (e.g. BigQuery `ga_sessions.geoNetwork.country`), so it is parsed
    with the model's dialect rather than reconstructed by hand.
    `tenant_id`/`region` are caller-supplied and NEVER touch the query as
    text -- they go in through exp.convert, exactly like intent_compiler.py's
    filter values.
    """
    column = exp.maybe_parse(policy.column, dialect=dialect)
    mode, value = _resolve_principal_value(policy, tenant_id, region)

    if mode == "eq":
        return exp.EQ(this=column, expression=exp.convert(value))

    # mode == "in"
    return exp.In(this=column, expressions=[exp.convert(v) for v in value])


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


def inject_security_context(
    expression: exp.Select,
    tenant_id: str | None,
    region: str | None,
    model: SemanticModel,
) -> exp.Select:
    """Rewrite `expression`'s AST to enforce every row_policy that applies to
    an entity actually referenced in this query.

    Args:
        expression: the compiled query's AST (CompiledQuery.expression from
            intent_compiler.py). Mutated via sqlglot's builder methods and
            also returned, matching sqlglot's own convention.
        tenant_id: caller-supplied identifier. See DECISION B -- carried
            through, not yet used to restrict any row today.
        region: caller-supplied identifier. Determines which rows a
            row-policy-guarded entity's rows are visible, per DECISION A.
        model: the loaded, schema-validated SemanticModel -- source of
            row_policies and of which entity backs which table.

    Returns:
        The same expression, with one AND-combined predicate per applicable
        row_policy. An expression with no policy-guarded entities in scope
        is returned unchanged.

    Raises:
        SecurityContextError: a row-policy-guarded entity is in scope and the
            given region/tenant_id does not resolve to any permitted access.

    See the module docstring for the full step-by-step contract and the two
    decisions (A, B) made above.
    """
    for entity_name in _entities_in_scope(expression):
        policy = model.policy_for(entity_name)
        if policy is None:
            # row_policies are opt-in per entity -- untouched, not denied.
            continue

        predicate = _build_policy_predicate(policy, tenant_id, region, model.dialect)
        expression = expression.where(predicate)

    return expression
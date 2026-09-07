"""
WHAT THIS FILE ACTUALLY DOES, IN PLAIN LANGUAGE
------------------------------------------------
This builds the shortcut key for a cache of past answers. If a question was
already answered once, and a semantically identical question comes in again,
we'd rather return the saved result instantly than re-run the whole pipeline
(LLM call, compile, execute) from scratch.

The obvious cache key -- the raw question text -- doesn't work, because two
different phrasings of the same question ("what's our revenue in NY" vs
"show me NY revenue") are different strings and would never hit the same
cache entry, even though they mean the same thing. So instead of hashing the
words someone typed, this hashes the already-validated, structured Intent
those words got translated into. Two paraphrases of the same question
produce the identical Intent, so the cache key becomes a property of
MEANING, not of phrasing.

There's a second requirement that makes this more than a one-line hash: WHO
is asking matters. Two callers can send the identical question and be
entitled to see different rows (that's the entire reason Layer 2 row
security exists). If the cache key ignored identity, the first caller to ask
a question would silently determine what every later caller sees for that
same question -- reopening, one layer up, exactly the leak Layer 2 exists to
close. So the caller's identity has to be folded into the key, not just the
question's meaning.

WHY THIS MODULE EXISTS
------------------------
The naive cache key is the raw question string. It is fragile in exactly the
way that matters: "what's our revenue in NY" and "show me NY revenue" mean
the same thing to the compiler but hash to two different strings, so the
cache never hits for the case it exists to speed up -- paraphrase, not
identical wording, is the common case in real usage.

The fix already lives one layer down: Intent JSON is the compiler's actual
input, and two paraphrases of the same question resolve to the identical
Intent. Hash THAT -- post-validation, pre-SQL (see Intent.canonical(),
src/semantic/intent.py:141, which already produces sorted-key JSON for
exactly this purpose) -- and the cache key becomes a property of MEANING,
not of phrasing.

CONTRACT
--------
    hash_intent(intent, principal) -> str

Steps:

1. START FROM intent.canonical(), NOT model_dump() OR str(intent)
   canonical() already sorts keys and strips whitespace so two
   semantically-identical Intent objects serialize byte-identically
   regardless of construction order. Do not re-derive this here -- if the
   canonical form ever needs to change, it should change in exactly one
   place (src/semantic/intent.py), not be duplicated into this module too.

2. FOLD IN THE PRINCIPAL -- THIS STEP IS NOT OPTIONAL
   Two callers with the identical Intent but different row-level access
   (src/compiler/security.py, Layer 2) must NEVER collide on the same cache
   entry. Without the principal in the key, a cache hit for one tenant's
   request could serve another tenant's rows -- exactly the leak Layer 2
   exists to prevent, reopened one layer up. Include whatever of
   (tenant_id, region) actually participates in row filtering for the
   entities this intent touches; a principal of None should still occupy a
   distinct, stable key (e.g. an explicit sentinel), not be treated the same
   as "no restriction applies to this query" if the query doesn't touch a
   policy-guarded entity at all -- see decision A.

3. HASH WITH SHA-256, RETURN A HEX DIGEST
   Not for secrecy (nothing here is a secret) -- for a fixed-length,
   filesystem/URL/SQLite-key-safe string regardless of how long the
   canonical JSON gets.

DECISIONS MADE (see the module docstring's original DECISIONS TO MAKE section)
-------------------------------------------------------------------------------

  A. DOES A QUERY WITH NO POLICY-GUARDED ENTITY NEED THE PRINCIPAL IN ITS KEY
     AT ALL?
     Always include it, unconditionally. The narrower alternative -- omit
     the principal when none of the intent's entities carry a row_policy --
     raises the cache hit rate slightly, but it means this module has to
     know which entities are policy-guarded, coupling a caching concern to
     semantic_model.yaml's row_policies. That coupling is a second place a
     future policy change has to be remembered (add a row_policy to a
     previously-unguarded entity, and every OLD cache key for that entity
     is now silently wrong unless this module's exemption list is updated
     too). "Always include it" can never leak across principals; it can
     only ever cost an avoidable cache miss between two callers who happen
     to ask the identical unguarded question. That asymmetry is why this is
     the right default absent a measured reason to build the coupling.

  B. WHAT IDENTIFIES "NO PRINCIPAL WAS SUPPLIED" IN THE KEY?
     A fixed literal sentinel (_NO_PRINCIPAL_SENTINEL), folded in as a JSON
     STRING value where a real principal is always folded in as a JSON
     OBJECT value. The two are different JSON types in the hashed payload,
     so no real principal dict -- however it's shaped -- can ever collide
     with the "no principal" case, even by accident (unlike, say, an empty
     dict `{}` or an empty string, either of which a real caller might
     plausibly produce).

WHAT THIS MODULE MUST NOT DO
------------------------------
- No hashing intent.model_dump() or str(intent) directly -- canonical()
  already exists so key order can never cause a false cache miss.
- No omitting the principal by default. Only omit it under decision A's
  narrower, explicit condition, never as a general shortcut. (This build
  does not implement that narrower condition at all -- see decision A.)

Owner: Ajinkya.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.semantic.intent import Intent

# DECISION B: a real principal is always a dict (JSON object) once folded
# into the hash payload; this sentinel is a plain string instead, so the two
# can never collide regardless of what keys/values a real principal has.
_NO_PRINCIPAL_SENTINEL = "__no_principal__"


def hash_intent(intent: Intent, principal: dict | None) -> str:
    """Compute the Layer 4 cache key for a validated Intent and the identity
    asking it.

    Args:
        intent: an already-validated Intent (post src/semantic/intent.py
            validation, pre-compilation -- the same object
            src/compiler/intent_compiler.py::compile() receives).
        principal: the caller's simulated identity as a dict
            (src/models.py::OnBehalfOf.model_dump()), or None if no identity
            was supplied on this request.

    Returns:
        A SHA-256 hex digest identifying this (intent, principal) pair.
        Two calls with semantically-identical intents (regardless of
        surface phrasing that produced them) and the same principal MUST
        return the same digest; two calls differing only in principal MUST
        NOT collide when that principal affects the result set.

    See the module docstring for the full contract and the two decisions
    (A, B) that have to be made before this can be written.
    """
    canonical_intent = intent.canonical()

    # DECISION A: principal is always folded in, unconditionally -- no check
    # here for whether this intent's entities are actually policy-guarded.
    principal_payload: Any = (
        principal if principal is not None else _NO_PRINCIPAL_SENTINEL
    )

    # A single JSON object nesting both pieces, rather than string-
    # concatenating them with a separator: this sidesteps any need to worry
    # about a delimiter accidentally appearing inside the canonical intent
    # string or a principal value. sort_keys applies recursively, so a
    # dict-valued principal is normalized the same way canonical() already
    # normalizes the intent -- key order in `principal` can never cause a
    # false cache miss either.
    payload = json.dumps(
        {"intent": canonical_intent, "principal": principal_payload},
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
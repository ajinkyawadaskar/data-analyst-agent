"""Spec for the Layer 4 cache key (src/cache/intent_hash.py).

Owner of the module under test: Ajinkya. Same discipline as
tests/test_compiler.py and tests/test_security.py: written before the body,
xfail(raises=NotImplementedError) so the suite stays green while it's a
stub, flips to XPASS the moment it lands. Remove the marker then.

Two of the module's own decisions (whether an unguarded query needs the
principal in its key at all, and what sentinel represents "no principal
supplied") are explicitly left to Ajinkya -- see intent_hash.py's module
docstring, decisions A and B. Every test below holds regardless of how B
lands; the one that depends on A is marked.
"""

from __future__ import annotations

import pytest

from src.cache.intent_hash import hash_intent
from src.semantic.intent import Filter, Intent


# ---- the property the whole layer exists for: paraphrase-invariance

def test_identical_intent_from_different_construction_order_hashes_the_same():
    """Two Intent objects that mean the same thing must hash the same, even
    if the caller happened to build them with fields/filters in a different
    order -- this is the paraphrase problem intent_hash.py exists to solve."""
    a = Intent(measure="total_revenue", dimensions=["product_category"], limit=5)
    b = Intent(limit=5, dimensions=["product_category"], measure="total_revenue")
    assert hash_intent(a, principal=None) == hash_intent(b, principal=None)


def test_different_measure_hashes_differently():
    a = Intent(measure="total_revenue")
    b = Intent(measure="order_count")
    assert hash_intent(a, principal=None) != hash_intent(b, principal=None)


def test_different_filter_value_hashes_differently():
    a = Intent(measure="user_count", filters=[Filter(field="user_country", operator="=", value="Japan")])
    b = Intent(measure="user_count", filters=[Filter(field="user_country", operator="=", value="Germany")])
    assert hash_intent(a, principal=None) != hash_intent(b, principal=None)


# ---- the property Layer 2 depends on: principal isolation

def test_same_intent_different_principal_hashes_differently():
    """Without this, a cache hit for one identity could serve another
    identity's row-filtered results -- the exact leak Layer 2 exists to
    prevent, reopened one layer up. This is Layer 4's DoD-equivalent test."""
    intent = Intent(measure="user_count", dimensions=["user_country"])
    amer = hash_intent(intent, principal={"tenant_id": "t1", "region": "AMER"})
    apac = hash_intent(intent, principal={"tenant_id": "t1", "region": "APAC"})
    assert amer != apac


def test_same_intent_different_tenant_same_region_hashes_differently():
    intent = Intent(measure="user_count", dimensions=["user_country"])
    t1 = hash_intent(intent, principal={"tenant_id": "t1", "region": "AMER"})
    t2 = hash_intent(intent, principal={"tenant_id": "t2", "region": "AMER"})
    assert t1 != t2


def test_no_principal_and_a_real_principal_never_collide():
    """Decision B: whatever sentinel represents "no identity supplied" must
    not coincide with a real principal's encoding, however unlikely."""
    intent = Intent(measure="user_count", dimensions=["user_country"])
    none_key = hash_intent(intent, principal=None)
    real_key = hash_intent(intent, principal={"tenant_id": "t1", "region": "AMER"})
    assert none_key != real_key


# ---- determinism and shape

def test_hash_is_deterministic_across_calls():
    intent = Intent(measure="total_revenue", dimensions=["product_category"])
    principal = {"tenant_id": "t1", "region": "AMER"}
    assert hash_intent(intent, principal) == hash_intent(intent, principal)


def test_hash_is_a_hex_sha256_digest():
    intent = Intent(measure="total_revenue")
    key = hash_intent(intent, principal=None)
    assert isinstance(key, str)
    assert len(key) == 64
    int(key, 16)  # raises ValueError if not valid hex

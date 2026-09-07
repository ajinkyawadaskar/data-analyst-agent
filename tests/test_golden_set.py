"""Tests for evals/golden_set.py's schema and loader (owner: Claude).

The 15-25 actual questions in evals/golden_cases.json are Ajinkya's to
write -- these tests only check the schema/validation logic against
hand-built cases, not the eventual real file's content.
"""

from __future__ import annotations

from evals.golden_set import GoldenCase, GoldenCaseSet


def test_missing_file_loads_as_empty_set(tmp_path):
    from evals.golden_set import load

    result = load(tmp_path / "does-not-exist.json")
    assert result.cases == []


def test_structured_case_without_expected_sql_is_a_problem():
    cs = GoldenCaseSet(cases=[GoldenCase(id="g1", kind="structured", question="q")])
    problems = cs.validate_shape()
    assert any("expected_sql" in p for p in problems)


def test_permission_denied_case_without_on_behalf_of_is_a_problem():
    cs = GoldenCaseSet(cases=[GoldenCase(id="g1", kind="permission_denied", question="q")])
    problems = cs.validate_shape()
    assert any("on_behalf_of" in p for p in problems)


def test_cache_hit_repeat_case_without_paraphrase_of_is_a_problem():
    cs = GoldenCaseSet(cases=[GoldenCase(id="g1", kind="cache_hit_repeat", question="q")])
    problems = cs.validate_shape()
    assert any("paraphrase_of" in p for p in problems)


def test_cache_hit_repeat_case_referencing_unknown_id_is_a_problem():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id="g1", kind="cache_hit_repeat", question="q", paraphrase_of="ghost"),
    ])
    problems = cs.validate_shape()
    assert any("unknown case id" in p for p in problems)


def test_valid_cache_hit_repeat_pair_has_no_problems_on_that_front():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id="g1", kind="structured", question="q1", expected_sql="SELECT 1"),
        GoldenCase(id="g2", kind="cache_hit_repeat", question="q2", paraphrase_of="g1"),
    ])
    problems = cs.validate_shape()
    assert not any("paraphrase_of" in p or "unknown case id" in p for p in problems)


def test_stack_case_without_expected_category_is_a_problem():
    cs = GoldenCaseSet(cases=[GoldenCase(id="g1", kind="stack", question="q")])
    problems = cs.validate_shape()
    assert any("expected_note_category" in p for p in problems)


def test_duplicate_ids_are_a_problem():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id="g1", kind="structured", question="q1", expected_sql="SELECT 1"),
        GoldenCase(id="g1", kind="structured", question="q2", expected_sql="SELECT 2"),
    ])
    problems = cs.validate_shape()
    assert any("duplicate id" in p for p in problems)


def test_too_few_cases_is_a_problem():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id=f"g{i}", kind="structured", question="q", expected_sql="SELECT 1")
        for i in range(5)
    ])
    problems = cs.validate_shape()
    assert any("at least 15" in p for p in problems)


def test_too_many_cases_is_a_problem():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id=f"g{i}", kind="structured", question="q", expected_sql="SELECT 1")
        for i in range(30)
    ])
    problems = cs.validate_shape()
    assert any("caps this set at 25" in p for p in problems)


def test_of_kind_filters_correctly():
    cs = GoldenCaseSet(cases=[
        GoldenCase(id="g1", kind="structured", question="q", expected_sql="SELECT 1"),
        GoldenCase(id="g2", kind="stack", question="q", expected_note_category="billing"),
    ])
    assert [c.id for c in cs.of_kind("structured")] == ["g1"]
    assert [c.id for c in cs.of_kind("stack")] == ["g2"]

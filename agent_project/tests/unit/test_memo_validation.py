"""Tests for memo.py — proposal field validation + summary formatting.

Pure deterministic. No LLM calls.

Covers:
    1. _validate_proposal_fields — required vs optional split
    2. _fmt_assumptions_line — display with glide + new mechanics
    3. _TIER_B_REQUIRED / _TIER_B_OPTIONAL set composition
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.graphs.workflows.dcf.memo import (
    _TIER_B_REQUIRED,
    _TIER_B_OPTIONAL,
    _TIER_B_PROPOSABLE,
    _validate_proposal_fields,
    _fmt_assumptions_line,
    AssumptionMemo,
    AssumptionProposal,
)


# ---------------------------------------------------------------------------
# 1. Tier B set composition
# ---------------------------------------------------------------------------

def test_required_set_unchanged():
    """Legacy 4 fields are still required."""
    assert _TIER_B_REQUIRED == frozenset({
        "revenue_growth", "fcff_margin", "terminal_growth", "tax_rate",
    })


def test_optional_set_has_4_mechanics():
    """4 real-world mechanics added as optional."""
    assert _TIER_B_OPTIONAL == frozenset({
        "buyback_yield", "sbc_pct_revenue",
        "revenue_growth_terminal", "fcff_margin_terminal",
    })


def test_proposable_is_union():
    assert _TIER_B_PROPOSABLE == _TIER_B_REQUIRED | _TIER_B_OPTIONAL
    assert len(_TIER_B_PROPOSABLE) == 8


def test_required_and_optional_disjoint():
    assert not (_TIER_B_REQUIRED & _TIER_B_OPTIONAL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_memo(fields_with_values: dict[str, float]) -> AssumptionMemo:
    """Build a minimal AssumptionMemo from {field: value} dict."""
    proposals = [
        AssumptionProposal(
            field=field,
            value=value,
            rationale="test rationale",
            evidence_refs=["ev-test"],
            confidence=0.7,
        )
        for field, value in fields_with_values.items()
    ]
    return AssumptionMemo(
        ticker="TEST",
        horizon_years=5,
        proposals=proposals,
        overall_narrative="test narrative",
        key_uncertainties=["test risk"],
        overall_confidence=0.7,
        evidence_refs=["ev-test"],
    )


# ---------------------------------------------------------------------------
# 2. _validate_proposal_fields
# ---------------------------------------------------------------------------

def test_validate_all_required_only_passes():
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
    })
    assert _validate_proposal_fields(memo) == []


def test_validate_required_plus_optional_passes():
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "buyback_yield": 0.03,
        "sbc_pct_revenue": 0.04,
    })
    assert _validate_proposal_fields(memo) == []


def test_validate_all_8_fields_passes():
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "buyback_yield": 0.03,
        "sbc_pct_revenue": 0.04,
        "revenue_growth_terminal": 0.05,
        "fcff_margin_terminal": 0.28,
    })
    assert _validate_proposal_fields(memo) == []


def test_validate_missing_required_field_fails():
    """Missing one of the 4 required fields → error."""
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        # missing tax_rate
    })
    errors = _validate_proposal_fields(memo)
    assert len(errors) == 1
    assert "tax_rate" in errors[0]
    assert "REQUIRED" in errors[0]


def test_validate_missing_optional_is_fine():
    """Optional fields are not required."""
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        # all 4 optional fields missing — should pass
    })
    assert _validate_proposal_fields(memo) == []


def test_validate_disallowed_field_fails():
    """Proposing a field outside REQUIRED ∪ OPTIONAL → error."""
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "wacc": 0.09,  # not proposable — computed deterministically
    })
    errors = _validate_proposal_fields(memo)
    assert any("wacc" in e and "not allowed" in e for e in errors)


def test_validate_disallowed_tier_a_field_fails():
    """Tier A fields (base_revenue, etc.) are not proposable."""
    memo = _build_memo({
        "revenue_growth": 0.08,
        "fcff_margin": 0.25,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "base_revenue": 100000.0,  # Tier A, locked from canonical
    })
    errors = _validate_proposal_fields(memo)
    assert any("base_revenue" in e for e in errors)


def test_validate_multiple_errors_reported():
    """Missing required + disallowed field → both errors surface."""
    memo = _build_memo({
        "revenue_growth": 0.08,
        # missing fcff_margin, terminal_growth, tax_rate
        "wacc": 0.09,  # disallowed
    })
    errors = _validate_proposal_fields(memo)
    assert len(errors) >= 2  # at least one for missing required, one for disallowed


# ---------------------------------------------------------------------------
# 3. _fmt_assumptions_line
# ---------------------------------------------------------------------------

def test_fmt_shows_required_fields():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "terminal_growth": 0.025, "tax_rate": 0.21, "wacc": 0.09},
        {"method": "capm"},
    )
    assert "growth=8.00%" in line
    assert "margin=25.00%" in line
    assert "terminal_growth=2.50%" in line
    assert "tax_rate=21.00%" in line
    assert "wacc=9.00%(capm)" in line


def test_fmt_growth_glide_shown():
    """When revenue_growth_terminal differs from revenue_growth, show glide."""
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.25, "fcff_margin": 0.40, "revenue_growth_terminal": 0.12},
        {},
    )
    assert "growth=25.0%→12.0%" in line


def test_fmt_growth_no_glide_when_equal():
    """When terminal == start, no arrow."""
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "revenue_growth_terminal": 0.08},
        {},
    )
    assert "→" not in line.split("growth=")[1].split(",")[0]


def test_fmt_margin_glide_shown():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.04, "fcff_margin": 0.019, "fcff_margin_terminal": 0.035},
        {},
    )
    assert "margin=1.9%→3.5%" in line


def test_fmt_buyback_shown_when_nonzero():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "buyback_yield": 0.035},
        {},
    )
    assert "buyback=3.5%" in line


def test_fmt_buyback_hidden_when_zero():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "buyback_yield": 0.0},
        {},
    )
    assert "buyback" not in line


def test_fmt_sbc_shown_when_nonzero():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "sbc_pct_revenue": 0.10},
        {},
    )
    assert "sbc=10.0%" in line


def test_fmt_sbc_hidden_when_zero():
    line = _fmt_assumptions_line(
        {"revenue_growth": 0.08, "fcff_margin": 0.25, "sbc_pct_revenue": 0.0},
        {},
    )
    assert "sbc" not in line


def test_fmt_empty_assumptions_returns_fallback():
    line = _fmt_assumptions_line({}, {})
    assert line == "assumptions built"


def test_fmt_full_glide_and_mechanics():
    """All glides + mechanics combined."""
    line = _fmt_assumptions_line(
        {
            "revenue_growth": 0.15,
            "revenue_growth_terminal": 0.08,
            "fcff_margin": 0.30,
            "fcff_margin_terminal": 0.35,
            "terminal_growth": 0.025,
            "tax_rate": 0.21,
            "wacc": 0.09,
            "buyback_yield": 0.03,
            "sbc_pct_revenue": 0.10,
        },
        {"method": "capm"},
    )
    assert "growth=15.0%→8.0%" in line
    assert "margin=30.0%→35.0%" in line
    assert "buyback=3.0%" in line
    assert "sbc=10.0%" in line

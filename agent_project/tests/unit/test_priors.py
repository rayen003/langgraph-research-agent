"""Tests for priors.py — profile classification, plausibility bands,
valuation sanity checks, confidence breakdown / label.

All deterministic. No LLM, no network.

Test groups:
    1. classify_profile           — sector + market_cap → profile bucket
    2. prior_band_midpoint        — soft band center, fallback to default
    3. check_against_band         — soft/hard band severity emission
    4. check_assumption_plausibility — per-field plausibility scan
    5. check_valuation_sanity     — implied/spot ratio + TV share rails
    6. compute_confidence_breakdown — multi-component scoring + validity gate
    7. compute_confidence_label   — thin wrapper aggregate label
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.graphs.workflows.dcf.priors import (
    classify_profile,
    prior_band_midpoint,
    check_against_band,
    check_assumption_plausibility,
    check_valuation_sanity,
    compute_confidence_breakdown,
    compute_confidence_label,
    enforce_hard_bands,
)


# ---------------------------------------------------------------------------
# 1. classify_profile
# ---------------------------------------------------------------------------

def test_mega_cap_tech_profile():
    """Tech sector + ≥$200B market cap → mega_cap_tech."""
    assert classify_profile("Technology", 3_000_000_000_000) == "mega_cap_tech"


def test_large_cap_tech_under_200B():
    """Tech sector + $50B → large_cap_tech."""
    assert classify_profile("Technology", 50_000_000_000) == "large_cap_tech"


def test_mature_industrial_profile():
    assert classify_profile("Industrials", 100_000_000_000) == "mature_consumer_or_industrial"


def test_communication_services_treated_as_tech():
    """Comms services with mega-cap → mega_cap_tech."""
    assert classify_profile("Communication Services", 500_000_000_000) == "mega_cap_tech"


def test_unknown_sector_falls_back_to_default():
    assert classify_profile("Cryptocurrency", 10_000_000_000) == "default"


def test_none_sector_returns_default():
    assert classify_profile(None, None) == "default"


def test_small_tech_company_below_threshold():
    """Tech but <$10B → not tech bucket → default."""
    assert classify_profile("Technology", 5_000_000_000) == "default"


# ---------------------------------------------------------------------------
# 2. prior_band_midpoint
# ---------------------------------------------------------------------------

def test_midpoint_returns_band_center():
    """mega_cap_tech wacc band = 0.07–0.10 → mid = 0.085."""
    mid = prior_band_midpoint("mega_cap_tech", "wacc")
    assert mid == pytest.approx(0.085, abs=1e-6)


def test_midpoint_unknown_field_returns_none():
    assert prior_band_midpoint("mega_cap_tech", "unknown_field") is None


def test_midpoint_unknown_profile_falls_back():
    """Unknown profile uses default bands → still returns midpoint."""
    mid = prior_band_midpoint("nonsense_profile", "terminal_growth")
    assert mid is not None
    assert 0.01 < mid < 0.05


# ---------------------------------------------------------------------------
# 3. check_against_band
# ---------------------------------------------------------------------------

_TEST_BAND = {"soft_min": 0.05, "soft_max": 0.20, "hard_min": 0.0, "hard_max": 0.40}


def test_band_value_in_soft_range_no_flag():
    flags = check_against_band(field="test", value=0.10, band=_TEST_BAND, profile="default")
    assert flags == []


def test_band_value_below_soft_warns():
    flags = check_against_band(field="test", value=0.03, band=_TEST_BAND, profile="default")
    assert len(flags) == 1
    assert flags[0]["severity"] == "warn"
    assert flags[0]["code"] == "test_below_soft_min"


def test_band_value_above_soft_warns():
    flags = check_against_band(field="test", value=0.30, band=_TEST_BAND, profile="default")
    assert len(flags) == 1
    assert flags[0]["severity"] == "warn"


def test_band_value_below_hard_blocks():
    flags = check_against_band(field="test", value=-0.05, band=_TEST_BAND, profile="default")
    assert len(flags) == 1
    assert flags[0]["severity"] == "block"


def test_band_value_above_hard_blocks():
    flags = check_against_band(field="test", value=0.50, band=_TEST_BAND, profile="default")
    assert len(flags) == 1
    assert flags[0]["severity"] == "block"


def test_band_value_exactly_at_soft_boundary_no_flag():
    """Soft boundaries are inclusive (< not ≤)."""
    flags = check_against_band(field="test", value=0.05, band=_TEST_BAND, profile="default")
    assert flags == []


# ---------------------------------------------------------------------------
# 4. check_assumption_plausibility
# ---------------------------------------------------------------------------

def test_plausibility_all_in_range_returns_empty():
    assumptions = {
        "fcff_margin": 0.25, "wacc": 0.09, "revenue_growth": 0.08,
        "terminal_growth": 0.025, "tax_rate": 0.20,
    }
    flags = check_assumption_plausibility(assumptions, "mega_cap_tech")
    assert flags == []


def test_plausibility_extreme_growth_blocks():
    """50% growth for mega-cap tech (band hard_max 0.40) → block."""
    flags = check_assumption_plausibility({"revenue_growth": 0.50}, "mega_cap_tech")
    assert any(f["severity"] == "block" and f["field"] == "revenue_growth" for f in flags)


def test_plausibility_unknown_profile_uses_default():
    """Unknown profile name falls back to default bands."""
    flags = check_assumption_plausibility({"wacc": 0.09}, "nonsense_profile")
    assert flags == []  # 0.09 is inside default wacc band


def test_plausibility_only_checks_provided_fields():
    """Missing fields don't raise, don't flag."""
    flags = check_assumption_plausibility({"wacc": 0.09}, "mega_cap_tech")
    fields = {f["field"] for f in flags}
    assert "revenue_growth" not in fields
    assert "fcff_margin" not in fields


# ---------------------------------------------------------------------------
# 5. check_valuation_sanity
# ---------------------------------------------------------------------------

def test_sanity_implied_close_to_spot_no_flag():
    valuation = {
        "implied_share_price": 180.0,
        "pv_cash_flows": 900_000.0,
        "enterprise_value": 3_000_000.0,
    }
    flags = check_valuation_sanity(
        valuation=valuation,
        profile="default",
        market_snapshot={"price": 180.0},
    )
    # TV share = 2.1/3.0 = 70% → within soft band 60–95% → no flag
    # implied/spot = 1.0 → within 0.5–2.0 → no flag
    assert flags == []


def test_sanity_implied_far_below_spot_warns():
    """implied=80, spot=180 → ratio 0.44 → soft_min 0.5 violated → warn."""
    valuation = {"implied_share_price": 80.0, "pv_cash_flows": 900_000.0, "enterprise_value": 3_000_000.0}
    flags = check_valuation_sanity(
        valuation=valuation, profile="default",
        market_snapshot={"price": 180.0},
    )
    severities = {f["severity"] for f in flags if f["field"] == "implied_to_spot_price_ratio"}
    assert "warn" in severities or "block" in severities


def test_sanity_implied_collapsed_blocks():
    """ratio < 0.25 → block."""
    valuation = {"implied_share_price": 30.0, "pv_cash_flows": 900_000.0, "enterprise_value": 3_000_000.0}
    flags = check_valuation_sanity(
        valuation=valuation, profile="default",
        market_snapshot={"price": 180.0},
    )
    severities = [f["severity"] for f in flags if f["field"] == "implied_to_spot_price_ratio"]
    assert "block" in severities


def test_sanity_missing_spot_skips_ratio_check():
    """No market price → ratio check skipped, TV share still checked."""
    valuation = {"implied_share_price": 180.0, "pv_cash_flows": 900_000.0, "enterprise_value": 3_000_000.0}
    flags = check_valuation_sanity(
        valuation=valuation, profile="default",
        market_snapshot={"price": 0.0},
    )
    fields = {f["field"] for f in flags}
    assert "implied_to_spot_price_ratio" not in fields


# ---------------------------------------------------------------------------
# 6. compute_confidence_breakdown
# ---------------------------------------------------------------------------

def _good_provenance():
    """All fields from canonical sources, high confidence."""
    return {
        "base_revenue": {"source": "fmp", "confidence": 1.0},
        "revenue_growth": {"source": "llm_memo", "confidence": 0.8},
        "fcff_margin": {"source": "llm_memo", "confidence": 0.8},
        "wacc": {"source": "capm", "confidence": 0.9},
        "terminal_growth": {"source": "llm_memo", "confidence": 0.7},
        "net_debt": {"source": "fmp", "confidence": 1.0},
        "shares_outstanding": {"source": "fmp", "confidence": 1.0},
        "tax_rate": {"source": "fmp", "confidence": 1.0},
    }


def test_confidence_breakdown_has_required_keys():
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    for key in ("components", "aggregate_score", "label", "summary"):
        assert key in result


def test_confidence_label_is_high_with_clean_inputs():
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    assert result["label"] == "high"
    assert result["aggregate_score"] >= 0.70


def test_confidence_block_flag_forces_low():
    """Any block-severity flag → label forced to low regardless of score."""
    result = compute_confidence_breakdown(
        assumption_flags=[{"field": "wacc", "severity": "block"}],
        valuation_flags=[], provenance=_good_provenance(),
    )
    assert result["label"] == "low"


def test_confidence_invalid_model_forces_low():
    """model_validity=invalid → label=low even with perfect inputs."""
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
        model_validity="invalid",
    )
    assert result["label"] == "low"
    assert "validity_penalty" in result["components"]


def test_confidence_adjusting_model_multiplies_score():
    """model_validity=adjusting → aggregate × 0.70."""
    base = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    adjusting = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
        model_validity="adjusting",
    )
    assert adjusting["aggregate_score"] < base["aggregate_score"]
    assert adjusting["aggregate_score"] == pytest.approx(base["aggregate_score"] * 0.70, abs=0.01)


def test_confidence_solver_failed_caps_wacc():
    """solver_failed=True → wacc_reliability component capped at 0.30."""
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
        solver_failed=True,
    )
    wacc_score = result["components"]["wacc_reliability"]["score"]
    assert wacc_score <= 0.30


def test_confidence_unexplained_divergences_penalty():
    """Each unexplained divergence subtracts 0.05, capped at 0.20."""
    no_div = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    with_div = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
        unexplained_count=2,
    )
    delta = no_div["aggregate_score"] - with_div["aggregate_score"]
    assert delta == pytest.approx(0.10, abs=0.01)


def test_confidence_unexplained_penalty_capped_at_020():
    """10 divergences still only subtracts 0.20 (cap)."""
    no_div = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    many_div = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
        unexplained_count=10,
    )
    delta = no_div["aggregate_score"] - many_div["aggregate_score"]
    assert delta <= 0.20 + 0.01  # cap with float epsilon


def test_confidence_fallback_sources_lower_score():
    """Provenance from fallback sources caps individual field score at 0.50."""
    fallback_prov = {
        "wacc": {"source": "profile_prior_mid", "confidence": 0.9},
    }
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance=fallback_prov,
    )
    assert result["components"]["wacc_reliability"]["score"] <= 0.55


def test_confidence_empty_provenance_returns_low_data_quality():
    result = compute_confidence_breakdown(
        assumption_flags=[], valuation_flags=[], provenance={},
    )
    assert result["components"]["data_quality"]["score"] == pytest.approx(0.5, abs=0.01)


def test_confidence_tier_a_block_tanks_data_quality():
    """Block flag on base_revenue / shares / net_debt → data_quality capped at 0.30."""
    result = compute_confidence_breakdown(
        assumption_flags=[{"field": "base_revenue", "severity": "block"}],
        valuation_flags=[], provenance=_good_provenance(),
    )
    assert result["components"]["data_quality"]["score"] <= 0.30


def test_confidence_aggregate_score_in_unit_interval():
    """aggregate_score must always be in [0, 1]."""
    result = compute_confidence_breakdown(
        assumption_flags=[{"field": "wacc", "severity": "block"}] * 10,
        valuation_flags=[{"field": "x", "severity": "block"}] * 10,
        provenance=_good_provenance(),
        model_validity="invalid",
        solver_failed=True,
        unexplained_count=20,
    )
    assert 0.0 <= result["aggregate_score"] <= 1.0


# ---------------------------------------------------------------------------
# 7. compute_confidence_label (thin wrapper)
# ---------------------------------------------------------------------------

def test_label_wrapper_returns_string():
    label = compute_confidence_label(
        assumption_flags=[], valuation_flags=[], provenance=_good_provenance(),
    )
    assert label in ("high", "medium", "low")


def test_label_wrapper_matches_breakdown():
    """compute_confidence_label === compute_confidence_breakdown(...).label"""
    args = {"assumption_flags": [], "valuation_flags": [], "provenance": _good_provenance()}
    assert compute_confidence_label(**args) == compute_confidence_breakdown(**args)["label"]


# ---------------------------------------------------------------------------
# 8. enforce_hard_bands — floor/ceiling clamping (Tier 0 correctness guard)
# ---------------------------------------------------------------------------


def test_enforce_clamps_sub_floor_fcff_margin():
    """The AMZN -$7.64 bug: fcff_margin 0.0283 < mega_cap_tech floor 0.05."""
    out, flags = enforce_hard_bands({"fcff_margin": 0.0283}, "mega_cap_tech")
    assert out["fcff_margin"] == 0.05
    assert len(flags) == 1
    assert flags[0]["field"] == "fcff_margin"
    assert flags[0]["clamped_from"] == 0.0283
    assert flags[0]["clamped_to"] == 0.05
    assert flags[0]["severity"] == "warn"


def test_enforce_clamps_both_margin_fields():
    out, flags = enforce_hard_bands(
        {"fcff_margin": 0.02, "fcff_margin_terminal": 0.01}, "mega_cap_tech"
    )
    assert out["fcff_margin"] == 0.05
    assert out["fcff_margin_terminal"] == 0.05
    assert {f["field"] for f in flags} == {"fcff_margin", "fcff_margin_terminal"}


def test_enforce_clamps_above_hard_max():
    out, flags = enforce_hard_bands({"terminal_growth": 0.10}, "mega_cap_tech")
    assert out["terminal_growth"] == 0.045  # hard_max
    assert flags[0]["code"].endswith("hard_cap")


def test_enforce_noop_when_in_band():
    """In-band assumptions pass through untouched, no flags."""
    a = {"fcff_margin": 0.25, "revenue_growth": 0.15, "terminal_growth": 0.025}
    out, flags = enforce_hard_bands(a, "mega_cap_tech")
    assert out == a
    assert flags == []


def test_enforce_does_not_mutate_input():
    a = {"fcff_margin": 0.01}
    out, _ = enforce_hard_bands(a, "mega_cap_tech")
    assert a["fcff_margin"] == 0.01  # original untouched
    assert out["fcff_margin"] == 0.05


def test_enforce_fields_filter():
    """fields= restricts the clamp (used at the valuation chokepoint)."""
    a = {"fcff_margin": 0.01, "terminal_growth": 0.10}
    out, flags = enforce_hard_bands(a, "mega_cap_tech", fields={"fcff_margin"})
    assert out["fcff_margin"] == 0.05         # clamped
    assert out["terminal_growth"] == 0.10     # untouched (not in fields)
    assert {f["field"] for f in flags} == {"fcff_margin"}


def test_enforce_prevents_negative_valuation():
    """End-to-end: a clamped sub-floor margin yields a POSITIVE implied price.

    Reproduces the AMZN inputs (margin below SBC drag) and asserts the clamp
    turns a degenerate negative price positive.
    """
    from agent_project.graphs.workflows.dcf.valuation import _dcf_value_from_assumptions

    raw = {
        "base_revenue": 716924.0, "revenue_growth": 0.16,
        "fcff_margin": 0.0283, "fcff_margin_terminal": 0.015,
        "revenue_growth_terminal": 0.07, "terminal_growth": 0.02,
        "wacc": 0.10, "sbc_pct_revenue": 0.0272,
        "net_debt": -57381.0, "shares_outstanding": 10827.0,
    }
    assert _dcf_value_from_assumptions(raw) < 0  # the bug: negative price

    clamped, _ = enforce_hard_bands(raw, "mega_cap_tech")
    assert _dcf_value_from_assumptions(clamped) > 0  # fixed: positive price

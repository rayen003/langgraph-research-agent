"""Regression tests for the profile-based WACC stack and the coherence gate.

These cover the general fixes — never ticker-specific:
  * mega-cap quality features pull WACC below pure CAPM
  * non-mega-cap profiles get no quality discount
  * the profile soft band always clamps the final WACC
  * refinement / analysis adjustments cannot escape the band
  * the coherence gate detects ops/WACC mismatches and auto-corrects
    when WACC is not user-pinned
"""

from __future__ import annotations

import pytest

from graphs.workflows.dcf.coherence import (
    assess_assumption_coherence,
    coherence_gate_node,
)
from graphs.workflows.dcf.refinement import refine_assumptions_node
from graphs.workflows.dcf.wacc import (
    _profile_wacc_band,
    append_wacc_stack_delta,
    apply_profile_wacc_stack,
    clip_wacc_to_profile_band,
    resolve_wacc_from_features,
)


# ---------------------------------------------------------------------------
# WACC stack
# ---------------------------------------------------------------------------


def test_mega_cap_quality_features_apply_durability_discount():
    """A mega-cap with low beta + high margin + net cash gets a discount."""
    features = {
        "market_cap_usd": 3.5e12,
        "beta": 1.05,
        "net_debt_usd": -50_000_000_000.0,  # net cash
    }
    assumptions = {"fcff_margin": 0.27}
    out = apply_profile_wacc_stack(
        0.10,
        profile="mega_cap_tech",
        features=features,
        assumptions=assumptions,
    )
    # Three discounts: durability (-75bps) + high margin (-50bps) + net-cash (-25bps).
    assert out["quality_delta"] == -0.0150
    assert out["pre_clip"] == 0.0850
    # Within the mega_cap_tech soft band (7-10%), so no clip.
    assert out["clipped"] is False
    assert out["final_wacc"] == 0.0850


def test_mega_cap_stack_clips_to_ceiling_when_capm_runs_hot():
    """Even if CAPM gives 11%+, the stack clips to the profile soft ceiling."""
    out = apply_profile_wacc_stack(
        0.115,
        profile="mega_cap_tech",
        features={"market_cap_usd": 3.0e12, "beta": 1.05, "net_debt_usd": -50e9},
        assumptions={"fcff_margin": 0.25},
    )
    band = _profile_wacc_band("mega_cap_tech")
    # quality_delta = -0.0150, pre_clip = 0.1000
    assert out["quality_delta"] == -0.0150
    assert out["pre_clip"] == 0.1000
    assert out["clipped"] is False
    assert out["final_wacc"] == band["soft_max"] == 0.10


def test_default_profile_gets_no_quality_discount():
    """Profile=default deserves no implicit quality premium."""
    out = apply_profile_wacc_stack(
        0.09,
        profile="default",
        features={"market_cap_usd": 1.0e10, "beta": 1.4, "net_debt_usd": 5e9},
        assumptions={"fcff_margin": 0.10},
    )
    assert out["quality_delta"] == 0.0
    assert out["final_wacc"] == 0.09


def test_clip_wacc_respects_user_override():
    """User-pinned WACC must pass through unchanged."""
    raw, was_clipped = clip_wacc_to_profile_band(
        0.14, profile="mega_cap_tech", allow_override=True,
    )
    assert raw == 0.14 and was_clipped is False

    raw, was_clipped = clip_wacc_to_profile_band(
        0.14, profile="mega_cap_tech", allow_override=False,
    )
    assert raw == _profile_wacc_band("mega_cap_tech")["soft_max"]
    assert was_clipped is True


def test_resolve_wacc_writes_stack_components_into_audit_trail():
    """resolve_wacc_from_features must persist the stack for the report."""
    assumptions: dict[str, float] = {"tax_rate": 0.21, "fcff_margin": 0.26}
    provenance: dict[str, dict] = {}
    features = {
        "beta": 1.10,
        "equity_value_usd": 3.5e12,
        "market_cap_usd": 3.5e12,
        "total_debt_usd": 100e9,
        "interest_expense_usd": 4e9,
        "net_debt_usd": -50e9,
    }
    out = resolve_wacc_from_features(
        assumptions, provenance,
        features=features, profile="mega_cap_tech", overrides={},
    )
    assert "wacc_stack" in out
    stack = out["wacc_stack"]
    assert stack["base_capm"] > 0
    # Discount must show up.
    assert stack["quality_delta"] < 0
    band = stack["profile_band"]
    assert band["soft_min"] <= assumptions["wacc"] <= band["soft_max"]


# ---------------------------------------------------------------------------
# Refinement / analysis cannot escape the band
# ---------------------------------------------------------------------------


def test_refinement_clamps_wacc_to_profile_band():
    """A critique that pushes WACC above the soft ceiling is clipped."""
    state = {
        "profile": "mega_cap_tech",
        "assumptions": {"wacc": 0.099, "revenue_growth": 0.06, "fcff_margin": 0.25},
        "assumption_provenance": {"wacc": {"source": "capm"}},
        "critique": {
            "suggested_adjustments": {"wacc": 0.025},  # +250bps
            "interpretation": "test",
        },
        "parent_step_id": "test",
    }
    out = refine_assumptions_node(state)  # type: ignore[arg-type]
    band = _profile_wacc_band("mega_cap_tech")
    assert out["assumptions"]["wacc"] <= band["soft_max"] + 1e-9


def test_refinement_respects_user_override_outside_band():
    state = {
        "profile": "mega_cap_tech",
        "assumptions": {"wacc": 0.13, "revenue_growth": 0.06, "fcff_margin": 0.25},
        "assumption_provenance": {"wacc": {"source": "user_override"}},
        "critique": {
            "suggested_adjustments": {"wacc": 0.005},
            "interpretation": "test",
        },
        "parent_step_id": "test",
    }
    out = refine_assumptions_node(state)  # type: ignore[arg-type]
    # User override allows the value to stay above the band.
    assert out["assumptions"]["wacc"] > _profile_wacc_band("mega_cap_tech")["soft_max"]


# ---------------------------------------------------------------------------
# Coherence gate
# ---------------------------------------------------------------------------


def test_coherence_detects_strong_ops_with_high_wacc():
    """Bullish growth + margin + buybacks + WACC above soft_max → mismatch."""
    result = assess_assumption_coherence(
        profile="mega_cap_tech",
        assumptions={
            "revenue_growth": 0.18,
            "fcff_margin": 0.30,
            "buyback_yield": 0.03,
            "wacc": 0.115,
        },
    )
    assert result["status"] == "mismatch"
    assert result["ops_tier"] == "strong"
    assert result["wacc_tier"] in {"high", "above_band"}
    assert "wacc" in result["suggested_adjustments"]
    assert result["suggested_adjustments"]["wacc"] < 0


def test_coherence_silent_when_assumptions_are_consistent():
    """Strong ops + WACC near profile midpoint → no flag."""
    result = assess_assumption_coherence(
        profile="mega_cap_tech",
        assumptions={
            "revenue_growth": 0.15,
            "fcff_margin": 0.28,
            "buyback_yield": 0.03,
            "wacc": 0.085,
        },
    )
    assert result["status"] == "ok"
    assert result["suggested_adjustments"] == {}


def test_coherence_gate_node_auto_corrects_when_wacc_not_user_pinned():
    """The gate pulls WACC down to the profile midpoint when ops are strong."""
    state = {
        "profile": "mega_cap_tech",
        "assumptions": {
            "revenue_growth": 0.18,
            "fcff_margin": 0.30,
            "buyback_yield": 0.03,
            "wacc": 0.115,
        },
        "assumption_provenance": {"wacc": {"source": "capm"}},
        "features": {},
        "parent_step_id": "test",
    }
    out = coherence_gate_node(state)
    band = _profile_wacc_band("mega_cap_tech")
    new_wacc = out["assumptions"]["wacc"]
    assert band["soft_min"] <= new_wacc <= band["soft_max"]
    assert "wacc" in out["coherence_adjustments"]
    adj = out["coherence_adjustments"]["wacc"]
    assert adj["new"] < adj["old"]
    prov = out["assumption_provenance"]["wacc"]
    assert prov["coherence_adjusted"] is True


def test_coherence_gate_enforces_profile_band_before_mismatch_logic():
    """Review-loop drift above the soft ceiling is clipped even without mismatch."""
    state = {
        "profile": "mega_cap_tech",
        "assumptions": {
            "revenue_growth": 0.15,
            "fcff_margin": 0.28,
            "buyback_yield": 0.03,
            "wacc": 0.1165,
        },
        "assumption_provenance": {"wacc": {"source": "capm"}},
        "features": {},
        "wacc_components": {"wacc_stack": {"final_wacc": 0.1165}},
        "parent_step_id": "test",
    }
    out = coherence_gate_node(state)
    band = _profile_wacc_band("mega_cap_tech")
    assert out["assumptions"]["wacc"] <= band["soft_max"] + 1e-9
    assert out["coherence_adjustments"]["wacc"]["old"] == 0.1165


def test_append_wacc_stack_delta_records_post_stack_review_bump():
    stack = apply_profile_wacc_stack(
        0.1015,
        profile="mega_cap_tech",
        features={"market_cap_usd": 4.5e12, "beta": 1.065, "net_debt_usd": 24e9},
        assumptions={"fcff_margin": 0.255},
    )
    out = append_wacc_stack_delta(
        {"wacc_stack": stack, "method": "capm"},
        old_wacc=stack["final_wacc"],
        new_wacc=stack["final_wacc"] + 0.01,
        label="Review-loop adjustment (pass 1)",
        source="review_loop",
    )
    assert out["wacc_stack"]["profile_stack_wacc"] == stack["final_wacc"]
    assert out["wacc_stack"]["final_wacc"] == stack["final_wacc"] + 0.01
    assert out["wacc_stack"]["components"][-1]["delta"] == pytest.approx(0.01)


def test_report_shows_valuation_wacc_after_review_stack_bump():
    from agent_project.graphs.workflows.dcf.payload import summarize_dcf_payload

    stack = apply_profile_wacc_stack(
        0.1015,
        profile="mega_cap_tech",
        features={"market_cap_usd": 4.5e12, "beta": 1.065, "net_debt_usd": 24e9},
        assumptions={"fcff_margin": 0.255},
    )
    wacc_components = append_wacc_stack_delta(
        {"wacc_stack": stack, "method": "capm"},
        old_wacc=stack["final_wacc"],
        new_wacc=stack["final_wacc"] + 0.01,
        label="Review-loop adjustment (pass 1)",
        source="review_loop",
    )
    payload = {
        "ticker": "AAPL",
        "horizon_years": 5,
        "assumptions": {
            "wacc": stack["final_wacc"] + 0.01,
            "base_revenue": 416_161.0,
            "shares_outstanding": 15_005.0,
            "net_debt": 23_631.0,
            "revenue_growth": 0.20,
            "fcff_margin": 0.255,
            "terminal_growth": 0.02,
            "tax_rate": 0.176,
        },
        "valuation": {
            "implied_share_price": 138.11,
            "current_price": 308.82,
            "pv_cash_flows": 478_636.0,
            "terminal_pv": 1_418_126.0,
            "enterprise_value": 1_896_763.0,
            "equity_value": 1_873_132.0,
        },
        "wacc_components": wacc_components,
        "model_validity": "valid",
        "confidence_label": "high",
    }
    summary = summarize_dcf_payload(payload)
    assert "Review-loop adjustment (pass 1): +1.00%" in summary
    assert "**WACC used in valuation: 9.90%**" in summary
    assert "**Final WACC: 8.90%**" not in summary


def test_coherence_gate_respects_user_pinned_wacc():
    """User overrides must never be auto-corrected by the gate."""
    state = {
        "profile": "mega_cap_tech",
        "assumptions": {
            "revenue_growth": 0.18,
            "fcff_margin": 0.30,
            "buyback_yield": 0.03,
            "wacc": 0.115,
        },
        "assumption_provenance": {"wacc": {"source": "user_override"}},
        "features": {},
        "parent_step_id": "test",
    }
    out = coherence_gate_node(state)
    assert out["assumptions"]["wacc"] == 0.115
    assert out.get("coherence_adjustments", {}) == {}
    # Assessment still surfaces the mismatch for transparency.
    assert out["coherence_assessment"]["status"] == "mismatch"

"""Regression tests for DCF valuation math and review routing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.analysis import (
    assess_evidence_grounding,
    confidence_assessment_from_positions,
)
from agent_project.graphs.workflows.dcf.review_loop import route_after_review, route_after_review_val
from agent_project.graphs.workflows.dcf.valuation import (
    _dcf_value_from_assumptions,
    classify_implied_signal,
    compute_market_signals_node,
    compute_implied_growth,
    compute_implied_margin,
    project_cashflows_node,
    sensitivity_node,
    wacc_gap_is_binding,
)

AAPL_ASSUMPTIONS = {
    "base_revenue": 416_161.0,
    "revenue_growth": 0.16,
    "revenue_growth_terminal": 0.03,
    "fcff_margin": 0.24,
    "fcff_margin_terminal": 0.22,
    "sbc_pct_revenue": 0.04,
    "buyback_yield": 0.035,
    "wacc": 0.1015,
    "terminal_growth": 0.02,
    "net_debt": 23_631.0,
    "shares_outstanding": 15_005.0,
}


def test_aapl_forward_price_includes_terminal_buyback_compounding():
    """A2: terminal buyback compounding boosts per-share price ~10-25% for AAPL."""
    price = _dcf_value_from_assumptions(AAPL_ASSUMPTIONS)
    no_bb = _dcf_value_from_assumptions({**AAPL_ASSUMPTIONS, "buyback_yield": 0.0})
    assert price > no_bb * 1.05, (
        f"Buyback compounding should lift price meaningfully; got {price} vs no-bb {no_bb}"
    )
    assert 110 <= price <= 200


def test_sensitivity_center_matches_forward_valuation_with_buybacks(monkeypatch):
    monkeypatch.setattr("agent_project.graphs.workflows.dcf.valuation.emit_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent_project.graphs.workflows.dcf.valuation._render_sensitivity_heatmap", lambda *_args, **_kwargs: None)

    state = {
        "ticker": "AAPL",
        "horizon_years": 5,
        "assumptions": dict(AAPL_ASSUMPTIONS),
        "parent_step_id": "test",
    }
    state.update(project_cashflows_node(state))
    result = sensitivity_node(state)
    center = next(
        row for row in result["sensitivity_table"]
        if row["wacc"] == round(AAPL_ASSUMPTIONS["wacc"], 4)
        and row["terminal_growth"] == round(AAPL_ASSUMPTIONS["terminal_growth"], 4)
    )

    assert center["implied_share_price"] == round(_dcf_value_from_assumptions(AAPL_ASSUMPTIONS), 4)


def test_market_signals_compare_implied_wacc_to_final_assumption_wacc(monkeypatch):
    monkeypatch.setattr("agent_project.graphs.workflows.dcf.valuation.emit_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent_project.graphs.workflows.dcf.wacc.solve_implied_wacc", lambda *_args, **_kwargs: 0.08)

    state = {
        "ticker": "AAPL",
        "assumptions": {**AAPL_ASSUMPTIONS, "wacc": 0.1115},
        "wacc_components": {"wacc_pre_clip": 0.1015, "risk_free_rate": 0.045},
        "projected_fcff": [{"year": 1, "fcff": 100_000.0}],
        "market_snapshot": {"price": 308.82},
        "valuation": {"implied_share_price": 177.82},
        "parent_step_id": "test",
    }

    result = compute_market_signals_node(state)

    assert result["wacc_sanity"]["capm_wacc"] == 0.1115
    assert result["wacc_sanity"]["gap_bps"] == 315


def test_classify_implied_signal_thresholds():
    """A1: spread<150bps = implausible, <300bps = aggressive, <600 = reasonable, ≥600 = conservative."""
    # AAPL-style 4.32% on 4.5% rf → implausible
    assert classify_implied_signal(0.0432, 0.045)["label"] == "economically_implausible"
    # 5.94% (the user's report) → still implausible (only 144bps spread)
    assert classify_implied_signal(0.0594, 0.045)["label"] == "economically_implausible"
    # 6.5% → aggressive (200bps)
    assert classify_implied_signal(0.065, 0.045)["label"] == "aggressive"
    # 8.0% → reasonable (350bps)
    assert classify_implied_signal(0.080, 0.045)["label"] == "reasonable"
    # 12% → conservative (750bps)
    assert classify_implied_signal(0.120, 0.045)["label"] == "conservative"
    assert classify_implied_signal(None, 0.045)["label"] == "unavailable"


def test_classify_implied_signal_narrative_warns_on_implausible():
    result = classify_implied_signal(0.045, 0.045)
    assert "economically_implausible" == result["label"]
    assert "implausible" in result["narrative"].lower()


def test_evidence_grounding_uses_source_tier_field():
    """Evidence items use source_tier, not src_tier — must detect filings in pack."""
    grounding = assess_evidence_grounding(
        assumption_memo={
            "proposals": [{"field": "revenue_growth", "evidence_refs": ["ev_web_1"]}],
        },
        evidence_pack={
            "items": [
                {"evidence_id": "ev_sec_a", "source_tier": "filing"},
                {"evidence_id": "ev_web_1", "source_tier": "news"},
            ],
        },
    )
    assert grounding["label"] == "weak_grounding"
    assert grounding["interpretive_multiplier"] == 0.80


def test_evidence_grounding_counts_sec_refs_from_reasoning_artifacts():
    """Rendered SEC refs outside evidence_pack still mean filings were available."""
    grounding = assess_evidence_grounding(
        assumption_memo={
            "proposals": [{"field": "revenue_growth", "evidence_refs": ["ev_web_1"]}],
        },
        evidence_pack={
            "items": [{"evidence_id": "ev_web_1", "source_tier": "news"}],
        },
        extra_evidence_refs=["ev_sec_0000320193260000_risk_factors"],
    )

    assert grounding["label"] == "weak_grounding"
    assert "filings are present" in grounding["reason"]


def test_evidence_grounding_strong_when_filings_cited():
    grounding = assess_evidence_grounding(
        assumption_memo={
            "proposals": [{"field": "revenue_growth", "evidence_refs": ["ev_sec_a"]}],
        },
        evidence_pack={
            "items": [{"evidence_id": "ev_sec_a", "src_tier": "filing"}],
        },
    )
    assert grounding["label"] == "grounded"
    assert grounding["interpretive_multiplier"] == 1.0


def test_evidence_grounding_penalty_drags_interpretive_only():
    """A5 + procedural decoupling: grounding penalty must NOT touch procedural."""
    assessment = confidence_assessment_from_positions(
        positions=[],
        model_validity="valid",
        procedural_base=0.85,
        evidence_grounding={
            "interpretive_multiplier": 0.80,
            "label": "weak_grounding",
            "reason": "no SEC filings cited",
        },
    )
    assert assessment["procedural_confidence"] == 0.85
    assert assessment["interpretive_confidence"] < 0.85
    assert assessment["evidence_grounding"]["label"] == "weak_grounding"


def test_wacc_binding_detects_aapl_like_gap():
    assert wacc_gap_is_binding(
        {"solver_status": "ok", "gap_bps": 369},
        implied_share_price=154.04,
        spot_price=308.82,
    )
    assert not wacc_gap_is_binding(
        {"solver_status": "ok", "gap_bps": 50},
        implied_share_price=280.0,
        spot_price=308.82,
    )


def test_review_routes_to_coherence_gate_when_should_refine():
    state = {"critique": {"should_refine": True}}
    assert route_after_review(state) == "coherence_gate"
    assert route_after_review_val(state) == "coherence_gate"


def test_review_routes_to_divergences_when_no_refine():
    state = {"critique": {"should_refine": False}}
    assert route_after_review(state) == "detect_divergences"
    assert route_after_review_val(state) == "detect_divergences"

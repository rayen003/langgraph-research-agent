"""Tests for convergence_gate validity vs reconciliation posture."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.analysis import (
    confidence_assessment_from_positions,
    convergence_gate_node,
    conviction_direction_from_positions,
    wacc_gap_interpretation,
)
from agent_project.graphs.workflows.dcf.payload import summarize_dcf_payload
from agent_project.tests.helpers import build_test_payload


def _gate(positions, iteration=2, aggregate_score=0.82):
    return convergence_gate_node({
        "parent_step_id": "test",
        "analysis_positions": positions,
        "analysis_iteration": iteration,
        "confidence_breakdown": {"aggregate_score": aggregate_score},
        "confidence_label": "high",
    })


def test_unexplained_market_gaps_stay_valid_with_structural_gap():
    result = _gate([
        {
            "position": "UNEXPLAINED",
            "divergence_verdict": "unsupported",
            "divergence_severity": "high",
            "divergence_summary": "WACC gap 369bps",
        },
    ], iteration=2)
    assert result["model_validity"] == "valid"
    assert result["reconciliation_status"] == "structural_gap"
    assert result["invalidation_reason"] == ""
    assert result["conviction_direction"] == "unresolved_expectations"
    assert result["confidence_assessment"]["procedural_confidence"] >= 0.5
    assert result["confidence_assessment"]["interpretive_confidence"] < 0.7


def test_procedural_confidence_not_penalized_by_divergence_penalty():
    assessment = confidence_assessment_from_positions(
        positions=[
            {"position": "UNEXPLAINED", "divergence_verdict": "unsupported", "evidence_used": ["ev1"]},
            {"position": "UNEXPLAINED", "divergence_verdict": "unsupported", "evidence_used": ["ev2"]},
            {"position": "UNEXPLAINED", "divergence_verdict": "unsupported", "evidence_used": ["ev3"]},
        ],
        model_validity="valid",
        procedural_base=0.82,
    )
    assert assessment["procedural_confidence"] == 0.82
    assert assessment["interpretive_confidence"] < 0.82


def test_critical_unexplained_still_invalid():
    result = _gate([
        {
            "position": "UNEXPLAINED",
            "divergence_verdict": "insufficient_evidence",
            "divergence_severity": "critical",
            "divergence_summary": "Implied WACC solver failed (no_input).",
        },
    ])
    assert result["model_validity"] == "invalid"
    assert result["reconciliation_status"] == "critical_unresolved"


def test_contradicted_gap_does_not_imply_market_overpaying():
    unsupported = conviction_direction_from_positions([
        {"position": "UNEXPLAINED", "divergence_verdict": "unsupported"},
    ])
    contradicted = conviction_direction_from_positions([
        {"position": "UNEXPLAINED", "divergence_verdict": "contradicted"},
    ])

    assert unsupported == "unresolved_expectations"
    assert contradicted == "evidence_conflicts_with_implied"


def test_confidence_assessment_splits_procedural_and_interpretive_axes():
    assessment = confidence_assessment_from_positions(
        positions=[
            {"position": "UNEXPLAINED", "divergence_verdict": "unsupported", "evidence_used": ["ev1"]},
            {"position": "UNEXPLAINED", "divergence_verdict": "insufficient_evidence", "evidence_used": []},
        ],
        model_validity="valid",
        procedural_base=0.85,
    )

    assert assessment["procedural_confidence"] == 0.85
    assert assessment["interpretive_confidence"] < assessment["procedural_confidence"]
    assert assessment["verdict_counts"]["unsupported"] == 1


def test_wacc_gap_interpretation_is_directional():
    interpretation = wacc_gap_interpretation({
        "capm_wacc": 0.11,
        "implied_wacc": 0.07,
    })

    assert interpretation
    assert interpretation["gap_direction"] == "market_lower_than_model"
    assert "do not increase WACC solely because the gap is large" in interpretation["suggested_actions"]


def test_structural_gap_summary_tells_assistant_model_is_valid():
    payload = build_test_payload(
        model_validity="valid",
        reconciliation_status="structural_gap",
        reconciliation_note="Price embeds higher growth than model.",
        wacc_sanity={
            "capm_wacc": 0.101,
            "implied_wacc": 0.065,
            "gap_bps": 369,
            "solver_status": "ok",
        },
        implied_growth=0.353,
        implied_margin=0.473,
        confidence_assessment={
            "procedural_confidence": 0.82,
            "interpretive_confidence": 0.42,
            "evidence_coverage": 0.7,
            "reconciliation_confidence": 0.3,
        },
        assumptions={
            **build_test_payload()["assumptions"],
            "revenue_growth": 0.165,
            "fcff_margin": 0.237,
            "wacc": 0.101,
        },
    )
    summary = summarize_dcf_payload(payload)
    assert "MODEL MARKED INVALID" not in summary
    assert "**Model validity:** VALID" in summary
    assert "**Procedural confidence:** HIGH (82%)" in summary
    assert "**Interpretive confidence:** LOW (42%)" in summary
    assert "## Market Reconciliation" in summary
    assert "DCF-consistent implied" in summary
    assert "not direct market forecasts" in summary
    assert "How to present this report (MANDATORY)" not in summary

"""Tests for convergence_gate validity vs reconciliation posture."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.analysis import convergence_gate_node
from agent_project.graphs.workflows.dcf.payload import summarize_dcf_payload
from agent_project.tests.helpers import build_test_payload


def _gate(positions, iteration=2):
    return convergence_gate_node({
        "parent_step_id": "test",
        "analysis_positions": positions,
        "analysis_iteration": iteration,
    })


def test_unexplained_market_gaps_stay_valid_with_structural_gap():
    result = _gate([
        {
            "position": "UNEXPLAINED",
            "divergence_severity": "high",
            "divergence_summary": "WACC gap 369bps",
        },
        {
            "position": "UNEXPLAINED",
            "divergence_severity": "medium",
            "divergence_summary": "Growth gap 18.8pp",
        },
    ])
    assert result["model_validity"] == "valid"
    assert result["reconciliation_status"] == "structural_gap"
    assert result["invalidation_reason"] == ""


def test_critical_unexplained_still_invalid():
    result = _gate([
        {
            "position": "UNEXPLAINED",
            "divergence_severity": "critical",
            "divergence_summary": "Implied WACC solver failed (no_input).",
        },
    ])
    assert result["model_validity"] == "invalid"
    assert result["reconciliation_status"] == "critical_unresolved"


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
    assert "## Market Reconciliation" in summary
    assert "spreadsheet failed or the model is invalid" in summary
    assert "How to present this report (MANDATORY)" not in summary

"""Tier 3 DCF tests — severity convergence (#6), adjustment causality (#3),
driver-based scenarios (#5). Deterministic, no LLM, no network."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import os as _os
_os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from agent_project.graphs.workflows.dcf.review_graph import (
    _severity_score,
    _expected_effect,
    synthesize_adjustments_node,
)
from agent_project.graphs.workflows.dcf.review_state import (
    ReviewFindings,
    ScenarioFinding,
)


def _finding(field, direction, sev, conf=0.8, scenario="all"):
    return ScenarioFinding(
        scenario=scenario, field=field, direction=direction,
        confidence=conf, severity=sev, layer="consistency",
        reasoning=f"{field} {direction} because evidence X.",
    )


def _findings(*items, should_stop=False):
    return ReviewFindings(
        evidence_memo_findings=list(items),
        thesis_assumption_findings=[],
        consistency_findings=[],
        scenario_distinguishability_findings=[],
        anchoring_flags=[],
        should_stop=should_stop,
        stop_reasoning="",
    )


# ── #6 severity score ──────────────────────────────────────────────────────


def test_severity_score_weights_by_severity_and_confidence():
    f = _findings(
        _finding("wacc", "higher", "high", 1.0),    # 3.0 * 1.0 = 3.0
        _finding("fcff_margin", "lower", "medium", 0.5),  # 2.0 * 0.5 = 1.0
        _finding("tax_rate", "higher", "low", 1.0),  # 1.0 * 1.0 = 1.0
    )
    assert _severity_score(f) == 5.0


def test_severity_score_none_is_zero():
    assert _severity_score(None) == 0.0


# ── #3 expected effect direction ───────────────────────────────────────────


def test_expected_effect_growth_up_raises_price():
    assert "raises implied price" in _expected_effect("revenue_growth", "higher")


def test_expected_effect_growth_down_lowers_price():
    assert "lowers implied price" in _expected_effect("revenue_growth", "lower")


def test_expected_effect_wacc_up_lowers_price():
    # higher discount rate → lower PV
    assert "lowers implied price" in _expected_effect("wacc", "higher")


# ── #3 synthesize_adjustments emits causal change_records ───────────────────


def test_synthesize_adjustments_emits_change_records():
    findings = _findings(
        _finding("revenue_growth", "lower", "high", 0.9, scenario="base"),
    )
    state = {
        "findings": findings,
        "scenarios": [
            {"name": "base", "assumptions": {"revenue_growth": 0.20}},
        ],
        "assumption_history": [],
        "current_assumptions": {"revenue_growth": 0.20},
        "ticker": "TEST",
        "review_iteration": 0,
        "severity_score": _severity_score(findings),
    }
    out = synthesize_adjustments_node(state)
    recs = out["change_records"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["scenario"] == "base" and rec["field"] == "revenue_growth"
    assert rec["delta"] < 0  # lowered
    assert "evidence X" in rec["finding"]
    assert "lowers implied price" in rec["expected_effect"]
    assert out["severity_score"] == state["severity_score"]


def test_synthesize_adjustments_stop_when_findings_say_stop():
    findings = _findings(should_stop=True)
    state = {
        "findings": findings, "scenarios": [{"name": "base", "assumptions": {}}],
        "assumption_history": [], "current_assumptions": {}, "ticker": "T",
        "review_iteration": 0,
    }
    out = synthesize_adjustments_node(state)
    assert out["should_stop"] is True
    assert out["suggested_adjustments"] == {}

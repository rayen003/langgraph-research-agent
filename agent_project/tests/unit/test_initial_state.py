"""Tests for _build_initial_state in graph.py.

Verifies the initial state has correct defaults and honours overrides.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.graph import (
    _build_initial_state,
    _canonical_assumptions_from_snapshot,
)
from agent_project.graphs.workflows.dcf.state import filter_user_assumption_overrides


def _make(**kwargs):
    defaults = dict(
        ticker="AAPL",
        horizon_years=5,
        assumption_review_mode=False,
        allow_external_assumptions=True,
        assumption_overrides={},
        parent_step_id="test",
        session_id="test-session",
    )
    defaults.update(kwargs)
    return _build_initial_state(**defaults)


def test_ticker_propagated():
    s = _make(ticker="MSFT")
    assert s["ticker"] == "MSFT"


def test_horizon_years_propagated():
    s = _make(horizon_years=10)
    assert s["horizon_years"] == 10


def test_defaults_not_approved():
    s = _make()
    assert s["assumptions_approved"] is False


def test_empty_assumptions_by_default():
    s = _make()
    assert s["assumptions"] == {}


def test_empty_scenarios_by_default():
    s = _make()
    assert s["scenarios"] == []


def test_analysis_iteration_zero():
    s = _make()
    assert s["analysis_iteration"] == 0


def test_model_validity_valid():
    s = _make()
    assert s["model_validity"] == "valid"


def test_all_required_keys_present():
    s = _make()
    required = [
        "ticker", "horizon_years", "session_id", "assumption_review_mode",
        "allow_external_assumptions", "assumption_overrides", "assumptions",
        "assumption_provenance", "assumptions_approved", "fundamentals",
        "profile", "confidence_label", "market_snapshot", "projected_fcff",
        "valuation", "sensitivity_table", "result_path", "parent_step_id",
        "scenarios", "scenario_results", "analysis_iteration", "model_validity",
    ]
    for key in required:
        assert key in s, f"Missing key: {key}"


def test_result_path_none_initially():
    s = _make()
    assert s["result_path"] is None


def test_assumption_overrides_stored():
    overrides = {"wacc": 0.10, "terminal_growth": 0.025}
    s = _make(assumption_overrides=overrides)
    assert s["assumption_overrides"] == overrides


def test_user_assumption_overrides_drop_canonical_facts():
    overrides = {
        "base_revenue": 215_938.0,
        "shares_outstanding": 24_432.0,
        "net_debt": 807.0,
        "revenue_growth": 0.06,
        "fcff_margin": 0.22,
        "terminal_growth": 0.025,
        "tax_rate": 0.16,
        "wacc": 0.087,
    }

    assert filter_user_assumption_overrides(overrides) == {
        "revenue_growth": 0.06,
        "fcff_margin": 0.22,
        "terminal_growth": 0.025,
        "tax_rate": 0.16,
        "wacc": 0.087,
    }


def test_canonical_assumptions_from_hitl_snapshot_keep_locked_facts():
    snapshot = {
        "assumptions": {
            "base_revenue": 416_161.0,
            "shares_outstanding": 15_004.7,
            "revenue_growth": 0.06,
        },
        "fundamentals": {
            "net_debt": {"value": 23_631.0, "source": "fmp"},
        },
    }

    assert _canonical_assumptions_from_snapshot(snapshot) == {
        "base_revenue": 416_161.0,
        "shares_outstanding": 15_004.7,
        "net_debt": 23_631.0,
    }


def test_session_id_propagated():
    s = _make(session_id="my-custom-session")
    assert s["session_id"] == "my-custom-session"

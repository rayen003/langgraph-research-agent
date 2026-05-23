"""Tests for _build_initial_state in graph.py.

Verifies the initial state has correct defaults and honours overrides.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.graph import _build_initial_state


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


def test_session_id_propagated():
    s = _make(session_id="my-custom-session")
    assert s["session_id"] == "my-custom-session"

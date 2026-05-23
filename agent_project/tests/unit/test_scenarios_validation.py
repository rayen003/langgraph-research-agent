"""Tests for scenario monotonicity validation (_violates_monotonicity)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.scenarios import _violates_monotonicity


def _make_scenarios(bear_g, base_g, bull_g, bear_m=0.20, base_m=0.25, bull_m=0.30, tg=0.03):
    return [
        {"name": "bear", "assumptions": {"revenue_growth": bear_g, "fcff_margin": bear_m, "terminal_growth": tg - 0.005}},
        {"name": "base", "assumptions": {"revenue_growth": base_g, "fcff_margin": base_m, "terminal_growth": tg}},
        {"name": "bull", "assumptions": {"revenue_growth": bull_g, "fcff_margin": bull_m, "terminal_growth": tg + 0.005}},
    ]


def test_monotonic_scenarios_no_violations():
    scenarios = _make_scenarios(0.03, 0.06, 0.10)
    assert _violates_monotonicity(scenarios) == []


def test_bull_less_than_base_violation():
    """Bull growth < base → violation."""
    scenarios = _make_scenarios(0.03, 0.10, 0.05)  # bull < base
    violations = _violates_monotonicity(scenarios)
    assert "revenue_growth" in violations


def test_base_less_than_bear_violation():
    """Base growth < bear → violation."""
    scenarios = _make_scenarios(0.10, 0.03, 0.15)  # base < bear
    violations = _violates_monotonicity(scenarios)
    assert "revenue_growth" in violations


def test_all_equal_not_a_violation():
    """Equal values across scenarios are fine (≥ not >)."""
    scenarios = _make_scenarios(0.06, 0.06, 0.06, bear_m=0.25, base_m=0.25, bull_m=0.25)
    assert _violates_monotonicity(scenarios) == []


def test_missing_field_skipped():
    """Missing field in any scenario → skipped, not counted as violation."""
    scenarios = [
        {"name": "bear", "assumptions": {"fcff_margin": 0.20}},
        {"name": "base", "assumptions": {"revenue_growth": 0.06, "fcff_margin": 0.25}},
        {"name": "bull", "assumptions": {"revenue_growth": 0.10, "fcff_margin": 0.30}},
    ]
    violations = _violates_monotonicity(scenarios)
    assert "revenue_growth" not in violations  # missing in bear → skipped


def test_margin_violation_detected():
    """Bull margin < base → fcff_margin flagged."""
    scenarios = _make_scenarios(0.03, 0.06, 0.10, bear_m=0.20, base_m=0.30, bull_m=0.25)
    violations = _violates_monotonicity(scenarios)
    assert "fcff_margin" in violations


def test_terminal_growth_violation():
    """Terminal growth out of order → flagged."""
    scenarios = [
        {"name": "bear", "assumptions": {"revenue_growth": 0.03, "fcff_margin": 0.20, "terminal_growth": 0.04}},
        {"name": "base", "assumptions": {"revenue_growth": 0.06, "fcff_margin": 0.25, "terminal_growth": 0.03}},
        {"name": "bull", "assumptions": {"revenue_growth": 0.10, "fcff_margin": 0.30, "terminal_growth": 0.035}},
    ]
    violations = _violates_monotonicity(scenarios)
    assert "terminal_growth" in violations


def test_missing_scenario_name_handled():
    """Scenarios without bear/base/bull keys → all fields skipped gracefully."""
    scenarios = [
        {"name": "aggressive", "assumptions": {"revenue_growth": 0.15}},
    ]
    assert _violates_monotonicity(scenarios) == []

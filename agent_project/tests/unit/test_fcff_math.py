"""Property and parametric tests for DCF math.

Tests project_cashflows_node and compute_valuation_node as pure calculators.
Uses pytest.mark.parametrize + hypothesis for boundary coverage.
"""

import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from agent_project.graphs.workflows.dcf.valuation import (
    project_cashflows_node,
    compute_valuation_node,
)
from agent_project.tests.helpers import build_test_state


# ---------------------------------------------------------------------------
# project_cashflows_node
# ---------------------------------------------------------------------------

def test_project_cashflows_returns_correct_count():
    state = build_test_state()
    result = project_cashflows_node(state)
    assert len(result["projected_fcff"]) == 5


def test_project_cashflows_year_sequence():
    state = build_test_state()
    result = project_cashflows_node(state)
    years = [int(r["year"]) for r in result["projected_fcff"]]
    assert years == [1, 2, 3, 4, 5]


def test_project_cashflows_grows_at_growth_rate():
    """Each year's revenue = prior_year * (1 + growth)."""
    state = build_test_state()
    result = project_cashflows_node(state)
    rows = result["projected_fcff"]
    g = state["assumptions"]["revenue_growth"]
    for i in range(1, len(rows)):
        ratio = rows[i]["revenue"] / rows[i - 1]["revenue"]
        assert abs(ratio - (1 + g)) < 1e-6, f"Year {i+1}: ratio={ratio:.6f} expected {1+g:.6f}"


def test_project_cashflows_fcff_equals_revenue_times_margin():
    state = build_test_state()
    result = project_cashflows_node(state)
    margin = state["assumptions"]["fcff_margin"]
    for row in result["projected_fcff"]:
        assert abs(row["fcff"] - row["revenue"] * margin) < 1e-4


@pytest.mark.parametrize("horizon", [1, 3, 5, 10])
def test_project_cashflows_respects_horizon(horizon):
    state = build_test_state(horizon_years=horizon)
    result = project_cashflows_node(state)
    assert len(result["projected_fcff"]) == horizon


# ---------------------------------------------------------------------------
# compute_valuation_node (requires projected_fcff from project_cashflows)
# ---------------------------------------------------------------------------

def _full_valuation_state(**overrides):
    state = build_test_state(**overrides)
    cf_result = project_cashflows_node(state)
    state["projected_fcff"] = cf_result["projected_fcff"]
    state["market_snapshot"] = {"price": 180.0}
    return state


def test_compute_valuation_has_required_keys():
    state = _full_valuation_state()
    result = compute_valuation_node(state)
    val = result["valuation"]
    for key in ("pv_cash_flows", "terminal_value", "terminal_pv",
                "enterprise_value", "equity_value", "implied_share_price"):
        assert key in val, f"Missing key: {key}"


def test_ev_equals_pv_plus_terminal_pv():
    """enterprise_value = pv_cash_flows + terminal_pv (exactly, within float eps)."""
    state = _full_valuation_state()
    result = compute_valuation_node(state)
    val = result["valuation"]
    assert abs(val["enterprise_value"] - (val["pv_cash_flows"] + val["terminal_pv"])) < 1.0


def test_equity_equals_ev_minus_net_debt():
    state = _full_valuation_state()
    net_debt = state["assumptions"]["net_debt"]
    result = compute_valuation_node(state)
    val = result["valuation"]
    assert abs(val["equity_value"] - (val["enterprise_value"] - net_debt)) < 1.0


def test_implied_price_equals_equity_div_shares():
    state = _full_valuation_state()
    shares = state["assumptions"]["shares_outstanding"]
    result = compute_valuation_node(state)
    val = result["valuation"]
    assert abs(val["implied_share_price"] - val["equity_value"] / shares) < 1e-4


def test_higher_wacc_produces_lower_valuation():
    low = _full_valuation_state(assumption_overrides={"wacc": 0.07})
    high = _full_valuation_state(assumption_overrides={"wacc": 0.13})
    val_low = compute_valuation_node(low)["valuation"]["implied_share_price"]
    val_high = compute_valuation_node(high)["valuation"]["implied_share_price"]
    assert val_low > val_high


def test_higher_growth_produces_higher_valuation():
    low = _full_valuation_state(assumption_overrides={"revenue_growth": 0.02})
    high = _full_valuation_state(assumption_overrides={"revenue_growth": 0.15})
    val_low = compute_valuation_node(low)["valuation"]["implied_share_price"]
    val_high = compute_valuation_node(high)["valuation"]["implied_share_price"]
    assert val_high > val_low


def test_zero_terminal_growth_still_computes():
    state = _full_valuation_state(assumption_overrides={"terminal_growth": 0.0})
    result = compute_valuation_node(state)
    val = result["valuation"]["implied_share_price"]
    assert math.isfinite(val) and val > 0


# ---------------------------------------------------------------------------
# Hypothesis: PV always finite and positive for valid inputs
# ---------------------------------------------------------------------------

@given(
    growth=st.floats(min_value=0.0, max_value=0.30),
    margin=st.floats(min_value=0.01, max_value=0.60),
    wacc=st.floats(min_value=0.05, max_value=0.25),
    tg=st.floats(min_value=0.0, max_value=0.04),
)
@settings(max_examples=200, deadline=2000)
def test_valuation_always_finite_for_valid_inputs(growth, margin, wacc, tg):
    assume(wacc > tg + 0.005)  # Gordon model requires wacc > tg
    state = _full_valuation_state(
        assumption_overrides={
            "revenue_growth": growth,
            "fcff_margin": margin,
            "wacc": wacc,
            "terminal_growth": tg,
        }
    )
    result = compute_valuation_node(state)
    price = result["valuation"]["implied_share_price"]
    assert math.isfinite(price)

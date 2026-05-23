"""Tests for refine_assumptions_node in refinement.py.

Verifies bounded adjustments are applied correctly to assumptions.
Pure deterministic — no LLM calls (the critique is passed in via state).

Test groups:
    1. Adjustments from critique.suggested_adjustments applied
    2. Fallback: derive adjustments from severe flags if none suggested
    3. Out-of-range fields are skipped (not added)
    4. Empty critique → no changes
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest

from agent_project.graphs.workflows.dcf.refinement import refine_assumptions_node
from agent_project.tests.helpers import build_test_state


# ---------------------------------------------------------------------------
# 1. Suggested adjustments are applied
# ---------------------------------------------------------------------------

def test_single_adjustment_applied():
    state = build_test_state(critique={
        "suggested_adjustments": {"revenue_growth": 0.02},
        "flags": [],
        "interpretation": "Growth too low for thesis.",
    })
    orig_growth = state["assumptions"]["revenue_growth"]
    result = refine_assumptions_node(state)
    assert result["assumptions"]["revenue_growth"] == pytest.approx(orig_growth + 0.02, abs=1e-6)


def test_negative_adjustment_applied():
    state = build_test_state(critique={
        "suggested_adjustments": {"wacc": -0.005},
        "flags": [],
    })
    orig_wacc = state["assumptions"]["wacc"]
    result = refine_assumptions_node(state)
    assert result["assumptions"]["wacc"] == pytest.approx(orig_wacc - 0.005, abs=1e-6)


def test_multiple_adjustments_applied():
    state = build_test_state(critique={
        "suggested_adjustments": {
            "revenue_growth": 0.01,
            "fcff_margin": -0.02,
            "terminal_growth": -0.003,
        },
        "flags": [],
    })
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    a = result["assumptions"]
    assert a["revenue_growth"] == pytest.approx(orig["revenue_growth"] + 0.01, abs=1e-6)
    assert a["fcff_margin"] == pytest.approx(orig["fcff_margin"] - 0.02, abs=1e-6)
    assert a["terminal_growth"] == pytest.approx(orig["terminal_growth"] - 0.003, abs=1e-6)


def test_unchanged_fields_preserved():
    """Fields not in suggested_adjustments must keep original values."""
    state = build_test_state(critique={
        "suggested_adjustments": {"wacc": 0.01},
        "flags": [],
    })
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    a = result["assumptions"]
    for field in ("revenue_growth", "fcff_margin", "terminal_growth",
                  "base_revenue", "shares_outstanding"):
        assert a[field] == orig[field], f"{field} should be unchanged"


# ---------------------------------------------------------------------------
# 2. Fallback: derive adjustments from severe flags
# ---------------------------------------------------------------------------

def test_fallback_terminal_weight_severe_lowers_tgr():
    """Severe terminal_weight flag + no suggestions → lower terminal_growth by 0.003."""
    state = build_test_state(critique={
        "suggested_adjustments": {},
        "flags": [{"signal": "terminal_weight", "severity": "severe", "value": 80.0}],
    })
    orig_tg = state["assumptions"]["terminal_growth"]
    result = refine_assumptions_node(state)
    assert result["assumptions"]["terminal_growth"] == pytest.approx(orig_tg - 0.003, abs=1e-6)


def test_fallback_wacc_gap_positive_lowers_wacc():
    """gap_bps>0 means model WACC > implied → lower wacc."""
    state = build_test_state(critique={
        "suggested_adjustments": {},
        "flags": [{"signal": "wacc_sanity_gap", "severity": "severe", "value_bps": 300}],
    })
    orig_wacc = state["assumptions"]["wacc"]
    result = refine_assumptions_node(state)
    assert result["assumptions"]["wacc"] == pytest.approx(orig_wacc - 0.01, abs=1e-6)


def test_fallback_wacc_gap_negative_raises_wacc():
    """gap_bps<0 means model WACC < implied → raise wacc."""
    state = build_test_state(critique={
        "suggested_adjustments": {},
        "flags": [{"signal": "wacc_sanity_gap", "severity": "severe", "value_bps": -300}],
    })
    orig_wacc = state["assumptions"]["wacc"]
    result = refine_assumptions_node(state)
    assert result["assumptions"]["wacc"] == pytest.approx(orig_wacc + 0.01, abs=1e-6)


def test_fallback_warning_flag_does_not_trigger():
    """Only severe flags trigger fallback adjustments."""
    state = build_test_state(critique={
        "suggested_adjustments": {},
        "flags": [{"signal": "terminal_weight", "severity": "warning", "value": 72.0}],
    })
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    assert result["assumptions"] == orig


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

def test_adjustment_for_missing_field_ignored():
    """Suggesting a field that's not in assumptions → silently skipped."""
    state = build_test_state(critique={
        "suggested_adjustments": {"nonsense_field": 1.0},
        "flags": [],
    })
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    assert result["assumptions"] == orig
    assert "nonsense_field" not in result["assumptions"]


def test_empty_critique_no_changes():
    state = build_test_state(critique={})
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    assert result["assumptions"] == orig


def test_none_critique_no_changes():
    state = build_test_state(critique=None)
    orig = dict(state["assumptions"])
    result = refine_assumptions_node(state)
    assert result["assumptions"] == orig


def test_result_contains_only_assumptions_key():
    """Node returns just {"assumptions": ...} — no other state pollution."""
    state = build_test_state(critique={"suggested_adjustments": {"wacc": 0.005}, "flags": []})
    result = refine_assumptions_node(state)
    assert set(result.keys()) == {"assumptions"}


def test_assumption_values_rounded_to_4_decimals():
    """Refined values are rounded to 4 decimal places (round(.., 4))."""
    state = build_test_state(critique={
        "suggested_adjustments": {"wacc": 0.001234567},
        "flags": [],
    })
    orig_wacc = state["assumptions"]["wacc"]
    result = refine_assumptions_node(state)
    new_wacc = result["assumptions"]["wacc"]
    # Should be rounded to 4 decimals
    assert new_wacc == round(orig_wacc + 0.001234567, 4)

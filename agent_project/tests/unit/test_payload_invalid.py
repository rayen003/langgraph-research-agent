"""Tests for summarize_dcf_payload with invalid/borderline payloads.

Verifies that invalid model suppresses point estimate and shows warning banner.
Uses real fixture where available, falls back to build_test_payload.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import pytest
from agent_project.graphs.workflows.dcf.payload import SENSITIVITY_CHART_MARKER, summarize_dcf_payload
from agent_project.tests.helpers import build_test_payload


# ---------------------------------------------------------------------------
# Invalid model — must suppress point estimate
# ---------------------------------------------------------------------------

def test_invalid_model_shows_warning_banner():
    payload = build_test_payload(model_validity="invalid",
                                  invalidation_reason="WACC solver diverged.")
    summary = summarize_dcf_payload(payload)
    # Must contain some form of invalid/unreliable warning
    lower = summary.lower()
    assert any(w in lower for w in ("invalid", "unreliable", "not reliable", "⚠", "warning"))


def test_invalid_model_suppresses_implied_price():
    payload = build_test_payload(model_validity="invalid",
                                  invalidation_reason="WACC solver diverged.")
    summary = summarize_dcf_payload(payload)
    # The point estimate $195 should NOT appear as a confident target
    # (it may appear with a caveat, but not as "Implied: $195")
    assert "Implied: $195" not in summary


def test_valid_model_shows_implied_price():
    payload = build_test_payload(model_validity="valid")
    summary = summarize_dcf_payload(payload)
    assert "195" in summary  # somewhere in implied price section


# ---------------------------------------------------------------------------
# Non-dict input
# ---------------------------------------------------------------------------

def test_summarize_none_returns_fallback():
    result = summarize_dcf_payload(None)
    assert "without payload" in result.lower() or len(result) < 100


def test_summarize_empty_dict_returns_string():
    result = summarize_dcf_payload({})
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Fixture-based test (skips if fixture missing)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aapl_payload():
    import json
    from pathlib import Path
    p = Path(__file__).parent.parent / "fixtures" / "payloads" / "valid_aapl.json"
    if not p.exists():
        pytest.skip("Fixture not found")
    return json.loads(p.read_text())


def test_aapl_payload_summary_contains_ticker(aapl_payload):
    summary = summarize_dcf_payload(aapl_payload)
    assert "AAPL" in summary


def test_aapl_payload_summary_is_string(aapl_payload):
    summary = summarize_dcf_payload(aapl_payload)
    assert isinstance(summary, str)
    assert len(summary) > 200


def test_aapl_payload_has_required_keys(aapl_payload):
    for key in ("ticker", "valuation", "assumptions", "confidence_label", "model_validity"):
        assert key in aapl_payload, f"Missing: {key}"


def test_aapl_payload_valuation_has_required_keys(aapl_payload):
    val = aapl_payload.get("valuation", {})
    for key in ("implied_share_price", "enterprise_value", "terminal_pv", "pv_cash_flows"):
        assert key in val, f"valuation missing: {key}"


def test_aapl_scenarios_are_3(aapl_payload):
    assert len(aapl_payload.get("scenarios", [])) == 3


def test_aapl_sensitivity_table_is_9(aapl_payload):
    assert len(aapl_payload.get("sensitivity_table", [])) == 9


def test_sensitivity_matrix_before_assumptions(aapl_payload):
    summary = summarize_dcf_payload(aapl_payload)
    assert "## Sensitivity Matrix" in summary
    assert summary.index("## Sensitivity Matrix") < summary.index("## Assumptions")
    assert SENSITIVITY_CHART_MARKER in summary


def test_aapl_invalid_model_summary_has_banner(aapl_payload):
    """Live AAPL run detected divergence → model_validity=invalid."""
    if aapl_payload.get("model_validity") != "invalid":
        pytest.skip("Fixture is valid — skipping invalid-banner check")
    summary = summarize_dcf_payload(aapl_payload)
    lower = summary.lower()
    assert any(w in lower for w in ("invalid", "unreliable", "⚠", "warning", "not reliable"))

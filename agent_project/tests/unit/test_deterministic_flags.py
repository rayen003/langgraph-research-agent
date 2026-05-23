"""Tests for _build_deterministic_flags in refinement.py.

Verifies signal extraction, severity thresholds, and flag structure.
All tests are pure Python — no LLM, no network.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.graphs.workflows.dcf.refinement import _build_deterministic_flags
from agent_project.tests.helpers import build_test_state


def _state_with_valuation(
    terminal_pv=2_000_000.0,
    enterprise_value=3_000_000.0,
    implied_share_price=200.0,
    current_price=180.0,
    wacc_gap_bps=50,
    confidence_label="high",
    sensitivity_prices=None,
    terminal_growth=0.03,
) -> dict:
    return build_test_state(
        valuation={
            "terminal_pv": terminal_pv,
            "enterprise_value": enterprise_value,
            "implied_share_price": implied_share_price,
            "current_price": current_price,
        },
        wacc_sanity={"gap_bps": wacc_gap_bps},
        confidence_label=confidence_label,
        confidence_breakdown={"label": confidence_label},
        sensitivity_table=(
            [{"implied_share_price": p} for p in sensitivity_prices]
            if sensitivity_prices else []
        ),
        assumptions={
            **build_test_state()["assumptions"],
            "terminal_growth": terminal_growth,
        },
    )


# ---------------------------------------------------------------------------
# Structural contract
# ---------------------------------------------------------------------------

def test_flags_always_returns_list():
    state = _state_with_valuation()
    flags = _build_deterministic_flags(state)
    assert isinstance(flags, list)
    assert len(flags) >= 3  # terminal_weight, implied_vs_spot, wacc_sanity_gap always present


def test_every_flag_has_signal_and_severity():
    state = _state_with_valuation()
    flags = _build_deterministic_flags(state)
    for f in flags:
        assert "signal" in f, f"Missing 'signal' key: {f}"
        assert "severity" in f, f"Missing 'severity' key: {f}"
        assert f["severity"] in ("ok", "warning", "severe"), f"Bad severity: {f}"


# ---------------------------------------------------------------------------
# terminal_weight
# ---------------------------------------------------------------------------

def test_terminal_weight_ok():
    state = _state_with_valuation(terminal_pv=1_500_000.0, enterprise_value=3_000_000.0)  # 50%
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["terminal_weight"]["severity"] == "ok"
    assert flags["terminal_weight"]["value"] == 50.0


def test_terminal_weight_warning_above_70():
    state = _state_with_valuation(terminal_pv=2_200_000.0, enterprise_value=3_000_000.0)  # ~73%
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["terminal_weight"]["severity"] == "warning"


def test_terminal_weight_severe_above_75():
    state = _state_with_valuation(terminal_pv=2_400_000.0, enterprise_value=3_000_000.0)  # 80%
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["terminal_weight"]["severity"] == "severe"


# ---------------------------------------------------------------------------
# implied_vs_spot
# ---------------------------------------------------------------------------

def test_implied_vs_spot_ok_within_30pct():
    state = _state_with_valuation(implied_share_price=190.0, current_price=180.0)  # ~5.5% gap
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["implied_vs_spot"]["severity"] == "ok"


def test_implied_vs_spot_warning_above_30pct():
    state = _state_with_valuation(implied_share_price=240.0, current_price=180.0)  # 33%
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["implied_vs_spot"]["severity"] == "warning"


def test_implied_vs_spot_severe_above_50pct():
    state = _state_with_valuation(implied_share_price=80.0, current_price=180.0)  # -55.5%
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["implied_vs_spot"]["severity"] == "severe"


# ---------------------------------------------------------------------------
# wacc_sanity_gap
# ---------------------------------------------------------------------------

def test_wacc_gap_ok_under_100bps():
    state = _state_with_valuation(wacc_gap_bps=80)
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["wacc_sanity_gap"]["severity"] == "ok"


def test_wacc_gap_warning_100_to_200bps():
    state = _state_with_valuation(wacc_gap_bps=150)
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["wacc_sanity_gap"]["severity"] == "warning"


def test_wacc_gap_severe_above_200bps():
    state = _state_with_valuation(wacc_gap_bps=250)
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["wacc_sanity_gap"]["severity"] == "severe"


# ---------------------------------------------------------------------------
# sensitivity signal
# ---------------------------------------------------------------------------

def test_sensitivity_flag_added_with_3_plus_prices():
    prices = [100.0, 150.0, 200.0, 250.0, 300.0]
    state = _state_with_valuation(sensitivity_prices=prices)
    signals = [f["signal"] for f in _build_deterministic_flags(state)]
    assert "wacc_sensitivity" in signals


def test_sensitivity_flag_absent_with_fewer_than_3():
    state = _state_with_valuation(sensitivity_prices=[100.0, 200.0])
    signals = [f["signal"] for f in _build_deterministic_flags(state)]
    assert "wacc_sensitivity" not in signals


# ---------------------------------------------------------------------------
# confidence flag
# ---------------------------------------------------------------------------

def test_confidence_severe_when_low():
    state = _state_with_valuation(confidence_label="low")
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["confidence"]["severity"] == "severe"


def test_confidence_warning_when_medium():
    state = _state_with_valuation(confidence_label="medium")
    flags = {f["signal"]: f for f in _build_deterministic_flags(state)}
    assert flags["confidence"]["severity"] == "warning"

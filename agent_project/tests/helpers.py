"""Test helpers: state factory, payload builder, fixture loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "payloads"

# ---------------------------------------------------------------------------
# DCFState factory
# ---------------------------------------------------------------------------

_BASE_ASSUMPTIONS = {
    "base_revenue": 400_000.0,
    "revenue_growth": 0.06,
    "fcff_margin": 0.25,
    "wacc": 0.09,
    "terminal_growth": 0.03,
    "net_debt": 50_000.0,
    "shares_outstanding": 15_500.0,
    "tax_rate": 0.15,
}

_BASE_STATE: dict[str, Any] = {
    "ticker": "AAPL",
    "horizon_years": 5,
    "session_id": "test-session",
    "assumption_review_mode": False,
    "allow_external_assumptions": True,
    "assumption_overrides": {},
    "assumptions": _BASE_ASSUMPTIONS,
    "assumption_provenance": {k: {"source": "test", "confidence": 1.0} for k in _BASE_ASSUMPTIONS},
    "assumptions_approved": True,
    "fundamentals": {},
    "assumption_conflicts": [],
    "profile": "default",
    "profile_meta": {},
    "assumption_flags": [],
    "valuation_flags": [],
    "confidence_label": "high",
    "market_snapshot": {"current_price": 180.0, "market_cap": 2_790_000.0},
    "projected_fcff": [],
    "valuation": {},
    "sensitivity_table": [],
    "result_path": None,
    "parent_step_id": "test",
    "features": {},
    "wacc_components": {},
    "evidence_pack": {},
    "company_state": None,
    "assumption_memo": None,
    "confidence_breakdown": None,
    "wacc_sanity": None,
    "implied_growth": None,
    "implied_margin": None,
    "thesis": {"conviction": "buy", "key_drivers": []},
    "scenarios": [],
    "scenario_results": [],
    "analysis_iteration": 0,
    "critique": None,
    "previous_valuation": None,
    "assumption_history": [],
    "initial_assumptions": {},
    "kg_cache_flags": {},
    "kg_fundamentals_hint": {},
    "kg_cache_results": [],
    "divergences": [],
    "analysis_positions": [],
    "model_validity": "valid",
    "invalidation_reason": "",
    "reconciliation_status": "aligned",
    "reconciliation_note": "",
    "effective_confidence": None,
}


def build_test_state(**overrides: Any) -> dict[str, Any]:
    """Return a complete DCFState dict suitable for unit testing.

    Call with keyword overrides to swap individual fields::

        state = build_test_state(ticker="MSFT", assumptions={...})

    Nested assumption overrides via ``assumption_overrides`` key::

        state = build_test_state(assumption_overrides={"wacc": 0.12})
        # assumptions dict is also updated for convenience
    """
    state = {**_BASE_STATE}
    # Deep-copy mutable defaults to prevent cross-test pollution.
    state["assumptions"] = dict(_BASE_ASSUMPTIONS)
    state["assumption_provenance"] = {k: {"source": "test", "confidence": 1.0} for k in _BASE_ASSUMPTIONS}

    if "assumption_overrides" in overrides:
        ao = overrides.pop("assumption_overrides")
        state["assumption_overrides"] = ao
        state["assumptions"].update(ao)

    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Payload builder (for payload / consistency-check tests)
# ---------------------------------------------------------------------------

def build_test_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal DCF output payload for testing summarize_dcf_payload / checks."""
    base: dict[str, Any] = {
        "ticker": "AAPL",
        "horizon_years": 5,
        "assumptions": dict(_BASE_ASSUMPTIONS),
        "valuation": {
            "implied_share_price": 195.0,
            "enterprise_value": 3_100_000.0,
            "equity_value": 3_050_000.0,
            "terminal_pv": 2_100_000.0,
            "pv_cash_flows": 950_000.0,
            "current_price": 180.0,
        },
        "projected_fcff": [
            {"year": i + 1, "fcff": 100_000.0 * (1.06 ** i)}
            for i in range(5)
        ],
        "sensitivity_table": [
            {"wacc": 0.08 + 0.01 * i, "terminal_growth": 0.03, "implied_share_price": 200.0 - 15.0 * i}
            for i in range(5)
        ],
        "scenarios": [
            {"name": "bear", "assumptions": {**_BASE_ASSUMPTIONS, "revenue_growth": 0.03}},
            {"name": "base", "assumptions": _BASE_ASSUMPTIONS},
            {"name": "bull", "assumptions": {**_BASE_ASSUMPTIONS, "revenue_growth": 0.10}},
        ],
        "scenario_results": [
            {"scenario": "bear", "valuation": {"implied_share_price": 140.0}},
            {"scenario": "base", "valuation": {"implied_share_price": 195.0}},
            {"scenario": "bull", "valuation": {"implied_share_price": 260.0}},
        ],
        "confidence_label": "high",
        "confidence_breakdown": {"label": "high", "score": 0.82},
        "wacc_sanity": {"gap_bps": 30, "market_implied_wacc": 0.092, "model_wacc": 0.09},
        "assumption_provenance": {k: {"source": "test", "confidence": 1.0} for k in _BASE_ASSUMPTIONS},
        "assumption_history": [],
        "model_validity": "valid",
        "invalidation_reason": "",
        "thesis": {"conviction": "buy"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixture loader (direct, for non-fixture-based tests)
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))

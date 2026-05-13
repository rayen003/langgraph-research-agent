"""DCF state model, assumption field specs, and shared constants."""

from __future__ import annotations

import os
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Environment-configurable defaults
# ---------------------------------------------------------------------------

_DEFAULT_RISK_FREE_RATE = float(os.getenv("DCF_RISK_FREE_RATE", "0.045"))
_DEFAULT_EQUITY_RISK_PREMIUM = float(os.getenv("DCF_EQUITY_RISK_PREMIUM", "0.055"))


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class DCFState(TypedDict):
    ticker: str
    horizon_years: int
    session_id: str
    assumption_review_mode: bool
    allow_external_assumptions: bool
    assumption_overrides: dict[str, float]
    assumptions: dict[str, float]
    assumption_provenance: dict[str, dict[str, Any]]
    assumptions_approved: bool
    fundamentals: dict[str, dict[str, Any]]
    assumption_conflicts: list[dict[str, Any]]
    profile: str
    profile_meta: dict[str, Any]
    assumption_flags: list[dict[str, Any]]
    valuation_flags: list[dict[str, Any]]
    confidence_label: str
    market_snapshot: dict[str, float]
    projected_fcff: list[dict[str, float]]
    valuation: dict[str, float]
    sensitivity_table: list[dict[str, float]]
    result_path: str | None
    parent_step_id: str
    # Numeric/categorical inputs from fundamentals/market APIs (CAPM inputs).
    features: dict[str, Any]
    # Decomposition of estimated WACC (audit trail).
    wacc_components: dict[str, Any]
    # Unified evidence pack (all observations before interpretation).
    evidence_pack: dict[str, Any]
    # Structured company state from semantic synthesis (LLM).
    company_state: dict[str, Any] | None
    # Full assumption memo from LLM proposal (audit trail).
    assumption_memo: dict[str, Any] | None
    # Per-component confidence decomposition (computed after valuation).
    confidence_breakdown: dict[str, Any] | None
    # Market-implied WACC vs CAPM sanity check (computed after valuation).
    wacc_sanity: dict[str, Any] | None
    # Investment thesis (formulated before assumptions).
    thesis: dict[str, Any] | None
    # Analysis loop state.
    analysis_iteration: int
    critique: dict[str, Any] | None
    previous_valuation: dict[str, float] | None


# ---------------------------------------------------------------------------
# Tier A — canonical level/scale fields that MUST come from fundamentals
# or explicit user override. External hints can refine rates (Tier B)
# but cannot overwrite these.
# ---------------------------------------------------------------------------

_TIER_A_FIELDS: frozenset[str] = frozenset({
    "base_revenue",
    "shares_outstanding",
    "net_debt",
})


# ---------------------------------------------------------------------------
# Assumption field definitions
# ---------------------------------------------------------------------------

_ASSUMPTION_FIELDS = {
    "revenue_growth": {
        "label": "Revenue growth",
        "aliases": ("revenue growth", "sales growth", "topline growth"),
        "kind": "percent",
        "min": -0.5,
        "max": 0.75,
    },
    "fcff_margin": {
        "label": "FCFF margin",
        "aliases": ("fcff margin", "free cash flow margin", "free cash flow conversion"),
        "kind": "percent",
        "min": -0.25,
        "max": 0.75,
    },
    "wacc": {
        "label": "WACC",
        "aliases": ("wacc", "weighted average cost of capital", "discount rate"),
        "kind": "percent",
        "min": 0.03,
        "max": 0.25,
    },
    "terminal_growth": {
        "label": "Terminal growth",
        "aliases": ("terminal growth", "perpetuity growth", "terminal growth rate"),
        "kind": "percent",
        "min": -0.02,
        "max": 0.06,
    },
    "tax_rate": {
        "label": "Tax rate",
        "aliases": ("tax rate", "effective tax rate"),
        "kind": "percent",
        "min": 0.0,
        "max": 0.45,
    },
    "base_revenue": {
        "label": "Base revenue",
        "aliases": ("base revenue", "total revenue", "annual revenue", "latest revenue", "fy revenue", "total sales"),
        "kind": "money_millions",
        "min": 1.0,
        "max": 10_000_000.0,
    },
    "shares_outstanding": {
        "label": "Shares outstanding",
        "aliases": ("shares outstanding", "diluted shares", "share count"),
        "kind": "number_millions",
        "min": 1.0,
        "max": 1_000_000.0,
    },
    "net_debt": {
        "label": "Net debt",
        "aliases": ("net debt", "net cash", "debt net of cash"),
        "kind": "money_millions",
        "min": -1_000_000.0,
        "max": 10_000_000.0,
    },
}


# Provenance metadata keys that pass through from external candidates
# without transformation.
_PROVENANCE_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "as_of",
    "raw_unit",
    "raw_value",
    "field",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def coerce_finite_float(value: object) -> float | None:
    """Safely cast to float; return None for NaN, Inf, or non-numeric types."""
    if isinstance(value, (int, float)):
        out = float(value)
        if out == out and abs(out) != float("inf"):
            return out
    return None


def canonical_numeric(
    fundamentals: dict[str, dict[str, Any]],
    field: str,
) -> float | None:
    """Read a canonical fundamental value, with NaN/Inf guard."""
    meta = fundamentals.get(field)
    if isinstance(meta, dict):
        return coerce_finite_float(meta.get("value"))
    return None


def normalize_assumption_value(
    raw_value: float,
    raw_text: str,
    kind: str,
) -> float | None:
    """Normalize a raw parsed number into the correct unit for a field kind.

    - ``percent`` fields with values > 1 (or containing '%') are divided by 100.
    - ``money_millions`` / ``number_millions`` fields with "billion"/"bn"
      qualifiers are multiplied by 1000.
    """
    text = raw_text.lower()
    value = float(raw_value)
    if kind == "percent":
        if "%" in text or abs(value) > 1.0:
            value /= 100.0
        return value
    if kind in {"money_millions", "number_millions"}:
        if "billion" in text or "bn" in text:
            value *= 1000.0
        return value
    return value


def clip_to_field_range(field: str, value: float) -> float | None:
    """Return *value* clamped to the field's valid range, or None if out of bounds."""
    spec = _ASSUMPTION_FIELDS[field]
    min_value = float(spec["min"])
    max_value = float(spec["max"])
    if value < min_value or value > max_value:
        return None
    return value

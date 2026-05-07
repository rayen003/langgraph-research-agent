"""Deterministic DCF workflow subgraph with optional assumption review."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, TypedDict
import requests

from langgraph.graph import END, START, StateGraph
try:
    from langgraph.types import interrupt
except ImportError:  # LangGraph >=1.0 style
    from langgraph.types import Interrupt

    def interrupt(payload: dict):  # type: ignore[no-redef]
        raise Interrupt(payload)

from utils import emit_activity, emit_ui_event, get_run_dir
from web_search import search_exa

logger = logging.getLogger(__name__)

# CAPM defaults (override via env for macro calibration).
_DEFAULT_RISK_FREE_RATE = float(os.getenv("DCF_RISK_FREE_RATE", "0.045"))
_DEFAULT_EQUITY_RISK_PREMIUM = float(os.getenv("DCF_EQUITY_RISK_PREMIUM", "0.055"))


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


# Tier A: level/scale variables that must come from canonical fundamentals or
# explicit user override. We never let document/web heuristics overwrite these
# because order-of-magnitude mistakes here turn the entire valuation into noise.
_TIER_A_FIELDS: frozenset[str] = frozenset({
    "base_revenue",
    "shares_outstanding",
    "net_debt",
})


# ---------------------------------------------------------------------------
# Sector / size priors (used by build_assumptions_node and compute_valuation_node)
# ---------------------------------------------------------------------------
# Bands are intentionally broad. `soft_*` triggers a `warn` flag (assumption
# is unusual but not impossible). `hard_*` triggers a `block` flag (very
# likely modelling error). The bands are reusable across workflows: comps and
# LBO can read the same priors when judging multiples and IRRs.

PROFILE_PRIORS: dict[str, dict[str, dict[str, float]]] = {
    "mega_cap_tech": {
        "fcff_margin":     {"soft_min": 0.20, "soft_max": 0.40, "hard_min": 0.05, "hard_max": 0.55},
        "wacc":            {"soft_min": 0.07, "soft_max": 0.10, "hard_min": 0.04, "hard_max": 0.13},
        "revenue_growth":  {"soft_min": 0.05, "soft_max": 0.25, "hard_min": -0.10, "hard_max": 0.40},
        "terminal_growth": {"soft_min": 0.020, "soft_max": 0.035, "hard_min": -0.01, "hard_max": 0.045},
        "tax_rate":        {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
    },
    "large_cap_tech": {
        "fcff_margin":     {"soft_min": 0.12, "soft_max": 0.30, "hard_min": -0.10, "hard_max": 0.50},
        "wacc":            {"soft_min": 0.08, "soft_max": 0.12, "hard_min": 0.05, "hard_max": 0.15},
        "revenue_growth":  {"soft_min": 0.05, "soft_max": 0.30, "hard_min": -0.10, "hard_max": 0.50},
        "terminal_growth": {"soft_min": 0.020, "soft_max": 0.040, "hard_min": -0.01, "hard_max": 0.05},
        "tax_rate":        {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
    },
    "mature_consumer_or_industrial": {
        "fcff_margin":     {"soft_min": 0.04, "soft_max": 0.15, "hard_min": -0.05, "hard_max": 0.35},
        "wacc":            {"soft_min": 0.07, "soft_max": 0.10, "hard_min": 0.05, "hard_max": 0.13},
        "revenue_growth":  {"soft_min": 0.0, "soft_max": 0.10, "hard_min": -0.10, "hard_max": 0.25},
        "terminal_growth": {"soft_min": 0.015, "soft_max": 0.030, "hard_min": -0.01, "hard_max": 0.040},
        "tax_rate":        {"soft_min": 0.15, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
    },
    "default": {
        "fcff_margin":     {"soft_min": 0.05, "soft_max": 0.30, "hard_min": -0.20, "hard_max": 0.55},
        "wacc":            {"soft_min": 0.07, "soft_max": 0.13, "hard_min": 0.04, "hard_max": 0.20},
        "revenue_growth":  {"soft_min": 0.0, "soft_max": 0.25, "hard_min": -0.20, "hard_max": 0.50},
        "terminal_growth": {"soft_min": 0.015, "soft_max": 0.035, "hard_min": -0.01, "hard_max": 0.05},
        "tax_rate":        {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.45},
    },
}

# Output sanity rails (applied inside compute_valuation_node).
_VALUATION_PRIORS: dict[str, dict[str, dict[str, float]]] = {
    "default": {
        # implied / spot price ratio. <0.25 or >4 is almost always inputs error.
        "price_ratio":          {"soft_min": 0.5, "soft_max": 2.0, "hard_min": 0.25, "hard_max": 4.0},
        # terminal value should normally dominate enterprise value for going concerns
        "tv_share_of_ev":       {"soft_min": 0.60, "soft_max": 0.95, "hard_min": 0.40, "hard_max": 0.99},
    },
}


_TECH_SECTORS = {"Technology", "Communication Services", "Consumer Cyclical"}
_INDUSTRIAL_SECTORS = {
    "Industrials", "Basic Materials", "Energy", "Utilities",
    "Consumer Defensive", "Real Estate",
}


def _prior_band_midpoint(profile: str, field: str) -> float | None:
    """Centre of soft band — used only as deterministic fallback."""
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    band = bands.get(field)
    if not band:
        return None
    lo = float(band.get("soft_min", band.get("hard_min", 0.0)))
    hi = float(band.get("soft_max", band.get("hard_max", lo)))
    return (lo + hi) / 2.0


def _classify_profile(sector: str | None, market_cap_usd: float | None) -> str:
    """Pick a prior bucket from FMP profile data.

    Falls back to "default" when sector/market_cap unknown. Mega-cap is
    defined as USD 200B+ which captures Apple, Microsoft, Alphabet, Amazon,
    Nvidia, Meta, Tesla, etc. Below that we still split tech vs other.
    """
    sector_clean = (sector or "").strip()
    cap = float(market_cap_usd or 0.0)
    if sector_clean in _TECH_SECTORS:
        if cap >= 200_000_000_000:
            return "mega_cap_tech"
        if cap >= 10_000_000_000:
            return "large_cap_tech"
    if sector_clean in _INDUSTRIAL_SECTORS:
        return "mature_consumer_or_industrial"
    return "default"


def _merge_dcf_extras(
    fmp_extras: dict[str, Any],
    yfinance_extras: dict[str, Any],
) -> dict[str, Any]:
    """Combine API-specific markers; FMP wins on duplicate keys."""
    merged = dict(yfinance_extras)
    merged.update(fmp_extras)
    return merged


def _coerce_finite_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        out = float(value)
        if out == out and abs(out) != float("inf"):  # not NaN or inf
            return out
    return None


def _canonical_numeric(fundamentals: dict[str, dict[str, Any]], field: str) -> float | None:
    meta = fundamentals.get(field)
    if isinstance(meta, dict):
        return _coerce_finite_float(meta.get("value"))
    return None


def _build_feature_vector(
    *,
    ticker: str,
    profile_bucket: str,
    profile_meta: dict[str, Any],
    fundamentals: dict[str, dict[str, Any]],
    dcf_extras: dict[str, Any],
) -> dict[str, Any]:
    """Single dict of CAPM / capital-structure inputs (+ coarse labels).

    Lives on state for auditing and deterministic WACC; not an assumption merge target.
    """
    features: dict[str, Any] = {
        "ticker": ticker.upper(),
        "profile_bucket": profile_bucket,
        "sector": profile_meta.get("sector"),
        "industry": profile_meta.get("industry"),
    }
    for key in (
        "beta",
        "market_cap_usd",
        "total_debt_usd",
        "cash_usd",
        "interest_expense_usd",
    ):
        v = _coerce_finite_float(dcf_extras.get(key))
        if v is not None:
            features[key] = v

    tax = _canonical_numeric(fundamentals, "tax_rate")
    if tax is not None:
        features["effective_tax_rate_hint"] = tax

    mc = features.get("market_cap_usd")
    mc_f = _coerce_finite_float(mc)
    if mc_f is not None and mc_f > 0:
        features["equity_value_usd"] = mc_f
    else:
        spot = _coerce_finite_float(profile_meta.get("spot_price"))
        sh_mil = _canonical_numeric(fundamentals, "shares_outstanding")
        if spot is not None and sh_mil is not None and sh_mil > 0 and spot > 0:
            features["equity_value_usd"] = float(sh_mil) * 1_000_000.0 * float(spot)

    debt = features.get("total_debt_usd")
    cash = features.get("cash_usd")
    nd_mil = _canonical_numeric(fundamentals, "net_debt")
    if nd_mil is not None:
        features["net_debt_usd"] = float(nd_mil) * 1_000_000.0
    elif isinstance(debt, (int, float)) and isinstance(cash, (int, float)):
        features["net_debt_usd"] = float(debt) - float(cash)

    return features


def _estimate_capm_wacc(
    features: dict[str, Any],
    tax_rate: float,
    *,
    rf: float = _DEFAULT_RISK_FREE_RATE,
    erp: float = _DEFAULT_EQUITY_RISK_PREMIUM,
) -> tuple[float | None, dict[str, Any], str]:
    """Return (wacc_pre_clip_or_none, component dict, status).

    ``status`` is ``capm`` when a full decomposition is computed, else ``capm_incomplete``.
    """
    tr = float(min(max(tax_rate, 0.0), 0.35))
    components: dict[str, Any] = {
        "risk_free_rate": rf,
        "equity_risk_premium": erp,
        "marginal_tax_rate": tr,
    }
    beta = _coerce_finite_float(features.get("beta"))
    if beta is None or beta <= 0 or beta > 2.5:
        components["beta"] = beta
        components["capm_block_reason"] = "missing_or_extreme_beta"
        return None, components, "capm_incomplete"

    E = _coerce_finite_float(features.get("equity_value_usd"))
    if E is None or E <= 0:
        components["capm_block_reason"] = "missing_equity_value"
        return None, components, "capm_incomplete"

    D_raw = _coerce_finite_float(features.get("total_debt_usd"))
    D = max(float(D_raw or 0.0), 0.0)

    interest = _coerce_finite_float(features.get("interest_expense_usd"))
    if D > 1e6 and interest is not None and interest >= 0:
        rd_pre = min(max(interest / D, 0.02), 0.15)
    elif D > 0:
        rd_pre = 0.06
    else:
        rd_pre = 0.0

    re = rf + float(beta) * erp
    re = min(max(re, rf + 0.01), 0.35)

    v = E + D
    we, wd = E / v, D / v
    rd_at = rd_pre * (1.0 - tr)
    wacc = we * re + wd * rd_at

    components.update({
        "beta": float(beta),
        "cost_of_equity": re,
        "pre_tax_cost_of_debt": rd_pre,
        "after_tax_cost_of_debt": rd_at,
        "equity_weight": we,
        "debt_weight": wd,
        "equity_value_usd": E,
        "debt_value_usd": D,
        "enterprise_value_usd": v,
    })
    return wacc, components, "capm"


def _resolve_wacc_from_features(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    *,
    features: dict[str, Any],
    profile: str,
    overrides: dict[str, float],
) -> dict[str, Any]:
    """Set ``assumptions['wacc']`` from CAPM or PROFILE_PRIORS midpoint; never clobber user override."""
    if provenance.get("wacc", {}).get("source") == "user_override" or "wacc" in overrides:
        return {"method": "user_override"}

    tax_rate = float(assumptions.get("tax_rate", 0.21))
    wacc_raw, comp, capm_status = _estimate_capm_wacc(features, tax_rate)

    if wacc_raw is not None:
        clipped = _clip_to_field_range("wacc", float(wacc_raw))
        if clipped is not None:
            assumptions["wacc"] = float(clipped)
            provenance["wacc"] = {
                "source": "capm",
                "evidence": (
                    "WACC = w_e * (Rf + beta*ERP) + w_d * Rd * (1-T); "
                    "Rf/ERP from env DCF_RISK_FREE_RATE / DCF_EQUITY_RISK_PREMIUM."
                ),
                "confidence": 0.78,
            }
            out = dict(comp)
            out["method"] = capm_status
            out["wacc_pre_clip"] = float(wacc_raw)
            return out

    prior_mid = _prior_band_midpoint(profile, "wacc")
    merged_comp = dict(comp)
    if prior_mid is not None:
        clipped_prior = _clip_to_field_range("wacc", float(prior_mid))
        if clipped_prior is not None:
            assumptions["wacc"] = float(clipped_prior)
            provenance["wacc"] = {
                "source": "profile_prior_mid",
                "evidence": (
                    "WACC = soft-band midpoint from PROFILE_PRIORS "
                    f"for profile '{profile}' (CAPM inputs incomplete or clipped)."
                ),
                "confidence": 0.55,
            }
            merged_comp["method"] = "profile_prior_fallback"
            merged_comp["profile_prior_mid"] = float(prior_mid)
            merged_comp["capm_status"] = capm_status
            return merged_comp

    merged_comp["method"] = "merge_unchanged"
    merged_comp["capm_status"] = capm_status
    return merged_comp


def _quality_flag(
    *,
    code: str,
    severity: str,
    field: str,
    value: float | None,
    expected: dict[str, float | None],
    profile: str,
    message: str,
) -> dict[str, Any]:
    """Reusable quality flag schema. Same shape across workflows."""
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "value": value,
        "expected": expected,
        "profile": profile,
        "message": message,
    }


def _check_against_band(
    *,
    field: str,
    value: float,
    band: dict[str, float],
    profile: str,
) -> list[dict[str, Any]]:
    """Compare a value to a soft/hard band and return zero or one flag."""
    soft_min = float(band.get("soft_min", float("-inf")))
    soft_max = float(band.get("soft_max", float("inf")))
    hard_min = float(band.get("hard_min", float("-inf")))
    hard_max = float(band.get("hard_max", float("inf")))

    if value < hard_min:
        return [_quality_flag(
            code=f"{field}_below_hard_min",
            severity="block",
            field=field,
            value=value,
            expected={"min": hard_min, "max": hard_max},
            profile=profile,
            message=f"{field}={value:.4g} is implausibly low for profile '{profile}' (hard floor {hard_min:.4g}).",
        )]
    if value > hard_max:
        return [_quality_flag(
            code=f"{field}_above_hard_max",
            severity="block",
            field=field,
            value=value,
            expected={"min": hard_min, "max": hard_max},
            profile=profile,
            message=f"{field}={value:.4g} is implausibly high for profile '{profile}' (hard cap {hard_max:.4g}).",
        )]
    if value < soft_min:
        return [_quality_flag(
            code=f"{field}_below_soft_min",
            severity="warn",
            field=field,
            value=value,
            expected={"min": soft_min, "max": soft_max},
            profile=profile,
            message=f"{field}={value:.4g} is below typical range {soft_min:.4g}-{soft_max:.4g} for profile '{profile}'.",
        )]
    if value > soft_max:
        return [_quality_flag(
            code=f"{field}_above_soft_max",
            severity="warn",
            field=field,
            value=value,
            expected={"min": soft_min, "max": soft_max},
            profile=profile,
            message=f"{field}={value:.4g} is above typical range {soft_min:.4g}-{soft_max:.4g} for profile '{profile}'.",
        )]
    return []


def _check_assumption_plausibility(
    assumptions: dict[str, float],
    profile: str,
) -> list[dict[str, Any]]:
    """Run sector-aware plausibility checks on each assumption."""
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    flags: list[dict[str, Any]] = []
    for field, band in bands.items():
        if field in assumptions:
            flags.extend(
                _check_against_band(
                    field=field,
                    value=float(assumptions[field]),
                    band=band,
                    profile=profile,
                )
            )
    return flags


def _check_valuation_sanity(
    *,
    valuation: dict[str, float],
    profile: str,
    market_snapshot: dict[str, float],
) -> list[dict[str, Any]]:
    """Sanity-check valuation outputs with deterministic rails."""
    bands = _VALUATION_PRIORS.get(profile) or _VALUATION_PRIORS["default"]
    flags: list[dict[str, Any]] = []

    spot = float(market_snapshot.get("price") or 0.0)
    implied = float(valuation.get("implied_share_price") or 0.0)
    if spot > 0 and implied > 0:
        ratio = implied / spot
        flags.extend(
            _check_against_band(
                field="implied_to_spot_price_ratio",
                value=ratio,
                band=bands["price_ratio"],
                profile=profile,
            )
        )

    pv_cash = float(valuation.get("pv_cash_flows") or 0.0)
    terminal_pv_implied = (
        float(valuation.get("enterprise_value") or 0.0) - pv_cash
    )
    ev = float(valuation.get("enterprise_value") or 0.0)
    if ev > 0 and terminal_pv_implied >= 0:
        tv_share = terminal_pv_implied / ev
        flags.extend(
            _check_against_band(
                field="terminal_value_share_of_ev",
                value=tv_share,
                band=bands["tv_share_of_ev"],
                profile=profile,
            )
        )

    return flags


def _compute_confidence_label(
    *,
    assumption_flags: list[dict[str, Any]],
    valuation_flags: list[dict[str, Any]],
    provenance: dict[str, dict[str, Any]],
) -> str:
    """Aggregate flags + provenance into a coarse confidence label."""
    has_block = any(
        flag.get("severity") == "block"
        for flag in (assumption_flags + valuation_flags)
    )
    if has_block:
        return "low"
    warn_count = sum(
        1 for flag in (assumption_flags + valuation_flags)
        if flag.get("severity") == "warn"
    )
    default_count = sum(
        1 for meta in provenance.values()
        if isinstance(meta, dict) and meta.get("source") == "default"
    )
    if warn_count >= 2 or default_count >= 3:
        return "low"
    if warn_count >= 1 or default_count >= 1:
        return "medium"
    return "high"


_ACTIVITY_STATUS_MAP = {
    "start": "started",
    "complete": "completed",
    "skipped": "skipped",
    "awaiting_input": "awaiting_input",
    "edited": "completed",
    "approved": "completed",
    "rejected": "error",
    "fallback": "completed",
}


def _emit_step(step: str, status: str, parent_step_id: str, payload: dict | None = None) -> None:
    """Emit a DCF workflow substep as a unified activity event.

    Stable `activity_id` keyed by `(parent_step_id, step)` ensures the
    `started` and terminal events merge into one entry on the frontend.
    """
    activity_status = _ACTIVITY_STATUS_MAP.get(status, "completed")
    summary = ""
    meta: dict[str, Any] | None = None
    if payload:
        # Pull a one-line label off well-known payload keys for the row,
        # leave the full payload accessible via `meta`.
        if "ticker" in payload:
            summary = f"ticker={payload['ticker']}"
        elif "rows" in payload:
            summary = f"{payload['rows']} rows"
        elif "implied_share_price" in payload:
            summary = f"implied ${payload['implied_share_price']:.2f}"
        meta = dict(payload)
    emit_activity(
        activity_id=f"dcf_{parent_step_id}_{step}",
        kind="workflow_step",
        name=f"workflow:dcf:{step}",
        scope="workflow",
        status=activity_status,
        step_id=parent_step_id,
        parent_activity_id=f"workflow_dcf_{parent_step_id}",
        summary=summary or None,
        meta=meta,
        error=str(payload.get("error")) if status == "rejected" and payload else None,
    )


def _emit_workflow_terminal(
    *, parent_step_id: str, status: str, payload: dict | None = None,
) -> None:
    """Emit a terminal `kind="workflow"` activity for the whole DCF run.

    Mirrors what the legacy `workflow_started` / `workflow_complete`
    events represented but folded into the unified contract.
    """
    summary = None
    meta = dict(payload) if payload else None
    if payload and "implied_share_price" in payload:
        summary = f"implied ${payload['implied_share_price']:.2f}"
    emit_activity(
        activity_id=f"workflow_dcf_{parent_step_id}",
        kind="workflow",
        name="workflow:dcf",
        scope="workflow",
        status=status,  # type: ignore[arg-type]
        step_id=parent_step_id,
        summary=summary,
        confidence_label=payload.get("confidence_label") if payload else None,
        flag_count=payload.get("flag_count") if payload else None,
        meta=meta,
    )


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


def _default_assumptions() -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    assumptions = {
        "revenue_growth": 0.08,
        "fcff_margin": 0.17,
        "wacc": 0.10,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "base_revenue": 10000.0,       # in millions
        "shares_outstanding": 1000.0,  # in millions
        "net_debt": 5000.0,            # in millions
    }
    provenance = {
        key: {
            "source": "default",
            "evidence": "Deterministic workflow default.",
            "confidence": 0.35,
        }
        for key in assumptions
    }
    return assumptions, provenance


def _normalize_assumption_value(raw_value: float, raw_text: str, kind: str) -> float | None:
    text = raw_text.lower()
    value = float(raw_value)
    if kind == "percent":
        # Treat whole numbers near assumption ranges as percentages.
        if "%" in text or abs(value) > 1.0:
            value /= 100.0
        return value
    if kind in {"money_millions", "number_millions"}:
        if "billion" in text or "bn" in text:
            value *= 1000.0
        return value
    return value


def _clip_to_field_range(field: str, value: float) -> float | None:
    spec = _ASSUMPTION_FIELDS[field]
    min_value = float(spec["min"])
    max_value = float(spec["max"])
    if value < min_value or value > max_value:
        return None
    return value


def _extract_candidates_from_text(text: str, *, source: str, evidence_ref: str) -> dict[str, dict[str, Any]]:
    compact = " ".join(str(text).split())
    candidates: dict[str, dict[str, Any]] = {}
    for field, spec in _ASSUMPTION_FIELDS.items():
        for alias in spec["aliases"]:
            pattern = rf"(?i)\b{re.escape(alias)}\b[^-\d%]{{0,80}}(-?\d[\d,]*(?:\.\d+)?)\s*(%|percent|bps|x|million|millions|billion|bn)?"
            match = re.search(pattern, compact)
            if not match:
                continue
            raw_number = float(match.group(1).replace(",", ""))
            if (match.group(2) or "").lower() == "bps":
                raw_number /= 100.0
            value = _normalize_assumption_value(raw_number, match.group(0), str(spec["kind"]))
            if value is None:
                continue
            clipped = _clip_to_field_range(field, value)
            if clipped is None:
                continue
            candidates[field] = {
                "value": clipped,
                "source": source,
                "evidence": compact[max(match.start() - 80, 0): match.end() + 120],
                "reference": evidence_ref,
                "confidence": 0.7 if source == "document" else 0.55,
            }
            break
    return candidates


def _infer_assumptions_from_documents(session_id: str, ticker: str) -> dict[str, dict[str, Any]]:
    if not session_id:
        return {}
    try:
        from documents import hybrid_search, list_docs  # noqa: PLC0415

        ready_docs = [d for d in list_docs(session_id) if d.get("status") == "ready"]
        if not ready_docs:
            return {}
        query = (
            f"{ticker} DCF valuation assumptions revenue growth FCFF margin WACC "
            "terminal growth tax rate net debt shares outstanding"
        )
        results = hybrid_search(query, session_id, n_results=6)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF document assumptions unavailable session_id=%s error=%s", session_id, exc)
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for result in results:
        meta = result.get("metadata") or {}
        filename = meta.get("filename", "uploaded document")
        page = meta.get("page", "?")
        candidates = _extract_candidates_from_text(
            str(result.get("text") or ""),
            source="document",
            evidence_ref=f"{filename} p.{page}",
        )
        for field, candidate in candidates.items():
            if field not in merged:
                merged[field] = candidate
    return merged


def _infer_assumptions_from_web(ticker: str) -> dict[str, dict[str, Any]]:
    query = (
        f"{ticker} DCF assumptions WACC terminal growth revenue growth "
        "free cash flow margin tax rate net debt shares outstanding"
    )
    try:
        raw, _summary = search_exa(query, num_results=3, search_type="auto", max_characters=1200)
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF web assumptions unavailable ticker=%s error=%s", ticker, exc)
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        title = item.get("title") or "web result"
        url = item.get("url") or ""
        text_parts = []
        for highlight in item.get("highlights") or []:
            text_parts.append(str(highlight))
        if item.get("text"):
            text_parts.append(str(item["text"])[:1200])
        candidates = _extract_candidates_from_text(
            " ".join(text_parts),
            source="web",
            evidence_ref=f"{title} {url}".strip(),
        )
        for field, candidate in candidates.items():
            if field not in merged:
                merged[field] = candidate
    return merged


_PROVENANCE_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "as_of",
    "raw_unit",
    "raw_value",
    "field",
)


def _apply_candidates(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> list[str]:
    """Apply candidate assumptions and return the list of fields actually set.

    Each candidate goes through the per-field range check so an unreliable
    upstream source (web snippet, stale yfinance value) cannot inject an
    out-of-bounds number.
    """
    applied: list[str] = []
    for field, candidate in candidates.items():
        if field not in assumptions:
            continue
        try:
            value = float(candidate["value"])
        except (TypeError, ValueError, KeyError):
            continue
        clipped = _clip_to_field_range(field, value)
        if clipped is None:
            logger.warning(
                "DCF rejected out-of-range candidate field=%s value=%s source=%s",
                field,
                value,
                candidate.get("source"),
            )
            continue
        assumptions[field] = clipped
        meta: dict[str, Any] = {
            "source": candidate.get("source", "unknown"),
            "evidence": candidate.get("evidence", ""),
            "reference": candidate.get("reference", ""),
            "confidence": candidate.get("confidence", 0.5),
        }
        for key in _PROVENANCE_PASSTHROUGH_KEYS:
            if key in candidate:
                meta[key] = candidate[key]
        provenance[field] = meta
        applied.append(field)
    return applied


def _filter_tier_a_conflicts(
    candidates: dict[str, dict[str, Any]],
    canonical_fields: set[str],
    *,
    canonical_provenance: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Strip Tier A entries that conflict with canonical fundamentals.

    External hints can still inform Tier B fields (rates), but level variables
    are locked to canonical or user input.
    """
    filtered: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for field, candidate in candidates.items():
        if field in _TIER_A_FIELDS and field in canonical_fields:
            kept = canonical_provenance.get(field, {})
            conflicts.append({
                "field": field,
                "rejected_value": candidate.get("value"),
                "rejected_source": candidate.get("source"),
                "rejected_reference": candidate.get("reference"),
                "kept_value": kept.get("value") if isinstance(kept, dict) else None,
                "kept_source": kept.get("source") if isinstance(kept, dict) else None,
            })
            continue
        filtered[field] = candidate
    return filtered, conflicts


def _fetch_fundamentals_yfinance(ticker: str) -> dict[str, dict[str, Any]]:
    """Pull canonical Tier A fundamentals from yfinance, normalized to millions.

    Returned dict keys match assumption field names. Each entry carries the
    fully resolved value plus provenance metadata so reviewers can trace the
    number back to its source. Failures are swallowed: this layer is
    best-effort and the workflow falls back to defaults if it returns empty.
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals: yfinance unavailable error=%s", exc)
        return {}

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals fetch failed ticker=%s error=%s", ticker, exc)
        return {}

    as_of = time.strftime("%Y-%m-%d")
    out: dict[str, dict[str, Any]] = {}

    shares_raw = info.get("sharesOutstanding")
    if isinstance(shares_raw, (int, float)) and shares_raw > 0:
        out["shares_outstanding"] = {
            "value": float(shares_raw) / 1_000_000.0,
            "source": "yfinance.info",
            "field": "sharesOutstanding",
            "raw_value": float(shares_raw),
            "raw_unit": "shares",
            "as_of": as_of,
            "confidence": 0.95,
            "evidence": "Canonical share count from yfinance.",
        }

    revenue_raw = info.get("totalRevenue")
    if isinstance(revenue_raw, (int, float)) and revenue_raw > 0:
        out["base_revenue"] = {
            "value": float(revenue_raw) / 1_000_000.0,
            "source": "yfinance.info",
            "field": "totalRevenue",
            "raw_value": float(revenue_raw),
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.95,
            "evidence": "Trailing 12-month revenue from yfinance.",
        }

    debt_raw = info.get("totalDebt")
    cash_raw = info.get("totalCash")
    if isinstance(debt_raw, (int, float)) or isinstance(cash_raw, (int, float)):
        debt = float(debt_raw) if isinstance(debt_raw, (int, float)) else 0.0
        cash = float(cash_raw) if isinstance(cash_raw, (int, float)) else 0.0
        net_debt_usd = debt - cash
        out["net_debt"] = {
            "value": net_debt_usd / 1_000_000.0,
            "source": "yfinance.info",
            "field": "totalDebt - totalCash",
            "raw_value": net_debt_usd,
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.85,
            "evidence": "Net debt computed from yfinance balance sheet snapshot.",
        }

    # Free cash flow margin proxy (Tier B, but high-quality when both legs are
    # available). Note this is operating FCF, not strictly FCFF.
    fcf_raw = info.get("freeCashflow") or info.get("operatingCashflow")
    if (
        isinstance(fcf_raw, (int, float))
        and isinstance(revenue_raw, (int, float))
        and revenue_raw
    ):
        margin = float(fcf_raw) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "yfinance.info",
            "field": "freeCashflow / totalRevenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.6,
            "evidence": "FCF margin proxy from yfinance (FCF over revenue).",
        }

    extras_yf: dict[str, Any] = {}
    beta_yf = info.get("beta")
    if isinstance(beta_yf, (int, float)) and 0 < float(beta_yf) <= 2.5:
        extras_yf["beta"] = float(beta_yf)
    mcap_yf = info.get("marketCap")
    if isinstance(mcap_yf, (int, float)) and float(mcap_yf) > 0:
        extras_yf["market_cap_usd"] = float(mcap_yf)
    interest_yf = info.get("interestExpense")
    if isinstance(interest_yf, (int, float)) and float(interest_yf) >= 0:
        extras_yf["interest_expense_usd"] = float(interest_yf)
    if isinstance(debt_raw, (int, float)) and float(debt_raw) >= 0:
        extras_yf["total_debt_usd"] = float(debt_raw)
    if isinstance(cash_raw, (int, float)) and float(cash_raw) >= 0:
        extras_yf["cash_usd"] = float(cash_raw)

    if extras_yf:
        out["__dcf_extras__"] = extras_yf

    return out


def _fmp_get_json(path: str, api_key: str) -> list[dict[str, Any]]:
    # FMP moved most actively supported endpoints under /stable.
    url = f"https://financialmodelingprep.com/stable/{path}"
    try:
        response = requests.get(url, params={"apikey": api_key}, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF fundamentals: FMP request failed path=%s error=%s", path, exc)
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # FMP sometimes returns {"Error Message": "..."} and similar.
        if payload.get("Error Message") or payload.get("error"):
            logger.warning("DCF fundamentals: FMP returned error payload for path=%s", path)
        return []
    return []


def _compute_effective_tax_rate(income: dict[str, Any]) -> float | None:
    """Effective tax rate from FMP income statement.

    Returns None when components missing or non-positive pre-tax income makes
    the ratio unreliable.
    """
    pretax = income.get("incomeBeforeTax")
    tax = income.get("incomeTaxExpense")
    if not isinstance(pretax, (int, float)) or pretax <= 0:
        return None
    if not isinstance(tax, (int, float)):
        return None
    rate = float(tax) / float(pretax)
    if rate < 0.0 or rate > 0.45:
        return None
    return rate


def _compute_fcff_components(
    income: dict[str, Any],
    cashflow: dict[str, Any],
) -> dict[str, float] | None:
    """Compute unlevered free cash flow (FCFF) from FMP statement components.

    Uses the cleaner derivation:
        FCFF = FCF + InterestExpense * (1 - tax_rate)
    where FCF is FMP's `freeCashFlow` (operating cash flow + capex). This
    avoids manual sign-handling on working capital and gives a number much
    closer to what analysts use, vs. naive FCF/Revenue.
    """
    fcf_raw = cashflow.get("freeCashFlow")
    if not isinstance(fcf_raw, (int, float)) or fcf_raw == 0:
        return None
    interest_raw = income.get("interestExpense") or 0.0
    if not isinstance(interest_raw, (int, float)):
        interest_raw = 0.0
    tax_rate = _compute_effective_tax_rate(income)
    if tax_rate is None:
        tax_rate = 0.21  # neutral default when components missing
    fcff = float(fcf_raw) + float(interest_raw) * (1.0 - float(tax_rate))
    return {"fcff": fcff, "tax_rate": tax_rate, "fcf": float(fcf_raw)}


def _fetch_fundamentals_fmp(ticker: str) -> dict[str, dict[str, Any]]:
    """Pull canonical fundamentals from FMP, normalized to millions."""
    api_key = (
        os.getenv("FMP_API_KEY")
        or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
    )
    if not api_key:
        return {}

    income_rows = _fmp_get_json(f"income-statement?symbol={ticker}&period=annual&limit=1", api_key)
    balance_rows = _fmp_get_json(f"balance-sheet-statement?symbol={ticker}&period=annual&limit=1", api_key)
    cashflow_rows = _fmp_get_json(f"cash-flow-statement?symbol={ticker}&period=annual&limit=1", api_key)
    profile_rows = _fmp_get_json(f"profile?symbol={ticker}", api_key)

    income = income_rows[0] if income_rows else {}
    balance = balance_rows[0] if balance_rows else {}
    cashflow = cashflow_rows[0] if cashflow_rows else {}
    profile = profile_rows[0] if profile_rows else {}
    as_of = (
        str(income.get("date") or balance.get("date") or cashflow.get("date") or "")
        or time.strftime("%Y-%m-%d")
    )

    out: dict[str, dict[str, Any]] = {}

    weighted_shares = income.get("weightedAverageShsOutDil")
    # FMP /stable/profile renamed mktCap -> marketCap; accept both for safety.
    market_cap = None
    spot_price = None
    if isinstance(profile, dict):
        market_cap = profile.get("marketCap") or profile.get("mktCap")
        spot_price = profile.get("price")
    derived_shares = None
    if (
        isinstance(market_cap, (int, float)) and market_cap > 0
        and isinstance(spot_price, (int, float)) and spot_price > 0
    ):
        derived_shares = float(market_cap) / float(spot_price)
    shares_final = (
        weighted_shares
        if isinstance(weighted_shares, (int, float)) and weighted_shares > 0
        else derived_shares
    )
    if isinstance(shares_final, (int, float)) and shares_final > 0:
        out["shares_outstanding"] = {
            "value": float(shares_final) / 1_000_000.0,
            "source": "fmp",
            "field": "weightedAverageShsOutDil",
            "raw_value": float(shares_final),
            "raw_unit": "shares",
            "as_of": as_of,
            "confidence": 0.97,
            "evidence": "FMP annual statement share count.",
        }

    revenue_raw = income.get("revenue")
    if isinstance(revenue_raw, (int, float)) and revenue_raw > 0:
        out["base_revenue"] = {
            "value": float(revenue_raw) / 1_000_000.0,
            "source": "fmp",
            "field": "revenue",
            "raw_value": float(revenue_raw),
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.97,
            "evidence": "FMP annual revenue.",
        }

    debt_raw = balance.get("totalDebt")
    cash_raw = balance.get("cashAndCashEquivalents")
    if isinstance(debt_raw, (int, float)) or isinstance(cash_raw, (int, float)):
        debt = float(debt_raw) if isinstance(debt_raw, (int, float)) else 0.0
        cash = float(cash_raw) if isinstance(cash_raw, (int, float)) else 0.0
        net_debt_usd = debt - cash
        out["net_debt"] = {
            "value": net_debt_usd / 1_000_000.0,
            "source": "fmp",
            "field": "totalDebt - cashAndCashEquivalents",
            "raw_value": net_debt_usd,
            "raw_unit": "USD",
            "as_of": as_of,
            "confidence": 0.9,
            "evidence": "FMP annual balance sheet net debt.",
        }

    components = _compute_fcff_components(income, cashflow)
    if (
        components is not None
        and isinstance(revenue_raw, (int, float))
        and float(revenue_raw) != 0.0
    ):
        margin = float(components["fcff"]) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "fmp",
            "field": "(freeCashFlow + interestExpense*(1-tax)) / revenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.85,
            "evidence": (
                "FMP unlevered FCF margin: FCF + after-tax interest, divided by revenue."
            ),
        }

        # Also surface the effective tax rate as a Tier B canonical value.
        if (
            components.get("tax_rate") is not None
            and isinstance(components["tax_rate"], (int, float))
        ):
            out["tax_rate"] = {
                "value": float(components["tax_rate"]),
                "source": "fmp",
                "field": "incomeTaxExpense / incomeBeforeTax",
                "raw_value": float(components["tax_rate"]),
                "raw_unit": "ratio",
                "as_of": as_of,
                "confidence": 0.85,
                "evidence": "FMP effective tax rate from income statement.",
            }
    elif (
        isinstance(cashflow.get("freeCashFlow"), (int, float))
        and isinstance(revenue_raw, (int, float))
        and float(revenue_raw) != 0.0
    ):
        # Fall back to naive FCF/Revenue, marked as proxy with lower confidence.
        margin = float(cashflow.get("freeCashFlow", 0.0)) / float(revenue_raw)
        out["fcff_margin"] = {
            "value": margin,
            "source": "fmp",
            "field": "freeCashFlow / revenue",
            "raw_value": margin,
            "raw_unit": "ratio",
            "as_of": as_of,
            "confidence": 0.6,
            "evidence": "FMP FCF margin (proxy: components for full FCFF unavailable).",
        }

    profile_meta: dict[str, Any] = {}
    if isinstance(profile, dict) and profile:
        profile_meta = {
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "market_cap_usd": profile.get("marketCap") or profile.get("mktCap"),
            "spot_price": profile.get("price"),
            "currency": profile.get("currency"),
            "company_name": profile.get("companyName"),
        }
    if profile_meta:
        out["__profile_meta__"] = profile_meta  # consumed by hydrate node, not an assumption

    extras: dict[str, Any] = {}
    interest_raw = income.get("interestExpense")
    if isinstance(interest_raw, (int, float)) and interest_raw >= 0:
        extras["interest_expense_usd"] = float(interest_raw)
    debt_b = balance.get("totalDebt")
    if isinstance(debt_b, (int, float)) and debt_b >= 0:
        extras["total_debt_usd"] = float(debt_b)
    cash_b = balance.get("cashAndCashEquivalents")
    if isinstance(cash_b, (int, float)) and cash_b >= 0:
        extras["cash_usd"] = float(cash_b)
    if isinstance(market_cap, (int, float)) and market_cap > 0:
        extras["market_cap_usd"] = float(market_cap)

    km_rows = _fmp_get_json(
        f"key-metrics?symbol={ticker}&period=annual&limit=1",
        api_key,
    )
    km = km_rows[0] if km_rows else {}
    beta_km = km.get("beta") if isinstance(km, dict) else None
    beta_val = beta_km if isinstance(beta_km, (int, float)) else None
    if beta_val is None and isinstance(profile.get("beta"), (int, float)):
        beta_val = float(profile["beta"])
    if beta_val is not None and 0 < beta_val <= 2.5:
        extras["beta"] = float(beta_val)

    if extras:
        out["__dcf_extras__"] = extras

    return out


def _apply_overrides(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    overrides: dict[str, float],
) -> None:
    for key, value in overrides.items():
        if key not in assumptions:
            continue
        normalized = _clip_to_field_range(key, float(value))
        if normalized is None:
            logger.warning("DCF ignored out-of-range override field=%s value=%s", key, value)
            continue
        assumptions[key] = normalized
        provenance[key] = {
            "source": "user_override",
            "evidence": "User-provided assumption override.",
            "confidence": 1.0,
        }


def normalize_input_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    # Workflow-level span (mirrors the legacy `workflow_started` event).
    _emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="started",
        payload={"ticker": str(state.get("ticker") or "").upper()},
    )
    _emit_step("normalize_input", "start", parent_step_id)
    ticker = str(state.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required for DCF workflow.")
    horizon = int(state.get("horizon_years") or 5)
    horizon = min(max(horizon, 3), 10)
    logger.info("DCF normalize_input ticker=%s horizon_years=%d", ticker, horizon)
    _emit_step("normalize_input", "complete", parent_step_id, {"ticker": ticker, "horizon_years": horizon})
    return {"ticker": ticker, "horizon_years": horizon}


def hydrate_fundamentals_node(state: DCFState) -> dict:
    """Pull canonical Tier A fundamentals from a deterministic data source.

    These values (revenue, shares outstanding, net debt) anchor the model
    against real balance-sheet scale. Web/document hints can later refine
    rate variables (WACC, terminal growth, …) but cannot override the levels.

    Also classifies the ticker into a sector/size profile so downstream
    plausibility and sanity checks can pick appropriate priors.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state["ticker"]
    _emit_step("hydrate_fundamentals", "start", parent_step_id, {"ticker": ticker})

    fundamentals_fmp = _fetch_fundamentals_fmp(ticker)
    fundamentals_yf = _fetch_fundamentals_yfinance(ticker)

    extras_fmp = dict(fundamentals_fmp.pop("__dcf_extras__", {}))
    extras_yf = dict(fundamentals_yf.pop("__dcf_extras__", {}))
    dcf_extras = _merge_dcf_extras(extras_fmp, extras_yf)

    # Strip out the non-assumption profile meta carrier.
    profile_meta = dict(fundamentals_fmp.pop("__profile_meta__", {}))

    fundamentals = dict(fundamentals_fmp)
    for field, meta in fundamentals_yf.items():
        fundamentals.setdefault(field, meta)

    provider = "fmp"
    if not fundamentals_fmp and fundamentals_yf:
        provider = "yfinance"
    elif fundamentals_fmp and fundamentals_yf:
        provider = "fmp+fallback:yfinance"
    elif not fundamentals:
        provider = "none"

    profile = _classify_profile(
        sector=profile_meta.get("sector"),
        market_cap_usd=profile_meta.get("market_cap_usd"),
    )

    features = _build_feature_vector(
        ticker=ticker,
        profile_bucket=profile,
        profile_meta=profile_meta,
        fundamentals=fundamentals,
        dcf_extras=dcf_extras,
    )

    if fundamentals:
        logger.info(
            "DCF hydrate_fundamentals ticker=%s provider=%s profile=%s fields=%s",
            ticker,
            provider,
            profile,
            sorted(fundamentals.keys()),
        )
    else:
        logger.warning(
            "DCF hydrate_fundamentals ticker=%s: no canonical fundamentals available",
            ticker,
        )

    fields_summary = {
        field: {"value": meta["value"], "source": meta["source"], "as_of": meta.get("as_of")}
        for field, meta in fundamentals.items()
    }
    _emit_step(
        "hydrate_fundamentals",
        "complete" if fundamentals else "fallback",
        parent_step_id,
        {
            "fields": sorted(fundamentals.keys()),
            "provider": provider,
            "fundamentals": fields_summary,
            "profile": profile,
            "profile_meta": profile_meta,
            "features_summary": sorted(features.keys()),
        },
    )
    return {
        "fundamentals": fundamentals,
        "profile": profile,
        "profile_meta": profile_meta,
        "features": features,
        "wacc_components": {},
    }


def build_assumptions_node(state: DCFState) -> dict:
    """Merge default → web → doc → canonical → user with Tier A locked.

    Precedence (lowest to highest, last writer wins):
        1. deterministic defaults
        2. web hints (Tier B fields only when canonical exists for Tier A)
        3. document hints (overrides web)
        4. canonical fundamentals (locks Tier A, may also refine Tier B)
        5. explicit user overrides
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("build_assumptions", "start", parent_step_id)

    assumptions, provenance = _default_assumptions()
    ticker = state["ticker"]
    session_id = state.get("session_id") or ""
    allow_external = bool(state.get("allow_external_assumptions", True))
    fundamentals = state.get("fundamentals") or {}

    # Build a canonical-provenance scratch dict so the conflict filter knows
    # what value to "keep" before we actually apply canonical at step 4.
    canonical_fields = {field for field in fundamentals.keys() if field in _TIER_A_FIELDS}
    canonical_preview: dict[str, dict[str, Any]] = {
        field: {"value": meta.get("value"), "source": meta.get("source")}
        for field, meta in fundamentals.items()
    }

    web_candidates_raw = _infer_assumptions_from_web(ticker) if allow_external else {}
    doc_candidates_raw = _infer_assumptions_from_documents(session_id, ticker)

    web_candidates, web_conflicts = _filter_tier_a_conflicts(
        web_candidates_raw,
        canonical_fields,
        canonical_provenance=canonical_preview,
    )
    doc_candidates, doc_conflicts = _filter_tier_a_conflicts(
        doc_candidates_raw,
        canonical_fields,
        canonical_provenance=canonical_preview,
    )

    web_applied = _apply_candidates(assumptions, provenance, web_candidates)
    doc_applied = _apply_candidates(assumptions, provenance, doc_candidates)
    canonical_applied = _apply_candidates(assumptions, provenance, fundamentals)

    overrides = state.get("assumption_overrides") or {}
    _apply_overrides(assumptions, provenance, overrides)

    profile = state.get("profile") or "default"
    features = dict(state.get("features") or {})
    wacc_components = _resolve_wacc_from_features(
        assumptions,
        provenance,
        features=features,
        profile=profile,
        overrides=overrides,
    )

    conflicts = web_conflicts + doc_conflicts
    if conflicts:
        for c in conflicts:
            logger.info(
                "DCF assumption conflict field=%s rejected_value=%s rejected_source=%s kept_source=%s",
                c.get("field"),
                c.get("rejected_value"),
                c.get("rejected_source"),
                c.get("kept_source"),
            )

    assumption_flags = _check_assumption_plausibility(assumptions, profile)
    if assumption_flags:
        for flag in assumption_flags:
            logger.warning(
                "DCF assumption flag severity=%s field=%s value=%s expected=%s",
                flag.get("severity"),
                flag.get("field"),
                flag.get("value"),
                flag.get("expected"),
            )

    logger.info(
        "DCF build_assumptions assumptions=%s provenance=%s",
        json.dumps(assumptions, ensure_ascii=False),
        json.dumps(provenance, ensure_ascii=False),
    )
    _emit_step(
        "build_assumptions",
        "complete",
        parent_step_id,
        {
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "canonical_fields": sorted(canonical_applied),
            "document_fields": sorted(doc_applied),
            "web_fields": sorted(web_applied),
            "override_fields": sorted(overrides.keys()),
            "assumption_conflicts": conflicts,
            "assumption_flags": assumption_flags,
            "profile": profile,
            "wacc_components": wacc_components,
        },
    )
    return {
        "assumptions": assumptions,
        "assumption_provenance": provenance,
        "assumption_conflicts": conflicts,
        "assumption_flags": assumption_flags,
        "wacc_components": wacc_components,
    }


def review_assumptions_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    if not state.get("assumption_review_mode"):
        _emit_step("assumption_review", "skipped", parent_step_id)
        return {"assumptions_approved": True}

    _emit_step(
        "assumption_review",
        "awaiting_input",
        parent_step_id,
        {
            "assumptions": state.get("assumptions", {}),
            "assumption_provenance": state.get("assumption_provenance", {}),
        },
    )
    decision = interrupt(
        {
            "action": "review_assumptions",
            "workflow": "dcf",
            "message": "Approve or edit DCF assumptions before valuation.",
            "assumptions": state.get("assumptions", {}),
            "assumption_provenance": state.get("assumption_provenance", {}),
            "choices": ["approve", "reject", "edit"],
        }
    )
    action = str(decision.get("action") or "approve").lower()
    if action == "reject":
        _emit_step("assumption_review", "rejected", parent_step_id)
        return {"assumptions_approved": False}

    if action == "edit":
        edits = decision.get("assumptions")
        if isinstance(edits, dict):
            merged = dict(state.get("assumptions", {}))
            provenance = dict(state.get("assumption_provenance", {}))
            for key, value in edits.items():
                if key in merged:
                    normalized = _clip_to_field_range(key, float(value))
                    if normalized is None:
                        continue
                    merged[key] = normalized
                    provenance[key] = {
                        "source": "user_override",
                        "evidence": "User edited assumption during review.",
                        "confidence": 1.0,
                    }
            _emit_step(
                "assumption_review",
                "edited",
                parent_step_id,
                {"assumptions": merged, "assumption_provenance": provenance},
            )
            logger.info("DCF assumption_review edited_assumptions=%s", json.dumps(merged, ensure_ascii=False))
            return {"assumptions": merged, "assumption_provenance": provenance, "assumptions_approved": True}

    _emit_step("assumption_review", "approved", parent_step_id)
    return {"assumptions_approved": True}


def route_after_assumptions(state: DCFState) -> str:
    return "collect_market_data" if state.get("assumptions_approved") else END


def collect_market_data_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("collect_market_data", "start", parent_step_id)
    ticker = state["ticker"]
    snapshot = {"price": 100.0, "source": "fallback"}
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        history = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not history.empty:
            snapshot = {"price": float(history["Close"].iloc[-1]), "source": "yfinance"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF market data fallback ticker=%s error=%s", ticker, exc)

    logger.info("DCF collect_market_data ticker=%s snapshot=%s", ticker, json.dumps(snapshot))
    _emit_step("collect_market_data", "complete", parent_step_id, {"market_snapshot": snapshot})
    return {"market_snapshot": snapshot}


def project_cashflows_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("project_cashflows", "start", parent_step_id)
    a = state["assumptions"]
    revenue = float(a["base_revenue"])
    growth = float(a["revenue_growth"])
    margin = float(a["fcff_margin"])
    projected: list[dict[str, float]] = []
    for year in range(1, int(state["horizon_years"]) + 1):
        revenue *= 1.0 + growth
        fcff = revenue * margin
        projected.append({"year": float(year), "revenue": revenue, "fcff": fcff})

    _emit_step("project_cashflows", "complete", parent_step_id, {"rows": len(projected)})
    logger.info("DCF project_cashflows rows=%d", len(projected))
    return {"projected_fcff": projected}


def compute_valuation_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("compute_valuation", "start", parent_step_id)
    a = state["assumptions"]
    wacc = float(a["wacc"])
    terminal_growth = float(a["terminal_growth"])
    projected = state["projected_fcff"]

    pv_sum = 0.0
    for row in projected:
        year = int(row["year"])
        pv_sum += float(row["fcff"]) / ((1.0 + wacc) ** year)

    terminal_fcf = float(projected[-1]["fcff"]) * (1.0 + terminal_growth)
    terminal_value = terminal_fcf / max((wacc - terminal_growth), 1e-6)
    terminal_pv = terminal_value / ((1.0 + wacc) ** int(projected[-1]["year"]))
    enterprise_value = pv_sum + terminal_pv
    equity_value = enterprise_value - float(a["net_debt"])
    implied_share_price = equity_value / max(float(a["shares_outstanding"]), 1e-6)

    valuation = {
        "pv_cash_flows": pv_sum,
        "terminal_value": terminal_value,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "implied_share_price": implied_share_price,
        "current_price": float(state["market_snapshot"].get("price", 0.0)),
    }

    profile = state.get("profile") or "default"
    valuation_flags = _check_valuation_sanity(
        valuation=valuation,
        profile=profile,
        market_snapshot=state.get("market_snapshot") or {},
    )
    if valuation_flags:
        for flag in valuation_flags:
            logger.warning(
                "DCF valuation flag severity=%s field=%s value=%s expected=%s",
                flag.get("severity"),
                flag.get("field"),
                flag.get("value"),
                flag.get("expected"),
            )

    confidence_label = _compute_confidence_label(
        assumption_flags=state.get("assumption_flags") or [],
        valuation_flags=valuation_flags,
        provenance=state.get("assumption_provenance") or {},
    )

    logger.info(
        "DCF compute_valuation valuation=%s confidence=%s",
        json.dumps(valuation, ensure_ascii=False),
        confidence_label,
    )
    _emit_step(
        "compute_valuation",
        "complete",
        parent_step_id,
        {
            "valuation": valuation,
            "valuation_flags": valuation_flags,
            "confidence_label": confidence_label,
        },
    )
    return {
        "valuation": valuation,
        "valuation_flags": valuation_flags,
        "confidence_label": confidence_label,
    }


def sensitivity_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("sensitivity", "start", parent_step_id)
    projected = state["projected_fcff"]
    terminal_base = float(state["assumptions"]["terminal_growth"])
    wacc_base = float(state["assumptions"]["wacc"])
    shares = max(float(state["assumptions"]["shares_outstanding"]), 1e-6)
    net_debt = float(state["assumptions"]["net_debt"])
    table: list[dict[str, float]] = []

    for wacc in [wacc_base - 0.01, wacc_base, wacc_base + 0.01]:
        for tg in [terminal_base - 0.005, terminal_base, terminal_base + 0.005]:
            pv_sum = 0.0
            for row in projected:
                year = int(row["year"])
                pv_sum += float(row["fcff"]) / ((1.0 + wacc) ** year)
            terminal_fcf = float(projected[-1]["fcff"]) * (1.0 + tg)
            terminal_value = terminal_fcf / max((wacc - tg), 1e-6)
            terminal_pv = terminal_value / ((1.0 + wacc) ** int(projected[-1]["year"]))
            equity = pv_sum + terminal_pv - net_debt
            table.append(
                {
                    "wacc": round(wacc, 4),
                    "terminal_growth": round(tg, 4),
                    "implied_share_price": round(equity / shares, 4),
                }
            )

    logger.info("DCF sensitivity rows=%d", len(table))
    _emit_step("sensitivity", "complete", parent_step_id, {"rows": len(table)})
    return {"sensitivity_table": table}


def finalize_node(state: DCFState) -> dict:
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    _emit_step("finalize", "start", parent_step_id)
    run_dir = get_run_dir()
    out_path = Path(run_dir) / "dcf_output.json"
    payload = {
        "workflow": "dcf",
        "generated_at": time.time(),
        "ticker": state["ticker"],
        "horizon_years": state["horizon_years"],
        "profile": state.get("profile", "default"),
        "profile_meta": state.get("profile_meta", {}),
        "confidence_label": state.get("confidence_label", "medium"),
        "assumptions": state["assumptions"],
        "assumption_provenance": state.get("assumption_provenance", {}),
        "fundamentals": state.get("fundamentals", {}),
        "assumption_conflicts": state.get("assumption_conflicts", []),
        "assumption_flags": state.get("assumption_flags", []),
        "valuation_flags": state.get("valuation_flags", []),
        "market_snapshot": state["market_snapshot"],
        "valuation": state["valuation"],
        "sensitivity_table": state["sensitivity_table"],
        "features": state.get("features") or {},
        "wacc_components": state.get("wacc_components") or {},
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "DCF finalize output_path=%s confidence=%s flags=%d",
        out_path,
        payload["confidence_label"],
        len(payload["assumption_flags"]) + len(payload["valuation_flags"]),
    )
    _emit_step(
        "finalize",
        "complete",
        parent_step_id,
        {
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "assumption_flags": payload["assumption_flags"],
            "valuation_flags": payload["valuation_flags"],
        },
    )
    _emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="completed",
        payload={
            "result_path": str(out_path),
            "implied_share_price": payload["valuation"]["implied_share_price"],
            "confidence_label": payload["confidence_label"],
            "flag_count": len(payload["assumption_flags"]) + len(payload["valuation_flags"]),
        },
    )
    return {"result_path": str(out_path)}


def summarize_dcf_payload(payload: dict[str, Any]) -> str:
    """One-line summary suitable for LLM tool result feedback.

    Surfaces the trust signals up-front so the assistant cannot present a
    flagged DCF as authoritative without acknowledging the caveats.
    """
    if not isinstance(payload, dict):
        return "DCF workflow finished without payload."

    ticker = str(payload.get("ticker") or "?").upper()
    valuation = payload.get("valuation") or {}
    implied = valuation.get("implied_share_price")
    spot = valuation.get("current_price") or 0.0
    confidence = str(payload.get("confidence_label") or "medium")
    flags = list(payload.get("assumption_flags") or []) + list(payload.get("valuation_flags") or [])
    block_count = sum(1 for f in flags if f.get("severity") == "block")
    warn_count = sum(1 for f in flags if f.get("severity") == "warn")

    base = f"DCF for {ticker}"
    if isinstance(implied, (int, float)) and implied:
        base += f" implied share price={implied:.2f}"
        if isinstance(spot, (int, float)) and spot:
            base += f" vs spot={spot:.2f}"
    base += f" | confidence={confidence}"
    if block_count or warn_count:
        base += f" | flags: blocks={block_count} warns={warn_count}"
    if block_count:
        sample = next((f for f in flags if f.get("severity") == "block"), None)
        if isinstance(sample, dict) and sample.get("message"):
            base += f" | first_block: {sample['message']}"

    wacc_comp = payload.get("wacc_components") or {}
    wacc_method = wacc_comp.get("method")
    if wacc_method:
        base += f" | WACC_source={wacc_method}"
    return base + "."


def run_dcf_workflow_sync(
    *,
    ticker: str,
    horizon_years: int = 5,
    assumption_review_mode: bool = False,
    allow_external_assumptions: bool = True,
    assumption_overrides: dict[str, float] | None = None,
    parent_step_id: str = "workflow_dcf",
    session_id: str = "",
) -> dict:
    """Run DCF workflow synchronously and return parsed result payload."""
    if assumption_review_mode:
        raise ValueError(
            "assumption_review_mode=true requires HITL flow. "
            "Use the standalone workflow endpoint for reviewed runs."
        )

    initial_state: DCFState = {
        "ticker": ticker,
        "horizon_years": horizon_years,
        "session_id": session_id,
        "assumption_review_mode": assumption_review_mode,
        "allow_external_assumptions": allow_external_assumptions,
        "assumption_overrides": assumption_overrides or {},
        "assumptions": {},
        "assumption_provenance": {},
        "assumptions_approved": False,
        "fundamentals": {},
        "assumption_conflicts": [],
        "profile": "default",
        "profile_meta": {},
        "assumption_flags": [],
        "valuation_flags": [],
        "confidence_label": "medium",
        "market_snapshot": {},
        "projected_fcff": [],
        "valuation": {},
        "sensitivity_table": [],
        "result_path": None,
        "parent_step_id": parent_step_id,
        "features": {},
        "wacc_components": {},
    }
    result = dcf_workflow_app.invoke(initial_state, config={"configurable": {"thread_id": get_run_dir().name}})
    result_path = result.get("result_path")
    if not result_path:
        raise RuntimeError("DCF workflow finished without a result path.")
    out_path = Path(result_path)
    if not out_path.exists():
        raise FileNotFoundError(f"DCF workflow result not found: {result_path}")
    return json.loads(out_path.read_text(encoding="utf-8"))


graph = StateGraph(DCFState)
graph.add_node("normalize_input", normalize_input_node)
graph.add_node("hydrate_fundamentals", hydrate_fundamentals_node)
graph.add_node("build_assumptions", build_assumptions_node)
graph.add_node("review_assumptions", review_assumptions_node)
graph.add_node("collect_market_data", collect_market_data_node)
graph.add_node("project_cashflows", project_cashflows_node)
graph.add_node("compute_valuation", compute_valuation_node)
graph.add_node("sensitivity", sensitivity_node)
graph.add_node("finalize", finalize_node)

graph.add_edge(START, "normalize_input")
graph.add_edge("normalize_input", "hydrate_fundamentals")
graph.add_edge("hydrate_fundamentals", "build_assumptions")
graph.add_edge("build_assumptions", "review_assumptions")
graph.add_conditional_edges(
    "review_assumptions",
    route_after_assumptions,
    {"collect_market_data": "collect_market_data", END: END},
)
graph.add_edge("collect_market_data", "project_cashflows")
graph.add_edge("project_cashflows", "compute_valuation")
graph.add_edge("compute_valuation", "sensitivity")
graph.add_edge("sensitivity", "finalize")
graph.add_edge("finalize", END)

dcf_workflow_app = graph.compile()

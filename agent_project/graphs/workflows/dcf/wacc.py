"""CAPM-style WACC estimation and resolution.

Produces auditable ``wacc_components`` on the DCF state for transparency.
"""

from __future__ import annotations

from typing import Any

from .priors import prior_band_midpoint
from .state import (
    _DEFAULT_EQUITY_RISK_PREMIUM,
    _DEFAULT_RISK_FREE_RATE,
    clip_to_field_range,
    coerce_finite_float,
)


def estimate_capm_wacc(
    features: dict[str, Any],
    tax_rate: float,
    *,
    rf: float = _DEFAULT_RISK_FREE_RATE,
    erp: float = _DEFAULT_EQUITY_RISK_PREMIUM,
) -> tuple[float | None, dict[str, Any], str]:
    """Return (wacc_pre_clip_or_none, component dict, status).

    ``status`` is ``"capm"`` when a full decomposition is computed, else
    ``"capm_incomplete"``.
    """
    tr = float(min(max(tax_rate, 0.0), 0.35))
    components: dict[str, Any] = {
        "risk_free_rate": rf,
        "equity_risk_premium": erp,
        "marginal_tax_rate": tr,
    }
    beta = coerce_finite_float(features.get("beta"))
    if beta is None or beta <= 0 or beta > 2.5:
        components["beta"] = beta
        components["capm_block_reason"] = "missing_or_extreme_beta"
        return None, components, "capm_incomplete"

    E = coerce_finite_float(features.get("equity_value_usd"))
    if E is None or E <= 0:
        components["capm_block_reason"] = "missing_equity_value"
        return None, components, "capm_incomplete"

    D_raw = coerce_finite_float(features.get("total_debt_usd"))
    D = max(float(D_raw or 0.0), 0.0)

    interest = coerce_finite_float(features.get("interest_expense_usd"))
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


def solve_implied_wacc(
    projected_fcff: list[dict[str, float]],
    terminal_growth: float,
    implied_ev_M: float,
) -> float | None:
    """Bisection solver for market-implied WACC (no scipy dependency).

    Finds the discount rate such that DCF NPV equals implied_ev_M.
    All monetary values in millions. Returns None if unsolvable.
    """
    if implied_ev_M <= 0 or not projected_fcff:
        return None

    last_fcff = float(projected_fcff[-1]["fcff"])
    n_years = len(projected_fcff)

    def npv(wacc: float) -> float:
        if wacc <= terminal_growth:
            return float("inf")
        pv = sum(
            float(row["fcff"]) / ((1.0 + wacc) ** int(row["year"]))
            for row in projected_fcff
        )
        terminal_fcf = last_fcff * (1.0 + terminal_growth)
        tv = terminal_fcf / (wacc - terminal_growth)
        tv_pv = tv / ((1.0 + wacc) ** n_years)
        return pv + tv_pv

    lo, hi = 0.01, 0.50
    try:
        f_lo = npv(lo) - implied_ev_M
        f_hi = npv(hi) - implied_ev_M
    except (ZeroDivisionError, OverflowError):
        return None

    if f_lo * f_hi > 0:
        return None  # no root in [lo, hi]

    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            f_mid = npv(mid) - implied_ev_M
        except (ZeroDivisionError, OverflowError):
            return None
        if abs(f_mid) < 1.0:  # within $1M
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo = mid
            f_lo = f_mid

    return (lo + hi) / 2.0


def resolve_wacc_from_features(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    *,
    features: dict[str, Any],
    profile: str,
    overrides: dict[str, float],
) -> dict[str, Any]:
    """Set ``assumptions['wacc']`` from CAPM or PROFILE_PRIORS midpoint.

    Never clobbers a user override.
    """
    if provenance.get("wacc", {}).get("source") == "user_override" or "wacc" in overrides:
        return {"method": "user_override"}

    tax_rate = float(assumptions.get("tax_rate", 0.21))
    wacc_raw, comp, capm_status = estimate_capm_wacc(features, tax_rate)

    if wacc_raw is not None:
        clipped = clip_to_field_range("wacc", float(wacc_raw))
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

    prior_mid = prior_band_midpoint(profile, "wacc")
    merged_comp = dict(comp)
    if prior_mid is not None:
        clipped_prior = clip_to_field_range("wacc", float(prior_mid))
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

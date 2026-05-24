"""CAPM-style WACC estimation and resolution.

Produces auditable ``wacc_components`` on the DCF state for transparency.

The pipeline is:

1. ``estimate_capm_wacc``  — bottoms-up CAPM with size/leverage decomposition.
2. ``apply_profile_wacc_stack`` — explicit, observable adjustments by profile
   (mega-cap quality discount, low-beta defensive discount, etc.) plus a hard
   clip to the profile soft band. Never ticker-specific.
3. ``resolve_wacc_from_features`` — orchestrates the above and writes back to
   ``assumptions['wacc']`` + ``provenance['wacc']``.

Downstream nodes (refinement, analysis adjustments) re-clip via
``clip_wacc_to_profile_band`` so the discount rate cannot drift outside the
profile envelope without a user override.
"""

from __future__ import annotations

from typing import Any

from .priors import PROFILE_PRIORS, prior_band_midpoint
from .state import (
    _DEFAULT_EQUITY_RISK_PREMIUM,
    _DEFAULT_RISK_FREE_RATE,
    clip_to_field_range,
    coerce_finite_float,
)


# ---------------------------------------------------------------------------
# Profile WACC bands (final rails) — sourced from PROFILE_PRIORS so the
# plausibility checker and the discount-rate stack stay in sync.
# ---------------------------------------------------------------------------


def _profile_wacc_band(profile: str) -> dict[str, float]:
    """Return the soft+hard WACC band for *profile* with sane fallbacks."""
    bands = (PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]).get("wacc") or {}
    soft_min = float(bands.get("soft_min", 0.07))
    soft_max = float(bands.get("soft_max", 0.13))
    hard_min = float(bands.get("hard_min", soft_min))
    hard_max = float(bands.get("hard_max", soft_max))
    return {
        "soft_min": soft_min,
        "soft_max": soft_max,
        "hard_min": hard_min,
        "hard_max": hard_max,
        "midpoint": (soft_min + soft_max) / 2.0,
    }


def apply_profile_wacc_stack(
    base_wacc: float,
    *,
    profile: str,
    features: dict[str, Any] | None = None,
    assumptions: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Apply general profile-level adjustments + clip to profile band.

    Inputs are observable: profile bucket, features (size/beta/net debt),
    and any already-resolved operating assumptions. No ticker-specific logic.

    Adjustments stack additively before the band clip, so the audit trail
    in the return dict shows base CAPM, each premium/discount, the pre-clip
    sum, the band rails, and the final WACC.
    """
    feats = dict(features or {})
    assumes = dict(assumptions or {})
    band = _profile_wacc_band(profile)

    market_cap = (
        coerce_finite_float(feats.get("market_cap_usd"))
        or coerce_finite_float(feats.get("equity_value_usd"))
        or 0.0
    )
    beta = coerce_finite_float(feats.get("beta"))
    net_debt = coerce_finite_float(feats.get("net_debt_usd"))
    fcff_margin = coerce_finite_float(assumes.get("fcff_margin"))

    components: list[dict[str, Any]] = [
        {"label": "Base CAPM", "delta": 0.0, "value": float(base_wacc)},
    ]

    # 1) Mega-cap durability — size + low-cyclicality signal applies to
    #    mega_cap_tech and large_cap_tech alike (size is a real risk factor).
    if profile in {"mega_cap_tech", "large_cap_tech"} and market_cap >= 1.0e12 and beta is not None and beta < 1.3:
        components.append({
            "label": "Mega-cap durability discount (≥$1T cap, β<1.3)",
            "delta": -0.0075,
        })

    # 1b) High FCFF conversion — durable cash economics, profile-wide not ticker-specific.
    if (
        profile in {"mega_cap_tech", "large_cap_tech", "mature_consumer_or_industrial"}
        and fcff_margin is not None
        and fcff_margin >= 0.22
    ):
        components.append({
            "label": "High FCFF margin (≥22%) quality discount",
            "delta": -0.0050,
        })

    # 2) Balance-sheet quality — net cash + double-digit FCFF margin.
    if (
        profile in {"mega_cap_tech", "large_cap_tech", "mature_consumer_or_industrial"}
        and net_debt is not None
        and net_debt < 0
        and fcff_margin is not None
        and fcff_margin >= 0.20
    ):
        components.append({
            "label": "Net-cash + ≥20% FCFF margin (balance-sheet quality)",
            "delta": -0.0025,
        })

    # 3) Low-beta defensive — broad rule for utilities/staples/industrials
    #    that lean lower-volatility.
    if profile == "mature_consumer_or_industrial" and beta is not None and beta < 1.0:
        components.append({
            "label": "Low-beta defensive (β<1.0)",
            "delta": -0.0025,
        })

    quality_delta = sum(c["delta"] for c in components if "delta" in c and c.get("label") != "Base CAPM")
    pre_clip = float(base_wacc) + quality_delta

    # Clip to soft band (the soft band is the mainstream envelope; hard band
    # exists only to flag truly absurd inputs in the plausibility checker).
    final_wacc = max(band["soft_min"], min(band["soft_max"], pre_clip))
    clip_delta = final_wacc - pre_clip
    clipped = abs(clip_delta) > 1e-9
    if clipped:
        if pre_clip > band["soft_max"]:
            label = f"Clipped to profile ceiling {band['soft_max']:.2%}"
        else:
            label = f"Clipped to profile floor {band['soft_min']:.2%}"
        components.append({"label": label, "delta": float(clip_delta)})

    return {
        "base_capm": float(base_wacc),
        "quality_delta": float(quality_delta),
        "pre_clip": float(pre_clip),
        "final_wacc": float(final_wacc),
        "profile_band": band,
        "clipped": clipped,
        "components": components,
        "summary_line": (
            f"base={base_wacc:.2%} quality={quality_delta:+.2%} "
            f"pre_clip={pre_clip:.2%} → final={final_wacc:.2%} "
            f"(band {band['soft_min']:.0%}-{band['soft_max']:.0%}, profile={profile})"
        ),
    }


def append_wacc_stack_delta(
    wacc_components: dict[str, Any],
    *,
    old_wacc: float,
    new_wacc: float,
    label: str,
    source: str = "",
) -> dict[str, Any]:
    """Record a post-stack WACC change so the report matches valuation assumptions."""
    if abs(float(new_wacc) - float(old_wacc)) < 1e-6:
        return wacc_components

    out = dict(wacc_components)
    stack = dict(out.get("wacc_stack") or {})
    if "profile_stack_wacc" not in stack:
        stack["profile_stack_wacc"] = float(stack.get("final_wacc", old_wacc))

    components = list(stack.get("components") or [])
    if not components:
        components = [{"label": "Prior WACC", "delta": 0.0, "value": float(old_wacc)}]

    components.append({
        "label": label,
        "delta": float(new_wacc) - float(old_wacc),
        "source": source or label,
    })
    stack["components"] = components
    stack["final_wacc"] = float(new_wacc)
    stack["valuation_wacc"] = float(new_wacc)
    out["wacc_stack"] = stack
    out["wacc_valuation"] = float(new_wacc)
    return out


def clip_wacc_to_profile_band(
    wacc: float,
    *,
    profile: str,
    allow_override: bool = False,
) -> tuple[float, bool]:
    """Clamp *wacc* into the profile soft band.

    ``allow_override=True`` skips the clip (used when WACC source is a user
    override — we trust the user even outside the band, but log the choice).

    Returns ``(clipped_value, was_clipped)``.
    """
    if allow_override:
        return float(wacc), False
    band = _profile_wacc_band(profile)
    clipped = max(band["soft_min"], min(band["soft_max"], float(wacc)))
    return clipped, abs(clipped - float(wacc)) > 1e-9


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
    *,
    buyback_yield: float = 0.0,
    net_debt_M: float = 0.0,
) -> float | None:
    """Bisection solver for market-implied WACC (no scipy dependency).

    Finds the discount rate such that DCF NPV equals implied_ev_M. Mirrors
    ``compute_valuation_node``'s terminal formula, including perpetual
    buyback compounding capped by terminal FCFF yield. All monetary values
    in millions. Returns None if unsolvable.
    """
    if implied_ev_M <= 0 or not projected_fcff:
        return None

    last_fcff = float(projected_fcff[-1]["fcff"])
    n_years = len(projected_fcff)

    def _terminal_value(wacc: float) -> float:
        terminal_fcf = last_fcff * (1.0 + terminal_growth)
        # Match forward formula: cap perpetual buyback at FCFF yield against
        # pre-buyback terminal equity, hard-capped at 4%.
        pre_bb_tv = terminal_fcf / max((wacc - terminal_growth), 1e-9)
        pre_bb_eq = pre_bb_tv - net_debt_M
        fcff_yield = (terminal_fcf / pre_bb_eq) if pre_bb_eq > 0 else 0.0
        perp_bb = max(0.0, min(float(buyback_yield), fcff_yield, 0.04))
        if wacc - terminal_growth - perp_bb < 0.005:
            perp_bb = max(0.0, wacc - terminal_growth - 0.005)
        eff_g = terminal_growth + perp_bb
        return terminal_fcf / max((wacc - eff_g), 1e-9)

    def npv(wacc: float) -> float:
        if wacc <= terminal_growth:
            return float("inf")
        pv = sum(
            float(row["fcff"]) / ((1.0 + wacc) ** int(row["year"]))
            for row in projected_fcff
        )
        tv_pv = _terminal_value(wacc) / ((1.0 + wacc) ** n_years)
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
    """Set ``assumptions['wacc']`` from the profile WACC stack.

    Order of resolution:
      1. Honor user overrides verbatim.
      2. Compute CAPM seed.
      3. If CAPM is usable, run ``apply_profile_wacc_stack`` (quality discount
         + profile-band clip) and write the final value.
      4. Otherwise fall back to the profile prior midpoint.

    The full stack (base, deltas, clip, final) is captured in the returned
    ``wacc_components`` dict for the report and audit trail.
    """
    if provenance.get("wacc", {}).get("source") == "user_override" or "wacc" in overrides:
        return {"method": "user_override"}

    tax_rate = float(assumptions.get("tax_rate", 0.21))
    wacc_raw, comp, capm_status = estimate_capm_wacc(features, tax_rate)

    if wacc_raw is not None:
        stack = apply_profile_wacc_stack(
            float(wacc_raw),
            profile=profile,
            features=features,
            assumptions=assumptions,
        )
        final_wacc = stack["final_wacc"]
        clipped = clip_to_field_range("wacc", float(final_wacc))
        if clipped is not None:
            assumptions["wacc"] = float(clipped)
            evidence_extra = []
            if stack["quality_delta"] != 0.0:
                evidence_extra.append(
                    f"profile-stack adjustment {stack['quality_delta']:+.2%}"
                )
            if stack["clipped"]:
                band = stack["profile_band"]
                evidence_extra.append(
                    f"clipped to '{profile}' band {band['soft_min']:.0%}-{band['soft_max']:.0%}"
                )
            extra_str = (" (" + "; ".join(evidence_extra) + ")") if evidence_extra else ""
            provenance["wacc"] = {
                "source": "capm",
                "evidence": (
                    "WACC = w_e * (Rf + beta*ERP) + w_d * Rd * (1-T); "
                    "Rf/ERP from env DCF_RISK_FREE_RATE / DCF_EQUITY_RISK_PREMIUM."
                    + extra_str
                ),
                "confidence": 0.78,
            }
            out = dict(comp)
            out["method"] = capm_status
            out["wacc_pre_clip"] = float(wacc_raw)
            out["wacc_stack"] = stack
            out["wacc_profile_band"] = stack["profile_band"]
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
            merged_comp["wacc_profile_band"] = _profile_wacc_band(profile)
            return merged_comp

    merged_comp["method"] = "merge_unchanged"
    merged_comp["capm_status"] = capm_status
    return merged_comp

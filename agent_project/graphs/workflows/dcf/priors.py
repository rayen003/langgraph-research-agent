"""Profile priors, plausibility bands, valuation sanity checks, and confidence scoring.

These are reusable across valuation workflows (DCF, comps, LBO can share the
same priors when judging multiples, IRRs, etc.).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Profile classification helpers
# ---------------------------------------------------------------------------

_TECH_SECTORS = {"Technology", "Communication Services", "Consumer Cyclical"}
_INDUSTRIAL_SECTORS = {
    "Industrials", "Basic Materials", "Energy", "Utilities",
    "Consumer Defensive", "Real Estate",
}


def classify_profile(sector: str | None, market_cap_usd: float | None) -> str:
    """Pick a prior bucket from company profile data.

    Falls back to ``"default"`` when sector/market_cap unknown. Mega-cap is
    defined as USD 200B+ which captures Apple, Microsoft, Alphabet, Amazon,
    Nvidia, Meta, Tesla, etc.
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


def prior_band_midpoint(profile: str, field: str) -> float | None:
    """Centre of the soft band — used only as deterministic fallback."""
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    band = bands.get(field)
    if not band:
        return None
    lo = float(band.get("soft_min", band.get("hard_min", 0.0)))
    hi = float(band.get("soft_max", band.get("hard_max", lo)))
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Sector / size priors
# ---------------------------------------------------------------------------
# Bands are intentionally broad. ``soft_*`` triggers a ``warn`` flag
# (unusual but not impossible). ``hard_*`` triggers a ``block`` flag
# (very likely modelling error).

PROFILE_PRIORS: dict[str, dict[str, dict[str, float]]] = {
    "mega_cap_tech": {
        "fcff_margin":             {"soft_min": 0.20, "soft_max": 0.40, "hard_min": 0.05, "hard_max": 0.55},
        "fcff_margin_terminal":    {"soft_min": 0.20, "soft_max": 0.45, "hard_min": 0.05, "hard_max": 0.60},
        "wacc":                    {"soft_min": 0.07, "soft_max": 0.10, "hard_min": 0.04, "hard_max": 0.13},
        "revenue_growth":          {"soft_min": 0.05, "soft_max": 0.25, "hard_min": -0.10, "hard_max": 0.40},
        "revenue_growth_terminal": {"soft_min": 0.03, "soft_max": 0.15, "hard_min": -0.10, "hard_max": 0.30},
        "terminal_growth":         {"soft_min": 0.020, "soft_max": 0.035, "hard_min": -0.01, "hard_max": 0.045},
        "tax_rate":                {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
        "buyback_yield":           {"soft_min": 0.01, "soft_max": 0.05, "hard_min": -0.02, "hard_max": 0.08},
        "sbc_pct_revenue":         {"soft_min": 0.02, "soft_max": 0.10, "hard_min": 0.0, "hard_max": 0.15},
    },
    "large_cap_tech": {
        "fcff_margin":             {"soft_min": 0.12, "soft_max": 0.30, "hard_min": -0.10, "hard_max": 0.50},
        "fcff_margin_terminal":    {"soft_min": 0.12, "soft_max": 0.35, "hard_min": -0.10, "hard_max": 0.55},
        "wacc":                    {"soft_min": 0.08, "soft_max": 0.12, "hard_min": 0.05, "hard_max": 0.15},
        "revenue_growth":          {"soft_min": 0.05, "soft_max": 0.30, "hard_min": -0.10, "hard_max": 0.50},
        "revenue_growth_terminal": {"soft_min": 0.03, "soft_max": 0.20, "hard_min": -0.10, "hard_max": 0.35},
        "terminal_growth":         {"soft_min": 0.020, "soft_max": 0.040, "hard_min": -0.01, "hard_max": 0.05},
        "tax_rate":                {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
        "buyback_yield":           {"soft_min": 0.0, "soft_max": 0.04, "hard_min": -0.03, "hard_max": 0.08},
        "sbc_pct_revenue":         {"soft_min": 0.03, "soft_max": 0.12, "hard_min": 0.0, "hard_max": 0.20},
    },
    "mature_consumer_or_industrial": {
        "fcff_margin":             {"soft_min": 0.04, "soft_max": 0.15, "hard_min": -0.05, "hard_max": 0.35},
        "fcff_margin_terminal":    {"soft_min": 0.04, "soft_max": 0.18, "hard_min": -0.05, "hard_max": 0.40},
        "wacc":                    {"soft_min": 0.07, "soft_max": 0.10, "hard_min": 0.05, "hard_max": 0.13},
        "revenue_growth":          {"soft_min": 0.0, "soft_max": 0.10, "hard_min": -0.10, "hard_max": 0.25},
        "revenue_growth_terminal": {"soft_min": 0.0, "soft_max": 0.08, "hard_min": -0.10, "hard_max": 0.20},
        "terminal_growth":         {"soft_min": 0.015, "soft_max": 0.030, "hard_min": -0.01, "hard_max": 0.040},
        "tax_rate":                {"soft_min": 0.15, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.40},
        "buyback_yield":           {"soft_min": 0.0, "soft_max": 0.03, "hard_min": -0.02, "hard_max": 0.06},
        "sbc_pct_revenue":         {"soft_min": 0.0, "soft_max": 0.02, "hard_min": 0.0, "hard_max": 0.05},
    },
    "default": {
        "fcff_margin":             {"soft_min": 0.05, "soft_max": 0.30, "hard_min": -0.20, "hard_max": 0.55},
        "fcff_margin_terminal":    {"soft_min": 0.05, "soft_max": 0.35, "hard_min": -0.20, "hard_max": 0.60},
        "wacc":                    {"soft_min": 0.07, "soft_max": 0.13, "hard_min": 0.04, "hard_max": 0.20},
        "revenue_growth":          {"soft_min": 0.0, "soft_max": 0.25, "hard_min": -0.20, "hard_max": 0.50},
        "revenue_growth_terminal": {"soft_min": 0.0, "soft_max": 0.15, "hard_min": -0.20, "hard_max": 0.30},
        "terminal_growth":         {"soft_min": 0.015, "soft_max": 0.035, "hard_min": -0.01, "hard_max": 0.05},
        "tax_rate":                {"soft_min": 0.10, "soft_max": 0.30, "hard_min": 0.0, "hard_max": 0.45},
        "buyback_yield":           {"soft_min": 0.0, "soft_max": 0.03, "hard_min": -0.03, "hard_max": 0.08},
        "sbc_pct_revenue":         {"soft_min": 0.0, "soft_max": 0.10, "hard_min": 0.0, "hard_max": 0.20},
    },
}

# Output sanity rails (applied inside compute_valuation_node).
_VALUATION_PRIORS: dict[str, dict[str, dict[str, float]]] = {
    "default": {
        # implied / spot price ratio — <0.25 or >4 is almost always inputs error.
        "price_ratio":          {"soft_min": 0.5, "soft_max": 2.0, "hard_min": 0.25, "hard_max": 4.0},
        # terminal value should normally dominate enterprise value for going concerns.
        "tv_share_of_ev":       {"soft_min": 0.60, "soft_max": 0.95, "hard_min": 0.40, "hard_max": 0.99},
    },
}


# ---------------------------------------------------------------------------
# Quality flags
# ---------------------------------------------------------------------------


def quality_flag(
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


def check_against_band(
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
        return [quality_flag(
            code=f"{field}_below_hard_min",
            severity="block",
            field=field,
            value=value,
            expected={"min": hard_min, "max": hard_max},
            profile=profile,
            message=f"{field}={value:.4g} is implausibly low for profile '{profile}' (hard floor {hard_min:.4g}).",
        )]
    if value > hard_max:
        return [quality_flag(
            code=f"{field}_above_hard_max",
            severity="block",
            field=field,
            value=value,
            expected={"min": hard_min, "max": hard_max},
            profile=profile,
            message=f"{field}={value:.4g} is implausibly high for profile '{profile}' (hard cap {hard_max:.4g}).",
        )]
    if value < soft_min:
        return [quality_flag(
            code=f"{field}_below_soft_min",
            severity="warn",
            field=field,
            value=value,
            expected={"min": soft_min, "max": soft_max},
            profile=profile,
            message=f"{field}={value:.4g} is below typical range {soft_min:.4g}-{soft_max:.4g} for profile '{profile}'.",
        )]
    if value > soft_max:
        return [quality_flag(
            code=f"{field}_above_soft_max",
            severity="warn",
            field=field,
            value=value,
            expected={"min": soft_min, "max": soft_max},
            profile=profile,
            message=f"{field}={value:.4g} is above typical range {soft_min:.4g}-{soft_max:.4g} for profile '{profile}'.",
        )]
    return []


def enforce_hard_bands(
    assumptions: dict[str, float],
    profile: str,
    *,
    fields: set[str] | None = None,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Clamp any assumption that violates a HARD band to the nearest bound.

    A hard-band violation is economically implausible for the sector profile and,
    left unclamped, produces a DEGENERATE valuation — e.g. an FCFF margin below
    the SBC drag yields negative FCFF → negative terminal value → negative share
    price (the AMZN -$7.64 bug). Detection alone (a ``block`` quality flag) only
    lowered confidence; the bad value still reached the math. This clamps it.

    Returns a NEW assumptions dict (never mutates the input) plus one ``warn``
    quality flag per clamp carrying ``clamped_from`` / ``clamped_to`` for report
    transparency. Pass ``fields`` to restrict the clamp (e.g. the margin fields
    at the valuation chokepoint).
    """
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    out = dict(assumptions)
    flags: list[dict[str, Any]] = []
    for field, band in bands.items():
        if fields is not None and field not in fields:
            continue
        if field not in out:
            continue
        try:
            value = float(out[field])
        except (TypeError, ValueError):
            continue
        hard_min = float(band.get("hard_min", float("-inf")))
        hard_max = float(band.get("hard_max", float("inf")))
        if value < hard_min:
            bound, kind = hard_min, "floor"
        elif value > hard_max:
            bound, kind = hard_max, "cap"
        else:
            continue
        out[field] = bound
        flag = quality_flag(
            code=f"{field}_clamped_to_hard_{kind}",
            severity="warn",
            field=field,
            value=bound,
            expected={"min": hard_min, "max": hard_max},
            profile=profile,
            message=(
                f"{field} proposed at {value:.4g} — implausible for profile "
                f"'{profile}'; CLAMPED to hard {kind} {bound:.4g}. Unclamped this "
                f"would yield a degenerate valuation."
            ),
        )
        flag["clamped_from"] = value
        flag["clamped_to"] = bound
        flags.append(flag)
    return out, flags


def check_assumption_plausibility(
    assumptions: dict[str, float],
    profile: str,
) -> list[dict[str, Any]]:
    """Run sector-aware plausibility checks on each assumption."""
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    flags: list[dict[str, Any]] = []
    for field, band in bands.items():
        if field in assumptions:
            flags.extend(
                check_against_band(
                    field=field,
                    value=float(assumptions[field]),
                    band=band,
                    profile=profile,
                )
            )
    return flags


def check_valuation_sanity(
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
            check_against_band(
                field="implied_to_spot_price_ratio",
                value=ratio,
                band=bands["price_ratio"],
                profile=profile,
            )
        )

    pv_cash = float(valuation.get("pv_cash_flows") or 0.0)
    ev = float(valuation.get("enterprise_value") or 0.0)
    terminal_pv_implied = ev - pv_cash
    if ev > 0 and terminal_pv_implied >= 0:
        tv_share = terminal_pv_implied / ev
        flags.extend(
            check_against_band(
                field="terminal_value_share_of_ev",
                value=tv_share,
                band=bands["tv_share_of_ev"],
                profile=profile,
            )
        )

    return flags


_FALLBACK_SOURCES: frozenset[str] = frozenset({
    "default", "profile_prior_mid", "profile_prior_fallback", "merge_unchanged", "unknown",
})


def _score_field(
    field: str,
    provenance: dict[str, dict[str, Any]],
    flags: list[dict[str, Any]],
) -> tuple[float, str]:
    """Score a single assumption field (0–1) from its provenance + flags."""
    prov = provenance.get(field) or {}
    base = float(prov.get("confidence") or 0.5)
    source = str(prov.get("source") or "unknown")
    field_flags = [f for f in flags if f.get("field") == field]

    for flag in field_flags:
        if flag.get("severity") == "block":
            base = min(base, 0.25)
        elif flag.get("severity") == "warn":
            base = max(0.0, base - 0.15)

    if source in _FALLBACK_SOURCES:
        base = min(base, 0.50)
        reason = "Profile prior fallback"
    elif source == "user_override":
        base = 1.0
        reason = "User override"
    elif source in ("llm_memo", "memo_proposal"):
        flag_note = f", {len(field_flags)} flag(s)" if field_flags else ""
        reason = f"LLM memo{flag_note} ({base:.0%})"
    elif source in ("fmp", "canonical", "structured_api", "fmp+fallback:yfinance", "canonical+fmp"):
        reason = f"Canonical source ({base:.0%})"
    elif source == "capm":
        reason = "CAPM estimate"
    else:
        reason = f"Source: {source}"

    return max(0.0, min(1.0, base)), reason


def compute_confidence_breakdown(
    *,
    assumption_flags: list[dict[str, Any]],
    valuation_flags: list[dict[str, Any]],
    provenance: dict[str, dict[str, Any]],
    assumption_memo: dict[str, Any] | None = None,
    model_validity: str | None = None,
    solver_failed: bool = False,
    unexplained_count: int = 0,
) -> dict[str, Any]:
    """Return per-component confidence scores + aggregate label.

    All scores derived from existing state data — no new LLM calls.
    Base components: data_quality, revenue_growth, margin_stability,
                     wacc_reliability, terminal_assumptions.

    Optional validity gate (Phase 1, post-convergence_gate recompute):
      - ``model_validity``: when "invalid", multiplies aggregate by 0.3 AND
        forces label "low" regardless of score. "adjusting" multiplies by 0.7.
        "valid" or None leaves aggregate untouched.
      - ``solver_failed``: caps ``wacc_reliability`` at 0.3 (implied WACC
        solver could not converge — market signal unreliable).
      - ``unexplained_count``: subtracts 0.05 per unresolved divergence from
        the aggregate, capped at 0.20 total. Shown as ``validity_penalty``
        component so the UI can explain WHY confidence dropped.
    """
    all_flags = list(assumption_flags) + list(valuation_flags)

    # ── data_quality: fraction of fields with canonical (non-fallback) sources ──
    total = len(provenance)
    fallback_n = sum(
        1 for meta in provenance.values()
        if isinstance(meta, dict) and str(meta.get("source") or "") in _FALLBACK_SOURCES
    )
    if total == 0:
        dq_score, dq_reason = 0.5, "No provenance data"
    else:
        dq_score = 1.0 - (fallback_n / total)
        if fallback_n == 0:
            dq_reason = "All fields from canonical/API sources"
        elif fallback_n == 1:
            dq_reason = "1 field on fallback value"
        else:
            dq_reason = f"{fallback_n}/{total} fields on fallback values"
    # Block on a Tier A field tanks data quality
    tier_a = {"base_revenue", "shares_outstanding", "net_debt"}
    if any(f.get("field") in tier_a and f.get("severity") == "block" for f in assumption_flags):
        dq_score = min(dq_score, 0.30)
        dq_reason = "Tier A field block flag"

    # ── growth / margin / terminal: from memo provenance + flags ──
    rev_score, rev_reason = _score_field("revenue_growth", provenance, all_flags)
    mar_score, mar_reason = _score_field("fcff_margin", provenance, all_flags)
    tg_score, tg_reason = _score_field("terminal_growth", provenance, all_flags)

    # ── wacc_reliability ──
    wacc_prov = provenance.get("wacc") or {}
    wacc_source = str(wacc_prov.get("source") or "unknown")
    wacc_score = float(wacc_prov.get("confidence") or 0.5)
    wacc_flags = [f for f in all_flags if f.get("field") == "wacc"]
    for flag in wacc_flags:
        if flag.get("severity") == "block":
            wacc_score = min(wacc_score, 0.25)
        elif flag.get("severity") == "warn":
            wacc_score = max(0.0, wacc_score - 0.20)
    if wacc_source == "user_override":
        wacc_score, wacc_reason = 1.0, "User override"
    elif wacc_source == "capm" and not wacc_flags:
        wacc_reason = f"Full CAPM ({wacc_score:.0%})"
    elif wacc_source == "capm":
        wacc_reason = f"CAPM, {len(wacc_flags)} flag(s)"
    elif wacc_source in _FALLBACK_SOURCES:
        wacc_score = min(wacc_score, 0.55)
        wacc_reason = "Profile prior (CAPM inputs missing)"
    else:
        wacc_reason = f"Source: {wacc_source}"

    # Solver failure caps WACC reliability — market-implied WACC could not
    # be verified against the CAPM estimate.
    if solver_failed and wacc_score > 0.30:
        wacc_score = 0.30
        wacc_reason = f"{wacc_reason}; implied-WACC solver failed"

    # ── aggregate (weighted) ──
    weights = {
        "data_quality": 0.20,
        "revenue_growth": 0.20,
        "margin_stability": 0.20,
        "wacc_reliability": 0.25,
        "terminal_assumptions": 0.15,
    }
    raw_scores: dict[str, float] = {
        "data_quality": dq_score,
        "revenue_growth": rev_score,
        "margin_stability": mar_score,
        "wacc_reliability": wacc_score,
        "terminal_assumptions": tg_score,
    }
    aggregate = sum(raw_scores[k] * weights[k] for k in weights)

    # ── Validity gate (Phase 1) ──────────────────────────────────────────
    # Order: validity multiplier → unexplained-divergence penalty → block
    # flag floor → invalid-model hard cap. Track penalty contributions so
    # the UI can show WHY confidence dropped.
    validity_penalty_parts: list[str] = []
    validity_score = 1.0
    if model_validity == "invalid":
        aggregate *= 0.30
        validity_score = 0.30
        validity_penalty_parts.append("model invalid (×0.30)")
    elif model_validity == "adjusting":
        aggregate *= 0.70
        validity_score = 0.70
        validity_penalty_parts.append("model still adjusting (×0.70)")
    if unexplained_count > 0:
        unexplained_pen = min(0.20, 0.05 * unexplained_count)
        aggregate = max(0.0, aggregate - unexplained_pen)
        validity_score = max(0.0, validity_score - unexplained_pen)
        validity_penalty_parts.append(
            f"{unexplained_count} unexplained divergence(s) (−{unexplained_pen:.2f})"
        )
    if solver_failed and "implied-WACC solver failed" not in (wacc_reason or ""):
        validity_penalty_parts.append("solver failed")

    label = "high" if aggregate >= 0.70 else "medium" if aggregate >= 0.50 else "low"
    # Any block flag forces low
    if any(f.get("severity") == "block" for f in all_flags):
        label = "low"
        aggregate = min(aggregate, 0.40)
    # Invalid model hard-caps label at "low" regardless of base score
    if model_validity == "invalid":
        label = "low"

    reasons: dict[str, str] = {
        "data_quality": dq_reason,
        "revenue_growth": rev_reason,
        "margin_stability": mar_reason,
        "wacc_reliability": wacc_reason,
        "terminal_assumptions": tg_reason,
    }
    _label_from_score = lambda s: "high" if s >= 0.70 else "medium" if s >= 0.50 else "low"
    components = {
        k: {"score": round(raw_scores[k], 3), "label": _label_from_score(raw_scores[k]), "reason": reasons[k]}
        for k in raw_scores
    }
    # Surface the validity gate as a pseudo-component so the UI explains the
    # difference between the base score and the gated aggregate.
    if validity_penalty_parts or model_validity in {"invalid", "adjusting"} or solver_failed:
        components["validity_penalty"] = {
            "score": round(validity_score, 3),
            "label": _label_from_score(validity_score),
            "reason": "; ".join(validity_penalty_parts) or "ok",
        }

    _weak_names = {
        "data_quality": "data quality", "revenue_growth": "revenue growth",
        "margin_stability": "margin stability", "wacc_reliability": "WACC",
        "terminal_assumptions": "terminal growth",
    }
    weak = [k for k, v in raw_scores.items() if v < 0.55]
    if not weak:
        summary = "All assumption components adequately supported by available evidence."
    else:
        verb = "carry" if len(weak) > 1 else "carries"
        summary = f"{', '.join(_weak_names[k] for k in weak)} {verb} meaningful uncertainty."
    # Prepend validity issues to the summary so the headline reason is the
    # validity gate, not just weak components.
    if model_validity == "invalid":
        summary = f"Model marked invalid — {summary.lower()}"
    elif validity_penalty_parts and model_validity != "valid":
        summary = f"{' / '.join(validity_penalty_parts)}. {summary}"

    return {
        "components": components,
        "aggregate_score": round(aggregate, 3),
        "label": label,
        "summary": summary,
    }


def compute_confidence_label(
    *,
    assumption_flags: list[dict[str, Any]],
    valuation_flags: list[dict[str, Any]],
    provenance: dict[str, dict[str, Any]],
) -> str:
    """Return aggregate confidence label. Thin wrapper over compute_confidence_breakdown."""
    return compute_confidence_breakdown(
        assumption_flags=assumption_flags,
        valuation_flags=valuation_flags,
        provenance=provenance,
    )["label"]

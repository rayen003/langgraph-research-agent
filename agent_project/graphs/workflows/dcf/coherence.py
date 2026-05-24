"""Pre-valuation assumption coherence gate.

Detects internal contradictions between the operating bundle (growth, margin,
buybacks) and the discount-rate assumption. When the bundle says
"durable compounder" but the WACC says "distressed cyclical", we either
auto-pull WACC into the profile band or surface a coherence flag for the
report.

This is general by construction: it operates on profile + observable
assumptions only. No ticker-specific logic.

Pipeline order::

    review_assumptions (HITL) → scenario_runner → project_cashflows
                                              ↑
                                    coherence_gate inserted here

The gate runs before every fan-out into projection so adjustments stay
consistent across base + scenarios.
"""

from __future__ import annotations

import logging
from typing import Any

from .activity import emit_step
from .priors import PROFILE_PRIORS
from .state import clip_to_field_range
from .wacc import _profile_wacc_band, append_wacc_stack_delta, clip_wacc_to_profile_band

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier classifiers
# ---------------------------------------------------------------------------


def _band_midpoint(band: dict[str, float]) -> float:
    return (float(band.get("soft_min", 0.0)) + float(band.get("soft_max", 0.0))) / 2.0


def _ops_tier(profile: str, assumptions: dict[str, float]) -> tuple[str, list[str]]:
    """Classify the operating bundle as strong / neutral / weak.

    "Strong" means the model is asserting a durable compounder profile:
    growth above midpoint, margin above midpoint, and meaningful capital
    returns. "Weak" means the inverse (sub-midpoint growth and margin,
    no buybacks).
    """
    bands = PROFILE_PRIORS.get(profile) or PROFILE_PRIORS["default"]
    growth = assumptions.get("revenue_growth")
    margin = assumptions.get("fcff_margin")
    buyback = float(assumptions.get("buyback_yield") or 0.0)

    score = 0
    rationale: list[str] = []

    growth_band = bands.get("revenue_growth") or {}
    if growth is not None and growth_band:
        gmid = _band_midpoint(growth_band)
        if float(growth) > gmid:
            score += 1
            rationale.append(f"growth {float(growth):.1%} above midpoint {gmid:.1%}")
        elif float(growth) < gmid - 0.02:
            score -= 1
            rationale.append(f"growth {float(growth):.1%} below midpoint {gmid:.1%}")

    margin_band = bands.get("fcff_margin") or {}
    if margin is not None and margin_band:
        mmid = _band_midpoint(margin_band)
        if float(margin) > mmid:
            score += 1
            rationale.append(f"margin {float(margin):.1%} above midpoint {mmid:.1%}")
        elif float(margin) < mmid - 0.02:
            score -= 1
            rationale.append(f"margin {float(margin):.1%} below midpoint {mmid:.1%}")

    if buyback >= 0.01:
        score += 1
        rationale.append(f"buyback yield {buyback:.1%}")
    elif buyback <= -0.01:
        score -= 1
        rationale.append(f"net dilution {buyback:.1%}")

    if score >= 2:
        tier = "strong"
    elif score <= -2:
        tier = "weak"
    else:
        tier = "neutral"
    return tier, rationale


def _wacc_tier(profile: str, wacc: float | None) -> str:
    """Position WACC within the profile soft band: low / mid / high."""
    if wacc is None:
        return "unknown"
    band = _profile_wacc_band(profile)
    soft_min = band["soft_min"]
    soft_max = band["soft_max"]
    if wacc > soft_max:
        return "above_band"
    if wacc < soft_min:
        return "below_band"
    midpoint = (soft_min + soft_max) / 2.0
    return "high" if wacc > midpoint else "low"


# ---------------------------------------------------------------------------
# Public assessment + node
# ---------------------------------------------------------------------------


def assess_assumption_coherence(
    *,
    profile: str,
    assumptions: dict[str, float],
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect ops / discount-rate mismatches.

    Returns a dict with ``status`` (``ok`` | ``mismatch``), tier labels,
    explanatory flags, and (when applicable) a deterministic suggested
    WACC adjustment that pulls the value into the profile band midpoint.
    """
    ops_tier, ops_rationale = _ops_tier(profile, assumptions)
    wacc = assumptions.get("wacc")
    band = _profile_wacc_band(profile)
    wacc_tier = _wacc_tier(profile, float(wacc) if wacc is not None else None)

    flags: list[dict[str, Any]] = []
    suggested: dict[str, float] = {}
    status = "ok"

    if ops_tier == "strong" and wacc_tier in {"high", "above_band"}:
        status = "mismatch"
        target = band["midpoint"]
        if wacc is not None:
            suggested["wacc"] = round(target - float(wacc), 4)
        flags.append({
            "code": "ops_strong_wacc_high",
            "severity": "warn",
            "field": "wacc",
            "message": (
                f"Strong operating bundle ({'; '.join(ops_rationale)}) is incompatible with "
                f"WACC {wacc:.2%} for profile '{profile}'. "
                f"Either WACC should sit closer to the profile midpoint ({band['midpoint']:.2%}) "
                f"or operating assumptions should compress."
            ),
        })
    elif ops_tier == "weak" and wacc_tier in {"low", "below_band"}:
        status = "mismatch"
        target = band["midpoint"]
        if wacc is not None:
            suggested["wacc"] = round(target - float(wacc), 4)
        flags.append({
            "code": "ops_weak_wacc_low",
            "severity": "warn",
            "field": "wacc",
            "message": (
                f"Weak operating bundle ({'; '.join(ops_rationale)}) combined with low "
                f"WACC {wacc:.2%} for profile '{profile}'. "
                f"Either WACC should rise toward the midpoint ({band['midpoint']:.2%}) "
                f"or operating assumptions should improve."
            ),
        })

    return {
        "status": status,
        "ops_tier": ops_tier,
        "wacc_tier": wacc_tier,
        "ops_rationale": ops_rationale,
        "profile_band": band,
        "flags": flags,
        "suggested_adjustments": suggested,
    }


def _is_user_pinned_wacc(provenance: dict[str, dict[str, Any]], field: str = "wacc") -> bool:
    wacc_prov = provenance.get(field) or {}
    return (
        wacc_prov.get("source") in {"user_override", "user_provided", "user_edited"}
        or bool(wacc_prov.get("user_edited"))
    )


def coherence_gate_node(state: dict) -> dict:
    """Pre-valuation coherence check + opt-in WACC auto-correction.

    The gate runs before ``project_cashflows``. When ops/WACC mismatch and the
    WACC value is not user-pinned, it pulls WACC toward the profile midpoint
    (still within the soft band) so the valuation engine sees an internally
    consistent bundle. Every change is logged to provenance.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("coherence_gate", "start", parent_step_id)

    profile = state.get("profile") or "default"
    assumptions = dict(state.get("assumptions") or {})
    provenance = dict(state.get("assumption_provenance") or {})
    features = state.get("features") or {}
    wacc_components = dict(state.get("wacc_components") or {})

    enforce_applied: dict[str, dict[str, float]] = {}
    is_user_wacc = _is_user_pinned_wacc(provenance)
    if assumptions.get("wacc") is not None and not is_user_wacc:
        old_wacc = float(assumptions["wacc"])
        clipped, was_clipped = clip_wacc_to_profile_band(
            old_wacc,
            profile=profile,
            allow_override=False,
        )
        if was_clipped:
            new_wacc = float(round(clipped, 6))
            assumptions["wacc"] = new_wacc
            enforce_applied["wacc"] = {
                "old": old_wacc,
                "new": new_wacc,
                "delta": float(round(new_wacc - old_wacc, 6)),
                "reason": f"profile_band_enforcement ({profile})",
            }
            wacc_components = append_wacc_stack_delta(
                wacc_components,
                old_wacc=old_wacc,
                new_wacc=new_wacc,
                label=f"Profile band enforcement ({profile})",
                source="coherence_band_enforcement",
            )
            merged_prov = dict(provenance.get("wacc") or {})
            merged_prov["band_enforced"] = True
            provenance["wacc"] = merged_prov

    assessment = assess_assumption_coherence(
        profile=profile,
        assumptions=assumptions,
        features=features,
    )

    suggested = assessment.get("suggested_adjustments") or {}

    applied: dict[str, dict[str, float]] = dict(enforce_applied)
    if "wacc" in suggested and not is_user_wacc and assumptions.get("wacc") is not None:
        delta = float(suggested["wacc"])
        old_value = float(assumptions["wacc"])
        candidate = round(old_value + delta, 4)
        within_field_range = clip_to_field_range("wacc", candidate)
        if within_field_range is not None:
            new_value, _ = clip_wacc_to_profile_band(
                within_field_range,
                profile=profile,
                allow_override=False,
            )
            if abs(new_value - old_value) > 1e-6:
                assumptions["wacc"] = float(round(new_value, 6))
                applied["wacc"] = {
                    "old": old_value,
                    "new": float(round(new_value, 6)),
                    "delta": float(round(new_value - old_value, 6)),
                    "reason": (
                        f"coherence_gate({assessment['ops_tier']}↔{assessment['wacc_tier']}): "
                        "pulled WACC toward profile midpoint"
                    ),
                }
                wacc_components = append_wacc_stack_delta(
                    wacc_components,
                    old_wacc=old_value,
                    new_wacc=float(round(new_value, 6)),
                    label=(
                        f"Coherence gate ({assessment['ops_tier']} ops, "
                        f"{assessment['wacc_tier']} WACC)"
                    ),
                    source="coherence_gate",
                )
                merged_prov = dict(provenance.get("wacc") or {})
                merged_prov["coherence_adjusted"] = True
                merged_prov["coherence_delta"] = applied["wacc"]["delta"]
                existing_evidence = str(merged_prov.get("evidence") or "")
                merged_prov["evidence"] = (
                    existing_evidence
                    + (" | " if existing_evidence else "")
                    + f"coherence-gate adjustment {applied['wacc']['delta']:+.2%} "
                    f"(ops={assessment['ops_tier']}, wacc_tier={assessment['wacc_tier']})"
                )
                provenance["wacc"] = merged_prov

    summary_line = (
        f"ops={assessment['ops_tier']} wacc={assessment['wacc_tier']} "
        f"status={assessment['status']} adjusted={list(applied.keys()) or 'none'}"
    )
    emit_step(
        "coherence_gate", "complete", parent_step_id,
        {
            "summary_line": summary_line,
            "ops_tier": assessment["ops_tier"],
            "wacc_tier": assessment["wacc_tier"],
            "status": assessment["status"],
            "flags": assessment["flags"],
            "applied": applied,
            "ops_rationale": assessment["ops_rationale"],
            "profile_band": assessment["profile_band"],
        },
    )
    logger.info("DCF coherence_gate %s", summary_line)

    out: dict[str, Any] = {
        "assumptions": assumptions,
        "assumption_provenance": provenance,
        "coherence_assessment": assessment,
        "wacc_components": wacc_components,
    }
    if applied:
        out["coherence_adjustments"] = applied
    return out

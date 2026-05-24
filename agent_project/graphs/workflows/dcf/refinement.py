"""Refinement layer — deterministic flags, self-critique, bounded adjustments."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .activity import emit_step
from .state import DCFState
from .wacc import clip_wacc_to_profile_band, append_wacc_stack_delta

logger = logging.getLogger(__name__)


_ANALYSIS_LLM = ChatOpenAI(
    model=os.getenv("DCF_ANALYSIS_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=60,
)

_CRITIQUE_ADJUSTMENT_BOUNDS: dict[str, tuple[float, float]] = {
    "revenue_growth": (-0.03, 0.03),
    "fcff_margin": (-0.03, 0.03),
    "terminal_growth": (-0.005, 0.005),
    "wacc": (-0.01, 0.01),
    "tax_rate": (-0.02, 0.02),
}

_MAX_ANALYSIS_ITERATIONS = 2


class AnalysisCritique(BaseModel):
    """Structured critique from the analysis node."""
    interpretation: str = Field(description="Why these flags matter — 1-2 sentences.")
    suggested_adjustments: dict[str, float] = Field(
        description="Adjustments to apply. Keys: revenue_growth, fcff_margin, terminal_growth, wacc, tax_rate. Values: absolute deltas."
    )


def _build_deterministic_flags(state: DCFState) -> list[dict[str, Any]]:
    """Extract structured signals from valuation output for the critique node."""
    valuation = state.get("valuation") or {}
    wacc_sanity = state.get("wacc_sanity") or {}
    assumptions = state.get("assumptions") or {}
    confidence = state.get("confidence_breakdown") or {}
    sensitivity = state.get("sensitivity_table") or []

    flags: list[dict[str, Any]] = []

    tv = valuation.get("terminal_pv", 0) or 0
    ev = valuation.get("enterprise_value", 1) or 1
    tv_pct = tv / ev if ev else 0
    flags.append({
        "signal": "terminal_weight",
        "value": round(tv_pct * 100, 1),
        "threshold": "> 70% is concerning",
        "severity": "severe" if tv_pct > 0.75 else ("warning" if tv_pct > 0.70 else "ok"),
    })

    implied = valuation.get("implied_share_price", 0) or 0
    spot = valuation.get("current_price", 1) or 1
    gap_pct = round(((implied / spot) - 1) * 100, 1) if spot else 0
    flags.append({
        "signal": "implied_vs_spot",
        "value": gap_pct,
        "threshold": "> ±50% is extreme",
        "severity": "severe" if abs(gap_pct) > 50 else ("warning" if abs(gap_pct) > 30 else "ok"),
    })

    wacc_gap_bps = wacc_sanity.get("gap_bps", 0) or 0
    flags.append({
        "signal": "wacc_sanity_gap",
        "value_bps": wacc_gap_bps,
        "threshold": "> 200 bps is concerning",
        "severity": "severe" if abs(wacc_gap_bps) > 200 else ("warning" if abs(wacc_gap_bps) > 100 else "ok"),
    })

    if sensitivity:
        prices = [r.get("implied_share_price", 0) for r in sensitivity if r.get("implied_share_price")]
        if len(prices) >= 3:
            price_range_pct = round(((max(prices) - min(prices)) / (prices[len(prices)//2] or 1)) * 100, 1)
            flags.append({
                "signal": "wacc_sensitivity",
                "value_pct": price_range_pct,
                "threshold": "> 30% swing is high sensitivity",
                "severity": "warning" if price_range_pct > 30 else "ok",
            })

    flags.append({
        "signal": "confidence",
        "value": confidence.get("label", state.get("confidence_label", "medium")),
        "severity": "severe" if state.get("confidence_label") == "low" else ("warning" if state.get("confidence_label") == "medium" else "ok"),
    })

    tg = assumptions.get("terminal_growth")
    if tg is not None:
        flags.append({
            "signal": "tgr_vs_rf",
            "value": round(tg * 100, 2),
            "threshold": "TGR should not exceed Rf by more than 50 bps",
            "severity": "warning",
        })

    return flags


def analyze_result_node(state: DCFState) -> dict:
    """Self-critique node — the analyst reads the DCF output and challenges it.

    Uses deterministic flags as structured input; the LLM interprets why
    they matter and suggests bounded adjustments. Never free-form critique.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    iteration = state.get("analysis_iteration", 0)
    ticker = state["ticker"]
    emit_step("analyze_result", "start", parent_step_id)
    logger.info("DCF analyze_result node RUNNING ticker=%s iteration=%d", ticker, iteration)
    thesis = state.get("thesis") or {}
    valuation = state.get("valuation") or {}
    assumptions = state.get("assumptions") or {}

    flags = _build_deterministic_flags(state)
    severe_count = sum(1 for f in flags if f.get("severity") == "severe")
    warning_count = sum(1 for f in flags if f.get("severity") == "warning")

    prev_val = state.get("previous_valuation") or {}
    prev_implied = prev_val.get("implied_share_price", 0)
    curr_implied = valuation.get("implied_share_price", 0)
    delta_pct = abs((curr_implied - prev_implied) / max(prev_implied, 1)) * 100 if prev_implied and curr_implied else float("inf")
    converged = delta_pct < 5 and prev_implied > 0

    should_refine = (
        severe_count > 0
        and iteration < _MAX_ANALYSIS_ITERATIONS
        and not converged
    )

    critique: dict[str, Any] = {
        "iteration": iteration,
        "flags": flags,
        "severe_count": severe_count,
        "warning_count": warning_count,
        "converged": converged,
        "delta_pct": round(delta_pct, 1),
        "should_refine": should_refine,
    }

    if not should_refine:
        reason = (
            "Reconciliation stable across iterations (Δ<5%)" if converged
            else f"No severe quality flags ({severe_count} severe, {warning_count} warnings)"
            if severe_count == 0
            else (
                f"Further iterations unlikely to materially reduce reconciliation gaps "
                f"(reviewed {iteration}/{_MAX_ANALYSIS_ITERATIONS} passes)."
            )
        )
        critique["stop_reason"] = reason
        emit_step(
            "analyze_result", "complete", parent_step_id,
            {
                "summary_line": f"Analysis: {reason}",
                "severe_count": severe_count,
                "warning_count": warning_count,
                "should_refine": False,
                "stop_reason": reason,
                "flags": [{"signal": f["signal"], "severity": f["severity"], "value": f.get("value", f.get("value_bps", f.get("value_pct")))} for f in flags],
            },
        )
        return {"critique": critique, "analysis_iteration": iteration + 1}

    severe_desc = "\n".join(
        f"- {f['signal']}: {f.get('value', f.get('value_bps', f.get('value_pct', '?')))} (threshold: {f.get('threshold', 'N/A')})"
        for f in flags if f.get("severity") == "severe"
    )

    prompt = (
        f"You are a senior analyst reviewing a junior's DCF valuation for {ticker}.\n\n"
        f"## Thesis\n{json.dumps(thesis, ensure_ascii=False)}\n\n"
        f"## Current assumptions\n{json.dumps(assumptions, ensure_ascii=False)}\n\n"
        f"## Valuation output\n{json.dumps({k: v for k, v in valuation.items() if k in ('implied_share_price', 'enterprise_value', 'terminal_pv', 'pv_cash_flows')}, ensure_ascii=False)}\n\n"
        f"## Severe flags (these MUST be addressed)\n{severe_desc}\n\n"
        "## Instructions\n"
        "You identified these severe issues.  Suggest BOUNDED adjustments to fix them.\n"
        f"Growth can shift ±2-3%, margin ±2-3%, terminal growth ±0.5%, WACC ±1%.\n\n"
        "Output valid JSON ONLY:\n"
        "{\n"
        '  "interpretation": "Why these flags matter — 1-2 sentences.",\n'
        '  "suggested_adjustments": {\n'
        '    "revenue_growth": 0.02,   // absolute adjustment, NOT new value\n'
        '    "fcff_margin": -0.01,\n'
        '    "terminal_growth": -0.003,\n'
        '    "wacc": 0.005\n'
        "  }\n"
        "}\n\n"
        "Only include fields you actually want to adjust. Leave others out.\n"
        "If the thesis contradicts the assumptions, flag that in interpretation."
    )

    try:
        structured = _ANALYSIS_LLM.with_structured_output(AnalysisCritique)
        analysis_obj = structured.invoke(prompt)
        analysis = analysis_obj.model_dump() if isinstance(analysis_obj, AnalysisCritique) else {}
    except Exception:
        logger.warning("Analysis LLM failed for %s — using flag-only critique", ticker)
        analysis = {
            "interpretation": f"{severe_count} severe flags detected in {ticker} valuation.",
            "suggested_adjustments": {},
        }

    critique.update(analysis)

    suggested = critique.get("suggested_adjustments") or {}
    clamped: dict[str, float] = {}
    for field, delta in suggested.items():
        bounds = _CRITIQUE_ADJUSTMENT_BOUNDS.get(field)
        if bounds is None:
            continue
        low, high = bounds
        clamped[field] = max(low, min(high, float(delta)))
    critique["suggested_adjustments"] = clamped
    critique["adjustments_clamped"] = clamped != suggested

    emit_step(
        "analyze_result", "complete", parent_step_id,
        {
            "summary_line": f"{severe_count} severe, {warning_count} warnings → {'refine' if should_refine else 'done'}",
            "severe_count": severe_count,
            "warning_count": warning_count,
            "should_refine": should_refine,
            "stop_reason": critique.get("stop_reason", ""),
            "interpretation": critique.get("interpretation", ""),
            "flags": [{"signal": f["signal"], "severity": f["severity"], "value": f.get("value", f.get("value_bps", f.get("value_pct")))} for f in flags],
        },
    )
    return {
        "critique": critique,
        "analysis_iteration": iteration + 1,
        "previous_valuation": {
            "implied_share_price": valuation.get("implied_share_price"),
            "enterprise_value": valuation.get("enterprise_value"),
        },
    }


def refine_assumptions_node(state: DCFState) -> dict:
    """Apply bounded adjustments from the critique and re-enter valuation."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("refine_assumptions", "start", parent_step_id)

    critique = state.get("critique") or {}
    adjustments = dict(critique.get("suggested_adjustments") or {})
    assumptions = dict(state.get("assumptions") or {})

    if not adjustments:
        flags = critique.get("flags") or []
        for f in flags:
            if f.get("severity") != "severe":
                continue
            signal = f.get("signal", "")
            if signal == "terminal_weight" and "terminal_growth" in assumptions:
                adjustments["terminal_growth"] = -0.003
            elif signal == "wacc_sanity_gap" and "wacc" in assumptions:
                gap_bps = f.get("value_bps", 0)
                direction = -0.01 if gap_bps > 0 else 0.01
                adjustments["wacc"] = round(direction, 4)

    profile = state.get("profile") or "default"
    provenance = state.get("assumption_provenance") or {}
    wacc_components = dict(state.get("wacc_components") or {})
    wacc_prov_source = (provenance.get("wacc") or {}).get("source")
    user_wacc = wacc_prov_source in {"user_override", "user_provided", "user_edited"}

    changes: list[str] = []
    for field, delta in adjustments.items():
        if field in assumptions:
            old = assumptions[field]
            new_value = round(old + delta, 4)
            if field == "wacc":
                clipped, was_clipped = clip_wacc_to_profile_band(
                    new_value,
                    profile=profile,
                    allow_override=user_wacc,
                )
                if was_clipped:
                    new_value = round(clipped, 4)
                wacc_components = append_wacc_stack_delta(
                    wacc_components,
                    old_wacc=float(old),
                    new_wacc=float(new_value),
                    label="Analysis refinement adjustment",
                    source="refinement",
                )
            assumptions[field] = new_value
            changes.append(f"{field}: {old:.4f} → {assumptions[field]:.4f}")

    interpretation = critique.get("interpretation", "No interpretation provided.")

    emit_step(
        "refine_assumptions", "complete", parent_step_id,
        {
            "summary_line": f"Refined: {', '.join(changes) if changes else 'no changes'}",
            "changes": changes,
            "adjustments_applied": dict(adjustments),
            "interpretation": interpretation[:200],
            "had_changes": len(changes) > 0,
        },
    )
    return {"assumptions": assumptions, "wacc_components": wacc_components}


def route_after_analysis(state: DCFState) -> str:
    """Route to refinement or finalization based on critique."""
    critique = state.get("critique") or {}
    should = critique.get("should_refine")
    dest = "refine_assumptions" if should else "finalize"
    logger.info("DCF route_after_analysis should_refine=%s → %s", should, dest)
    return dest

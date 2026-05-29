"""Expectations-first block builders for the DCF adapter.

The legacy ``DcfOutputAdapter`` emits descriptive blocks (metric, scenarios,
risks, valuation table). These helpers add a parallel set of analytical
blocks that frame the deck around the **model vs market** reconciliation:

  - ``expectations_table`` — reconciliation of model vs market-implied inputs
  - ``three_box``          — "priced / assumed / required" exec summary
  - ``debate``             — bull vs bear two-column narrative
  - ``capital_flow``       — per-share growth derivation chain
  - ``variable_impact``    — multi-variable Δ sensitivity (linearized)
  - ``decision``           — "what must happen for upside" closing block

Each builder accepts the raw ``dcf_output`` payload + a source_ref and
returns ``None`` when the required upstream data is missing, so the
adapter can append only the blocks it can actually fill. Per-slide LLM
calls write the framing prose; these helpers ship structured raw inputs.
"""

from __future__ import annotations

import logging
from typing import Any

from ..state import NormalizedBlock
from .base import make_block_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _pct(v: float | int | None, places: int = 1) -> str | None:
    if v is None:
        return None
    try:
        return f"{float(v) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _consolidated_market_signals(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a single consolidated market-signals view.

    Prefers the enriched ``market_signals_meta`` dict written by
    ``compute_market_signals_node`` (new runs). Falls back to synthesizing
    from ``wacc_sanity`` + ``implied_growth`` + ``divergences`` for payloads
    produced before the enrichment was added.

    Returns a dict with keys: ``implied_wacc``, ``model_wacc``, ``wacc_gap_bps``,
    ``implied_growth``, ``growth_gap_pp``, ``plausibility_label``, etc.
    Missing fields stay None; caller decides whether the block is worth emitting.
    """
    msm = payload.get("market_signals_meta") or {}
    # Rich market_signals_meta: produced by enriched compute_market_signals_node.
    # Detect by presence of model_wacc (old version only had wacc_binding).
    if msm.get("model_wacc") is not None or msm.get("implied_wacc") is not None:
        wacc_sanity = payload.get("wacc_sanity") or {}
        return {
            "implied_wacc": _safe_float(msm.get("implied_wacc")),
            "model_wacc": _safe_float(msm.get("model_wacc")),
            "wacc_gap_bps": _safe_float(msm.get("wacc_gap_bps")),
            "wacc_direction": (payload.get("wacc_sanity") or {}).get("direction"),
            "wacc_flag": (payload.get("wacc_sanity") or {}).get("flag"),
            "implied_growth": _safe_float(msm.get("implied_growth")),
            "implied_margin": _safe_float(payload.get("implied_margin")),
            "growth_gap_pp": _safe_float(msm.get("growth_gap_pp")),
            "plausibility_label": msm.get("plausibility_label"),
            "plausibility_narrative": msm.get("plausibility_narrative"),
            "solver_status": msm.get("solver_status") or wacc_sanity.get("solver_status"),
        }

    # Legacy fallback — synthesize from raw fields (pre-enrichment payloads).
    wacc_sanity = payload.get("wacc_sanity") or {}
    plausibility = wacc_sanity.get("implied_plausibility") or {}
    divergences = payload.get("divergences") or []

    growth_gap_pp = None
    for d in divergences:
        if d.get("kind") == "growth_vs_implied":
            details = d.get("details") or {}
            gap = details.get("gap_pct")
            if gap is not None:
                growth_gap_pp = float(gap) * 100  # gap_pct stored as decimal

    return {
        "implied_wacc": _safe_float(wacc_sanity.get("implied_wacc")),
        "model_wacc": _safe_float(wacc_sanity.get("capm_wacc")),
        "wacc_gap_bps": _safe_float(wacc_sanity.get("gap_bps")),
        "wacc_direction": wacc_sanity.get("direction"),
        "wacc_flag": wacc_sanity.get("flag"),
        "implied_growth": _safe_float(payload.get("implied_growth")),
        "implied_margin": _safe_float(payload.get("implied_margin")),
        "growth_gap_pp": growth_gap_pp,
        "plausibility_label": plausibility.get("label"),
        "plausibility_narrative": plausibility.get("narrative"),
        "solver_status": wacc_sanity.get("solver_status"),
    }


# ---------------------------------------------------------------------------
# Builder 1 — Expectations reconciliation table
# ---------------------------------------------------------------------------


def build_expectations_table(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Model vs market-implied reconciliation table — the core insight block.

    Skipped when neither implied WACC nor implied growth is available — the
    reconciliation has nothing to anchor against.
    """
    signals = _consolidated_market_signals(payload)
    if signals.get("implied_wacc") is None and signals.get("implied_growth") is None:
        return None

    assumptions = payload.get("assumptions") or {}
    valuation = payload.get("valuation") or {}
    model_wacc = signals.get("model_wacc") or _safe_float(assumptions.get("wacc"))
    model_growth = _safe_float(assumptions.get("revenue_growth"))
    model_terminal = _safe_float(assumptions.get("terminal_growth"))
    buyback = _safe_float(assumptions.get("buyback_yield"))
    effective_terminal = _safe_float(valuation.get("effective_terminal_growth"))

    rows: list[dict[str, Any]] = []
    if model_wacc is not None or signals.get("implied_wacc") is not None:
        rows.append({
            "metric": "Discount rate (WACC)",
            "model": _pct(model_wacc, 2),
            "market_implied": _pct(signals.get("implied_wacc"), 2),
            "delta_bps": signals.get("wacc_gap_bps"),
            "narrative_hint": signals.get("plausibility_narrative"),
        })
    if model_growth is not None or signals.get("implied_growth") is not None:
        rows.append({
            "metric": "Near-term revenue growth",
            "model": _pct(model_growth, 1),
            "market_implied": _pct(signals.get("implied_growth"), 1),
            "delta_pp": signals.get("growth_gap_pp"),
        })
    if model_terminal is not None or effective_terminal is not None:
        rows.append({
            "metric": "Effective terminal compounding",
            "model": _pct(model_terminal, 1),
            "effective_with_buybacks": _pct(effective_terminal, 1),
            "buyback_yield": _pct(buyback, 1),
        })

    sig = f"expectations:{signals.get('implied_wacc')}:{signals.get('implied_growth')}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=100,
            content_signature=sig,
        ),
        kind="expectations_table",
        title=f"{ticker} — Model vs Market-Implied",
        content={
            "rows": rows,
            "signals": signals,
            "summary_hint": (
                signals.get("plausibility_narrative")
                or payload.get("reconciliation_note")
                or ""
            ),
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["reconciliation_table"],
    )


# ---------------------------------------------------------------------------
# Builder 2 — Three-box exec summary (priced / assumed / required)
# ---------------------------------------------------------------------------


def build_three_box(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Three-column exec summary block — the heart of the institutional deck.

    Skipped when there are no divergences AND no implied data — without
    market context the "required" column has nothing to anchor.
    """
    signals = _consolidated_market_signals(payload)
    divergences = payload.get("divergences") or []
    critique = payload.get("critique") or {}
    reconciliation_note = payload.get("reconciliation_note") or ""
    if not divergences and signals.get("implied_wacc") is None and not reconciliation_note:
        return None

    assumptions = payload.get("assumptions") or {}
    valuation = payload.get("valuation") or {}
    company_state = payload.get("company_state") or {}

    priced_signals = {
        "implied_wacc": _pct(signals.get("implied_wacc"), 2),
        "implied_growth": _pct(signals.get("implied_growth"), 1),
        "wacc_plausibility": signals.get("plausibility_label"),
        "wacc_narrative": signals.get("plausibility_narrative"),
        "divergence_kinds": [d.get("kind") for d in divergences],
        "lifecycle_stage": company_state.get("lifecycle_stage"),
        "margin_trajectory": company_state.get("margin_trajectory"),
        "capital_return_policy": company_state.get("capital_return_policy"),
    }
    wacc_stack = (payload.get("wacc_components") or {}).get("wacc_stack") or {}
    wacc_stack_components = wacc_stack.get("components") or []
    assumed = {
        "revenue_growth_near": _pct(assumptions.get("revenue_growth"), 1),
        "revenue_growth_terminal": _pct(assumptions.get("revenue_growth_terminal"), 1),
        "fcff_margin_near": _pct(assumptions.get("fcff_margin"), 1),
        "fcff_margin_terminal": _pct(assumptions.get("fcff_margin_terminal"), 1),
        "terminal_growth": _pct(assumptions.get("terminal_growth"), 1),
        "buyback_yield": _pct(assumptions.get("buyback_yield"), 1),
        "model_wacc": _pct(assumptions.get("wacc"), 2),
        "tax_rate": _pct(assumptions.get("tax_rate"), 1),
        "implied_share_price": valuation.get("implied_share_price"),
        "current_price": valuation.get("current_price"),
        # WACC decomposition — gives the LLM the "why" behind the model WACC
        # (base CAPM minus durability/quality discounts). Used by three_box
        # framing and by the framework_advantage angle in later slides.
        "wacc_stack": {
            "base_capm": _pct(wacc_stack.get("base_capm"), 2),
            "quality_delta": _pct(wacc_stack.get("quality_delta"), 2),
            "final_wacc": _pct(wacc_stack.get("final_wacc"), 2),
            "clipped": wacc_stack.get("clipped"),
            "components": [
                {
                    "label": c.get("label"),
                    "delta_bps": (
                        round(float(c.get("delta", 0)) * 10000, 1)
                        if c.get("delta") is not None else None
                    ),
                }
                for c in wacc_stack_components
            ],
        } if wacc_stack else None,
    }
    required = {
        "divergences": [
            {
                "id": d.get("id"),
                "kind": d.get("kind"),
                "severity": d.get("severity"),
                "summary": d.get("summary"),
                "details": d.get("details"),
            }
            for d in divergences
        ],
        "reconciliation_status": payload.get("reconciliation_status"),
        "reconciliation_note": reconciliation_note,
        "critique_summary": critique.get("summary") if isinstance(critique, dict) else None,
        "critique_concerns": critique.get("concerns") if isinstance(critique, dict) else None,
    }
    sig = f"threebox:{len(divergences)}:{signals.get('implied_wacc')}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=101,
            content_signature=sig,
        ),
        kind="three_box",
        title=f"{ticker} — What is Priced, What We Assume, What Must Be True",
        content={
            "priced": priced_signals,
            "assumed": assumed,
            "required": required,
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["three_box", "executive_summary"],
    )


# ---------------------------------------------------------------------------
# Builder 3 — Bull vs Bear core debate
# ---------------------------------------------------------------------------


def build_debate(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Bull vs bear core debate block — sharper than "Investment Thesis"."""
    thesis = payload.get("thesis") or {}
    critique = payload.get("critique") or {}
    bull = (thesis.get("bull_thesis") or "").strip()
    bear = (thesis.get("bear_thesis") or "").strip()
    if not (bull or bear):
        return None
    sig = f"debate:{len(bull)}:{len(bear)}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=102,
            content_signature=sig,
        ),
        kind="debate",
        title=f"{ticker} — Core Debate",
        content={
            "bull": bull,
            "bear": bear,
            "key_drivers": thesis.get("key_drivers") or [],
            "narrative_context": thesis.get("narrative") or "",
            "adversarial_critique": critique if isinstance(critique, dict) else None,
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["two_col_narrative", "thesis"],
    )


# ---------------------------------------------------------------------------
# Builder 4 — Capital allocation flow
# ---------------------------------------------------------------------------


def build_capital_flow(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Per-share growth derivation: g_per_share ≈ g_business + buyback_yield.

    Skipped when buyback_yield is missing or zero — the slide adds no
    value without the buyback amplification story.
    """
    assumptions = payload.get("assumptions") or {}
    valuation = payload.get("valuation") or {}
    buyback = _safe_float(assumptions.get("buyback_yield"))
    if buyback is None or buyback <= 0:
        return None

    business_growth = _safe_float(assumptions.get("terminal_growth")) or 0.0
    effective_terminal = _safe_float(valuation.get("effective_terminal_growth"))
    shares_initial = _safe_float(valuation.get("shares_initial"))
    shares_end = _safe_float(valuation.get("shares_end"))
    perpetual_buyback = _safe_float(valuation.get("perpetual_buyback_yield"))

    share_shrinkage_pct = None
    if shares_initial and shares_end:
        share_shrinkage_pct = (1.0 - shares_end / shares_initial) * 100

    sig = f"capflow:{buyback}:{business_growth}:{effective_terminal}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=103,
            content_signature=sig,
        ),
        kind="capital_flow",
        title=f"{ticker} — Capital Allocation: Enterprise → Per-Share Growth",
        content={
            "business_terminal_growth": business_growth,
            "buyback_yield": buyback,
            "perpetual_buyback_yield": perpetual_buyback,
            "effective_per_share_terminal": effective_terminal,
            "shares_initial_m": shares_initial,
            "shares_end_m": shares_end,
            "shares_shrinkage_pct": share_shrinkage_pct,
            # Pre-formatted display values so the LLM doesn't have to re-format
            "display": {
                "business_growth": _pct(business_growth, 1),
                "buyback_yield": _pct(buyback, 1),
                "effective_per_share": _pct(effective_terminal, 1),
                "shares_shrinkage": (
                    f"{share_shrinkage_pct:.1f}%" if share_shrinkage_pct is not None else None
                ),
            },
            "derivation_hint": (
                "g_per_share ≈ g_business + buyback_yield. "
                "Steady buybacks amplify terminal compounding without raising "
                "the underlying business growth assumption."
            ),
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["flow_diagram"],
    )


# ---------------------------------------------------------------------------
# Builder 5 — Multi-variable Δ sensitivity (linearized)
# ---------------------------------------------------------------------------


def build_variable_impact(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Analytical linearized sensitivities on the value drivers that matter.

    Today the DCF only runs WACC × TGR. We approximate the impact of other
    variables (buyback persistence, growth duration, terminal margin) by
    perturbing one input and recomputing implied share price analytically
    from the existing payload's FCFF/terminal math. Linearized — caption
    must call this out.
    """
    assumptions = payload.get("assumptions") or {}
    valuation = payload.get("valuation") or {}
    sens_table = payload.get("sensitivity_table") or []

    wacc = _safe_float(assumptions.get("wacc"))
    terminal_growth = _safe_float(assumptions.get("terminal_growth"))
    fcff_margin_terminal = _safe_float(assumptions.get("fcff_margin_terminal"))
    base_price = _safe_float(valuation.get("implied_share_price"))
    if not all([wacc, terminal_growth, base_price]):
        return None

    rows: list[dict[str, Any]] = []

    # WACC -100bps — read directly from existing sensitivity grid for accuracy.
    if sens_table:
        target_wacc = wacc - 0.01
        target_tgr = terminal_growth
        match = min(
            sens_table,
            key=lambda r: (
                abs(_safe_float(r.get("wacc"), 0) - target_wacc)
                + abs(_safe_float(r.get("terminal_growth"), 0) - target_tgr)
            ),
        )
        target_price = _safe_float(match.get("implied_share_price"))
        if target_price:
            delta_pct = (target_price / base_price - 1.0) * 100
            rows.append({
                "variable": "WACC −100 bps",
                "delta_label": "−100 bps",
                "impact_pct": round(delta_pct, 1),
                "method": "exact (from sensitivity_table)",
                "rationale": "Lower discount rate raises PV of all future cash flows; effect amplified by terminal value share.",
            })

    # Buyback yield +1% — analytical approx via effective_terminal_growth shift.
    buyback = _safe_float(assumptions.get("buyback_yield"), 0.0) or 0.0
    spread = wacc - terminal_growth
    if spread and spread > 0.01:
        # Linear approx: terminal PV scales with 1 / (wacc - effective_g).
        # Adding 1pp to buyback raises effective_g by ~1pp.
        new_spread = max(spread - 0.01, 0.002)
        terminal_uplift_pct = (spread / new_spread - 1.0) * 100
        # Terminal share of equity value
        terminal_pv = _safe_float(valuation.get("terminal_pv"), 0.0) or 0.0
        equity_value = _safe_float(valuation.get("equity_value"), 1.0) or 1.0
        terminal_share = terminal_pv / equity_value if equity_value else 0.0
        impact_pct = terminal_uplift_pct * terminal_share
        rows.append({
            "variable": "Buyback yield +100 bps (sustained)",
            "delta_label": "+100 bps",
            "impact_pct": round(impact_pct, 1),
            "method": "linearized (1 / (wacc − g) approximation)",
            "rationale": "Persistent buybacks lift effective terminal compounding; impact scales with terminal-value share of equity.",
        })

    # Terminal margin +200 bps — linear scaling of terminal FCFF.
    if fcff_margin_terminal and fcff_margin_terminal > 0:
        margin_uplift_ratio = (fcff_margin_terminal + 0.02) / fcff_margin_terminal
        terminal_pv = _safe_float(valuation.get("terminal_pv"), 0.0) or 0.0
        equity_value = _safe_float(valuation.get("equity_value"), 1.0) or 1.0
        terminal_share = terminal_pv / equity_value if equity_value else 0.0
        impact_pct = (margin_uplift_ratio - 1.0) * 100 * terminal_share
        rows.append({
            "variable": "Terminal FCFF margin +200 bps",
            "delta_label": "+200 bps",
            "impact_pct": round(impact_pct, 1),
            "method": "linearized (terminal FCFF scales linearly)",
            "rationale": "Margin glide controls long-duration cash extraction; impact scales with terminal-value share.",
        })

    # Growth duration +3 years — proxy via terminal_pv share shift.
    # Approximation: extending the explicit forecast by 3yrs at near-term
    # growth shifts ~3 years of terminal-value into explicit period at the
    # higher near-term growth rate.
    near_growth = _safe_float(assumptions.get("revenue_growth"))
    if near_growth is not None and near_growth > terminal_growth:
        excess = near_growth - terminal_growth
        # 3 years of compounded excess on terminal-dominated portion
        terminal_pv = _safe_float(valuation.get("terminal_pv"), 0.0) or 0.0
        equity_value = _safe_float(valuation.get("equity_value"), 1.0) or 1.0
        terminal_share = terminal_pv / equity_value if equity_value else 0.0
        impact_pct = ((1 + excess) ** 3 - 1.0) * 100 * terminal_share
        rows.append({
            "variable": "High-growth duration +3 years",
            "delta_label": "+3 yrs",
            "impact_pct": round(impact_pct, 1),
            "method": "linearized (excess growth compounded over 3 yrs)",
            "rationale": "Each extra year at near-term growth pulls terminal value forward and compounds the gap vs steady-state.",
        })

    if not rows:
        return None

    sig = f"varimpact:{len(rows)}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=104,
            content_signature=sig,
        ),
        kind="variable_impact",
        title=f"{ticker} — Value Drivers That Actually Matter",
        content={
            "rows": rows,
            "base_price": base_price,
            "caveat": (
                "One-variable sensitivities vs. the base case. Treat magnitudes "
                "as directional — actual DCF responses are non-linear at the "
                "extremes."
            ),
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["variable_impact_table"],
    )


# ---------------------------------------------------------------------------
# Builder 6 — Decision summary
# ---------------------------------------------------------------------------


def build_decision(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> NormalizedBlock | None:
    """Closing decision block — "what must happen for upside from here".

    Skipped when there is neither a reconciliation note nor any
    divergences AND no critique to anchor the framing.
    """
    divergences = payload.get("divergences") or []
    reconciliation_note = (payload.get("reconciliation_note") or "").strip()
    critique = payload.get("critique") or {}
    reconciliation_status = payload.get("reconciliation_status")
    company_state = payload.get("company_state") or {}
    valuation = payload.get("valuation") or {}
    if not divergences and not reconciliation_note and not (isinstance(critique, dict) and critique):
        return None

    signals = _consolidated_market_signals(payload)
    sig = f"decision:{len(divergences)}:{reconciliation_status}"
    return NormalizedBlock(
        block_id=make_block_id(
            source_type="dcf_output", source_ref=source_ref, idx=105,
            content_signature=sig,
        ),
        kind="decision",
        title=f"{ticker} — What Must Happen for Upside from Here",
        content={
            "implied_share_price": valuation.get("implied_share_price"),
            "current_price": valuation.get("current_price"),
            "reconciliation_status": reconciliation_status,
            "reconciliation_note": reconciliation_note,
            "divergences": [
                {
                    "id": d.get("id"),
                    "kind": d.get("kind"),
                    "severity": d.get("severity"),
                    "summary": d.get("summary"),
                    "details": d.get("details"),
                }
                for d in divergences
            ],
            "critique": critique if isinstance(critique, dict) else None,
            "signals": signals,
            # Conditions the LLM can mine to write the framing — supplied as
            # rich context not pre-formatted if/then pairs (per user's
            # preference for LLM-driven personalization).
            "framing_context": {
                "key_risks": company_state.get("key_risks") or [],
                "growth_drivers": company_state.get("growth_drivers") or [],
                "macro_context": company_state.get("macro_context"),
                "competitive_position": company_state.get("competitive_position"),
                "lifecycle_stage": company_state.get("lifecycle_stage"),
            },
        },
        source_type="dcf_output",
        source_ref=source_ref,
        suggested_slide_layouts=["decision_summary"],
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def build_expectations_blocks(
    payload: dict[str, Any], source_ref: str, ticker: str,
) -> list[NormalizedBlock]:
    """Build every expectations-first block this payload can support.

    Each builder returns ``None`` when its required upstream data is
    missing, so the result list only contains blocks that have real
    content. Order is significant: the outline template prefers them
    in this sequence (exec → reconciliation → drivers → flow → debate → decision).
    """
    builders = [
        ("three_box", build_three_box),
        ("expectations_table", build_expectations_table),
        ("variable_impact", build_variable_impact),
        ("capital_flow", build_capital_flow),
        ("debate", build_debate),
        ("decision", build_decision),
    ]
    out: list[NormalizedBlock] = []
    for name, fn in builders:
        try:
            block = fn(payload, source_ref, ticker)
        except Exception:
            logger.exception("Expectations builder %s failed for ticker=%s", name, ticker)
            continue
        if block is not None:
            out.append(block)
            logger.info(
                "DCF expectations: built %s block for %s (block_id=%s)",
                name, ticker, block.block_id,
            )
        else:
            logger.info(
                "DCF expectations: skipped %s block for %s (insufficient data)",
                name, ticker,
            )
    return out


__all__ = [
    "build_expectations_blocks",
    "build_expectations_table",
    "build_three_box",
    "build_debate",
    "build_capital_flow",
    "build_variable_impact",
    "build_decision",
]

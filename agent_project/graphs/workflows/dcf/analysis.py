"""Divergence detection + evidence-first analysis layer.

Pipeline::

    detect_divergences_node → analysis_node → convergence_gate

Design principles
-----------------
1. **Detection is deterministic.** ``detect_divergences_node`` computes gaps
   (model WACC vs implied, evidence-vs-assumption, solver failures, missing
   anchoring) and emits structured ``divergences`` records. No LLM here.

2. **Analysis is evidence-first.** For each divergence, ``analysis_node``:
     a. inventories evidence already in state (evidence_pack, KG, thesis)
     b. identifies *specific* missing data (e.g. sector spreads for a WACC gap)
     c. issues at most ``_MAX_TARGETED_FETCHES`` targeted tool calls
     d. asks the LLM to produce a structured ``AnalysisPosition``: EXPLAINED
        (with justified adjustment) or UNEXPLAINED (with uncertainty note).

3. **Same evidence in → same position out.** The LLM only translates
   evidence into a structured verdict; it never invents facts. Re-running with
   identical evidence yields the same adjustment (auditable).

4. **Three-way convergence.** ``convergence_gate_node`` reads positions +
   solver flags and sets ``model_validity`` to ``"valid"``, ``"adjusting"``,
   or ``"invalid"`` (with reason). Routing in graph.py keys off this field.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from .activity import emit_step

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_MAX_TARGETED_FETCHES: int = 3  # per divergence
_MAX_DIVERGENCES_ANALYZED: int = 4  # cap LLM calls per iteration
_WACC_GAP_BPS_THRESHOLD: int = 100  # flag gap above this
_GROWTH_GAP_PCT_THRESHOLD: float = 0.04  # 4pp gap = divergence

# severity → confidence multiplier (penalty when divergence is UNEXPLAINED)
_SEVERITY_PENALTY: dict[str, float] = {
    "critical": 0.5,
    "high": 0.7,
    "medium": 0.9,
    "low": 1.0,
}

_ANALYSIS_LLM = ChatOpenAI(
    model=os.getenv("DCF_ANALYSIS_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=90,
)


# ---------------------------------------------------------------------------
# Pydantic schema for structured analysis output
# ---------------------------------------------------------------------------


class AnalysisAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(description="Assumption field to adjust (wacc, revenue_growth, fcff_margin, terminal_growth, tax_rate)")
    delta: float = Field(description="Signed delta in absolute units (e.g., +0.005 = +50bps)")
    reason: str = Field(description="Why this adjustment is justified by the evidence")


class AnalysisPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    divergence_id: str
    position: str = Field(description="EXPLAINED if evidence justifies a view, UNEXPLAINED if gap remains unresolved")
    explanation: str = Field(description="Reasoning chain — what the evidence shows and why it does or doesn't close the gap")
    evidence_used: list[str] = Field(description="evidence_ids cited from state (existing or newly fetched)")
    adjustment: AnalysisAdjustment | None = Field(
        default=None,
        description="Justified adjustment if position=EXPLAINED and adjustment is warranted. Null if EXPLAINED but no change needed or if UNEXPLAINED.",
    )
    uncertainty_note: str | None = Field(
        default=None,
        description="Required when position=UNEXPLAINED. Describes the unresolved gap for the final report.",
    )


class AnalysisBatch(BaseModel):
    """Container so all divergences can be analyzed in one LLM call."""
    model_config = ConfigDict(extra="forbid")
    positions: list[AnalysisPosition]


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------


def detect_divergences_node(state: dict) -> dict:
    """Compute structured divergence signals from valuation + market outputs.

    No LLM. Produces a list of records like::

        {
          "id": "wacc_gap",
          "kind": "wacc_vs_implied",
          "severity": "high",
          "details": {...},
          "summary": "CAPM 6.5% vs implied 8.2% (170bps overvalued)"
        }
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("detect_divergences", "start", parent_step_id)

    divergences: list[dict[str, Any]] = []
    assumptions = state.get("assumptions") or {}
    wacc_sanity = state.get("wacc_sanity") or {}
    implied_growth = state.get("implied_growth")
    implied_margin = state.get("implied_margin")
    thesis = state.get("thesis") or {}
    evidence_pack = state.get("evidence_pack") or {}

    # ── WACC: solver failure (highest severity, blocks valuation) ───────────
    solver_status = wacc_sanity.get("solver_status", "ok")
    if solver_status in {"no_convergence", "degenerate", "exception", "no_input"}:
        divergences.append({
            "id": "wacc_solver_failure",
            "kind": "solver_failure",
            "severity": "critical",
            "details": {
                "solver_status": solver_status,
                "interpretation": wacc_sanity.get("interpretation", ""),
            },
            "summary": f"Implied WACC solver failed ({solver_status}). Market signal unavailable.",
        })
    elif wacc_sanity.get("flag") in {"warning", "severe"}:
        gap = wacc_sanity.get("gap_bps", 0)
        divergences.append({
            "id": "wacc_gap",
            "kind": "wacc_vs_implied",
            "severity": "high" if wacc_sanity["flag"] == "severe" else "medium",
            "details": {
                "modeled_wacc": assumptions.get("wacc"),
                "capm_wacc": wacc_sanity.get("capm_wacc"),
                "implied_wacc": wacc_sanity.get("implied_wacc"),
                "gap_bps": gap,
                "direction": wacc_sanity.get("direction"),
            },
            "summary": wacc_sanity.get("interpretation", f"WACC gap {gap}bps"),
        })

    # ── Revenue growth: model vs market-implied ─────────────────────────────
    model_growth = assumptions.get("revenue_growth")
    if isinstance(implied_growth, (int, float)) and isinstance(model_growth, (int, float)):
        gap = abs(model_growth - implied_growth)
        if gap > _GROWTH_GAP_PCT_THRESHOLD:
            divergences.append({
                "id": "growth_gap",
                "kind": "growth_vs_implied",
                "severity": "high" if gap > 0.08 else "medium",
                "details": {
                    "modeled_growth": round(model_growth, 4),
                    "implied_growth": round(implied_growth, 4),
                    "gap_pct": round(gap, 4),
                },
                "summary": f"Model growth {model_growth:.1%} vs market-implied {implied_growth:.1%} ({gap*100:+.1f}pp)",
            })

    # ── Margin: model vs market-implied ─────────────────────────────────────
    model_margin = assumptions.get("fcff_margin")
    if isinstance(implied_margin, (int, float)) and isinstance(model_margin, (int, float)):
        gap = abs(model_margin - implied_margin)
        if gap > _GROWTH_GAP_PCT_THRESHOLD:
            divergences.append({
                "id": "margin_gap",
                "kind": "margin_vs_implied",
                "severity": "medium",
                "details": {
                    "modeled_margin": round(model_margin, 4),
                    "implied_margin": round(implied_margin, 4),
                    "gap_pct": round(gap, 4),
                },
                "summary": f"Model margin {model_margin:.1%} vs market-implied {implied_margin:.1%} ({gap*100:+.1f}pp)",
            })

    # ── Unanchored thesis: no key drivers cited from evidence ───────────────
    drivers = thesis.get("key_drivers") if isinstance(thesis, dict) else None
    if not drivers and (evidence_pack.get("total_items") or 0) > 0:
        divergences.append({
            "id": "unanchored_thesis",
            "kind": "anchoring_gap",
            "severity": "medium",
            "details": {"evidence_items_available": evidence_pack.get("total_items", 0)},
            "summary": "Thesis has no key drivers despite evidence available.",
        })

    emit_step(
        "detect_divergences", "complete", parent_step_id,
        {
            "summary_line": f"{len(divergences)} divergences detected",
            "divergences": divergences,
            "count": len(divergences),
        },
    )
    return {"divergences": divergences}


# ---------------------------------------------------------------------------
# Evidence inventory (no tool calls; reads state)
# ---------------------------------------------------------------------------


def _inventory_evidence_for(divergence: dict, state: dict) -> dict[str, Any]:
    """Pull evidence already in state relevant to a given divergence."""
    evidence_pack = state.get("evidence_pack") or {}
    items = evidence_pack.get("items") or []
    thesis = state.get("thesis") or {}
    company_state = state.get("company_state") or {}
    wacc_components = state.get("wacc_components") or {}

    kind = divergence.get("kind", "")
    relevant_items: list[dict] = []
    for it in items:
        text = (it.get("text") or "").lower()
        title = (it.get("title") or "").lower()
        if kind in {"wacc_vs_implied", "solver_failure"}:
            if any(k in text or k in title for k in ("beta", "credit spread", "rate", "risk-free", "wacc", "discount")):
                relevant_items.append(it)
        elif kind == "growth_vs_implied":
            if any(k in text or k in title for k in ("growth", "guidance", "consensus", "revenue", "topline")):
                relevant_items.append(it)
        elif kind == "margin_vs_implied":
            if any(k in text or k in title for k in ("margin", "profitability", "operating leverage", "cost")):
                relevant_items.append(it)
        elif kind == "anchoring_gap":
            relevant_items.append(it)

    return {
        "evidence_items": relevant_items[:8],
        "thesis": thesis,
        "company_state_summary": {
            k: v for k, v in company_state.items()
            if k in {"trajectory", "key_risks", "competitive_position"}
        } if company_state else {},
        "wacc_components_summary": {
            k: wacc_components.get(k) for k in
            ("beta", "cost_of_equity", "pre_tax_cost_of_debt", "equity_weight", "debt_weight")
            if k in wacc_components
        },
    }


def _fetch_analyst_estimates(ticker: str, divergence_id: str) -> list[dict[str, Any]]:
    """Pull FMP analyst revenue consensus estimates for a growth-gap divergence.

    Returns structured evidence items containing forward revenue estimates that
    the analysis LLM can use to explain or dismiss a growth_vs_implied gap.
    No-ops silently when FMP key is absent or the call fails.
    """
    import os as _os  # noqa: PLC0415
    try:
        from .fundamentals import _fmp_get_json  # noqa: PLC0415
    except Exception:
        return []

    api_key = _os.getenv("FMP_API_KEY") or _os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
    if not api_key:
        return []

    try:
        rows = _fmp_get_json(f"analyst-estimates/{ticker}", api_key)
        if not rows:
            return []
        # Format the nearest 2 annual estimates as a text summary for the LLM.
        lines = []
        for r in rows[:2]:
            year = str(r.get("date", "?"))[:4]
            rev_low = r.get("estimatedRevenueLow")
            rev_avg = r.get("estimatedRevenueAvg")
            rev_high = r.get("estimatedRevenueHigh")
            lines.append(
                f"FY{year}: revenue consensus avg=${rev_avg:,.0f} "
                f"(low=${rev_low:,.0f}, high=${rev_high:,.0f})"
                if all(isinstance(x, (int, float)) for x in [rev_avg, rev_low, rev_high])
                else f"FY{year}: revenue avg={rev_avg}"
            )
        text = "Analyst consensus revenue estimates (FMP): " + " | ".join(lines)
        return [{
            "evidence_id": f"ev_analysis_{divergence_id}_fmp_estimates",
            "query": f"FMP analyst-estimates/{ticker}",
            "title": f"{ticker} analyst revenue consensus",
            "url": "",
            "text": text,
            "source_tier": "structured_api",
        }]
    except Exception as exc:  # noqa: BLE001
        logger.debug("FMP analyst estimates fetch failed ticker=%s err=%s", ticker, exc)
        return []


def _fetch_targeted(ticker: str, divergence: dict) -> list[dict[str, Any]]:
    """Issue at most _MAX_TARGETED_FETCHES targeted web searches for the divergence.

    For growth_vs_implied gaps, also queries FMP analyst consensus estimates
    before falling back to web search — gives the LLM concrete forward numbers
    to compare against the market-implied growth rate.

    Queries are derived from divergence kind, not free-form (keeps fetches
    non-redundant with assemble_evidence).
    """
    try:
        from web_search import search_exa  # noqa: PLC0415
    except Exception:
        return []

    kind = divergence.get("kind", "")
    d_id = divergence.get("id", "unknown")
    fetched: list[dict[str, Any]] = []

    # For growth gaps, prepend FMP analyst estimates (structured, reliable).
    if kind == "growth_vs_implied":
        fetched.extend(_fetch_analyst_estimates(ticker, d_id))

    queries: list[str]
    if kind in {"wacc_vs_implied", "solver_failure"}:
        queries = [
            f"{ticker} cost of capital sector credit spread 2025",
            f"{ticker} beta volatility risk premium recent",
        ]
    elif kind == "growth_vs_implied":
        queries = [
            f"{ticker} revenue growth guidance analyst forecast",
        ]
    elif kind == "margin_vs_implied":
        queries = [
            f"{ticker} operating margin trends profitability outlook",
        ]
    elif kind == "anchoring_gap":
        queries = [
            f"{ticker} key business drivers competitive position",
        ]
    else:
        return fetched  # return FMP results even if no web queries

    # Cap total fetches (FMP item counts as 1 if present)
    remaining = _MAX_TARGETED_FETCHES - len(fetched)
    queries = queries[:max(remaining, 1)]
    for q in queries:
        try:
            raw, _summary = search_exa(q, num_results=2, search_type="auto", max_characters=1500)
            payload = json.loads(raw)
            for r in (payload.get("results") or [])[:2]:
                excerpt = " ".join(str(h) for h in (r.get("highlights") or [])[:2])[:800]
                if not excerpt:
                    continue
                fetched.append({
                    "evidence_id": f"ev_analysis_{d_id}_{len(fetched)}",
                    "query": q,
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "text": excerpt,
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Analysis fetch failed q=%s err=%s", q, exc)
    return fetched


# ---------------------------------------------------------------------------
# Analysis prompt + LLM call
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are a valuation analyst examining a single divergence between a DCF model and observable evidence.

Your job:
1. Read the divergence signal and the evidence inventory.
2. Reason about whether the evidence EXPLAINS the gap or leaves it UNEXPLAINED.
3. If EXPLAINED and an adjustment is warranted, propose a small, justified delta with a reason citing evidence_ids.
4. If EXPLAINED but no adjustment needed (model is right, market is wrong, and you have evidence), say so — adjustment=null.
5. If UNEXPLAINED, do NOT invent an adjustment. Write an uncertainty_note for the final report.

Rules:
- NEVER invent facts. Cite only evidence_ids present in the input.
- Adjustments must be small: WACC ≤±100bps, growth ≤±3pp, margin ≤±3pp, terminal_growth ≤±50bps.
- Same evidence in → same position out. Be deterministic.
- Adjustment field must match the divergence (WACC gap → wacc, growth gap → revenue_growth, etc.)."""


def _analyze_batch(
    ticker: str,
    items: list[dict],  # [{divergence, existing_evidence, fetched_evidence}, ...]
) -> dict[str, AnalysisPosition] | None:
    """One LLM call covering all divergences. Returns {divergence_id: position}.

    Batching cuts LLM cost N→1 per iteration vs. _analyze_one per divergence.
    """
    if not items:
        return {}
    payload = {
        "ticker": ticker,
        "instructions": (
            "Return one AnalysisPosition per divergence below. "
            "Use the same divergence_id we provided. Apply the rules in the "
            "system prompt to EACH divergence independently."
        ),
        "divergences": items,
    }
    try:
        structured = _ANALYSIS_LLM.with_structured_output(AnalysisBatch)
        result = structured.invoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, default=str)[:16000]},
        ])
        if not isinstance(result, AnalysisBatch):
            return None
        out: dict[str, AnalysisPosition] = {}
        for pos in result.positions:
            out[pos.divergence_id] = pos
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analysis batch LLM failed err=%s", exc)
        return None


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


def analysis_node(state: dict) -> dict:
    """Reason over divergences using evidence-first ReAct pattern.

    Output:
      - state["analysis_positions"]: list of AnalysisPosition dicts
      - state["assumptions"]: updated with any justified adjustments
      - state["effective_confidence"]: penalized by unexplained divergences
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state.get("ticker", "?")
    divergences = state.get("divergences") or []

    emit_step(
        "analysis", "start", parent_step_id,
        {"summary_line": f"Analyzing {len(divergences)} divergences", "count": len(divergences)},
    )

    if not divergences:
        emit_step(
            "analysis", "complete", parent_step_id,
            {"summary_line": "No divergences — model accepted as-is", "positions": []},
        )
        return {
            "analysis_positions": [],
            "effective_confidence": _confidence_from_label(state.get("confidence_label", "medium")),
        }

    positions: list[dict[str, Any]] = []
    new_assumptions = dict(state.get("assumptions") or {})
    changes: list[str] = []

    # Cap divergences analyzed per iteration to bound cost
    targeted = sorted(
        divergences,
        key=lambda d: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(d.get("severity", "low"), 4),
    )[:_MAX_DIVERGENCES_ANALYZED]

    # Build batch payload: inventory + targeted fetch per divergence (no LLM yet)
    batch_items: list[dict] = []
    fetched_by_id: dict[str, list[dict]] = {}
    for divergence in targeted:
        existing = _inventory_evidence_for(divergence, state)
        fetched = _fetch_targeted(ticker, divergence)
        fetched_by_id[divergence["id"]] = fetched
        batch_items.append({
            "divergence": divergence,
            "existing_evidence": existing,
            "newly_fetched_evidence": fetched,
        })

    # ONE LLM call for all divergences (vs N before — kinder to rate limits)
    batch_result = _analyze_batch(ticker, batch_items)

    for divergence in targeted:
        d_id = divergence["id"]
        position = batch_result.get(d_id) if batch_result else None
        fetched = fetched_by_id.get(d_id, [])

        if position is None:
            # Treat LLM failure as UNEXPLAINED (conservative)
            positions.append({
                "divergence_id": d_id,
                "position": "UNEXPLAINED",
                "explanation": "Analysis LLM call failed (rate limit, quota, or schema error).",
                "evidence_used": [],
                "new_evidence_fetched": [f["evidence_id"] for f in fetched],
                "adjustment": None,
                "uncertainty_note": f"Could not analyze {divergence['summary']}",
                "divergence_summary": divergence.get("summary", ""),
                "divergence_severity": divergence.get("severity", "medium"),
            })
            continue

        pos_dict = position.model_dump()
        pos_dict["divergence_summary"] = divergence.get("summary", "")
        pos_dict["divergence_severity"] = divergence.get("severity", "medium")
        pos_dict["new_evidence_fetched"] = [f["evidence_id"] for f in fetched]

        # Phase 4: solver failures are STRUCTURAL — no narrative justifies
        # them. Override any LLM "EXPLAINED" verdict so convergence_gate sees
        # the failure as critical_unexplained → forces model_validity=invalid.
        if divergence.get("kind") == "solver_failure" and pos_dict.get("position") != "UNEXPLAINED":
            logger.warning(
                "Overriding LLM verdict on solver_failure divergence %s → UNEXPLAINED",
                d_id,
            )
            pos_dict["position"] = "UNEXPLAINED"
            pos_dict["explanation"] = (
                "Solver failure cannot be explained narratively — the numerical "
                "computation itself did not produce a usable result."
            )
            pos_dict["adjustment"] = None
        positions.append(pos_dict)

        # Apply justified adjustments
        adj = position.adjustment
        if adj and position.position == "EXPLAINED" and adj.field in new_assumptions:
            delta = _clamp_adjustment(adj.field, adj.delta)
            if delta != 0.0:
                old = new_assumptions[adj.field]
                new_assumptions[adj.field] = round(old + delta, 6)
                changes.append(f"{adj.field}: {old:.4f} → {new_assumptions[adj.field]:.4f} ({adj.reason[:60]})")

    # ── Confidence propagation ──────────────────────────────────────────────
    base_conf = _confidence_from_label(state.get("confidence_label", "medium"))
    unexplained = [p for p in positions if p["position"] == "UNEXPLAINED"]
    penalty = 1.0
    for p in unexplained:
        sev = p.get("divergence_severity", "medium")
        penalty *= _SEVERITY_PENALTY.get(sev, 0.9)
    effective_confidence = round(base_conf * penalty, 3)

    emit_step(
        "analysis", "complete", parent_step_id,
        {
            "summary_line": (
                f"{len(positions)} analyzed: "
                f"{sum(1 for p in positions if p['position'] == 'EXPLAINED')} explained, "
                f"{len(unexplained)} unexplained, {len(changes)} adjustments"
            ),
            "positions": positions,
            "changes": changes,
            "effective_confidence": effective_confidence,
            "base_confidence": base_conf,
        },
    )

    return {
        "analysis_positions": positions,
        "assumptions": new_assumptions,
        "effective_confidence": effective_confidence,
    }


# ---------------------------------------------------------------------------
# Convergence gate (three-way routing)
# ---------------------------------------------------------------------------


def convergence_gate_node(state: dict) -> dict:
    """Decide model_validity from analysis output + iteration count.

    States:
      - "valid"     → all divergences EXPLAINED or none present → finalize
      - "adjusting" → at least one justified adjustment queued; re-run valuation
      - "invalid"   → critical/solver failure unresolved OR hard-stop reached
                       with remaining unexplained divergences → halt with reason

    Option-C rule: when max_iter is reached AND analysis_node produced
    justified adjustments, allow ONE extra valuation pass so those adjustments
    are reflected in the final output before we decide validity.  The hard stop
    is iteration > max_iter (i.e., max_iter + 1 total scenario-runner calls).
    Without this, analysis-derived adjustments computed at the boundary would
    be applied to state["assumptions"] but never re-run through valuation,
    leaving the reported price stale.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    positions = state.get("analysis_positions") or []
    iteration = state.get("analysis_iteration", 0)
    max_iter = 2  # mirrors _MAX_ANALYSIS_ITERATIONS in graph.py

    unexplained = [p for p in positions if p["position"] == "UNEXPLAINED"]
    explained_with_adj = [
        p for p in positions
        if p["position"] == "EXPLAINED" and p.get("adjustment")
    ]
    critical_unexplained = [
        p for p in unexplained
        if p.get("divergence_severity") == "critical"
    ]

    # hard_stop = True only after the bonus final-adjustment pass is consumed.
    hard_stop = iteration > max_iter

    validity: str
    reason: str
    reconciliation_status: str

    if critical_unexplained:
        # Solver / structural failure — math or inputs are not trustworthy.
        validity = "invalid"
        reconciliation_status = "critical_unresolved"
        reason = (
            f"Critical divergence unresolved: "
            f"{critical_unexplained[0].get('divergence_summary', 'solver/structural failure')}"
        )
    elif not positions:
        validity = "valid"
        reconciliation_status = "aligned"
        reason = "No divergences detected."
    elif explained_with_adj and not hard_stop:
        validity = "adjusting"
        reconciliation_status = "refining"
        if iteration >= max_iter:
            reason = (
                f"Max iterations reached but {len(explained_with_adj)} justified adjustment(s) "
                f"pending — applying final valuation pass before verdict (Option C)."
            )
        else:
            reason = f"{len(explained_with_adj)} justified adjustments queued; re-running valuation."
    elif unexplained:
        # Market-implied gaps remain — DCF math is fine; price embeds different expectations.
        validity = "valid"
        reconciliation_status = "structural_gap"
        reason = (
            f"{len(unexplained)} market reconciliation gap(s) remain after analysis. "
            f"The DCF math is intact; the share price may embed expectations "
            f"the cited assumptions do not support."
        )
    else:
        validity = "valid"
        reconciliation_status = "aligned"
        reason = "All divergences explained; model accepted."

    emit_step(
        "convergence_gate", "complete", parent_step_id,
        {
            "summary_line": f"model_validity={validity} reconciliation={reconciliation_status} iter={iteration} ({reason[:80]})",
            "validity": validity,
            "reconciliation_status": reconciliation_status,
            "reason": reason,
            "iteration": iteration,
            "hard_stop": hard_stop,
            "unexplained_count": len(unexplained),
            "adjustments_pending": len(explained_with_adj),
            "critical_unexplained": len(critical_unexplained),
        },
    )

    return {
        "model_validity": validity,
        "reconciliation_status": reconciliation_status,
        "reconciliation_note": reason if reconciliation_status == "structural_gap" else "",
        "invalidation_reason": reason if validity == "invalid" else "",
        "analysis_iteration": iteration + 1,
    }


def route_after_convergence_gate(state: dict) -> str:
    """Three-way route based on model_validity."""
    validity = state.get("model_validity", "valid")
    if validity == "adjusting":
        return "scenario_runner"  # re-run valuation chain with adjusted assumptions
    # Both "valid" and "invalid" route to finalize — invalid is surfaced in the
    # output payload with reason; we don't drop the report.
    return "finalize"


def route_after_convergence_gate_val(state: dict) -> str:
    """Fast-path variant: re-enter project_cashflows instead of scenario_runner."""
    validity = state.get("model_validity", "valid")
    if validity == "adjusting":
        return "project_cashflows"
    return "finalize"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ADJUSTMENT_CAP: dict[str, float] = {
    "wacc": 0.010,
    "revenue_growth": 0.030,
    "fcff_margin": 0.030,
    "terminal_growth": 0.005,
    "tax_rate": 0.030,
}


def _clamp_adjustment(field: str, delta: float) -> float:
    cap = _ADJUSTMENT_CAP.get(field, 0.0)
    if cap == 0.0:
        return 0.0
    return max(-cap, min(cap, float(delta)))


def _confidence_from_label(label: str) -> float:
    return {"high": 0.85, "medium": 0.65, "low": 0.40}.get((label or "").lower(), 0.65)

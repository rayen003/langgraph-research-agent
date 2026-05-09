"""DCF workflow — graph wiring and public API.

Target architecture::

    START → normalize_input → assemble_evidence → semantic_synthesis
    → propose_assumptions → review_assumptions → [collect_market_data | END]
    → project_cashflows → compute_valuation → sensitivity → finalize → END

Two engines:
    Reasoning layer (assemble_evidence → synthesis → memo → review):
        Turn messy reality into explicit, cited assumptions.
    Valuation layer (project → compute → sensitivity → finalize):
        Deterministic FCFF math — sacred, unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from utils import get_run_dir

from .activity import emit_step, emit_workflow_terminal
from .evidence import assemble_evidence_node
from .memo import propose_assumptions_node
from .review import review_assumptions_node, route_after_assumptions
from .synthesis import semantic_synthesis_node
from .state import (
    _TIER_A_FIELDS,
    DCFState,
)
from .valuation import (
    collect_market_data_node,
    compute_implied_wacc_node,
    compute_valuation_node,
    finalize_node,
    project_cashflows_node,
    sensitivity_node,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# normalize_input — validates and normalizes the incoming request
# ---------------------------------------------------------------------------


def normalize_input_node(state: DCFState) -> dict:
    """Validate ticker and horizon, emit workflow-started span."""
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="started",
        payload={"ticker": str(state.get("ticker") or "").upper()},
    )
    emit_step("normalize_input", "start", parent_step_id)
    ticker = str(state.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required for DCF workflow.")
    horizon = int(state.get("horizon_years") or 5)
    horizon = min(max(horizon, 3), 10)
    logger.info(
        "DCF normalize_input ticker=%s horizon_years=%d", ticker, horizon,
    )
    emit_step(
        "normalize_input", "complete", parent_step_id,
        {"ticker": ticker, "horizon_years": horizon, "summary_line": f"{ticker} {horizon}yr horizon"},
    )
    return {"ticker": ticker, "horizon_years": horizon}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize_dcf_payload(payload: dict[str, Any]) -> str:
    """Rich summary for LLM tool-result feedback.

    Surfaces: per-assumption provenance, synthesis insights, evidence refs,
    confidence, flags, and WACC decomposition. Gives the LLM structured
    context to write a high-quality report.
    """
    if not isinstance(payload, dict):
        return "DCF workflow finished without payload."

    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────
    ticker = str(payload.get("ticker") or "?").upper()
    valuation = payload.get("valuation") or {}
    implied = valuation.get("implied_share_price")
    spot = valuation.get("current_price") or 0.0
    confidence = str(payload.get("confidence_label") or "medium")
    horizon = payload.get("horizon_years", 5)

    lines.append(f"# DCF Valuation: {ticker}")
    lines.append(f"Horizon: {horizon} years")
    if isinstance(implied, (int, float)) and implied:
        gap = ""
        if isinstance(spot, (int, float)) and spot > 0:
            pct = ((implied / spot) - 1) * 100
            gap = f" (vs spot ${spot:.2f}, {pct:+.1f}%)"
        lines.append(f"Implied share price: ${implied:.2f}{gap}")
    lines.append(f"Confidence: **{confidence.upper()}**")
    lines.append("")

    # ── Assumptions with provenance ─────────────────────────────────────
    assumptions = payload.get("assumptions") or {}
    provenance = payload.get("assumption_provenance") or {}
    memo = payload.get("assumption_memo") or {}

    if assumptions:
        lines.append("## Assumptions")
        lines.append("| Field | Value | Source | Confidence |")
        lines.append("|-------|-------|--------|------------|")
        for field in [
            "base_revenue", "revenue_growth", "fcff_margin",
            "terminal_growth", "tax_rate", "wacc",
            "shares_outstanding", "net_debt",
        ]:
            val = assumptions.get(field)
            if val is None:
                continue
            prov = provenance.get(field, {}) if isinstance(provenance, dict) else {}
            source = prov.get("source", "unknown")
            conf = prov.get("confidence", "?")
            if isinstance(conf, (int, float)):
                conf = f"{conf:.0%}"
            # Format: millions for scale fields, % for rates
            if field in _TIER_A_FIELDS:
                val_str = f"${val:,.0f}M"
            elif field == "shares_outstanding":
                val_str = f"{val:,.0f}M"
            else:
                val_str = f"{val:.2%}"
            lines.append(f"| {field} | {val_str} | {source} | {conf} |")
        lines.append("")

    # ── WACC decomposition ──────────────────────────────────────────────
    wacc_comp = payload.get("wacc_components") or {}
    if wacc_comp:
        lines.append("## WACC Decomposition")
        lines.append(f"Method: {wacc_comp.get('method', 'unknown')}")
        for key in (
            "risk_free_rate", "equity_risk_premium", "beta",
            "cost_of_equity", "pre_tax_cost_of_debt",
            "after_tax_cost_of_debt", "equity_weight", "debt_weight",
            "marginal_tax_rate",
        ):
            v = wacc_comp.get(key)
            if v is not None:
                if isinstance(v, float) and key not in ("beta",):
                    lines.append(f"  {key}: {v:.2%}")
                else:
                    lines.append(f"  {key}: {v}")
        lines.append("")

    # ── Memo rationale (if available) ───────────────────────────────────
    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    evidence_items = payload.get("_evidence_items") or []
    if proposals:
        lines.append("## Assumption Rationale")
        for prop in proposals:
            field = prop.get("field", "?")
            rationale = prop.get("rationale", "")
            conf = prop.get("confidence", 0.5)
            refs = prop.get("evidence_refs", [])
            human_refs = _humanize_evidence_refs(refs, evidence_items) if evidence_items else refs
            ref_str = "; ".join(human_refs[:3]) if human_refs else "none cited"
            lines.append(f"**{field}** (confidence: {conf:.0%})")
            lines.append(f"  {rationale}")
            lines.append(f"  Sources: {ref_str}")
            lines.append("")
        overall = memo.get("overall_narrative", "")
        if overall:
            lines.append(f"**Narrative:** {overall}")
            lines.append("")
        uncertainties = memo.get("key_uncertainties", [])
        if uncertainties:
            lines.append("**Key uncertainties:**")
            for u in uncertainties:
                lines.append(f"  - {u}")
            lines.append("")

    # ── Synthesis summary ───────────────────────────────────────────────
    company_state = payload.get("company_state") or {}
    if company_state:
        lines.append("## Company Context")
        for key, label in [
            ("growth_outlook", "Growth outlook"),
            ("margin_trend", "Margin trend"),
            ("margin_narrative", "Margin narrative"),
            ("competitive_position", "Competitive position"),
        ]:
            val = company_state.get(key)
            if val:
                lines.append(f"**{label}:** {val}")
        risks = company_state.get("key_risks", [])
        if risks:
            lines.append("**Key risks:**")
            for r in risks[:5]:
                lines.append(f"  - {r}")
        conflicts = company_state.get("conflicts", [])
        if conflicts:
            lines.append("**Source conflicts:**")
            for c in conflicts:
                lines.append(f"  - {c}")
        lines.append("")

    # ── Flags ───────────────────────────────────────────────────────────
    flags = (
        list(payload.get("assumption_flags") or [])
        + list(payload.get("valuation_flags") or [])
    )
    if flags:
        lines.append("## Quality Flags")
        for flag in flags:
            sev = flag.get("severity", "?").upper()
            msg = flag.get("message", "")
            lines.append(f"  [{sev}] {msg}")
        lines.append("")

    # ── Valuation detail ────────────────────────────────────────────────
    if valuation:
        lines.append("## Valuation Detail")
        for key, label in [
            ("pv_cash_flows", "PV of explicit cash flows"),
            ("terminal_value", "Terminal value (undiscounted)"),
            ("terminal_pv", "Terminal value (discounted to present)"),
            ("enterprise_value", "Enterprise value (= PV + discounted TV)"),
            ("equity_value", "Equity value (= EV − net debt)"),
        ]:
            v = valuation.get(key)
            if isinstance(v, (int, float)):
                lines.append(f"  {label}: ${v:,.0f}M")
        # Reconciliation check
        pv = valuation.get("pv_cash_flows", 0)
        tv_pv = valuation.get("terminal_pv", 0)
        ev = valuation.get("enterprise_value", 0)
        implied_ev = pv + tv_pv
        if abs(ev - implied_ev) > 1:
            lines.append(f"  ⚠ EV reconciliation: PV (${pv:,.0f}M) + discounted TV (${tv_pv:,.0f}M) = ${implied_ev:,.0f}M ≠ stated EV (${ev:,.0f}M)")
        else:
            lines.append(f"  ✓ EV = ${pv:,.0f}M + ${tv_pv:,.0f}M = ${ev:,.0f}M")
        lines.append("")

    # ── Sensitivity highlights ──────────────────────────────────────────
    sensitivity = payload.get("sensitivity_table") or []
    if sensitivity:
        prices = [r.get("implied_share_price", 0) for r in sensitivity if r.get("implied_share_price")]
        if prices:
            lines.append(f"## Sensitivity Range")
            lines.append(f"  Implied price range: ${min(prices):.2f} – ${max(prices):.2f}")
            lines.append(f"  Across WACC ±1% and terminal growth ±0.5%")
            lines.append("")

    # ── Consistency checks ─────────────────────────────────────────────
    checks = _run_consistency_checks(payload)
    if checks:
        lines.append("## Consistency Checks")
        for check in checks:
            status = "✓" if check["ok"] else "⚠"
            lines.append(f"  {status} {check['label']}: {check['detail']}")
        lines.append("")

    return "\n".join(lines)


def _run_consistency_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-validate the DCF output for internal consistency."""
    checks: list[dict[str, Any]] = []
    val = payload.get("valuation") or {}
    features = payload.get("features") or {}
    wacc_comp = payload.get("wacc_components") or {}
    memo = payload.get("assumption_memo") or {}

    # 1. EV reconciliation
    pv = val.get("pv_cash_flows", 0) or 0
    tv_pv = val.get("terminal_pv", 0) or 0
    ev = val.get("enterprise_value", 0) or 0
    if abs((pv + tv_pv) - ev) > 0.01 * max(ev, 1):
        checks.append({"ok": False, "label": "EV reconciliation",
                        "detail": f"PV (${pv:,.0f}M) + TV_discounted (${tv_pv:,.0f}M) ≠ EV (${ev:,.0f}M)"})
    else:
        checks.append({"ok": True, "label": "EV reconciliation",
                        "detail": f"${pv:,.0f}M + ${tv_pv:,.0f}M = ${ev:,.0f}M"})

    # 2. Memo vs features WACC consistency
    memo_wacc = (payload.get("assumptions") or {}).get("wacc")
    capm_beta = features.get("beta")
    capm_erp = wacc_comp.get("equity_risk_premium", 0.055)
    capm_rf = wacc_comp.get("risk_free_rate", 0.045)
    if memo_wacc and capm_beta:
        implied_capm_wacc = capm_rf + float(capm_beta) * capm_erp
        wacc_method = wacc_comp.get("method", "?")
        if wacc_method == "capm" and abs(memo_wacc - implied_capm_wacc) > 0.02:
            checks.append({"ok": False, "label": "WACC vs CAPM",
                           "detail": f"WACC={memo_wacc:.2%} vs CAPM-implied Rf+β·ERP={implied_capm_wacc:.2%} (gap > 2%)"})
        else:
            checks.append({"ok": True, "label": "WACC vs CAPM",
                           "detail": f"WACC={memo_wacc:.2%} via {wacc_method}, β={capm_beta}"})

    # 3. Terminal growth vs risk-free rate
    tg = (payload.get("assumptions") or {}).get("terminal_growth")
    rf = wacc_comp.get("risk_free_rate", 0.045)
    if tg is not None and rf:
        if tg > rf + 0.005:
            checks.append({"ok": False, "label": "Terminal growth vs Rf",
                           "detail": f"TGR={tg:.2%} exceeds Rf={rf:.2%} by >50bps — implies company outgrows economy forever"})
        else:
            checks.append({"ok": True, "label": "Terminal growth vs Rf",
                           "detail": f"TGR={tg:.2%} ≤ Rf={rf:.2%}+buffer ✓"})

    # 4. Evidence coverage
    proposals = memo.get("proposals") if isinstance(memo, dict) else None
    if proposals:
        total_refs = sum(len(p.get("evidence_refs", [])) for p in proposals)
        filing_refs = 0
        evidence_items = payload.get("_evidence_items") or []
        by_id = {item.get("evidence_id", ""): item for item in evidence_items}
        for p in proposals:
            for ref in p.get("evidence_refs", []):
                if by_id.get(ref, {}).get("source_tier") == "filing":
                    filing_refs += 1
        if total_refs == 0:
            checks.append({"ok": False, "label": "Evidence coverage",
                           "detail": "No evidence refs cited in memo proposals"})
        elif filing_refs == 0:
            checks.append({"ok": False, "label": "Evidence coverage",
                           "detail": f"{total_refs} refs but 0 from SEC filings — assumptions may lack grounding"})
        else:
            checks.append({"ok": True, "label": "Evidence coverage",
                           "detail": f"{total_refs} refs, {filing_refs} from SEC filings ✓"})

    return checks


def _humanize_evidence_refs(
    refs: list[str],
    evidence_items: list[dict[str, Any]],
) -> list[str]:
    """Map opaque evidence_ids to human-readable labels."""
    by_id: dict[str, dict[str, Any]] = {
        item.get("evidence_id", ""): item for item in evidence_items
    }
    result: list[str] = []
    for ref in refs:
        item = by_id.get(ref)
        if not item:
            result.append(ref)
            continue
        kind = item.get("kind", "?")
        if kind == "filing_excerpt":
            result.append(f"{item.get('filing_type','')} {item.get('section','')} ({item.get('as_of','')})")
        elif kind == "structured_fundamental":
            result.append(f"{item.get('source','')}: {item.get('field','')}={item.get('value','?')}")
        elif kind == "web_excerpt":
            result.append(f"web: {item.get('title','?')[:60]}")
        elif kind == "document_excerpt":
            result.append(f"doc: {item.get('filename','?')} p.{item.get('page','?')}")
        elif kind == "market_data":
            result.append(f"market: {item.get('field','?')}={item.get('value','?')}")
        else:
            result.append(f"{kind}: {ref[:40]}")
    return result


def _build_initial_state(
    ticker: str,
    horizon_years: int,
    assumption_review_mode: bool,
    allow_external_assumptions: bool,
    assumption_overrides: dict[str, float],
    parent_step_id: str,
    session_id: str,
) -> DCFState:
    """Build the initial DCFState dict."""
    return {
        "ticker": ticker,
        "horizon_years": horizon_years,
        "session_id": session_id,
        "assumption_review_mode": assumption_review_mode,
        "allow_external_assumptions": allow_external_assumptions,
        "assumption_overrides": assumption_overrides,
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
        "evidence_pack": {},
        "company_state": None,
        "assumption_memo": None,
        "confidence_breakdown": None,
        "wacc_sanity": None,
    }


def _build_memo_proposals(state_or_payload: dict) -> dict:
    memo = state_or_payload.get("assumption_memo") or {}
    result = {}
    if isinstance(memo, dict):
        for p in (memo.get("proposals") or []):
            if isinstance(p, dict) and p.get("field"):
                result[p["field"]] = {
                    "rationale": p.get("rationale", ""),
                    "confidence": p.get("confidence", 0.5),
                }
    return result


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
    """Run the DCF workflow synchronously and return the result payload.

    When ``assumption_review_mode=True``, the workflow runs up to the
    assumption review gate, then returns a structured HITL payload
    containing the proposed assumptions and their provenance. The caller
    (agent tool) can surface this to the user for review. After the user
    approves/edits, re-call with ``assumption_overrides`` and
    ``assumption_review_mode=False`` to complete the valuation.

    When ``assumption_review_mode=False``, auto-approves and runs to
    completion, returning the full dcf_output.json payload.
    """
    overrides = assumption_overrides or {}
    # Fast path: all assumptions already approved — skip evidence/synthesis/memo.
    use_fast_path = (
        not assumption_review_mode
        and _ALL_ASSUMPTION_FIELDS.issubset(overrides.keys())
    )
    initial_state = _build_initial_state(
        ticker=ticker,
        horizon_years=horizon_years,
        assumption_review_mode=assumption_review_mode,
        allow_external_assumptions=allow_external_assumptions,
        assumption_overrides=overrides,
        parent_step_id=parent_step_id,
        session_id=session_id,
    )

    config = {"configurable": {"thread_id": get_run_dir().name}}

    try:
        if use_fast_path:
            # Pre-populate state with approved assumptions so valuation nodes
            # can run without evidence assembly or LLM memo.
            initial_state["assumptions"] = {k: float(v) for k, v in overrides.items()}
            initial_state["assumptions_approved"] = True
            initial_state["assumption_provenance"] = {
                k: {"source": "user_approved", "confidence": 1.0}
                for k in overrides
            }
            logger.info(
                "DCF fast path ticker=%s assumptions=%s",
                ticker, json.dumps(initial_state["assumptions"], ensure_ascii=False),
            )
            result = dcf_valuation_app.invoke(initial_state, config=config)
        else:
            result = dcf_workflow_app.invoke(initial_state, config=config)
    except GraphInterrupt as gi:
        if not assumption_review_mode:
            raise
        interrupt_payload: dict[str, Any] = {}
        if gi.args:
            raw = gi.args[0]
            if isinstance(raw, dict):
                interrupt_payload = raw
        assumptions = interrupt_payload.get("assumptions") or {}
        provenance = interrupt_payload.get("assumption_provenance") or {}
        logger.info(
            "DCF interrupted for review ticker=%s assumptions=%s",
            ticker,
            json.dumps(assumptions, ensure_ascii=False),
        )
        memo_proposals = _build_memo_proposals(interrupt_payload)
        return {
            "__dcf_hitl__": True,
            "ticker": ticker,
            "horizon_years": horizon_years,
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "memo_proposals": memo_proposals,
            "evidence_items": interrupt_payload.get("evidence_items", []),
            "message": (
                "DCF assumptions are ready for review. "
                "Present these to the user for approval or edits. "
                "After the user responds, re-run with their edits as "
                "assumption_overrides and assumption_review_mode=False "
                "to complete the valuation."
            ),
        }
    except Exception:
        raise

    # Completed run — read the output file
    result_path = result.get("result_path")
    if not result_path:
        # Graph was interrupted (checkpointer swallowed GraphInterrupt).
        # The review node called interrupt() but invoke() returned partial state.
        if result.get("assumptions"):
            assumptions = result.get("assumptions") or {}
            provenance = result.get("assumption_provenance") or {}
            if assumption_review_mode:
                logger.info(
                    "DCF interrupted for review ticker=%s", ticker,
                )
                memo_proposals = _build_memo_proposals(result)
                return {
                    "__dcf_hitl__": True,
                    "ticker": ticker,
                    "horizon_years": horizon_years,
                    "assumptions": assumptions,
                    "assumption_provenance": provenance,
                    "memo_proposals": memo_proposals,
                    "evidence_items": [],
                    "message": "DCF assumptions ready for review.",
                }
            # review_mode off but no valuation — shouldn't happen; try to continue
            result["assumptions_approved"] = True
            result = dcf_valuation_app.invoke(result, config=config)
            result_path = result.get("result_path")
    if not result_path:
        raise RuntimeError("DCF workflow finished without a result path.")
    out_path = Path(result_path)
    if not out_path.exists():
        raise FileNotFoundError(f"DCF workflow result not found: {result_path}")
    return json.loads(out_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Graph definition
# ---------------------------------------------------------------------------

graph = StateGraph(DCFState)

graph.add_node("normalize_input", normalize_input_node)
graph.add_node("assemble_evidence", assemble_evidence_node)
graph.add_node("semantic_synthesis", semantic_synthesis_node)
graph.add_node("propose_assumptions", propose_assumptions_node)
graph.add_node("review_assumptions", review_assumptions_node)
graph.add_node("collect_market_data", collect_market_data_node)
graph.add_node("project_cashflows", project_cashflows_node)
graph.add_node("compute_valuation", compute_valuation_node)
graph.add_node("compute_implied_wacc", compute_implied_wacc_node)
graph.add_node("sensitivity", sensitivity_node)
graph.add_node("finalize", finalize_node)

graph.add_edge(START, "normalize_input")
graph.add_edge("normalize_input", "assemble_evidence")
graph.add_edge("assemble_evidence", "semantic_synthesis")
graph.add_edge("semantic_synthesis", "propose_assumptions")
graph.add_edge("propose_assumptions", "review_assumptions")
graph.add_conditional_edges(
    "review_assumptions",
    route_after_assumptions,
    {"collect_market_data": "collect_market_data", END: END},
)
graph.add_edge("collect_market_data", "project_cashflows")
graph.add_edge("project_cashflows", "compute_valuation")
graph.add_edge("compute_valuation", "compute_implied_wacc")
graph.add_edge("compute_implied_wacc", "sensitivity")
graph.add_edge("sensitivity", "finalize")
graph.add_edge("finalize", END)

dcf_workflow_app = graph.compile(checkpointer=MemorySaver())

# Valuation-only graph — skips evidence/synthesis/memo.
# Used when all assumptions are provided directly (fast path after HITL approval).
_val_graph = StateGraph(DCFState)
_val_graph.add_node("normalize_input", normalize_input_node)
_val_graph.add_node("collect_market_data", collect_market_data_node)
_val_graph.add_node("project_cashflows", project_cashflows_node)
_val_graph.add_node("compute_valuation", compute_valuation_node)
_val_graph.add_node("compute_implied_wacc", compute_implied_wacc_node)
_val_graph.add_node("sensitivity", sensitivity_node)
_val_graph.add_node("finalize", finalize_node)
_val_graph.add_edge(START, "normalize_input")
_val_graph.add_edge("normalize_input", "collect_market_data")
_val_graph.add_edge("collect_market_data", "project_cashflows")
_val_graph.add_edge("project_cashflows", "compute_valuation")
_val_graph.add_edge("compute_valuation", "compute_implied_wacc")
_val_graph.add_edge("compute_implied_wacc", "sensitivity")
_val_graph.add_edge("sensitivity", "finalize")
_val_graph.add_edge("finalize", END)
dcf_valuation_app = _val_graph.compile()

_ALL_ASSUMPTION_FIELDS = frozenset({
    "base_revenue", "revenue_growth", "fcff_margin", "wacc",
    "terminal_growth", "net_debt", "shares_outstanding", "tax_rate",
})

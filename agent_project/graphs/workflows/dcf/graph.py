"""DCF workflow — graph wiring and public API.

Target architecture::

    START → normalize_input → assemble_evidence → semantic_synthesis
    → formulate_thesis → propose_assumptions → review_assumptions
    → [collect_market_data | END]
    → project_cashflows → compute_valuation → compute_implied_wacc → sensitivity
    → analyze_result → [refine → project_cashflows | finalize → END]

Three layers:
    Evidence layer (assemble → synthesis):
        Turn messy sources into structured company understanding.
    Thesis layer (formulate_thesis → memo → review):
        Form a conviction, derive assumptions, get approval.
    Valuation + analysis layer (project → analyze → refine | finalize):
        Deterministic FCFF math + self-critique loop.

Node implementations live in sibling modules; this file owns wiring + the
public sync entrypoint:
  - lifecycle.py   — normalize_input, cache_check, route_after_cache_check
  - scenarios.py   — scenario_generator + monotonicity validation
  - execution.py   — formulate_thesis + scenario_runner
  - review_loop.py — run_review_subgraph + routers
  - refinement.py  — analyze_result + refine_assumptions + route_after_analysis
  - payload.py     — summarize_dcf_payload + consistency checks
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from utils import get_dcf_hitl_payload, get_run_dir

from .hitl_snapshot import apply_hitl_snapshot, build_hitl_snapshot
from .sources import merge_hitl_provenance, reconstruct_memo_from_provenance

from .analysis import (
    analysis_node,
    convergence_gate_node,
    detect_divergences_node,
    route_after_convergence_gate,
    route_after_convergence_gate_val,
)
from .coherence import coherence_gate_node
from .evidence import assemble_evidence_node
from .execution import formulate_thesis_node, scenario_runner_node
from .lifecycle import cache_check_node, normalize_input_node, route_after_cache_check
from .memo import propose_assumptions_node
from .payload import summarize_dcf_payload  # re-exported for public API
from .refinement import analyze_result_node, refine_assumptions_node, route_after_analysis
from .review import review_assumptions_node, route_after_assumptions
from .review_loop import route_after_review, route_after_review_val, run_review_subgraph
from .scenarios import scenario_generator_node
from .state import DCFState
from .synthesis import semantic_synthesis_node
from .valuation import (
    collect_market_data_node,
    compute_market_signals_node,
    compute_valuation_node,
    finalize_node,
    project_cashflows_node,
    sensitivity_node,
)

logger = logging.getLogger(__name__)

__all__ = [
    "dcf_workflow_app",
    "dcf_valuation_app",
    "dcf_scenario_val_app",
    "run_dcf_workflow_sync",
    "summarize_dcf_payload",
]


# ---------------------------------------------------------------------------
# Initial-state builder + memo helper
# ---------------------------------------------------------------------------

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
        "implied_growth": None,
        "implied_margin": None,
        "thesis": None,
        "analysis_iteration": 0,
        "critique": None,
        "previous_valuation": None,
        "scenarios": [],
        "scenario_results": [],
        "assumption_history": [],
        "initial_assumptions": {},
        "kg_cache_flags": {},
        "kg_fundamentals_hint": {},
        "kg_lifecycle_hint": {},
        "kg_anchored_corpus_meta": {},
        "kg_cache_results": [],
        "divergences": [],
        "analysis_positions": [],
        "model_validity": "valid",
        "invalidation_reason": "",
        "reconciliation_status": "aligned",
        "reconciliation_note": "",
        "market_signals_meta": {},
        "effective_confidence": None,
        "confidence_assessment": None,
        "conviction_direction": None,
        "coherence_assessment": None,
        "coherence_adjustments": {},
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


_ALL_ASSUMPTION_FIELDS = frozenset({
    "base_revenue", "revenue_growth", "fcff_margin", "wacc",
    "terminal_growth", "net_debt", "shares_outstanding", "tax_rate",
})


# ---------------------------------------------------------------------------
# Graph definition — main workflow
# ---------------------------------------------------------------------------

graph = StateGraph(DCFState)

graph.add_node("normalize_input", normalize_input_node)
graph.add_node("cache_check", cache_check_node)
graph.add_node("assemble_evidence", assemble_evidence_node)
graph.add_node("semantic_synthesis", semantic_synthesis_node)
graph.add_node("formulate_thesis", formulate_thesis_node)
graph.add_node("propose_assumptions", propose_assumptions_node)
graph.add_node("scenario_generator", scenario_generator_node)
graph.add_node("review_assumptions", review_assumptions_node)
graph.add_node("coherence_gate", coherence_gate_node)
graph.add_node("scenario_runner", scenario_runner_node)
graph.add_node("project_cashflows", project_cashflows_node)
graph.add_node("compute_valuation", compute_valuation_node)
graph.add_node("compute_market_signals", compute_market_signals_node)
graph.add_node("sensitivity", sensitivity_node)
graph.add_node("review_subgraph", run_review_subgraph)
graph.add_node("detect_divergences", detect_divergences_node)
graph.add_node("analysis", analysis_node)
graph.add_node("convergence_gate", convergence_gate_node)
graph.add_node("finalize", finalize_node)

graph.add_edge(START, "normalize_input")
graph.add_edge("normalize_input", "cache_check")
graph.add_conditional_edges(
    "cache_check",
    route_after_cache_check,
    {"assemble_evidence": "assemble_evidence", "formulate_thesis": "formulate_thesis"},
)
graph.add_edge("assemble_evidence", "semantic_synthesis")
graph.add_edge("semantic_synthesis", "formulate_thesis")
graph.add_edge("formulate_thesis", "propose_assumptions")
graph.add_edge("propose_assumptions", "scenario_generator")
graph.add_edge("scenario_generator", "review_assumptions")
graph.add_conditional_edges(
    "review_assumptions",
    route_after_assumptions,
    {"scenario_runner": "coherence_gate", END: END},
)
graph.add_edge("coherence_gate", "scenario_runner")
graph.add_edge("scenario_runner", "project_cashflows")
graph.add_edge("project_cashflows", "compute_valuation")
graph.add_edge("compute_valuation", "compute_market_signals")
graph.add_edge("compute_market_signals", "sensitivity")
graph.add_edge("sensitivity", "review_subgraph")
graph.add_conditional_edges(
    "review_subgraph",
    route_after_review,
    {"coherence_gate": "coherence_gate", "detect_divergences": "detect_divergences"},
)
graph.add_edge("detect_divergences", "analysis")
graph.add_edge("analysis", "convergence_gate")
graph.add_conditional_edges(
    "convergence_gate",
    route_after_convergence_gate,
    {"coherence_gate": "coherence_gate", "finalize": "finalize"},
)
graph.add_edge("finalize", END)

dcf_workflow_app = graph.compile(checkpointer=MemorySaver())

# Valuation-only graph — skips evidence/synthesis/memo (fast path after HITL approval).
_val_graph = StateGraph(DCFState)
_val_graph.add_node("normalize_input", normalize_input_node)
_val_graph.add_node("collect_market_data", collect_market_data_node)
_val_graph.add_node("coherence_gate", coherence_gate_node)
_val_graph.add_node("project_cashflows", project_cashflows_node)
_val_graph.add_node("compute_valuation", compute_valuation_node)
_val_graph.add_node("compute_market_signals", compute_market_signals_node)
_val_graph.add_node("sensitivity", sensitivity_node)
_val_graph.add_node("review_subgraph", run_review_subgraph)
_val_graph.add_node("detect_divergences", detect_divergences_node)
_val_graph.add_node("analysis", analysis_node)
_val_graph.add_node("convergence_gate", convergence_gate_node)
_val_graph.add_node("finalize", finalize_node)
_val_graph.add_edge(START, "normalize_input")
_val_graph.add_edge("normalize_input", "collect_market_data")
_val_graph.add_edge("collect_market_data", "coherence_gate")
_val_graph.add_edge("coherence_gate", "project_cashflows")
_val_graph.add_edge("project_cashflows", "compute_valuation")
_val_graph.add_edge("compute_valuation", "compute_market_signals")
_val_graph.add_edge("compute_market_signals", "sensitivity")
_val_graph.add_edge("sensitivity", "review_subgraph")
_val_graph.add_conditional_edges(
    "review_subgraph",
    route_after_review_val,
    {"coherence_gate": "coherence_gate", "detect_divergences": "detect_divergences"},
)
_val_graph.add_edge("detect_divergences", "analysis")
_val_graph.add_edge("analysis", "convergence_gate")
_val_graph.add_conditional_edges(
    "convergence_gate",
    route_after_convergence_gate_val,
    {"coherence_gate": "coherence_gate", "finalize": "finalize"},
)
_val_graph.add_edge("finalize", END)
dcf_valuation_app = _val_graph.compile()

# Scenario valuation graph — runs per scenario, no analysis loop.
_scenario_graph = StateGraph(DCFState)
_scenario_graph.add_node("normalize_input", normalize_input_node)
_scenario_graph.add_node("collect_market_data", collect_market_data_node)
_scenario_graph.add_node("coherence_gate", coherence_gate_node)
_scenario_graph.add_node("project_cashflows", project_cashflows_node)
_scenario_graph.add_node("compute_valuation", compute_valuation_node)
_scenario_graph.add_node("compute_market_signals", compute_market_signals_node)
_scenario_graph.add_node("sensitivity", sensitivity_node)
_scenario_graph.add_edge(START, "normalize_input")
_scenario_graph.add_edge("normalize_input", "collect_market_data")
_scenario_graph.add_edge("collect_market_data", "coherence_gate")
_scenario_graph.add_edge("coherence_gate", "project_cashflows")
_scenario_graph.add_edge("project_cashflows", "compute_valuation")
_scenario_graph.add_edge("compute_valuation", "compute_market_signals")
_scenario_graph.add_edge("compute_market_signals", "sensitivity")
_scenario_graph.add_edge("sensitivity", END)
dcf_scenario_val_app = _scenario_graph.compile()


# ---------------------------------------------------------------------------
# Public sync entrypoint
# ---------------------------------------------------------------------------

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
    containing the proposed assumptions and their provenance.

    When ``assumption_review_mode=False``, auto-approves and runs to
    completion, returning the full dcf_output.json payload.
    """
    overrides = assumption_overrides or {}
    # Fast path only when a prior HITL snapshot exists — ensures thesis and
    # company_state are always available (synthesis ran in the full workflow).
    # Without a snapshot, run the full workflow so evidence/synthesis execute.
    _has_hitl_snapshot = bool(get_dcf_hitl_payload())
    # Fast path: all explicit assumption fields supplied + no review needed.
    # A prior HITL snapshot is preferred (enriches provenance/memo) but NOT
    # required — if missing we just use simple user-provided provenance.
    # This ensures KG-triggered reruns (no snapshot in thread context) still
    # use the lightweight valuation graph instead of the full evidence+memo
    # workflow, which would exceed LangGraph's 25-step recursion limit.
    use_fast_path = (
        not assumption_review_mode
        and _ALL_ASSUMPTION_FIELDS.issubset(overrides.keys())
    )
    logger.info(
        "run_dcf_workflow_sync: ticker=%s review_mode=%s fast_path=%s "
        "has_snapshot=%s override_keys=%s",
        ticker, assumption_review_mode, use_fast_path,
        _has_hitl_snapshot, sorted(overrides.keys()),
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

    # 50-step limit: full workflow = ~18 steps/pass × up to 2 convergence
    # retries = 36 steps; fast-path = ~11 steps × 2 = 22. Default 25 is too
    # tight for the full workflow when convergence_gate loops.
    config = {"configurable": {"thread_id": get_run_dir().name}, "recursion_limit": 50}

    try:
        if use_fast_path:
            initial_state["assumptions"] = {k: float(v) for k, v in overrides.items()}
            initial_state["assumptions_approved"] = True
            hitl = get_dcf_hitl_payload()
            if hitl:
                original_assumptions = hitl.get("assumptions") or {}
                provenance = merge_hitl_provenance(
                    hitl.get("assumption_provenance") or {},
                    overrides,
                    original_assumptions,
                )
                initial_state["assumption_provenance"] = provenance
                apply_hitl_snapshot(initial_state, hitl)
                if not initial_state.get("assumption_memo"):
                    reconstructed = reconstruct_memo_from_provenance(
                        provenance, initial_state["assumptions"],
                    )
                    if reconstructed:
                        initial_state["assumption_memo"] = reconstructed
            else:
                initial_state["assumption_provenance"] = {
                    k: {
                        "source": "user_provided",
                        "evidence": "User-provided assumption value.",
                        "confidence": 1.0,
                    }
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
        snapshot = build_hitl_snapshot(interrupt_payload)
        return {
            "__dcf_hitl__": True,
            **snapshot,
            "memo_proposals": memo_proposals,
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

    result_path = result.get("result_path")
    if not result_path:
        # Graph was interrupted (checkpointer swallowed GraphInterrupt).
        if result.get("assumptions"):
            assumptions = result.get("assumptions") or {}
            provenance = result.get("assumption_provenance") or {}
            if assumption_review_mode:
                logger.info("DCF interrupted for review ticker=%s", ticker)
                memo_proposals = _build_memo_proposals(result)
                snapshot = build_hitl_snapshot(result)
                return {
                    "__dcf_hitl__": True,
                    **snapshot,
                    "memo_proposals": memo_proposals,
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

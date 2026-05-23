"""Review subgraph gateway — boundary crossing + adjustment application."""

from __future__ import annotations

import json
import logging
from typing import Any

from .activity import emit_step
from .review_graph import build_deterministic_flags, review_dcf_app
from .state import DCFState

logger = logging.getLogger(__name__)


_MAX_REVIEW_ITERATIONS = 2


def run_review_subgraph(state: DCFState) -> dict:
    """Gateway: project DCFState → ReviewState, invoke review subgraph, apply results.

    Convergence short-circuit:
      - Skips the subgraph if iteration > 0 AND delta_pct < 5% (already converged).
      - Enforces _MAX_REVIEW_ITERATIONS hard cap.

    Boundary crossing (one-way snapshot, structured return):
      - ReviewState receives a snapshot of evidence, thesis, memo, scenarios.
      - Valuation output is intentionally NOT passed to prevent backward anchoring.
      - Adjustments are applied here in Python after the subgraph returns.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state.get("ticker", "?")
    iteration = state.get("analysis_iteration", 0)
    emit_step("review_subgraph", "start", parent_step_id)

    prev_val = state.get("previous_valuation") or {}
    valuation = state.get("valuation") or {}
    prev_implied = prev_val.get("implied_share_price", 0)
    curr_implied = valuation.get("implied_share_price", 0)
    delta_pct = (
        abs((curr_implied - prev_implied) / max(prev_implied, 1)) * 100
        if prev_implied and curr_implied else float("inf")
    )
    converged = delta_pct < 5.0 and iteration > 0

    if converged or iteration >= _MAX_REVIEW_ITERATIONS:
        reason = (
            f"converged (delta={delta_pct:.1f}%)" if converged
            else f"max review iterations reached ({iteration}/{_MAX_REVIEW_ITERATIONS})"
        )
        emit_step(
            "review_subgraph", "complete", parent_step_id,
            {"summary_line": f"Review skipped: {reason}", "should_stop": True},
        )
        critique = {
            "iteration": iteration,
            "flags": [],
            "stop_reason": reason,
            "should_refine": False,
        }
        return {
            "critique": critique,
            "analysis_iteration": iteration + 1,
            "previous_valuation": {
                "implied_share_price": curr_implied,
                "enterprise_value": valuation.get("enterprise_value"),
            },
        }

    initial_assumptions_snapshot: dict[str, Any] = state.get("initial_assumptions") or {}
    if iteration == 0 and not initial_assumptions_snapshot:
        initial_assumptions_snapshot = {
            "base": dict(state.get("assumptions") or {}),
            "scenarios": {
                s.get("name", f"sc_{i}"): dict(s.get("assumptions") or {})
                for i, s in enumerate(state.get("scenarios") or [])
            },
        }

    quality_flags = build_deterministic_flags(
        assumptions=state.get("assumptions") or {},
        valuation=valuation,
        wacc_components=state.get("wacc_components") or {},
        wacc_sanity=state.get("wacc_sanity"),
        confidence_breakdown=state.get("confidence_breakdown"),
        confidence_label=state.get("confidence_label", "medium"),
        sensitivity_table=state.get("sensitivity_table") or [],
        thesis=state.get("thesis"),
    )

    review_state = {
        "ticker": ticker,
        "parent_step_id": parent_step_id,
        "evidence_pack": state.get("evidence_pack") or {},
        "implied_growth": state.get("implied_growth"),
        "implied_margin": state.get("implied_margin"),
        "wacc_sanity": state.get("wacc_sanity"),
        "company_state": state.get("company_state"),
        "thesis": state.get("thesis"),
        "assumption_memo": state.get("assumption_memo"),
        "current_assumptions": state.get("assumptions") or {},
        "scenarios": state.get("scenarios") or [],
        "quality_flags": quality_flags,
        "assumption_history": state.get("assumption_history") or [],
        "review_iteration": iteration,
        "findings": None,
        "suggested_adjustments": None,
        "review_summary": "",
        "should_stop": False,
    }

    result = review_dcf_app.invoke(review_state)

    should_stop: bool = result.get("should_stop", True)
    adjustments: dict[str, dict[str, float]] = result.get("suggested_adjustments") or {}
    review_summary: str = result.get("review_summary", "")
    findings = result.get("findings")

    logger.info(
        "DCF run_review_subgraph ticker=%s iteration=%d should_stop=%s "
        "adjustments=%s",
        ticker, iteration, should_stop,
        json.dumps({sc: list(f.keys()) for sc, f in adjustments.items() if f}, ensure_ascii=False),
    )

    new_assumptions = dict(state.get("assumptions") or {})
    new_scenarios = [dict(s) for s in (state.get("scenarios") or [])]

    changes: list[str] = []

    base_deltas = adjustments.get("base") or {}
    for field, delta in base_deltas.items():
        if field in new_assumptions:
            old = new_assumptions[field]
            new_assumptions[field] = round(old + delta, 6)
            changes.append(f"base.{field}: {old:.4f} → {new_assumptions[field]:.4f}")

    for i, scenario in enumerate(new_scenarios):
        sc_name = scenario.get("name", "")
        sc_deltas = adjustments.get(sc_name) or {}
        if not sc_deltas:
            continue
        sc_assumptions = dict(scenario.get("assumptions") or {})
        for field, delta in sc_deltas.items():
            if field in sc_assumptions:
                old = sc_assumptions[field]
                sc_assumptions[field] = round(old + delta, 6)
                changes.append(f"{sc_name}.{field}: {old:.4f} → {sc_assumptions[field]:.4f}")
        new_scenarios[i] = {**scenario, "assumptions": sc_assumptions}

    history_record = {
        "iteration": iteration,
        "adjustments": adjustments,
        "findings_summary": review_summary,
        "changes": changes,
    }
    new_history = list(state.get("assumption_history") or []) + [history_record]

    all_findings_list: list[dict] = []
    if findings:
        from .review_state import ReviewFindings  # local import to avoid circular
        if isinstance(findings, ReviewFindings):
            from .review_graph import _all_findings
            all_findings_list = [f.model_dump() for f in _all_findings(findings)]

    critique = {
        "iteration": iteration,
        "flags": quality_flags,
        "review_summary": review_summary,
        "findings": all_findings_list,
        "changes": changes,
        "should_refine": not should_stop,
        "stop_reason": "" if not should_stop else review_summary,
    }

    emit_step(
        "review_subgraph", "complete", parent_step_id,
        {
            "summary_line": f"Review iter {iteration + 1}: {len(changes)} adjustments, should_stop={should_stop}",
            "changes": changes,
            "should_stop": should_stop,
            "review_summary": review_summary[:200],
        },
    )

    return {
        "assumptions": new_assumptions,
        "scenarios": new_scenarios,
        "assumption_history": new_history,
        "critique": critique,
        "analysis_iteration": iteration + 1,
        "previous_valuation": {
            "implied_share_price": curr_implied,
            "enterprise_value": valuation.get("enterprise_value"),
        },
        "initial_assumptions": initial_assumptions_snapshot,
    }


def route_after_review_val(state: DCFState) -> str:
    """Fast-path router: re-enter project_cashflows or finalize."""
    critique = state.get("critique") or {}
    should_refine = critique.get("should_refine", False)
    dest = "project_cashflows" if should_refine else "finalize"
    logger.info("DCF route_after_review_val should_refine=%s → %s", should_refine, dest)
    return dest


def route_after_review(state: DCFState) -> str:
    """Route to scenario_runner (another pass) or finalize."""
    critique = state.get("critique") or {}
    should_refine = critique.get("should_refine", False)
    dest = "scenario_runner" if should_refine else "finalize"
    logger.info("DCF route_after_review should_refine=%s → %s", should_refine, dest)
    return dest

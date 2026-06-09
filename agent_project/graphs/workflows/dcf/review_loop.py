"""Review subgraph gateway — boundary crossing + adjustment application."""

from __future__ import annotations

import json
import logging
from typing import Any

from .activity import emit_step
from .review_graph import build_deterministic_flags, review_dcf_app
from .state import DCFState, clip_to_field_range
from .wacc import clip_wacc_to_profile_band, append_wacc_stack_delta

logger = logging.getLogger(__name__)


_MAX_REVIEW_ITERATIONS = 2


def _apply_bounded_delta(
    assumptions: dict[str, float],
    field: str,
    delta: float,
    *,
    profile: str,
    provenance: dict[str, dict[str, Any]],
) -> float | None:
    """Apply a review delta with profile-aware WACC clipping."""
    if field not in assumptions:
        return None
    old = float(assumptions[field])
    candidate = round(old + float(delta), 6)
    if field == "wacc":
        wacc_prov = provenance.get("wacc") or {}
        user_wacc = wacc_prov.get("source") in {
            "user_override", "user_provided", "user_edited",
        } or bool(wacc_prov.get("user_edited"))
        clipped, _ = clip_wacc_to_profile_band(
            candidate,
            profile=profile,
            allow_override=user_wacc,
        )
        candidate = round(clipped, 6)
    else:
        bounded = clip_to_field_range(field, candidate)
        if bounded is None:
            return None
        candidate = round(float(bounded), 6)
    assumptions[field] = candidate
    return candidate


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
            f"Reconciliation stable across iterations (Δ={delta_pct:.1f}%)" if converged
            else (
                f"Further iterations unlikely to materially reduce reconciliation gaps "
                f"(reviewed {iteration}/{_MAX_REVIEW_ITERATIONS} passes)."
            )
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
    severity_score: float = float(result.get("severity_score", 0.0) or 0.0)
    change_records: list[dict[str, Any]] = result.get("change_records") or []

    # Issue #6: TRUE convergence — measure whether finding severity actually
    # dropped, not just whether the LLM stopped emitting edits. Compare this
    # pass's severity to the previous iteration's.
    from .review_graph import _MIN_SEVERITY_IMPROVEMENT, _SEVERITY_FLOOR  # noqa: PLC0415

    history = state.get("assumption_history") or []
    prev_severity = (
        float(history[-1].get("severity_score", 0.0) or 0.0) if history else None
    )
    severity_history = [
        float(h.get("severity_score", 0.0) or 0.0) for h in history
    ] + [severity_score]
    severity_converged = severity_score < _SEVERITY_FLOOR or (
        prev_severity is not None
        and iteration > 0
        and (prev_severity - severity_score) < _MIN_SEVERITY_IMPROVEMENT
    )
    if severity_converged and not should_stop:
        should_stop = True
        review_summary = (
            (review_summary + " ") if review_summary else ""
        ) + (
            f"Converged on severity: {' → '.join(f'{s:.1f}' for s in severity_history)} "
            f"(floor {_SEVERITY_FLOOR}, min improvement {_MIN_SEVERITY_IMPROVEMENT})."
        )

    logger.info(
        "DCF run_review_subgraph ticker=%s iteration=%d should_stop=%s severity=%.2f "
        "trajectory=%s adjustments=%s",
        ticker, iteration, should_stop, severity_score,
        [round(s, 1) for s in severity_history],
        json.dumps({sc: list(f.keys()) for sc, f in adjustments.items() if f}, ensure_ascii=False),
    )

    new_assumptions = dict(state.get("assumptions") or {})
    new_scenarios = [dict(s) for s in (state.get("scenarios") or [])]
    profile = state.get("profile") or "default"
    provenance = dict(state.get("assumption_provenance") or {})
    wacc_components = dict(state.get("wacc_components") or {})

    changes: list[str] = []

    base_deltas = adjustments.get("base") or {}
    for field, delta in base_deltas.items():
        old = new_assumptions.get(field)
        if old is None:
            continue
        new_val = _apply_bounded_delta(
            new_assumptions,
            field,
            float(delta),
            profile=profile,
            provenance=provenance,
        )
        if new_val is not None:
            changes.append(f"base.{field}: {old:.4f} → {new_val:.4f}")
            if field == "wacc":
                wacc_components = append_wacc_stack_delta(
                    wacc_components,
                    old_wacc=float(old),
                    new_wacc=float(new_val),
                    label=f"Review-loop adjustment (pass {iteration + 1})",
                    source="review_loop",
                )
                wacc_prov = dict(provenance.get("wacc") or {})
                existing_evidence = str(wacc_prov.get("evidence") or "")
                delta = float(new_val) - float(old)
                wacc_prov["evidence"] = (
                    existing_evidence
                    + (" | " if existing_evidence else "")
                    + f"review-loop adjustment {delta:+.2%} (pass {iteration + 1})"
                )
                wacc_prov["review_adjusted"] = True
                provenance["wacc"] = wacc_prov

    for i, scenario in enumerate(new_scenarios):
        sc_name = scenario.get("name", "")
        sc_deltas = adjustments.get(sc_name) or {}
        if not sc_deltas:
            continue
        sc_assumptions = dict(scenario.get("assumptions") or {})
        for field, delta in sc_deltas.items():
            old = sc_assumptions.get(field)
            if old is None:
                continue
            new_val = _apply_bounded_delta(
                sc_assumptions,
                field,
                float(delta),
                profile=profile,
                provenance=provenance,
            )
            if new_val is not None:
                changes.append(f"{sc_name}.{field}: {old:.4f} → {new_val:.4f}")
        new_scenarios[i] = {**scenario, "assumptions": sc_assumptions}

    history_record = {
        "iteration": iteration,
        "adjustments": adjustments,
        "findings_summary": review_summary,
        "changes": changes,
        "severity_score": severity_score,
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
        # Issue #3: structured finding → adjustment → reasoning → expected_effect.
        "change_records": change_records,
        # Issue #6: severity trajectory for true-convergence display.
        "severity_score": severity_score,
        "severity_history": severity_history,
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
        "assumption_provenance": provenance,
        "wacc_components": wacc_components,
        "critique": critique,
        "analysis_iteration": iteration + 1,
        "previous_valuation": {
            "implied_share_price": curr_implied,
            "enterprise_value": valuation.get("enterprise_value"),
        },
        "initial_assumptions": initial_assumptions_snapshot,
    }


def route_after_review_val(state: DCFState) -> str:
    """Fast-path router: re-project cash flows after review, or proceed to divergences."""
    critique = state.get("critique") or {}
    should_refine = critique.get("should_refine", False)
    dest = "coherence_gate" if should_refine else "detect_divergences"
    logger.info("DCF route_after_review_val should_refine=%s → %s", should_refine, dest)
    return dest


def route_after_review(state: DCFState) -> str:
    """Route to coherence gate (re-valuation) after review, or proceed to divergences."""
    critique = state.get("critique") or {}
    should_refine = critique.get("should_refine", False)
    dest = "coherence_gate" if should_refine else "detect_divergences"
    logger.info("DCF route_after_review should_refine=%s → %s", should_refine, dest)
    return dest

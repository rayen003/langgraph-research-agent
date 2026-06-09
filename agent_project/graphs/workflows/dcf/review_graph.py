"""DCF review subgraph — adversarial cross-check of scenarios + assumptions.

Architecture::

    START → review_deep_dive → synthesize_adjustments → END

This graph is isolated from DCFState.  It receives a one-way snapshot
(ReviewState) and returns structured adjustments.  The gateway function
``run_review_subgraph`` in graph.py owns the boundary crossing.

Design principles:
  1. Adversarial framing  — LLM's ONLY job is to find problems, never to fix.
  2. No valuation output  — prevents backward anchoring from implied price.
  3. Evidence cross-check — LLM must cite specific evidence_refs from the pack.
  4. Deterministic fixes  — Python rule table applies deltas; LLM never mutates.
  5. Convergence damping  — same-direction repeat adjustments are halved.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .activity import emit_review_substep
from .review_state import ReviewFindings, ReviewState, ScenarioFinding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REVIEW_LLM = ChatOpenAI(
    model=os.getenv("DCF_REVIEW_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=90,
)

# Maximum absolute delta the synthesis node can apply per field per iteration.
# Tighter than the old _CRITIQUE_ADJUSTMENT_BOUNDS — we iterate, so each pass
# can be smaller and more targeted.
_MAX_DELTA: dict[str, float] = {
    "revenue_growth": 0.020,
    "fcff_margin": 0.015,
    "terminal_growth": 0.003,
    "tax_rate": 0.020,
    "wacc": 0.010,
}

# Issue #6: severity weights for true-convergence tracking. A review iteration's
# severity score = Σ weight(severity)·confidence over all findings. Convergence
# is then measured by the DROP in severity across iterations, not merely "no more
# edits were generated".
_SEVERITY_WEIGHT: dict[str, float] = {"high": 3.0, "medium": 2.0, "low": 1.0}
# Stop when residual severity is trivially small …
_SEVERITY_FLOOR: float = 2.0
# … or when an extra pass barely reduces it (diminishing returns).
_MIN_SEVERITY_IMPROVEMENT: float = 1.5


def _severity_score(findings: "ReviewFindings | None") -> float:
    """Aggregate finding severity (weight × confidence). 0.0 when no findings."""
    if findings is None:
        return 0.0
    all_f = (
        findings.evidence_memo_findings
        + findings.thesis_assumption_findings
        + findings.consistency_findings
        + findings.scenario_distinguishability_findings
    )
    return round(
        sum(_SEVERITY_WEIGHT.get(f.severity, 1.0) * float(f.confidence) for f in all_f),
        3,
    )


# Hard clamps — absolute floor/ceiling regardless of adjustments.
_FIELD_CLAMP: dict[str, tuple[float, float]] = {
    "revenue_growth":  (-0.50, 0.75),
    "fcff_margin":     (-0.25, 0.75),
    "terminal_growth": (-0.02, 0.06),
    "wacc":            (0.03,  0.25),
    "tax_rate":        (0.00,  0.45),
}

# Only act on high-confidence, high-severity findings.
_MIN_CONFIDENCE: float = 0.60
_MIN_SEVERITY = "high"

# Minimum absolute delta to count as "meaningful" for convergence check.
_MIN_MEANINGFUL_DELTA: float = 0.005  # 0.5 %


# ---------------------------------------------------------------------------
# Deterministic flag builder (moved here from graph.py)
# ---------------------------------------------------------------------------


def build_deterministic_flags(
    assumptions: dict[str, float],
    valuation: dict[str, Any],
    wacc_components: dict[str, Any],
    wacc_sanity: dict[str, Any] | None,
    confidence_breakdown: dict[str, Any] | None,
    confidence_label: str,
    sensitivity_table: list[dict[str, Any]],
    thesis: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract structured signals from DCF output for the review node.

    Identical logic to the old ``_build_deterministic_flags`` in graph.py;
    lives here so the review subgraph owns it end-to-end.
    """
    flags: list[dict[str, Any]] = []

    # Terminal value weight
    tv = (valuation.get("terminal_pv") or 0)
    ev = (valuation.get("enterprise_value") or 1) or 1
    tv_pct = tv / ev
    flags.append({
        "signal": "terminal_weight",
        "value": round(tv_pct * 100, 1),
        "threshold": "> 70% is concerning",
        "severity": "severe" if tv_pct > 0.75 else ("warning" if tv_pct > 0.70 else "ok"),
    })

    # Implied vs spot gap
    implied = valuation.get("implied_share_price") or 0
    spot = (valuation.get("current_price") or 1) or 1
    gap_pct = round(((implied / spot) - 1) * 100, 1) if spot else 0
    flags.append({
        "signal": "implied_vs_spot",
        "value": gap_pct,
        "threshold": "> ±50% is extreme",
        "severity": "severe" if abs(gap_pct) > 50 else ("warning" if abs(gap_pct) > 30 else "ok"),
    })

    # WACC sanity gap
    wacc_gap_bps = (wacc_sanity or {}).get("gap_bps") or 0
    flags.append({
        "signal": "wacc_sanity_gap",
        "value_bps": wacc_gap_bps,
        "threshold": "> 200 bps is concerning",
        "severity": "severe" if abs(wacc_gap_bps) > 200 else ("warning" if abs(wacc_gap_bps) > 100 else "ok"),
    })

    # WACC sensitivity swing
    if sensitivity_table:
        prices = [r.get("implied_share_price") or 0 for r in sensitivity_table if r.get("implied_share_price")]
        if len(prices) >= 3:
            mid = prices[len(prices) // 2] or 1
            swing = round(((max(prices) - min(prices)) / mid) * 100, 1)
            flags.append({
                "signal": "wacc_sensitivity",
                "value_pct": swing,
                "threshold": "> 30% swing is high sensitivity",
                "severity": "warning" if swing > 30 else "ok",
            })

    # Confidence
    flags.append({
        "signal": "confidence",
        "value": (confidence_breakdown or {}).get("label", confidence_label),
        "severity": "severe" if confidence_label == "low" else ("warning" if confidence_label == "medium" else "ok"),
    })

    # Terminal growth vs risk-free rate
    tg = assumptions.get("terminal_growth")
    rf = (wacc_components or {}).get("risk_free_rate", 0.045)
    if tg is not None:
        flags.append({
            "signal": "tgr_vs_rf",
            "value": round(tg * 100, 2),
            "threshold": f"TGR should not exceed Rf ({rf:.2%}) by more than 50 bps",
            "severity": "warning" if tg > rf + 0.005 else "ok",
        })

    # Thesis quality — fallback thesis means assumptions lack a narrative anchor,
    # which reduces credibility of the entire valuation.
    if (thesis or {}).get("_fallback"):
        flags.append({
            "signal": "thesis_fallback",
            "value": "fallback",
            "threshold": "Thesis should be grounded in evidence-specific narrative",
            "severity": "severe",
            "note": (
                "Investment thesis could not be formulated from available evidence. "
                "Assumptions may not reflect company-specific dynamics."
            ),
        })

    return flags


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _format_evidence_index(evidence_pack: dict[str, Any]) -> str:
    """Render evidence items as a compact lookup table for the reviewer."""
    items: list[dict[str, Any]] = evidence_pack.get("items") or []
    if not items:
        return "(no evidence items indexed)"
    lines: list[str] = []
    for item in items[:60]:  # cap to keep prompt bounded
        eid = item.get("evidence_id", "?")
        kind = item.get("kind", "?")
        if kind == "filing_excerpt":
            desc = (
                f"[{item.get('filing_type','')} {item.get('section','')} "
                f"{item.get('as_of','')}] {str(item.get('text',''))[:120]}"
            )
        elif kind == "structured_fundamental":
            desc = f"[fundamental] {item.get('source','')}: {item.get('field','')}={item.get('value','?')}"
        elif kind == "web_excerpt":
            desc = f"[web] {item.get('title','?')[:60]}: {str(item.get('text',''))[:80]}"
        elif kind == "market_data":
            desc = f"[market] {item.get('field','?')}={item.get('value','?')}"
        else:
            desc = f"[{kind}] {str(item.get('text', item.get('value', '')))[:100]}"
        lines.append(f"{eid}: {desc}")
    if len(items) > 60:
        lines.append(f"... ({len(items) - 60} more items not shown)")
    return "\n".join(lines)


def _format_scenarios(scenarios: list[dict[str, Any]]) -> str:
    """Render scenarios as a compact comparison table."""
    if not scenarios:
        return "(no scenarios)"
    lines: list[str] = []
    fields = ["revenue_growth", "fcff_margin", "wacc", "terminal_growth", "tax_rate"]
    header = "Scenario | Prob | " + " | ".join(fields)
    lines.append(header)
    lines.append("-" * len(header))
    for s in scenarios:
        name = s.get("name", "?")
        prob = s.get("probability", 0)
        ass = s.get("assumptions") or {}
        vals = " | ".join(f"{ass.get(f, 'N/A'):.3f}" if isinstance(ass.get(f), (int, float)) else "N/A" for f in fields)
        lines.append(f"{name} | {prob:.0%} | {vals}")
        rat = s.get("rationale", "")
        if rat:
            lines.append(f"  rationale: {rat[:120]}")
    return "\n".join(lines)


def _format_assumption_history(history: list[dict[str, Any]]) -> str:
    """Show prior iterations so the reviewer doesn't re-flag resolved issues."""
    if not history:
        return "(first iteration — no prior adjustments)"
    lines: list[str] = []
    for rec in history[-3:]:  # last 3 iterations
        iteration = rec.get("iteration", "?")
        adj = rec.get("adjustments") or {}
        lines.append(f"Iteration {iteration}: {json.dumps(adj, ensure_ascii=False)}")
        findings_summary = rec.get("findings_summary", "")
        if findings_summary:
            lines.append(f"  Summary: {findings_summary}")
    return "\n".join(lines)


def _format_memo_refs(assumption_memo: dict[str, Any] | None) -> str:
    """List the evidence_refs the memo LLM cited per field."""
    if not assumption_memo:
        return "(no memo)"
    proposals = assumption_memo.get("proposals") or []
    if not proposals:
        return "(memo has no proposals)"
    lines: list[str] = []
    for p in proposals:
        field = p.get("field", "?")
        refs = p.get("evidence_refs") or []
        conf = p.get("confidence", 0.5)
        rat = p.get("rationale", "")[:120]
        lines.append(f"{field} (conf={conf:.0%}): refs={refs!r} | rationale: {rat}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node 1 — review_deep_dive_node  (LLM, adversarial)
# ---------------------------------------------------------------------------


def review_deep_dive_node(state: ReviewState) -> dict:
    """Adversarial reviewer — finds problems only, never suggests fixes.

    Intentionally receives NO valuation output (implied_share_price,
    enterprise_value) to prevent backward anchoring from the price target.
    The reviewer cross-checks:
        1. Evidence ↔ Memo  — does the memo's evidence_refs actually support
                               each assumption?
        2. Thesis ↔ Assumptions — are assumptions consistent with the stated
                               bull/bear thesis?
        3. Internal consistency — TGR vs Rf, terminal weight, WACC vs CAPM.
        4. Scenario distinguishability — are bear/bull meaningfully different
                               from base?
    """
    ticker = state["ticker"]
    iteration = state.get("review_iteration", 0)
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    logger.info("DCF review_deep_dive ticker=%s iteration=%d", ticker, iteration)
    emit_review_substep("review_deep_dive", "start", parent_step_id, {
        "summary_line": f"Adversarial review — 4 layers, iteration {iteration + 1}",
        "iteration": iteration,
    })

    evidence_pack = state.get("evidence_pack") or {}
    company_state = state.get("company_state") or {}
    thesis = state.get("thesis") or {}
    assumption_memo = state.get("assumption_memo")
    current_assumptions = state.get("current_assumptions") or {}
    scenarios = state.get("scenarios") or []
    quality_flags = state.get("quality_flags") or []
    assumption_history = state.get("assumption_history") or []

    # Build the prompt — deliberately verbose to give LLM full context
    evidence_index = _format_evidence_index(evidence_pack)
    scenario_table = _format_scenarios(scenarios)
    memo_refs = _format_memo_refs(assumption_memo)
    history_text = _format_assumption_history(assumption_history)

    quality_flag_text = "\n".join(
        f"  [{f.get('severity','?').upper()}] {f.get('signal','?')}: {f.get('value', f.get('value_bps', f.get('value_pct', '?')))}"
        for f in quality_flags
    ) or "(none)"

    # Market-implied signals
    implied_growth = state.get("implied_growth")
    implied_margin = state.get("implied_margin")
    wacc_sanity = state.get("wacc_sanity") or {}
    model_wacc = current_assumptions.get("wacc", 0)
    model_growth = current_assumptions.get("revenue_growth", 0)
    model_margin = current_assumptions.get("fcff_margin", 0)
    market_signal_text = f"""  Implied WACC: {wacc_sanity.get('implied_wacc', 'N/A')} (vs model {model_wacc:.1%}, gap {wacc_sanity.get('gap_bps','?')}bps)
  Implied growth: {f'{implied_growth:.1%}' if implied_growth is not None else 'N/A'} (vs model {model_growth:.1%})
  Implied margin: {f'{implied_margin:.1%}' if implied_margin is not None else 'N/A'} (vs model {model_margin:.1%})
  → These are NOT targets. They are QUESTIONS.
  → What narrative justifies the gap?
  → If evidence supports stronger fundamentals, the base case may be too conservative.
  → If evidence does NOT support the gap, the market may be overpaying."""

    # Thesis summary
    bull = thesis.get("bull_thesis", "")
    bear = thesis.get("bear_thesis", "")
    drivers = thesis.get("key_drivers") or []
    driver_text = "\n".join(
        f"  - {d.get('driver','?')} ({d.get('direction','?')}, {d.get('conviction','?')} conviction)"
        for d in drivers[:5]
    ) or "  (none)"

    # Company context (no valuation numbers)
    co_lines: list[str] = []
    for key, label in [
        ("growth_outlook", "Growth outlook"),
        ("margin_trend", "Margin trend"),
        ("competitive_position", "Competitive position"),
    ]:
        v = company_state.get(key)
        if v:
            co_lines.append(f"  {label}: {v}")
    company_context = "\n".join(co_lines) or "  (not available)"

    prompt = f"""You are a senior DCF analyst conducting an adversarial review of a junior analyst's work on **{ticker}** (iteration {iteration + 1}).

YOUR ONLY JOB IS TO FIND PROBLEMS. Do not suggest fixes, do not validate good work, do not be constructive.
Be a skeptic. Assume the junior analyst made anchoring errors and wishful assumptions.

---

## Evidence Index (what the raw evidence actually says)
{evidence_index}

---

## Memo Evidence References (what the junior CLAIMS the evidence supports)
{memo_refs}

---

## Investment Thesis
Bull case: {bull}
Bear case: {bear}
Key drivers:
{driver_text}

---

## Company Context
{company_context}

---

## Current Base Assumptions
{json.dumps(current_assumptions, ensure_ascii=False, indent=2)}

---

## All Scenarios (bear / base / bull)
{scenario_table}

---

## Deterministic Quality Flags
{quality_flag_text}

---

## Market-Implied Expectations
{market_signal_text}

---

## Prior Adjustment History (already addressed — do NOT re-flag these)
{history_text}

---

## Your Review Tasks

### Layer 0 — Assumption Anchoring
For each assumption field (revenue_growth, fcff_margin, terminal_growth, wacc, tax_rate):
  - Is it explicitly supported by cited evidence_refs that appear in the Evidence Index?
  - Is it derived from the investment thesis?
  - Or is it a heuristic / round number / profile default with no clear backing?
Flag any assumption NOT grounded in evidence or thesis with `is_unanchored=True`.
If > 2 assumptions are unanchored, the model is guessing — this MUST result in should_stop=False.

### Layer 1 — Evidence ↔ Memo
For each assumption in the memo, check:
  - Are the cited evidence_refs real IDs that appear in the Evidence Index?
  - Does the evidence actually support the claimed direction (e.g., if memo says
    revenue_growth=0.12 citing evidence_id_X, does that item support 12% growth)?
  - Flag any refs that are missing, misquoted, or cited in a misleading direction.

### Layer 2 — Thesis ↔ Assumptions
Cross-check the investment thesis against the scenario assumptions:
  - Bear scenario should use assumptions consistent with the bear thesis.
  - Bull scenario should use assumptions consistent with the bull thesis.
  - Flag misalignments (e.g., bull thesis says "margin expansion" but bull
    fcff_margin < base fcff_margin).

### Layer 3 — Internal Consistency
  - Terminal growth rate vs risk-free rate (TGR > Rf + 50bps is a red flag).
  - Margin trajectory (FCFF margin trend vs revenue growth — do they make sense
    together given the company's competitive position?).
  - WACC vs capital structure (if high leverage, WACC should be higher).

### Layer 4 — Scenario Distinguishability
  - Bear should be meaningfully worse than base (not just slightly lower growth).
  - Bull should reflect genuine upside, not just +1% on every field.
  - Probabilities should sum to 1.0 and reflect genuine conviction.

---

## Output Format
Return ONLY valid JSON matching the ReviewFindings schema.
- evidence_memo_findings: Layer 1 issues
- thesis_assumption_findings: Layer 2 issues
- consistency_findings: Layer 3 issues
- scenario_distinguishability_findings: Layer 4 issues
- anchoring_flags: List of strings for suspiciously round numbers or prior-proximity
- should_stop: true ONLY if there are ZERO high-severity findings with confidence ≥ 0.60
- stop_reasoning: explain why you're stopping (if should_stop=true)

For each ScenarioFinding:
  - scenario: "bear" | "base" | "bull" | "all"
  - field: the specific assumption field
  - direction: "higher" | "lower" | "neutral"
  - confidence: 0.0–1.0 (how certain are you this is a real problem?)
  - severity: "high" | "medium" | "low"
  - layer: "evidence_memo" | "thesis_assumptions" | "consistency" | "scenario_distinguishability"
  - reasoning: ONE sentence citing a specific evidence ref or thesis element
  - evidence_refs: list of evidence IDs that support your finding

Only include findings with confidence ≥ 0.40. Omit trivial issues.
"""

    try:
        structured_llm = _REVIEW_LLM.with_structured_output(ReviewFindings)
        findings: ReviewFindings = structured_llm.invoke(prompt)
        all_findings_count = (
            len(findings.evidence_memo_findings)
            + len(findings.thesis_assumption_findings)
            + len(findings.consistency_findings)
            + len(findings.scenario_distinguishability_findings)
        )
        high_count = sum(
            1 for f in (
                findings.evidence_memo_findings
                + findings.thesis_assumption_findings
                + findings.consistency_findings
                + findings.scenario_distinguishability_findings
            )
            if f.severity == "high" and f.confidence >= _MIN_CONFIDENCE
        )
        logger.info(
            "DCF review_deep_dive ticker=%s iteration=%d total=%d high=%d should_stop=%s",
            ticker, iteration, all_findings_count, high_count, findings.should_stop,
        )
        emit_review_substep("review_deep_dive", "complete", parent_step_id, {
            "summary_line": f"{all_findings_count} findings ({high_count} high-severity)",
            "iteration": iteration,
            "total_findings": all_findings_count,
            "high_findings": high_count,
            "should_stop": findings.should_stop,
            "anchoring_flags": findings.anchoring_flags,
            "evidence_memo_count": len(findings.evidence_memo_findings),
            "thesis_assumption_count": len(findings.thesis_assumption_findings),
            "consistency_count": len(findings.consistency_findings),
            "distinguishability_count": len(findings.scenario_distinguishability_findings),
        })
    except Exception:
        logger.warning("Review LLM failed for %s iteration=%d", ticker, iteration, exc_info=True)
        findings = ReviewFindings(
            evidence_memo_findings=[],
            thesis_assumption_findings=[],
            consistency_findings=[],
            scenario_distinguishability_findings=[],
            anchoring_flags=[],
            should_stop=True,
            stop_reasoning="Review LLM unavailable — skipping adversarial review.",
        )
        emit_review_substep("review_deep_dive", "complete", parent_step_id, {
            "summary_line": "Review LLM unavailable — skipped",
            "iteration": iteration,
            "total_findings": 0,
            "high_findings": 0,
            "should_stop": True,
        })

    return {"findings": findings, "severity_score": _severity_score(findings)}


# ---------------------------------------------------------------------------
# Node 2 — synthesize_adjustments_node  (pure Python, deterministic)
# ---------------------------------------------------------------------------


# Issue #3: deterministic valuation-direction of each assumption move, so every
# adjustment states its expected effect on the implied price.
_EFFECT_DIRECTION: dict[str, str] = {
    "revenue_growth": "raises",
    "fcff_margin": "raises",
    "terminal_growth": "raises",
    "wacc": "lowers",      # higher discount rate → lower PV
    "tax_rate": "lowers",  # higher tax → lower after-tax FCFF
}


def _expected_effect(field: str, direction: str) -> str:
    """One-line expected effect of moving *field* in *direction* on implied price."""
    base = _EFFECT_DIRECTION.get(field, "changes")
    # If the assumption moves down, invert the price direction.
    price_dir = base
    if direction == "lower" and base in ("raises", "lowers"):
        price_dir = "lowers" if base == "raises" else "raises"
    why = {
        "revenue_growth": "explicit-horizon FCFF",
        "fcff_margin": "FCFF at every year",
        "terminal_growth": "terminal value",
        "wacc": "discounted present value of all cash flows",
        "tax_rate": "after-tax FCFF",
    }.get(field, "the valuation")
    return f"{direction.capitalize()} {field} {price_dir} implied price via {why}."


def _all_findings(findings: ReviewFindings) -> list[ScenarioFinding]:
    return (
        findings.evidence_memo_findings
        + findings.thesis_assumption_findings
        + findings.consistency_findings
        + findings.scenario_distinguishability_findings
    )


def _scenario_names_for(finding: ScenarioFinding, all_scenarios: list[str]) -> list[str]:
    if finding.scenario == "all":
        return all_scenarios
    return [finding.scenario]


def _last_iter_directions(
    assumption_history: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Return {(scenario, field): direction} from the most recent iteration."""
    if not assumption_history:
        return {}
    last = assumption_history[-1]
    adjustments = last.get("adjustments") or {}
    result: dict[tuple[str, str], str] = {}
    for scenario, fields in adjustments.items():
        for field, delta in fields.items():
            if isinstance(delta, (int, float)):
                result[(scenario, field)] = "higher" if delta > 0 else "lower"
    return result


def synthesize_adjustments_node(state: ReviewState) -> dict:
    """Convert ReviewFindings into bounded per-scenario deltas.

    Rules:
      1. Only act on findings with severity='high' AND confidence ≥ _MIN_CONFIDENCE.
      2. Delta magnitude = _MAX_DELTA[field] (full step per qualifying finding).
      3. Convergence damping: if same (scenario, field) was adjusted in the same
         direction last iteration, halve the delta.
      4. Hard-clamp result to _FIELD_CLAMP[field] bounds.
      5. should_stop = True when:
           a. findings.should_stop is already True (LLM says nothing left), OR
           b. no qualifying findings exist, OR
           c. all qualifying deltas are below _MIN_MEANINGFUL_DELTA.
    """
    findings: ReviewFindings | None = state.get("findings")
    scenarios = state.get("scenarios") or []
    assumption_history = state.get("assumption_history") or []
    current_assumptions = state.get("current_assumptions") or {}
    ticker = state.get("ticker", "?")
    iteration = state.get("review_iteration", 0)
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_review_substep("synthesize_adjustments", "start", parent_step_id, {
        "summary_line": "Computing bounded adjustments",
        "iteration": iteration,
    })

    if findings is None or findings.should_stop:
        reason = (findings.stop_reasoning if findings else "no findings object")
        logger.info("DCF synthesize_adjustments ticker=%s STOP: %s", ticker, reason)
        emit_review_substep("synthesize_adjustments", "complete", parent_step_id, {
            "summary_line": f"No adjustments — {reason[:80]}",
            "iteration": iteration,
            "should_stop": True,
            "changes": [],
        })
        return {
            "suggested_adjustments": {},
            "review_summary": reason,
            "should_stop": True,
        }

    scenario_names = [s.get("name", "") for s in scenarios if s.get("name")]
    if not scenario_names:
        scenario_names = ["base"]

    last_directions = _last_iter_directions(assumption_history)

    # --- Aggregate qualifying findings per (scenario, field) ---
    # direction votes: {"higher": count, "lower": count}
    votes: dict[tuple[str, str], dict[str, int]] = {}
    # Issue #3: keep the findings behind each (scenario, field) so the applied
    # adjustment can cite the specific finding that caused it.
    findings_by_key: dict[tuple[str, str], list[ScenarioFinding]] = {}

    for finding in _all_findings(findings):
        if finding.severity != "high":
            continue
        if finding.confidence < _MIN_CONFIDENCE:
            continue
        if finding.field not in _MAX_DELTA:
            continue
        for sc in _scenario_names_for(finding, scenario_names):
            key = (sc, finding.field)
            if key not in votes:
                votes[key] = {"higher": 0, "lower": 0}
            findings_by_key.setdefault(key, []).append(finding)
            if finding.direction in ("higher", "lower"):
                votes[key][finding.direction] += 1

    # --- Convert votes to deltas ---
    adjustments: dict[str, dict[str, float]] = {sc: {} for sc in scenario_names}
    change_records: list[dict[str, Any]] = []  # Issue #3
    meaningful_count = 0

    for (sc, field), vote_counts in votes.items():
        higher = vote_counts.get("higher", 0)
        lower = vote_counts.get("lower", 0)
        if higher == lower or (higher == 0 and lower == 0):
            continue  # tie or neutral — skip

        direction = "higher" if higher > lower else "lower"
        base_delta = _MAX_DELTA[field]
        sign = 1.0 if direction == "higher" else -1.0

        # Convergence damping — same direction as last iteration?
        last_dir = last_directions.get((sc, field))
        if last_dir == direction:
            base_delta *= 0.5  # halve repeated same-direction adjustment

        delta = round(sign * base_delta, 6)

        # Clamp to absolute field bounds
        lo, hi = _FIELD_CLAMP.get(field, (-1.0, 1.0))
        # We're adjusting from current value
        current_sc_val: float | None = None
        for s in scenarios:
            if s.get("name") == sc:
                current_sc_val = (s.get("assumptions") or {}).get(field)
                break
        if current_sc_val is None:
            current_sc_val = current_assumptions.get(field, 0.0)

        new_val = (current_sc_val or 0.0) + delta
        new_val_clamped = max(lo, min(hi, new_val))
        delta_clamped = round(new_val_clamped - (current_sc_val or 0.0), 6)

        if abs(delta_clamped) >= _MIN_MEANINGFUL_DELTA:
            adjustments[sc][field] = delta_clamped
            meaningful_count += 1
            # Issue #3: record finding → adjustment → reasoning → expected effect.
            driving = sorted(
                findings_by_key.get((sc, field), []),
                key=lambda f: float(f.confidence), reverse=True,
            )
            driver_finding = next(
                (f for f in driving if f.direction == direction), driving[0] if driving else None,
            )
            change_records.append({
                "scenario": sc,
                "field": field,
                "delta": delta_clamped,
                "direction": direction,
                "finding": driver_finding.reasoning if driver_finding else "",
                "reasoning": (
                    f"{field} moved {direction} by {abs(delta_clamped):.4g} to address: "
                    f"{driver_finding.reasoning if driver_finding else 'review finding'}"
                ),
                "expected_effect": _expected_effect(field, direction),
            })

    # --- Determine should_stop ---
    should_stop = meaningful_count == 0

    # --- Build summary ---
    all_f = _all_findings(findings)
    high_f = [f for f in all_f if f.severity == "high" and f.confidence >= _MIN_CONFIDENCE]
    anchoring = findings.anchoring_flags or []

    summary_parts: list[str] = [
        f"Review iteration {iteration + 1} for {ticker}.",
        f"Total findings: {len(all_f)} ({len(high_f)} high-confidence/high-severity).",
    ]
    if high_f:
        sample = high_f[0]
        summary_parts.append(
            f"Top finding: [{sample.scenario}] {sample.field} should be {sample.direction} — {sample.reasoning}"
        )
    if anchoring:
        summary_parts.append(f"Anchoring flags: {'; '.join(anchoring[:3])}")
    if meaningful_count > 0:
        applied = {sc: list(fields.keys()) for sc, fields in adjustments.items() if fields}
        summary_parts.append(f"Adjustments queued: {json.dumps(applied, ensure_ascii=False)}")
    else:
        summary_parts.append(
            "No meaningful adjustments — stopping." if findings.stop_reasoning == ""
            else findings.stop_reasoning
        )

    review_summary = " ".join(summary_parts)
    logger.info(
        "DCF synthesize_adjustments ticker=%s meaningful=%d should_stop=%s",
        ticker, meaningful_count, should_stop,
    )

    # Build flat changes list for the activity emit
    changes_flat: list[str] = []
    for sc, fields in adjustments.items():
        for field, delta in fields.items():
            direction = "↑" if delta > 0 else "↓"
            changes_flat.append(f"{sc}.{field} {direction}{abs(delta):.4f}")

    emit_review_substep("synthesize_adjustments", "complete", parent_step_id, {
        "summary_line": f"{meaningful_count} adjustments across {sum(1 for v in adjustments.values() if v)} scenarios",
        "iteration": iteration,
        "should_stop": should_stop,
        "meaningful_count": meaningful_count,
        "changes": changes_flat,
        "adjustments": adjustments,
    })

    return {
        "suggested_adjustments": adjustments,
        "review_summary": review_summary,
        "should_stop": should_stop,
        "change_records": change_records,
        "severity_score": state.get("severity_score", 0.0),
    }


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------

_review_graph = StateGraph(ReviewState)
_review_graph.add_node("review_deep_dive", review_deep_dive_node)
_review_graph.add_node("synthesize_adjustments", synthesize_adjustments_node)

_review_graph.add_edge(START, "review_deep_dive")
_review_graph.add_edge("review_deep_dive", "synthesize_adjustments")
_review_graph.add_edge("synthesize_adjustments", END)

review_dcf_app = _review_graph.compile()

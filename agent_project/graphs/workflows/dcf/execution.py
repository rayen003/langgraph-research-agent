"""Execution layer — thesis formation + per-scenario valuation runner."""

from __future__ import annotations

import json
import logging
import os

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from utils import get_run_dir

from .activity import emit_step
from .state import DCFState

logger = logging.getLogger(__name__)


_THESIS_LLM = ChatOpenAI(
    model=os.getenv("DCF_THESIS_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=60,
)


class KeyDriver(BaseModel):
    model_config = ConfigDict(extra='forbid')
    driver: str = Field(description="Name of the driver")
    direction: str = Field(description="positive, negative, or neutral")
    conviction: str = Field(description="high, medium, or low")


class ThesisOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    """Structured investment thesis from evidence + company synthesis."""
    bull_thesis: str = Field(description="Why the stock could outperform. 1-2 sentences.")
    bear_thesis: str = Field(description="Why the stock could underperform. 1-2 sentences.")
    key_drivers: list[KeyDriver] = Field(description="Key value drivers with direction and conviction.")
    narrative: str = Field(description="2-3 sentences tying the thesis together.")


def formulate_thesis_node(state: DCFState) -> dict:
    """Produce a structured investment thesis from evidence and company synthesis.

    The thesis anchors assumptions — every assumption must be explainable
    in terms of the thesis. Cache-aware: short-circuits on KG hit.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("formulate_thesis", "start", parent_step_id)

    ticker = state["ticker"]
    evidence = state.get("evidence_pack") or {}
    company_state = state.get("company_state") or {}

    cache_flags = state.get("kg_cache_flags") or {}
    cached_thesis = state.get("thesis")
    if cache_flags.get("skip_formulate_thesis") and cached_thesis:
        narrative = str(cached_thesis.get("narrative", ""))[:80]
        emit_step(
            "formulate_thesis", "complete", parent_step_id,
            {
                "summary_line": f"⚡ KG cache hit — {narrative}...",
                "thesis_quality": "cached",
                "kg_status": "hit",
                "bull_thesis": cached_thesis.get("bull_thesis", ""),
                "bear_thesis": cached_thesis.get("bear_thesis", ""),
                "key_drivers": cached_thesis.get("key_drivers", []),
                "narrative": cached_thesis.get("narrative", ""),
            },
        )
        logger.info("DCF formulate_thesis ticker=%s cache_hit", ticker)
        return {"thesis": cached_thesis}

    try:
        evidence_summary = json.dumps({
            "items_count": len(evidence.get("items", [])),
            "tier_counts": evidence.get("tier_counts", {}),
        }, ensure_ascii=False)

        prompt = (
            f"You are a senior equity analyst forming an investment thesis for {ticker}.\n\n"
            f"## Company context\n{json.dumps(company_state, ensure_ascii=False)}\n\n"
            f"## Evidence summary\n{evidence_summary}\n\n"
            "## Instructions\n"
            "Form a concise investment thesis.  Output valid JSON ONLY — no markdown, no preamble:\n\n"
            "{\n"
            '  "bull_thesis": "Why the stock could outperform. 1-2 sentences naming specific drivers.",\n'
            '  "bear_thesis": "Why the stock could underperform. 1-2 sentences naming specific risks.",\n'
            '  "key_drivers": [\n'
            '    {"driver": "name", "direction": "positive|negative|neutral", "conviction": "high|medium|low"}\n'
            "  ],\n"
            '  "narrative": "2-3 sentences tying the thesis together — what the investment case hinges on."\n'
            "}\n\n"
            f"Base the thesis on the evidence and company context above. Be specific to {ticker}."
        )

        structured = _THESIS_LLM.with_structured_output(ThesisOutput)
        thesis_obj = structured.invoke(prompt)
        thesis = thesis_obj.model_dump() if isinstance(thesis_obj, ThesisOutput) else {}
        logger.info("DCF thesis produced ticker=%s bull=%s", ticker, thesis.get("bull_thesis", "")[:60])
    except Exception:
        logger.warning("Thesis LLM failed for %s — using fallback", ticker, exc_info=True)
        thesis = {
            "bull_thesis": f"{ticker} is undervalued relative to its growth potential.",
            "bear_thesis": f"{ticker} faces headwinds from competition and margin pressure.",
            "key_drivers": [],
            "narrative": f"Unable to formulate thesis for {ticker} due to insufficient evidence.",
            "_fallback": True,
        }

    thesis_quality = "fallback" if thesis.get("_fallback") else "ok"
    emit_step(
        "formulate_thesis", "complete", parent_step_id,
        {
            "summary_line": (
                "⚠ Thesis fallback — assumptions will lack narrative anchor"
                if thesis_quality == "fallback"
                else f"Thesis: {thesis.get('narrative', '')[:80]}..."
            ),
            "thesis_quality": thesis_quality,
            "bull_thesis": thesis.get("bull_thesis", ""),
            "bear_thesis": thesis.get("bear_thesis", ""),
            "key_drivers": thesis.get("key_drivers", []),
            "narrative": thesis.get("narrative", ""),
        },
    )
    if thesis_quality == "fallback":
        logger.warning(
            "DCF thesis fallback ticker=%s — review subgraph will flag HIGH severity",
            ticker,
        )
    return {"thesis": thesis}


def scenario_runner_node(state: DCFState) -> dict:
    """Run the DCF valuation subgraph once per scenario and collect results."""
    # Lazy import to avoid circular dependency with graph.py (which wires this
    # node into the main app while also defining dcf_scenario_val_app).
    from .graph import dcf_scenario_val_app

    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("scenario_runner", "start", parent_step_id)

    scenarios = state.get("scenarios") or []
    if not scenarios:
        emit_step("scenario_runner", "skipped", parent_step_id)
        return {}

    results = []
    captured_market_snapshot: dict = state.get("market_snapshot") or {}
    config = {"configurable": {"thread_id": f"{get_run_dir().name}_scenarios"}}

    for sc in scenarios:
        name = sc["name"]
        # NOTE: per-scenario progress is surfaced as a workflow substep
        # (emit_step "scenario_runner" → right-bar BlockStack). Do NOT emit it
        # as a chat_token — that leaks "### Bear/Base/Bull scenario…" markdown
        # headings into the streaming chat bubble (junk above the final report).

        sc_state = {
            "ticker": state["ticker"],
            "horizon_years": state.get("horizon_years", 5),
            "session_id": state.get("session_id", ""),
            "assumption_review_mode": False,
            "allow_external_assumptions": True,
            "assumption_overrides": {},
            "assumptions": sc["assumptions"],
            "assumption_provenance": state.get("assumption_provenance", {}),
            "assumptions_approved": True,
            "fundamentals": state.get("fundamentals", {}),
            "assumption_conflicts": [],
            "profile": state.get("profile", "default"),
            "profile_meta": state.get("profile_meta", {}),
            "assumption_flags": [],
            "valuation_flags": [],
            "confidence_label": "medium",
            "market_snapshot": state.get("market_snapshot") or {},
            "projected_fcff": [],
            "valuation": {},
            "sensitivity_table": [],
            "result_path": None,
            "parent_step_id": parent_step_id,
            "features": state.get("features", {}),
            "wacc_components": {},
            "evidence_pack": {},
            "company_state": None,
            "assumption_memo": None,
            "confidence_breakdown": None,
            "wacc_sanity": None,
            "thesis": None,
            "analysis_iteration": 0,
            "critique": None,
            "previous_valuation": None,
            "scenarios": [],
            "scenario_results": [],
            "assumption_history": [],
        }

        try:
            sc_result = dcf_scenario_val_app.invoke(sc_state, config=config)
        except Exception as exc:
            logger.warning("Scenario %s failed for %s: %s", name, state["ticker"], exc)
            sc_result = {"valuation": {"implied_share_price": 0, "error": str(exc)}}

        # Capture market_snapshot from first scenario that fetched it — same ticker,
        # same price across scenarios, so any one is correct. Bubbles up to outer
        # state so main-graph compute_market_signals_node sees a valid spot price.
        if not captured_market_snapshot.get("price"):
            ms = sc_result.get("market_snapshot") or {}
            if ms.get("price"):
                captured_market_snapshot = ms

        results.append({
            "name": name,
            "probability": sc["probability"],
            "assumptions": sc["assumptions"],
            "rationale": sc.get("rationale", ""),
            "valuation": sc_result.get("valuation", {}),
        })

    prices = [r["valuation"].get("implied_share_price", 0) for r in results if r["valuation"].get("implied_share_price")]
    expected = sum(r["probability"] * r["valuation"].get("implied_share_price", 0) for r in results)
    range_low = min(prices) if prices else 0
    range_high = max(prices) if prices else 0

    analysis_iteration = state.get("analysis_iteration", 0)
    emit_step(
        "scenario_runner", "complete", parent_step_id,
        {
            "summary_line": f"Expected=${expected:.2f} range=${range_low:.2f}–${range_high:.2f}",
            "expected_value": expected,
            "range_low": range_low,
            "range_high": range_high,
            "scenario_results": results,
            "run_count": analysis_iteration + 1,
        },
    )
    return {
        "scenario_results": results,
        "valuation": {
            "implied_share_price": expected,
            "range_low": range_low,
            "range_high": range_high,
        },
        "market_snapshot": captured_market_snapshot,
    }

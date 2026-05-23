"""Scenario generation — bear/base/bull variants with monotonicity validation."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .activity import emit_step
from .state import DCFState

logger = logging.getLogger(__name__)


_SCENARIO_LLM = ChatOpenAI(
    model=os.getenv("DCF_SCENARIO_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=60,
)

_MONOTONIC_FIELDS = ("revenue_growth", "fcff_margin", "terminal_growth")


class ScenarioVariant(BaseModel):
    probability: float = Field(description="Probability weight, 0.0-1.0")
    revenue_growth: float = Field(description="Annual revenue growth rate")
    fcff_margin: float = Field(description="FCFF margin")
    terminal_growth: float = Field(description="Terminal growth rate")
    tax_rate: float = Field(description="Effective tax rate")
    rationale: str = Field(description="1 sentence explaining the scenario")


class ScenarioOutput(BaseModel):
    bear: ScenarioVariant
    bull: ScenarioVariant


def _violates_monotonicity(scenarios: list[dict]) -> list[str]:
    """Return list of fields where bull < base or base < bear (broken ordering).

    Empty list = monotonic. Tax rate intentionally excluded because lower-is-
    more-bullish there.
    """
    by_name = {s["name"]: s for s in scenarios}
    bear = by_name.get("bear", {}).get("assumptions", {})
    base = by_name.get("base", {}).get("assumptions", {})
    bull = by_name.get("bull", {}).get("assumptions", {})
    bad: list[str] = []
    for field in _MONOTONIC_FIELDS:
        b, m, u = bear.get(field), base.get(field), bull.get(field)
        if b is None or m is None or u is None:
            continue
        if not (u >= m >= b):
            bad.append(field)
    return bad


def scenario_generator_node(state: DCFState) -> dict:
    """Generate bear and bull scenarios from the base case + investment thesis.

    Phase 3 (monotonicity validator): after building the scenarios list, we
    require bull ≥ base ≥ bear for revenue_growth, fcff_margin, and
    terminal_growth. If the LLM violates this we regenerate ONCE with an
    explicit constraint. If the retry still fails we drop bull/bear and run
    base only — surfaces as a warn flag, no silent auto-correction.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("scenario_generator", "start", parent_step_id)

    ticker = state["ticker"]
    base_assumptions = state.get("assumptions") or {}
    thesis = state.get("thesis") or {}

    prompt = (
        f"You are a senior analyst generating valuation scenarios for {ticker}.\n\n"
        f"## Base case assumptions\n{json.dumps(base_assumptions, ensure_ascii=False)}\n\n"
        f"## Investment thesis\n{json.dumps(thesis, ensure_ascii=False)}\n\n"
        "## Instructions\n"
        "From the base case, derive BEAR and BULL scenarios. Output valid JSON ONLY — no markdown:\n\n"
        "{\n"
        '  "bear": {\n'
        '    "probability": 0.25,\n'
        '    "revenue_growth": 0.05,     // more pessimistic\n'
        '    "fcff_margin": 0.20,        // lower margin\n'
        '    "terminal_growth": 0.02,    // more conservative\n'
        '    "tax_rate": 0.30,\n'
        '    "rationale": "Why the bear case — 1 sentence tied to thesis."\n'
        "  },\n"
        '  "bull": {\n'
        '    "probability": 0.25,\n'
        '    "revenue_growth": 0.18,     // more optimistic\n'
        '    "fcff_margin": 0.28,        // higher margin\n'
        '    "terminal_growth": 0.035,   // stronger terminal\n'
        '    "tax_rate": 0.28,\n'
        '    "rationale": "Why the bull case — 1 sentence tied to thesis."\n'
        "  }\n"
        "}\n\n"
        f"Probabilities must sum to 1.0 with base=0.50, bear+bull=0.50.\n"
        f"Base each scenario on the thesis — bear challenges it, bull amplifies it."
    )

    try:
        structured = _SCENARIO_LLM.with_structured_output(ScenarioOutput)
        so = structured.invoke(prompt)
        variants = so.model_dump() if isinstance(so, ScenarioOutput) else {}
    except Exception:
        logger.warning("Scenario LLM failed for %s — using mechanical variance", ticker, exc_info=True)
        variants = {}

    bear = variants.get("bear") or {}
    bull = variants.get("bull") or {}

    scenarios = [
        {
            "name": "bear",
            "probability": bear.get("probability", 0.25),
            "assumptions": {
                **base_assumptions,
                "revenue_growth": bear.get("revenue_growth", base_assumptions.get("revenue_growth", 0.05) * 0.7),
                "fcff_margin": bear.get("fcff_margin", base_assumptions.get("fcff_margin", 0.20) - 0.03),
                "terminal_growth": bear.get("terminal_growth", base_assumptions.get("terminal_growth", 0.02) - 0.005),
                "tax_rate": bear.get("tax_rate", base_assumptions.get("tax_rate", 0.30)),
            },
            "rationale": bear.get("rationale", "Mechanically derived bear case."),
        },
        {
            "name": "base",
            "probability": base_assumptions.get("probability", 0.50),
            "assumptions": dict(base_assumptions),
            "rationale": "Base case from proposed assumptions.",
        },
        {
            "name": "bull",
            "probability": bull.get("probability", 0.25),
            "assumptions": {
                **base_assumptions,
                "revenue_growth": bull.get("revenue_growth", base_assumptions.get("revenue_growth", 0.10) * 1.4),
                "fcff_margin": bull.get("fcff_margin", base_assumptions.get("fcff_margin", 0.25) + 0.03),
                "terminal_growth": bull.get("terminal_growth", base_assumptions.get("terminal_growth", 0.03) + 0.005),
                "tax_rate": bull.get("tax_rate", base_assumptions.get("tax_rate", 0.28)),
            },
            "rationale": bull.get("rationale", "Mechanically derived bull case."),
        },
    ]

    bad_fields = _violates_monotonicity(scenarios)
    scenario_flags: list[dict[str, Any]] = []
    if bad_fields:
        logger.warning(
            "Scenario monotonicity violated for %s on %s — regenerating",
            ticker, bad_fields,
        )
        retry_prompt = (
            prompt
            + "\n\n## CONSTRAINT (previous attempt violated this)\n"
            + f"You MUST satisfy bull >= base >= bear for: {', '.join(bad_fields)}.\n"
            + "Numerical ordering, not opinions. Re-generate with this constraint."
        )
        try:
            structured = _SCENARIO_LLM.with_structured_output(ScenarioOutput)
            so = structured.invoke(retry_prompt)
            variants2 = so.model_dump() if isinstance(so, ScenarioOutput) else {}
            bear2 = variants2.get("bear") or {}
            bull2 = variants2.get("bull") or {}
            if bear2 and bull2:
                scenarios[0]["assumptions"].update({
                    "revenue_growth": bear2.get("revenue_growth", scenarios[0]["assumptions"]["revenue_growth"]),
                    "fcff_margin": bear2.get("fcff_margin", scenarios[0]["assumptions"]["fcff_margin"]),
                    "terminal_growth": bear2.get("terminal_growth", scenarios[0]["assumptions"]["terminal_growth"]),
                })
                scenarios[2]["assumptions"].update({
                    "revenue_growth": bull2.get("revenue_growth", scenarios[2]["assumptions"]["revenue_growth"]),
                    "fcff_margin": bull2.get("fcff_margin", scenarios[2]["assumptions"]["fcff_margin"]),
                    "terminal_growth": bull2.get("terminal_growth", scenarios[2]["assumptions"]["terminal_growth"]),
                })
        except Exception:
            logger.warning("Scenario regen failed for %s", ticker, exc_info=True)

        still_bad = _violates_monotonicity(scenarios)
        if still_bad:
            logger.error(
                "Scenario regen still violates monotonicity on %s — dropping bull/bear",
                still_bad,
            )
            scenarios = [s for s in scenarios if s["name"] == "base"]
            scenarios[0]["probability"] = 1.0
            scenario_flags.append({
                "field": "scenarios",
                "severity": "warn",
                "code": "scenario_monotonicity_violation",
                "message": (
                    f"Bull/bear scenarios rejected after retry: bull < base or base < bear on {still_bad}. "
                    f"Running base case only — scenario dispersion unavailable."
                ),
            })
        else:
            scenario_flags.append({
                "field": "scenarios",
                "severity": "info",
                "code": "scenario_monotonicity_retried",
                "message": f"Initial scenarios violated monotonicity on {bad_fields}; regenerated successfully.",
            })

    total_p = sum(s["probability"] for s in scenarios)
    if total_p > 0:
        for s in scenarios:
            s["probability"] = round(s["probability"] / total_p, 4)

    summary_line = (
        "1 scenario: base only (scenario validator rejected bull/bear)"
        if len(scenarios) == 1
        else f"3 scenarios: bear={scenarios[0]['probability']:.0%} base={scenarios[1]['probability']:.0%} bull={scenarios[2]['probability']:.0%}"
    )
    emit_step(
        "scenario_generator", "complete", parent_step_id,
        {
            "summary_line": summary_line,
            "scenarios": scenarios,
            "scenario_count": len(scenarios),
            "scenario_flags": scenario_flags,
        },
    )
    out: dict[str, Any] = {"scenarios": scenarios}
    if scenario_flags:
        existing = list(state.get("assumption_flags") or [])
        out["assumption_flags"] = existing + scenario_flags
    return out

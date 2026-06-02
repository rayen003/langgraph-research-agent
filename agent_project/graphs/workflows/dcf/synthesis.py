"""Semantic synthesis — compress evidence noise into structured company state.

One bounded LLM call that reads the evidence pack and produces a structured
JSON describing the company's position. This is *judgment* (the "reasoning
engine") but bounded by: strict schema, evidence_ref validation, and retry
on invalid citations.

Output feeds the assumption memo (propose_assumptions_node).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .activity import emit_step
from .state import DCFState

dotenv.load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM — capable model for judgment-heavy synthesis
# ---------------------------------------------------------------------------

_synthesis_model_name = os.getenv(
    "DCF_SYNTHESIS_MODEL",
    os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
)
synthesis_llm = ChatOpenAI(
    model=_synthesis_model_name,
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=90,
)

MAX_SYNTHESIS_RETRIES = 2


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    """A citation to a specific evidence item."""
    evidence_id: str = Field(description="Must match an evidence_id from the evidence pack")
    relevance: str = Field(description="One sentence on why this evidence matters for the claim")


class CompanyState(BaseModel):
    """Structured view of the company — what we know before proposing numbers."""

    ticker: str = Field(description="Ticker symbol")
    business_summary: str = Field(
        description="1-2 sentence description of what the company does and its sector"
    )

    # ── Lifecycle signals (drive memo's choice of optional DCF fields) ─────────
    lifecycle_stage: str = Field(
        description=(
            "Company lifecycle — one of: "
            "'hypergrowth' (>25% revenue growth, large TAM); "
            "'scaling' (10-25% growth, gaining share); "
            "'mature' (3-10% growth, GDP+, dominant or stable); "
            "'declining' (negative/zero growth, secular headwinds); "
            "'cyclical' (growth tied to economic cycle — autos, energy, materials). "
            "Critical: signals whether memo proposes revenue_growth_terminal — "
            "hypergrowth/scaling MUST fade; mature/declining can stay flat."
        )
    )
    margin_trajectory: str = Field(
        description=(
            "Direction of margin evolution over the next 5 years — one of: "
            "'expanding' (operating leverage, mix shift to high-margin segments, "
            "ad/subscription business scaling on low-margin core); "
            "'stable' (mature steady-state); "
            "'compressing' (commoditization, competitive intensification, "
            "input cost pressure). "
            "Critical: signals whether memo proposes fcff_margin_terminal "
            "(non-stable → yes; stable → no)."
        )
    )
    capital_return_policy: str = Field(
        description=(
            "1-2 sentences on capital return: buyback intensity (none/modest/aggressive), "
            "dividend yield, reinvestment-vs-return tilt. Mention specific authorizations "
            "from filings if known. Signals whether memo proposes buyback_yield."
        )
    )
    sbc_intensity: str = Field(
        description=(
            "Stock-based compensation magnitude — one of: "
            "'high' (>5% of revenue, typical for tech/software/internet); "
            "'moderate' (2-5%); "
            "'low' (<2%, industrials/retail/staples). "
            "Cite SBC line from cash flow statement if available. "
            "Signals whether memo proposes sbc_pct_revenue explicitly."
        )
    )

    growth_outlook: str = Field(
        description=(
            "Narrative on revenue growth trajectory over the forecast horizon. "
            "Be specific: cite growth rates from filings/analyst consensus if available, "
            "note segment-level dynamics, product cycles, geographic expansion. "
            "Distinguish near-term (1-2yr) vs medium-term (3-5yr) outlook."
        )
    )
    growth_drivers: list[str] = Field(
        description="3-5 specific growth drivers with evidence backing"
    )
    margin_trend: str = Field(
        description=(
            "One of: improving, stable, declining, volatile. "
            "Supported by margin history from fundamentals and forward commentary from filings."
        )
    )
    margin_narrative: str = Field(
        description=(
            "Explanation of margin trajectory: operating leverage, input costs, "
            "mix shift, pricing power, scale efficiencies. Reference actual margin "
            "data from evidence where available."
        )
    )
    key_risks: list[str] = Field(
        description=(
            "3-6 concrete risks that could materially alter the outlook. "
            "Prioritize risks explicitly discussed in filings (Risk Factors section) "
            "or credible news sources. Include regulatory, competitive, macro, "
            "and company-specific risks."
        )
    )
    competitive_position: str = Field(
        description="Moat assessment: market share, barriers, pricing power, disruption risk"
    )
    macro_context: str = Field(
        description=(
            "Brief macro/rates regime context. Only elaborate if the evidence "
            "surfaces specific macro exposures (FX, commodity, cyclical demand). "
            "Otherwise 1-2 sentences is sufficient."
        )
    )
    conflicts: list[str] = Field(
        description=(
            "Any disagreements between sources: management guidance vs street vs filings, "
            "or conflicting data points. Be explicit about what disagrees and why it matters."
        ),
        default_factory=list,
    )
    evidence_refs: list[EvidenceRef] = Field(
        description="All evidence items used as basis for this synthesis. Must be valid evidence_ids."
    )
    confidence_self_assessment: str = Field(
        description=(
            "One of: high, medium, low. Self-assess based on evidence quality: "
            "do we have filings + structured data (high), only web/news (low), "
            "mixed (medium)."
        )
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM_PROMPT = """You are an expert equity research analyst synthesizing a company profile from evidence.

## Role
You produce a structured CompanyState JSON from an evidence pack. Your output feeds a DCF assumption memo — the quality of your synthesis directly determines the quality of the valuation.

## Rules
1. **Evidence-first.** Every claim must be traceable to a specific evidence_id. If the evidence doesn't support a claim, don't make it.
2. **Be specific.** Instead of "strong growth outlook," write "Revenue grew 8% YoY in FY2024 per 10-K (ev_fmp_base_revenue), with management guiding mid-single-digit growth for FY2025 per MD&A (ev_sec_*_mda)."
3. **Surface conflicts.** If management guidance disagrees with street estimates or filing data shows a different trend, call it out explicitly.
4. **Conservative on unknowns.** If evidence is thin (only web excerpts, no filings), flag lower confidence and avoid over-precise claims.
5. **Evidence refs are mandatory.** Every evidence_ref must match an actual evidence_id from the provided evidence pack. Invalid IDs will cause rejection and regeneration.

## Lifecycle classification (critical downstream signal)
The memo uses your `lifecycle_stage`, `margin_trajectory`, `capital_return_policy`, and `sbc_intensity` signals to decide WHICH optional DCF mechanics to model explicitly. Be deliberate:

- **hypergrowth** (>25% rev growth): MUST eventually fade. Examples: NVDA, post-IPO software, AI infra plays.
- **scaling** (10-25% growth, share gains): Will decelerate but not collapse. Examples: META 2024, MSFT (Azure tailwind), SHOP.
- **mature** (3-10% growth, GDP+): Stable growth, strong moat. Examples: AAPL, KO, WMT, PG.
- **declining** (negative/zero growth, secular headwinds): Defensive posture. Examples: legacy media, ICE-only autos with no EV strategy.
- **cyclical** (growth tied to cycle, swings ±10-20%): Through-cycle normalization needed. Examples: F, XOM, basic materials.

For `margin_trajectory`, name the driver:
- **expanding**: ad business scaling (WMT Connect, AMZN ads), services mix shift (AAPL services), cloud operating leverage (MSFT Azure).
- **compressing**: commodity pressure (NVDA vs AMD/custom silicon), regulatory caps, input cost squeeze.
- **stable**: established margins with no business model transition in evidence.

For `capital_return_policy`, mention specific authorizations when in the evidence (e.g., "$110B annual buyback per 10-K" or "no formal program, dividends only").

For `sbc_intensity`, quote the SBC line from cash flow statement when present (e.g., "SBC of $16.4B = 10.0% of revenue per FY2024 CF statement").

## Source tier priority (highest first)
- filing (SEC 10-K/10-Q): highest weight — company's own words under legal obligation
- structured_api (FMP/yfinance): high weight — audited/regulated financial data
- document (user uploads): moderate weight — context-dependent
- news: moderate weight — timely but less verified
- generic_web: low weight — use only to supplement, never as primary basis

## Output
Produce a single CompanyState JSON object conforming to the schema. Every evidence_ref.evidence_id must be in the evidence pack."""


def _build_synthesis_user_message(evidence_pack: dict[str, Any]) -> str:
    """Build the user message containing the evidence pack for synthesis."""
    ticker = evidence_pack.get("ticker", "???")
    items = evidence_pack.get("items", [])
    tier_summary = evidence_pack.get("tier_summary", {})

    # Build a compact but complete evidence listing
    evidence_lines: list[str] = [
        f"## Evidence Pack for {ticker}",
        f"Total items: {len(items)}",
        f"Source tiers: {json.dumps(tier_summary)}",
        "",
    ]

    for item in items:
        eid = item.get("evidence_id", "?")
        tier = item.get("source_tier", "?")
        source = item.get("source", "?")
        kind = item.get("kind", "?")
        as_of = item.get("as_of", "")

        header = f"[{eid}] tier={tier} source={source} kind={kind} as_of={as_of}"

        if kind == "structured_fundamental":
            field = item.get("field", "?")
            value = item.get("value")
            unit = item.get("raw_unit", "")
            evidence_lines.append(f"{header}")
            evidence_lines.append(f"  field={field} value={value} {unit}")
        elif kind == "filing_excerpt":
            section = item.get("section", "?")
            filing_type = item.get("filing_type", "?")
            text = item.get("text", "")[:3000]
            evidence_lines.append(f"{header} {filing_type} {section}")
            evidence_lines.append(f"  {text}")
        elif kind in ("web_excerpt", "document_excerpt"):
            title = item.get("title") or item.get("filename", "")
            text = item.get("text", "")[:1500]
            evidence_lines.append(f"{header} {title}")
            evidence_lines.append(f"  {text}")
        elif kind == "profile":
            evidence_lines.append(f"{header}")
            evidence_lines.append(f"  profile={item.get('profile')} sector={item.get('sector')}")
        elif kind == "market_data":
            field = item.get("field", "?")
            value = item.get("value")
            evidence_lines.append(f"{header}")
            evidence_lines.append(f"  field={field} value={value}")
        else:
            text = item.get("text", "")[:1000]
            evidence_lines.append(f"{header}")
            if text:
                evidence_lines.append(f"  {text}")

        evidence_lines.append("")

    return "\n".join(evidence_lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_evidence_refs(
    company_state: CompanyState,
    valid_ids: set[str],
) -> list[str]:
    """Check that all evidence_refs reference real evidence_ids.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    for ref in company_state.evidence_refs:
        if ref.evidence_id not in valid_ids:
            errors.append(
                f"evidence_ref '{ref.evidence_id}' not found in evidence pack. "
                f"Valid IDs include: {', '.join(sorted(valid_ids)[:10])}..."
            )
    return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_synthesis_line(state_dict: dict[str, Any]) -> str:
    """One-line summary of synthesis for activity trace display."""
    parts: list[str] = []
    lifecycle = state_dict.get("lifecycle_stage", "")
    if lifecycle:
        parts.append(f"lifecycle: {lifecycle}")
    trajectory = state_dict.get("margin_trajectory", "")
    if trajectory:
        parts.append(f"margins: {trajectory}")
    sbc = state_dict.get("sbc_intensity", "")
    if sbc:
        parts.append(f"sbc: {sbc}")
    risks = len(state_dict.get("key_risks", []))
    parts.append(f"{risks} risks")
    conf = state_dict.get("confidence_self_assessment", "")
    if conf:
        parts.append(f"self-assessed: {conf}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def semantic_synthesis_node(state: dict) -> dict:
    """Synthesize the evidence pack into a structured CompanyState.

    Placed between ``assemble_evidence`` and ``propose_assumptions`` in the
    DCF graph. Produces ``company_state`` on the DCF state.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    evidence_pack = state.get("evidence_pack") or {}
    ticker = state.get("ticker", "???")

    if not evidence_pack.get("items"):
        logger.warning("DCF synthesis: empty evidence pack, skipping")
        emit_step("semantic_synthesis", "skipped", parent_step_id)
        return {"company_state": None}

    emit_step("semantic_synthesis", "start", parent_step_id, {"ticker": ticker})

    valid_ids = {item.get("evidence_id", "") for item in evidence_pack.get("items", [])}
    valid_ids.discard("")

    user_message = _build_synthesis_user_message(evidence_pack)

    company_state: CompanyState | None = None
    last_error: str | None = None

    for attempt in range(1 + MAX_SYNTHESIS_RETRIES):
        try:
            structured_llm = synthesis_llm.with_structured_output(CompanyState)
            company_state = structured_llm.invoke([
                SystemMessage(content=_SYNTHESIS_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ])

            if not isinstance(company_state, CompanyState):
                last_error = f"LLM returned {type(company_state).__name__} instead of CompanyState"
                logger.warning("DCF synthesis attempt %d: %s", attempt + 1, last_error)
                company_state = None
                continue

            # Validate evidence refs
            ref_errors = _validate_evidence_refs(company_state, valid_ids)
            if ref_errors:
                last_error = "; ".join(ref_errors)
                logger.warning(
                    "DCF synthesis attempt %d: invalid evidence_refs: %s",
                    attempt + 1, last_error,
                )
                # Append correction hint for retry
                user_message += (
                    f"\n\n## CORRECTION REQUIRED\n"
                    f"Your previous output had invalid evidence_refs:\n"
                    f"{last_error}\n"
                    f"Regenerate with ONLY valid evidence_ids from the pack above."
                )
                company_state = None
                continue

            # Success
            break

        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("DCF synthesis attempt %d failed: %s", attempt + 1, exc)
            company_state = None

    if company_state is None:
        logger.error(
            "DCF synthesis failed after %d attempts: %s",
            1 + MAX_SYNTHESIS_RETRIES, last_error,
        )
        emit_step(
            "semantic_synthesis", "fallback", parent_step_id,
            {"error": last_error},
        )
        # Return empty company_state so downstream memo can fall back gracefully
        return {"company_state": None}

    state_dict = company_state.model_dump()
    logger.info(
        "DCF synthesis complete ticker=%s lifecycle=%s trajectory=%s sbc=%s refs=%d confidence=%s",
        ticker,
        state_dict.get("lifecycle_stage"),
        state_dict.get("margin_trajectory"),
        state_dict.get("sbc_intensity"),
        len(state_dict.get("evidence_refs", [])),
        state_dict.get("confidence_self_assessment"),
    )

    # ── KG write-back: lifecycle signals as a separate cached node ────────────
    # Lifecycle changes slowly (~30d TTL) so it's worth caching separately
    # from full synthesis (7d TTL). Future runs can short-circuit synthesis
    # if lifecycle + structured data hasn't changed.
    try:
        from kg import get_cache, kg_write  # noqa: PLC0415
        cache = get_cache()
        session_id = state.get("session_id") or ""
        lifecycle_value = {
            "lifecycle_stage": state_dict.get("lifecycle_stage"),
            "margin_trajectory": state_dict.get("margin_trajectory"),
            "capital_return_policy": state_dict.get("capital_return_policy"),
            "sbc_intensity": state_dict.get("sbc_intensity"),
        }
        # Only write if at least lifecycle_stage was produced
        if lifecycle_value.get("lifecycle_stage"):
            confidence_label = state_dict.get("confidence_self_assessment", "medium")
            confidence_score = {"high": 0.9, "medium": 0.7, "low": 0.5}.get(
                confidence_label, 0.7,
            )
            kg_write(
                ticker=ticker,
                node_type="company_lifecycle",
                field="signals",
                value=lifecycle_value,
                source="llm_synthesis",
                confidence=confidence_score,
                session_id=session_id,
            )
            logger.info(
                "DCF synthesis: wrote company_lifecycle to KG ticker=%s stage=%s",
                ticker, lifecycle_value["lifecycle_stage"],
            )
    except Exception as exc:  # noqa: BLE001
        # KG write is best-effort; don't fail the workflow over a cache miss
        logger.warning("DCF synthesis: KG write-back failed ticker=%s err=%s", ticker, exc)

    emit_step(
        "semantic_synthesis", "complete", parent_step_id,
        {
            "ticker": ticker,
            "evidence_refs_count": len(state_dict.get("evidence_refs", [])),
            "confidence": state_dict.get("confidence_self_assessment"),
            "lifecycle_stage": state_dict.get("lifecycle_stage"),
            "margin_trajectory": state_dict.get("margin_trajectory"),
            "sbc_intensity": state_dict.get("sbc_intensity"),
            "capital_return_policy": state_dict.get("capital_return_policy"),
            "margin_trend": state_dict.get("margin_trend"),
            "risks_count": len(state_dict.get("key_risks", [])),
            "conflicts_count": len(state_dict.get("conflicts", [])),
            "growth_outlook": state_dict.get("growth_outlook", "")[:500],
            "key_risks": state_dict.get("key_risks", []),
            "summary_line": _fmt_synthesis_line(state_dict),
        },
    )
    return {"company_state": state_dict}

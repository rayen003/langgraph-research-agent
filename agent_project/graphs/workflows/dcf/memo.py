"""Assumption memo — LLM-driven DCF assumption proposal with rationale and citations.

Replaces the heuristic merge logic in ``build_assumptions_node`` with a
structured LLM call that produces per-field proposals backed by evidence_refs.

The workflow:
    1. Tier A fields (revenue, shares, net_debt) are LOCKED from canonical fundamentals.
    2. The LLM reads the evidence pack + company state (from synthesis).
    3. The LLM proposes Tier B assumptions (growth, margin, terminal growth, tax rate).
    4. Each proposal carries: value, rationale, evidence_refs, confidence, optional range.
    5. WACC is NOT proposed by the LLM — it's resolved from features/CAPM by the WACC engine.
    6. Plausibility band checks run as post-validation.
    7. The full memo (provenance + proposals + flags) feeds the review node.
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
from .priors import check_assumption_plausibility, enforce_hard_bands, prior_band_midpoint
from .state import (
    _DEFAULT_EQUITY_RISK_PREMIUM,
    _DEFAULT_RISK_FREE_RATE,
    _ASSUMPTION_FIELDS,
    _TIER_A_FIELDS,
    clip_to_field_range,
    filter_user_assumption_overrides,
)
from .wacc import resolve_wacc_from_features

dotenv.load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM — capable model for judgment-heavy assumption proposal
# ---------------------------------------------------------------------------

_memo_model_name = os.getenv(
    "DCF_MEMO_MODEL",
    os.getenv("DCF_SYNTHESIS_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
)
memo_llm = ChatOpenAI(
    model=_memo_model_name,
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=90,
)

MAX_MEMO_RETRIES = 2

# Tier B fields the LLM is allowed to propose.
# Tier A fields and WACC are handled deterministically.
#
# REQUIRED: every memo must propose these (legacy 4).
# OPTIONAL: LLM proposes when the company state warrants modelling them
#           explicitly (e.g., buybacks > 0 for capital-returning companies,
#           SBC > 2% for tech, margin glide for businesses in transition,
#           growth fade for hypergrowth names). Missing optional fields
#           default to backward-compatible behavior in valuation.py.
_TIER_B_REQUIRED = frozenset({
    "revenue_growth",
    "fcff_margin",
    "terminal_growth",
    "tax_rate",
})
_TIER_B_OPTIONAL = frozenset({
    "buyback_yield",           # annual share reduction rate
    "sbc_pct_revenue",         # SBC as % of revenue (real economic cost)
    "revenue_growth_terminal", # Y5 fade growth (2-stage model)
    "fcff_margin_terminal",    # Y5 terminal margin (margin glide)
})
_TIER_B_PROPOSABLE = _TIER_B_REQUIRED | _TIER_B_OPTIONAL


# ---------------------------------------------------------------------------
# Pydantic output schemas
# ---------------------------------------------------------------------------


class AssumptionProposal(BaseModel):
    """A single DCF assumption with rationale and evidence backing."""

    field: str = Field(
        description=(
            "REQUIRED fields: revenue_growth, fcff_margin, terminal_growth, tax_rate. "
            "OPTIONAL fields (propose when company state warrants): "
            "buyback_yield (annual share reduction), sbc_pct_revenue (SBC % of revenue), "
            "revenue_growth_terminal (Y5 fade growth), fcff_margin_terminal (Y5 margin). "
            "Do NOT propose base_revenue, shares_outstanding, net_debt, or wacc — "
            "those are set deterministically from canonical data."
        )
    )
    value: float = Field(description="The proposed numeric value (as a decimal, e.g. 0.08 for 8%)")
    rationale: str = Field(
        description=(
            "2-4 sentence justification for this value. Reference specific evidence "
            "and explain the judgment call. If the evidence is mixed, explain the "
            "trade-off you made."
        )
    )
    evidence_refs: list[str] = Field(
        description="Evidence IDs supporting this proposal (must exist in evidence pack)"
    )
    confidence: float = Field(
        description=(
            "0.0-1.0. How confident are you in this specific value? "
            "0.9+ = strongly supported by filings/structured data. "
            "0.7-0.89 = supported by multiple corroborating sources. "
            "0.5-0.69 = reasonable estimate with thin evidence. "
            "Below 0.5 = speculative — should be rare."
        ),
        ge=0.0,
        le=1.0,
    )
    range_low: float | None = Field(
        default=None,
        description="Optional lower bound of a reasonable range",
    )
    range_high: float | None = Field(
        default=None,
        description="Optional upper bound of a reasonable range",
    )


class AssumptionMemo(BaseModel):
    """Complete DCF assumption proposal with connecting narrative."""

    ticker: str = Field(description="Ticker symbol")
    horizon_years: int = Field(description="Forecast horizon in years")
    proposals: list[AssumptionProposal] = Field(
        description=(
            "Proposals for Tier B fields. MUST include the 4 required fields "
            "(revenue_growth, fcff_margin, terminal_growth, tax_rate). MAY include "
            "optional fields (buyback_yield, sbc_pct_revenue, revenue_growth_terminal, "
            "fcff_margin_terminal) when company state warrants explicit modelling."
        )
    )
    overall_narrative: str = Field(
        description=(
            "3-5 sentence narrative connecting the assumptions: why growth leads to "
            "this margin profile, how competitive dynamics shape terminal growth, "
            "what tax regime is assumed. This is the 'story' the numbers tell."
        )
    )
    key_uncertainties: list[str] = Field(
        description=(
            "2-4 specific uncertainties that could cause material deviation. "
            "E.g. 'tariff regime change could compress margins 200-400bps', "
            "'product cycle risk in FY2026-27'. Reference evidence where relevant."
        )
    )
    overall_confidence: float = Field(
        description="Aggregate confidence in the full assumption set (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )
    evidence_refs: list[str] = Field(
        description="All evidence_ids referenced across all proposals"
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_MEMO_SYSTEM_PROMPT = """You are an expert equity research analyst producing a DCF assumption memo.

## Your Task
Propose DCF assumptions for a company based on an evidence pack and a synthesized company profile. Your assumptions drive a deterministic DCF valuation.

## Rules

### REQUIRED proposals (always include all 4 — Tier B core)
- **revenue_growth**: annual growth rate for years 1-2 of the forecast (near-term). Ground in historical trends, forward guidance, sector outlook. For hypergrowth names use the analyst consensus near-term rate; deceleration is captured separately via `revenue_growth_terminal`.
- **fcff_margin**: free cash flow to firm as % of revenue at the START of the horizon (year 1). Derive from historical margins, trend direction, capital intensity. FCFF margin differs from net margin — account for capex + working capital. If a margin transition is expected, also propose `fcff_margin_terminal`.
- **terminal_growth**: perpetuity growth rate (post-horizon, beyond year N). Must be ≤ risk-free rate + ~50 bps. Typically 2-3% for stable companies. Higher only with very strong structural arguments.
- **tax_rate**: effective tax rate. Use canonical tax rate from fundamentals; otherwise estimate from sector/geography.

### OPTIONAL proposals (include WHEN the company state warrants — skip otherwise)
Only propose these if there is real signal in the evidence. Do not pad the memo with optional fields when defaults are appropriate.

- **buyback_yield**: ANNUAL share reduction rate (e.g., 0.035 = 3.5% fewer shares per year). Compute from announced/historical buyback programs (e.g., AAPL ~$110B/yr / ~$3T market cap = 3.5%). Propose when:
    - Filings disclose an active buyback authorization, OR
    - Trailing share count declines >0.5% per year.
  Skip (omit) when buybacks are immaterial or net issuance is offset by SBC.

- **sbc_pct_revenue**: stock-based comp as % of revenue. Extract directly from cash flow statement (SBC line item in OCF reconciliation). SBC is a real economic cost that OCF math hides — subtracting it gives a more honest FCF. Propose for ANY technology / software / internet name (typically 3-10%). Skip for industrials / retail / consumer staples where SBC is <1% and immaterial.

- **revenue_growth_terminal**: revenue growth rate IN YEAR N (the final forecast year), used to model 2-stage fade. Propose when:
    - Near-term growth > 15% (mandatory fade — law of large numbers), OR
    - Lifecycle is "hypergrowth" or "scaling" per company state.
  For mature companies with stable growth, omit (defaults to revenue_growth).

- **fcff_margin_terminal**: FCFF margin in year N. Propose when:
    - Business model is transitioning (e.g., adding ads/subscriptions = margin expansion; commodity pressure = compression), OR
    - Company state signals margin trajectory direction.
  For stable margin businesses, omit.

### What you DO NOT propose (Tier A — locked from canonical data)
- base_revenue, shares_outstanding, net_debt — FIXED from audited statements.
- wacc — computed deterministically from CAPM using market data.

### Citation discipline
- Every proposal must cite specific evidence_ids from the evidence pack.
- Prefer filing evidence (tier=filing) over web excerpts.
- If evidence is contradictory, acknowledge the tension in your rationale.

### Confidence
- 0.9+: strongly supported by filings + structured data + consistent across sources
- 0.7-0.89: supported by multiple sources with minor disagreement
- 0.5-0.69: reasonable estimate but evidence is thin or mixed
- Below 0.5: speculative — only use if evidence is genuinely absent

### Output
Produce a single AssumptionMemo JSON object. Use the EXACT evidence_ids from the evidence pack."""


def _build_memo_user_message(
    ticker: str,
    horizon_years: int,
    evidence_pack: dict[str, Any],
    company_state: dict[str, Any] | None,
    canonical: dict[str, float],
) -> str:
    """Build the user message for the assumption memo LLM call."""

    # ── Canonical (locked) fields ──────────────────────────────────────────
    canon_lines = ["## Locked Canonical Fields (DO NOT PROPOSE THESE)", ""]
    for field in sorted(_TIER_A_FIELDS):
        val = canonical.get(field)
        if val is not None:
            canon_lines.append(f"- {field} = {val:,.2f} (millions for revenue/debt/shares)")
    canon_lines.append(f"- wacc = COMPUTED SEPARATELY VIA CAPM (do not propose)")
    canon_lines.append("")

    # ── Evidence pack summary ──────────────────────────────────────────────
    items = evidence_pack.get("items", [])
    tier_summary = evidence_pack.get("tier_summary", {})
    evidence_lines = [
        f"## Evidence Pack for {ticker}",
        f"Total items: {len(items)}",
        f"Source tiers: {json.dumps(tier_summary)}",
        "",
        "### Evidence items",
        "",
    ]

    for item in items:
        eid = item.get("evidence_id", "?")
        tier = item.get("source_tier", "?")
        kind = item.get("kind", "?")

        if kind == "structured_fundamental":
            evidence_lines.append(
                f"[{eid}] tier={tier} field={item.get('field')} value={item.get('value')} "
                f"confidence={item.get('confidence')}"
            )
        elif kind == "filing_excerpt":
            text = item.get("text", "")[:2000]
            evidence_lines.append(
                f"[{eid}] tier={tier} {item.get('filing_type')} {item.get('section')}"
            )
            evidence_lines.append(f"  {text}")
        elif kind in ("web_excerpt", "document_excerpt"):
            text = item.get("text", "")[:1200]
            title = item.get("title") or item.get("filename", "")
            evidence_lines.append(f"[{eid}] tier={tier} {title}")
            evidence_lines.append(f"  {text}")
        elif kind == "market_data":
            evidence_lines.append(
                f"[{eid}] tier={tier} field={item.get('field')} value={item.get('value')}"
            )
        elif kind == "profile":
            evidence_lines.append(
                f"[{eid}] tier={tier} profile={item.get('profile')} "
                f"sector={item.get('sector')}"
            )
        else:
            text = item.get("text", "")[:800]
            evidence_lines.append(f"[{eid}] tier={tier} kind={kind}")
            if text:
                evidence_lines.append(f"  {text}")

        evidence_lines.append("")

    # ── Company state (from synthesis) ─────────────────────────────────────
    state_lines = ["## Company State (from Semantic Synthesis)", ""]
    if company_state and isinstance(company_state, dict):
        for key in (
            "business_summary",
            # Lifecycle signals — read these FIRST when deciding optional Tier B fields
            "lifecycle_stage", "margin_trajectory",
            "capital_return_policy", "sbc_intensity",
            # Narrative supporting context
            "growth_outlook", "growth_drivers",
            "margin_trend", "margin_narrative", "key_risks",
            "competitive_position", "macro_context", "conflicts",
            "confidence_self_assessment",
        ):
            val = company_state.get(key)
            if val:
                if isinstance(val, list):
                    state_lines.append(f"**{key}:**")
                    for v in val:
                        state_lines.append(f"  - {v}")
                else:
                    state_lines.append(f"**{key}:** {val}")
                state_lines.append("")
    else:
        state_lines.append("(No company state available — synthesis was skipped or failed)")
        state_lines.append("")

    # ── Task ───────────────────────────────────────────────────────────────
    task_lines = [
        "## Your Task",
        f"Propose DCF assumptions for {ticker} over a {horizon_years}-year horizon.",
        "",
        "**REQUIRED (always 4)**: revenue_growth, fcff_margin, terminal_growth, tax_rate.",
        "",
        "**OPTIONAL (include when warranted by company state — see system prompt)**:",
        "- buyback_yield (when active buyback program exists)",
        "- sbc_pct_revenue (for tech/software names — typically 3-10%)",
        "- revenue_growth_terminal (when near-term growth >15% or hypergrowth lifecycle)",
        "- fcff_margin_terminal (when margin trajectory is non-flat — expansion or compression)",
        "",
        "Do NOT propose: base_revenue, shares_outstanding, net_debt, wacc.",
        "",
        "For each proposal, provide:",
        "- **value**: your best estimate as a decimal (e.g. 0.08 for 8%)",
        "- **rationale**: 2-4 sentences grounded in evidence",
        "- **evidence_refs**: list of evidence_ids from the pack above",
        "- **confidence**: 0.0-1.0 per the guidelines",
        "- **range_low / range_high**: optional, if you want to express uncertainty",
        "",
        "Then provide:",
        "- **overall_narrative**: 3-5 sentences connecting the assumptions into a coherent story",
        "- **key_uncertainties**: 2-4 specific risks that could materially change the numbers",
        "- **overall_confidence**: aggregate confidence score",
        "",
        "Output a single AssumptionMemo JSON object.",
    ]

    return (
        "\n".join(canon_lines)
        + "\n"
        + "\n".join(evidence_lines)
        + "\n"
        + "\n".join(state_lines)
        + "\n"
        + "\n".join(task_lines)
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_memo_refs(
    memo: AssumptionMemo,
    valid_ids: set[str],
) -> list[str]:
    """Check that all evidence_refs in the memo reference real evidence_ids."""
    errors: list[str] = []
    all_refs: set[str] = set()

    for proposal in memo.proposals:
        for ref in proposal.evidence_refs:
            all_refs.add(ref)
            if ref not in valid_ids:
                errors.append(
                    f"proposal '{proposal.field}' references unknown evidence_id '{ref}'"
                )
    for ref in memo.evidence_refs:
        all_refs.add(ref)
        if ref not in valid_ids:
            errors.append(f"memo-level references unknown evidence_id '{ref}'")

    return errors


def _validate_proposal_fields(memo: AssumptionMemo) -> list[str]:
    """Check that proposals only target allowed Tier B fields.

    Required fields MUST be present. Optional fields MAY be present but
    are not required (math layer defaults handle their absence).
    """
    errors: list[str] = []
    proposed_fields = {p.field for p in memo.proposals}

    for field in proposed_fields:
        if field not in _TIER_B_PROPOSABLE:
            errors.append(
                f"proposal field '{field}' is not allowed. "
                f"Allowed (required): {', '.join(sorted(_TIER_B_REQUIRED))}. "
                f"Allowed (optional): {', '.join(sorted(_TIER_B_OPTIONAL))}."
            )

    missing_required = _TIER_B_REQUIRED - proposed_fields
    if missing_required:
        errors.append(
            f"Missing REQUIRED proposals for: {', '.join(sorted(missing_required))}"
        )

    return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_assumptions_line(
    assumptions: dict[str, float],
    wacc_components: dict[str, Any],
) -> str:
    """One-line summary of key assumptions for activity trace display."""
    parts: list[str] = []

    # Growth (with terminal glide if present)
    g = assumptions.get("revenue_growth")
    g_term = assumptions.get("revenue_growth_terminal")
    if g is not None:
        if g_term is not None and abs(g_term - g) > 1e-6:
            parts.append(f"growth={g:.1%}→{g_term:.1%}")
        else:
            parts.append(f"growth={g:.2%}")

    # Margin (with terminal glide if present)
    m = assumptions.get("fcff_margin")
    m_term = assumptions.get("fcff_margin_terminal")
    if m is not None:
        if m_term is not None and abs(m_term - m) > 1e-6:
            parts.append(f"margin={m:.1%}→{m_term:.1%}")
        else:
            parts.append(f"margin={m:.2%}")

    # Other required fields
    for field in ("terminal_growth", "tax_rate"):
        v = assumptions.get(field)
        if v is not None:
            parts.append(f"{field}={v:.2%}")

    # WACC
    wacc = assumptions.get("wacc")
    if wacc is not None:
        wacc_src = wacc_components.get("method", "?")
        parts.append(f"wacc={wacc:.2%}({wacc_src})")

    # Optional capital return / SBC
    buyback = assumptions.get("buyback_yield")
    if buyback is not None and abs(buyback) > 1e-6:
        parts.append(f"buyback={buyback:.1%}")
    sbc = assumptions.get("sbc_pct_revenue")
    if sbc is not None and sbc > 1e-6:
        parts.append(f"sbc={sbc:.1%}")

    return ", ".join(parts) if parts else "assumptions built"


def _evidence_refs_for_field(evidence_pack: dict[str, Any], field: str) -> list[str]:
    """Return evidence IDs for structured evidence carrying a specific field."""
    refs: list[str] = []
    for item in evidence_pack.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "structured_fundamental" and item.get("field") == field:
            eid = item.get("evidence_id")
            if eid:
                refs.append(str(eid))
    return refs


def _backfill_capital_mechanics(
    *,
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    fundamentals: dict[str, dict[str, Any]],
    evidence_pack: dict[str, Any],
    memo_dict: dict[str, Any] | None,
) -> None:
    """Ensure buyback/SBC mechanics reach HITL when structured data exists.

    The LLM is allowed to omit optional fields, but for repurchase-heavy tech
    names those omissions materially change per-share value. Structured
    statement data is more reliable than a memo omission, so backfill only from
    canonical fundamentals and leave any LLM/user value intact.
    """
    for field in ("buyback_yield", "sbc_pct_revenue"):
        if field in assumptions:
            continue
        meta = fundamentals.get(field)
        if not isinstance(meta, dict) or meta.get("value") is None:
            continue
        clipped = clip_to_field_range(field, float(meta["value"]))
        if clipped is None:
            logger.warning(
                "DCF memo capital backfill skipped field=%s value=%s out of range",
                field, meta.get("value"),
            )
            continue

        refs = _evidence_refs_for_field(evidence_pack, field)
        assumptions[field] = clipped
        provenance[field] = {
            "source": meta.get("source", "structured_fundamental"),
            "evidence": meta.get("evidence", "Structured financial-statement-derived assumption."),
            "reference": ", ".join(refs),
            "evidence_refs": refs,
            "confidence": meta.get("confidence", 0.8),
            "field": meta.get("field"),
            "as_of": meta.get("as_of"),
        }

        if memo_dict is not None:
            proposals = memo_dict.setdefault("proposals", [])
            if isinstance(proposals, list) and not any(
                isinstance(p, dict) and p.get("field") == field
                for p in proposals
            ):
                proposals.append({
                    "field": field,
                    "value": clipped,
                    "rationale": str(provenance[field]["evidence"]),
                    "evidence_refs": refs,
                    "confidence": float(provenance[field]["confidence"]),
                    "range_low": None,
                    "range_high": None,
                })
            evidence_refs = memo_dict.setdefault("evidence_refs", [])
            if isinstance(evidence_refs, list):
                for ref in refs:
                    if ref not in evidence_refs:
                        evidence_refs.append(ref)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def propose_assumptions_node(state: dict) -> dict:
    """Propose DCF assumptions from evidence + synthesis via structured LLM call.

    Replaces ``build_assumptions_node`` in the target architecture. Tier A
    fields are locked from canonical fundamentals; the LLM proposes Tier B;
    WACC is resolved deterministically via CAPM/priors.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    ticker = state.get("ticker", "???")
    horizon_years = int(state.get("horizon_years") or 5)
    evidence_pack = state.get("evidence_pack") or {}
    company_state = state.get("company_state")
    fundamentals = state.get("fundamentals") or {}
    profile = state.get("profile") or "default"
    features = dict(state.get("features") or {})
    overrides = state.get("assumption_overrides") or {}

    emit_step("propose_assumptions", "start", parent_step_id, {"ticker": ticker})

    # ── Step 1: Lock Tier A from canonical fundamentals ────────────────────
    assumptions: dict[str, float] = {}
    provenance: dict[str, dict[str, Any]] = {}

    for field in _TIER_A_FIELDS:
        meta = fundamentals.get(field)
        if isinstance(meta, dict) and meta.get("value") is not None:
            value = float(meta["value"])
            clipped = clip_to_field_range(field, value)
            if clipped is not None:
                assumptions[field] = clipped
                provenance[field] = {
                    "source": meta.get("source", "canonical"),
                    "evidence": meta.get("evidence", "Canonical fundamental from financial statements."),
                    "confidence": meta.get("confidence", 0.9),
                }

    # Safety net for downstream nodes: a missing Tier A field (e.g. JPM and
    # other banks where `net_debt` is meaningless and fundamentals omit it)
    # would crash compute_valuation / scenario_runner with KeyError. Default
    # the optional ones to 0 with a synthetic provenance so the math degrades
    # gracefully rather than aborting the whole run before finalize.
    for field, default in (("net_debt", 0.0),):
        if field not in assumptions:
            assumptions[field] = default
            provenance.setdefault(field, {
                "source": "default_zero",
                "evidence": (
                    f"{field} unavailable from canonical fundamentals "
                    "(common for financial-sector tickers); defaulted to 0."
                ),
                "confidence": 0.4,
            })

    canonical_for_prompt = {
        field: assumptions.get(field)
        for field in _TIER_A_FIELDS
    }

    # ── Step 2: LLM proposes Tier B ────────────────────────────────────────
    valid_ids = {
        item.get("evidence_id", "")
        for item in evidence_pack.get("items", [])
    }
    valid_ids.discard("")

    user_message = _build_memo_user_message(
        ticker=ticker,
        horizon_years=horizon_years,
        evidence_pack=evidence_pack,
        company_state=company_state,
        canonical=canonical_for_prompt,
    )

    memo: AssumptionMemo | None = None
    last_error: str | None = None

    for attempt in range(1 + MAX_MEMO_RETRIES):
        try:
            structured_llm = memo_llm.with_structured_output(AssumptionMemo)
            memo = structured_llm.invoke([
                SystemMessage(content=_MEMO_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ])

            if not isinstance(memo, AssumptionMemo):
                last_error = f"LLM returned {type(memo).__name__} instead of AssumptionMemo"
                logger.warning("DCF memo attempt %d: %s", attempt + 1, last_error)
                memo = None
                continue

            # Validate field restrictions
            field_errors = _validate_proposal_fields(memo)
            if field_errors:
                last_error = "; ".join(field_errors)
                logger.warning(
                    "DCF memo attempt %d: invalid fields: %s",
                    attempt + 1, last_error,
                )
                user_message += (
                    f"\n\n## CORRECTION REQUIRED\n{last_error}\n"
                    f"Regenerate with ONLY these fields: "
                    f"{', '.join(sorted(_TIER_B_PROPOSABLE))}"
                )
                memo = None
                continue

            # Validate evidence refs
            ref_errors = _validate_memo_refs(memo, valid_ids)
            if ref_errors:
                last_error = "; ".join(ref_errors)
                logger.warning(
                    "DCF memo attempt %d: invalid refs: %s",
                    attempt + 1, last_error,
                )
                user_message += (
                    f"\n\n## CORRECTION REQUIRED\n{last_error}\n"
                    f"Regenerate with ONLY valid evidence_ids from the pack above."
                )
                memo = None
                continue

            break

        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("DCF memo attempt %d failed: %s", attempt + 1, exc)
            memo = None

    # ── Step 3: Apply proposals or fall back to defaults ────────────────────
    if memo is not None:
        for proposal in memo.proposals:
            field = proposal.field
            value = float(proposal.value)
            clipped = clip_to_field_range(field, value)
            if clipped is None:
                logger.warning(
                    "DCF memo: proposal for '%s' value=%s out of range, skipping",
                    field, value,
                )
                continue
            assumptions[field] = clipped
            provenance[field] = {
                "source": "llm_memo",
                "evidence": proposal.rationale,
                "reference": ", ".join(proposal.evidence_refs),
                "evidence_refs": list(proposal.evidence_refs),
                "confidence": proposal.confidence,
                "range_low": proposal.range_low,
                "range_high": proposal.range_high,
            }
            logger.info(
                "DCF memo applied field=%s value=%s confidence=%s",
                field, clipped, proposal.confidence,
            )

        # Store the full memo for audit trail
        memo_dict = memo.model_dump()
    else:
        logger.error(
            "DCF memo failed after %d attempts, using profile-prior midpoints for Tier B: %s",
            1 + MAX_MEMO_RETRIES, last_error,
        )
        # Fall back to profile-prior midpoints (sector-appropriate) for REQUIRED
        # fields only. Optional fields stay unset — math layer defaults apply.
        for field in sorted(_TIER_B_REQUIRED):
            if field in assumptions:
                continue
            mid = prior_band_midpoint(profile, field)
            if mid is not None:
                clipped = clip_to_field_range(field, float(mid))
                if clipped is not None:
                    assumptions[field] = clipped
                    provenance[field] = {
                        "source": "profile_prior_fallback",
                        "evidence": (
                            f"Profile-prior midpoint for '{profile}' "
                            f"(LLM memo failed: {last_error})."
                        ),
                        "confidence": 0.35,
                    }
                else:
                    logger.warning(
                        "DCF memo fallback: prior midpoint for '%s' out of range", field,
                    )
            else:
                logger.warning(
                    "DCF memo fallback: no prior for field '%s' in profile '%s'",
                    field, profile,
                )
        memo_dict = None

    _backfill_capital_mechanics(
        assumptions=assumptions,
        provenance=provenance,
        fundamentals=fundamentals,
        evidence_pack=evidence_pack,
        memo_dict=memo_dict,
    )

    # ── Step 4: Apply user overrides ────────────────────────────────────────
    for key, value in filter_user_assumption_overrides(overrides).items():
        normalized = clip_to_field_range(key, float(value))
        if normalized is None:
            logger.warning(
                "DCF ignored out-of-range override field=%s value=%s",
                key, value,
            )
            continue
        assumptions[key] = normalized
        provenance[key] = {
            "source": "user_override",
            "evidence": "User-provided assumption override.",
            "confidence": 1.0,
        }

    # ── Step 5: Resolve WACC deterministically ──────────────────────────────
    wacc_components = resolve_wacc_from_features(
        assumptions, provenance,
        features=features, profile=profile, overrides=overrides,
    )

    # ── Step 6: Hard-band enforcement + plausibility checks ─────────────────
    # Clamp implausible assumptions to their profile hard bounds BEFORE the
    # valuation runs. A sub-floor FCFF margin (below the SBC drag) would
    # otherwise produce negative FCFF → a negative implied share price. The
    # clamp is recorded in provenance + surfaced as a warn flag so the report
    # stays honest about the override.
    assumptions, clamp_flags = enforce_hard_bands(assumptions, profile)
    for flag in clamp_flags:
        field = flag["field"]
        prev = dict(provenance.get(field) or {})
        prev_evidence = str(prev.get("evidence") or "")
        provenance[field] = {
            **prev,
            "clamped": True,
            "clamped_from": flag["clamped_from"],
            "clamped_to": flag["clamped_to"],
            "confidence": min(float(prev.get("confidence") or 0.5), 0.30),
            "evidence": (
                (prev_evidence + " ") if prev_evidence else ""
            ) + (
                f"[CLAMPED] proposed {flag['clamped_from']:.4g} is implausible for "
                f"profile '{profile}'; clamped to {flag['clamped_to']:.4g}."
            ),
        }
        logger.warning(
            "DCF assumption CLAMPED field=%s from=%.4g to=%.4g profile=%s",
            field, flag["clamped_from"], flag["clamped_to"], profile,
        )

    assumption_flags = clamp_flags + check_assumption_plausibility(assumptions, profile)
    for flag in assumption_flags:
        logger.warning(
            "DCF assumption flag severity=%s field=%s value=%s",
            flag.get("severity"), flag.get("field"), flag.get("value"),
        )

    logger.info(
        "DCF propose_assumptions assumptions=%s provenance=%s flags=%d",
        json.dumps(assumptions, ensure_ascii=False),
        json.dumps({k: v.get("source") for k, v in provenance.items()}, ensure_ascii=False),
        len(assumption_flags),
    )

    emit_step(
        "propose_assumptions", "complete", parent_step_id,
        {
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "assumption_flags": assumption_flags,
            "provenance_sources": {
                k: v.get("source") for k, v in provenance.items()
            },
            "wacc_components": wacc_components,
            "has_memo": memo_dict is not None,
            "memo_overall_confidence": memo_dict.get("overall_confidence") if memo_dict else None,
            "memo_proposals": [
                {
                    "field": p.field,
                    "value": p.value,
                    "rationale": p.rationale,
                    "confidence": p.confidence,
                }
                for p in (memo.proposals if memo else [])
            ],
            # Human-readable one-liner for activity trace
            "summary_line": _fmt_assumptions_line(assumptions, wacc_components),
        },
    )

    result: dict[str, Any] = {
        "assumptions": assumptions,
        "assumption_provenance": provenance,
        "assumption_flags": assumption_flags,
        "wacc_components": wacc_components,
    }
    if memo_dict is not None:
        result["assumption_memo"] = memo_dict

    return result

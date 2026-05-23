"""ReviewState TypedDict and Pydantic finding models for the DCF review subgraph.

The review subgraph is isolated from DCFState — it receives a one-way snapshot
at invocation time and returns structured adjustments that the gateway
(run_review_subgraph in graph.py) applies back to DCFState.

This isolation means every iteration is inspectable as a standalone artifact.
"""
from __future__ import annotations

from typing import Any, Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Finding models
# ---------------------------------------------------------------------------


class ScenarioFinding(BaseModel):
    """A single problem identified by the review_deep_dive node."""

    scenario: Literal["bear", "base", "bull", "all"] = Field(
        description=(
            "Which scenario this finding applies to. "
            "Use 'all' for systemic issues that affect every scenario."
        )
    )
    field: str = Field(
        description=(
            "The assumption field: revenue_growth, fcff_margin, "
            "terminal_growth, wacc, or tax_rate."
        )
    )
    direction: Literal["higher", "lower", "neutral"] = Field(
        description="Should this field's value be adjusted higher or lower?"
    )
    confidence: float = Field(
        description="Confidence in this finding, 0.0–1.0.",
        ge=0.0,
        le=1.0,
    )
    severity: Literal["high", "medium", "low"] = Field(
        description=(
            "high = directly contradicts evidence or creates material inconsistency; "
            "medium = weak support or ambiguous; "
            "low = minor stylistic concern."
        )
    )
    layer: Literal[
        "evidence_memo",
        "thesis_assumptions",
        "consistency",
        "scenario_distinguishability",
    ] = Field(description="Which review layer produced this finding.")
    reasoning: str = Field(
        description="One sentence citing a specific evidence ref or thesis element."
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence item IDs that support this finding.",
    )
    is_unanchored: bool = Field(
        default=False,
        description="True when the assumption lacks evidence/thesis backing.",
    )


class ReviewFindings(BaseModel):
    """All findings from a single review_deep_dive pass."""

    evidence_memo_findings: list[ScenarioFinding] = Field(
        default_factory=list,
        description="Evidence ↔ Memo consistency issues.",
    )
    thesis_assumption_findings: list[ScenarioFinding] = Field(
        default_factory=list,
        description="Thesis ↔ Assumption misalignments.",
    )
    consistency_findings: list[ScenarioFinding] = Field(
        default_factory=list,
        description="Internal consistency issues (TGR vs Rf, terminal weight, etc.).",
    )
    scenario_distinguishability_findings: list[ScenarioFinding] = Field(
        default_factory=list,
        description="Bear/bull scenarios not meaningfully differentiated from base.",
    )
    anchoring_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Round-number or prior-proximity flags, "
            "e.g. 'revenue_growth=0.10 is a suspiciously round number'."
        ),
    )
    should_stop: bool = Field(
        description="True if no meaningful issues remain and the model is coherent."
    )
    stop_reasoning: str = Field(
        default="",
        description="Why the reviewer says to stop, if should_stop=True.",
    )


# ---------------------------------------------------------------------------
# Isolated state
# ---------------------------------------------------------------------------


class ReviewState(TypedDict):
    # ── Snapshot inputs (one-way, populated at invocation time) ───────────
    ticker: str
    parent_step_id: str
    # Raw evidence sources — reviewer cross-checks memo evidence_refs here
    evidence_pack: dict[str, Any]
    # Market-implied signals
    implied_growth: float | None
    implied_margin: float | None
    wacc_sanity: dict[str, Any] | None
    # Structured company understanding from synthesis
    company_state: dict[str, Any] | None
    # Investment thesis (bull/bear narrative)
    thesis: dict[str, Any] | None
    # What the LLM proposed — treated as SUSPECT, cross-checked against evidence
    assumption_memo: dict[str, Any] | None
    # Base-case assumptions
    current_assumptions: dict[str, float]
    # Bear / base / bull scenario dicts: {"name", "probability", "assumptions": {...}, "rationale"}
    scenarios: list[dict[str, Any]]
    # Deterministic quality flags
    quality_flags: list[dict[str, Any]]
    # Prior review iterations — prevents re-flagging already-addressed issues
    assumption_history: list[dict[str, Any]]
    review_iteration: int

    # ── Outputs ────────────────────────────────────────────────────────────
    findings: ReviewFindings | None
    # Per-scenario bounded deltas: {"base": {"revenue_growth": -0.01}, "bear": {...}}
    suggested_adjustments: dict[str, dict[str, float]] | None
    review_summary: str
    should_stop: bool

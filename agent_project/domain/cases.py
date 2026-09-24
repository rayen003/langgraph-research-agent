"""Case, task, source, and valuation contracts for finance workflows."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .objects import AgentObject, ObjectReference, SourceRef


EntityKind = Literal["company", "security", "person", "market", "document", "case", "other"]
ListedStatus = Literal["listed", "private", "unknown"]
ValuationMethodId = Literal[
    "dcf_fcff",
    "fcfe",
    "ddm",
    "residual_income",
    "trading_comps",
    "precedent_transactions",
    "sotp",
    "nav",
    "lbo",
    "vc_method",
]


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    name: str = Field(min_length=1)
    ticker: str | None = None
    isin: str | None = None
    exchange: str | None = None
    country: str | None = None
    sector: str | None = None
    currency: str | None = None
    private_identifier: str | None = None


class ResearchCase(AgentObject):
    object_type: Literal["research_case"] = "research_case"
    target_entity: EntityRef
    listed_status: ListedStatus = "unknown"
    purpose: Literal["research", "transaction", "class_project", "other"] = "research"
    audience: str = "analyst"
    mandate: str = ""
    base_currency: str | None = None
    source_policy: Literal["any", "approved_only", "docs_only"] = "approved_only"
    constraints: dict[str, Any] = Field(default_factory=dict)
    entity_refs: list[EntityRef] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    valuation_method_ids: list[ValuationMethodId] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if not self.entity_refs:
            self.entity_refs.append(self.target_entity)


class ValuationMethodSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method_id: ValuationMethodId
    label: str
    method_family: Literal["intrinsic", "relative", "asset_based", "transaction", "sponsor", "early_stage"]
    listed_status_applicability: list[ListedStatus] = Field(default_factory=list)
    company_fit: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    output_schema_ref: str | None = None


class ValuationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    method_id: ValuationMethodId
    target_entity: EntityRef
    as_of_date: str | None = None
    input_object_ids: list[str] = Field(default_factory=list)
    assumptions: dict[str, Any] = Field(default_factory=dict)


class ValuationRunResult(AgentObject):
    object_type: Literal["valuation_run"] = "valuation_run"
    case_id: str = Field(min_length=1)
    method_id: ValuationMethodId
    target_entity: EntityRef
    currency: str | None = None
    implied_equity_value: float | None = None
    implied_share_price: float | None = None
    method_outputs: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[ObjectReference] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def default_valuation_methods() -> list[ValuationMethodSpec]:
    return [
        ValuationMethodSpec(
            method_id="dcf_fcff",
            label="DCF / FCFF",
            method_family="intrinsic",
            listed_status_applicability=["listed", "private"],
            company_fit=["operating company", "forecastable free cash flow"],
            required_inputs=["revenue", "fcff_margin", "wacc", "terminal_growth", "net_debt", "shares_outstanding"],
            optional_inputs=["scenarios", "peer_margin_range", "market_implied_wacc"],
            output_schema_ref="ValuationRunResult.method_outputs.dcf_fcff",
        ),
        ValuationMethodSpec(
            method_id="ddm",
            label="Dividend Discount Model",
            method_family="intrinsic",
            listed_status_applicability=["listed"],
            company_fit=["mature dividend payer", "bank", "insurer", "regulated capital return"],
            required_inputs=["dividend_per_share", "cost_of_equity", "terminal_dividend_growth"],
            optional_inputs=["payout_ratio", "book_value", "capital_ratio"],
            output_schema_ref="ValuationRunResult.method_outputs.ddm",
        ),
        ValuationMethodSpec(
            method_id="trading_comps",
            label="Trading Comparables",
            method_family="relative",
            listed_status_applicability=["listed", "private"],
            company_fit=["available peer set", "market multiple triangulation"],
            required_inputs=["peer_set", "selected_multiple", "target_metric"],
            optional_inputs=["outlier_policy", "calendarization", "growth_adjustment"],
            output_schema_ref="ValuationRunResult.method_outputs.trading_comps",
        ),
    ]

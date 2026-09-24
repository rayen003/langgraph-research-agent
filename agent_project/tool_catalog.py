"""Canonical capability catalog and generated caller exposure."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from domain.tool_execution import CapabilitySpec, ToolCachePolicy
from tool_runtime import CapabilityRegistry


_ALL_SURFACES = ["chat", "research", "case"]


CURRENT_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        tool_id="calculator", title="Calculator", execution_kind="compute",
        latency_class="instant", first_event_slo_ms=50, timeout_ms=1_000,
        exposed_in=_ALL_SURFACES,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=300),
    ),
    CapabilitySpec(
        tool_id="search_web", title="Web search", summary="Retrieve current external information.",
        latency_class="interactive", timeout_ms=8_000,
        exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=120, stale_while_revalidate_seconds=300),
    ),
    CapabilitySpec(
        tool_id="retrieve_tool_result", title="Stored result retrieval",
        exposed_in=_ALL_SURFACES,
        latency_class="instant", first_event_slo_ms=50, timeout_ms=1_000,
    ),
    CapabilitySpec(
        tool_id="get_company_financials", title="Company financial statements",
        summary="Fetch multi-period structured financial statements from the DCF data adapter.",
        latency_class="interactive", timeout_ms=20_000, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        output_schema_ref="company_financial_history.v1",
    ),
    CapabilitySpec(
        tool_id="render_financial_chart", title="Financial chart renderer",
        summary="Render a versioned chart artifact from a stored financial result ID.",
        execution_kind="write", latency_class="extended", execution_lane="background",
        timeout_ms=65_000, idempotent=False, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        input_schema_ref="tool_result_ref.v1", output_schema_ref="chart_artifact.v1",
        produces=["chart", "file"],
        metadata={
            "consumes_result_schema": "company_financial_history.v1",
        },
    ),
    CapabilitySpec(
        tool_id="get_stock_price_history", title="Stock-price history",
        summary="Fetch actual adjusted daily prices for a listed ticker.",
        latency_class="interactive", timeout_ms=20_000, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        output_schema_ref="market_price_history.v1",
    ),
    CapabilitySpec(
        tool_id="render_stock_price_chart", title="Stock-price chart renderer",
        summary="Render a versioned market-price chart from a stored price-history result.",
        execution_kind="write", latency_class="extended", execution_lane="background",
        timeout_ms=65_000, idempotent=False, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        input_schema_ref="tool_result_ref.v1", output_schema_ref="chart_artifact.v1",
        produces=["chart", "file"],
        metadata={
            "consumes_result_schema": "market_price_history.v1",
        },
    ),
    CapabilitySpec(
        tool_id="render_normalized_stock_comparison", title="Normalized stock comparison renderer",
        summary="Render a normalized multi-ticker market-price comparison from stored price-history result IDs.",
        execution_kind="write", latency_class="extended", execution_lane="background",
        timeout_ms=65_000, idempotent=False, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        input_schema_ref="tool_result_refs.v1", output_schema_ref="chart_artifact.v1",
        produces=["chart", "file"],
        metadata={
            "consumes_result_schema": "market_price_history.v1",
            "minimum_input_results": 2,
        },
    ),
    CapabilitySpec(
        tool_id="execute_python", title="Python execution", execution_kind="write",
        latency_class="extended", execution_lane="background",
        timeout_ms=65_000, idempotent=False, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        sensitive_arg_names=["code"],
    ),
    CapabilitySpec(
        tool_id="search_documents", title="Document search",
        latency_class="interactive", timeout_ms=15_000, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=60),
    ),
    CapabilitySpec(
        tool_id="fetch_sec_filing", title="SEC filing retrieval",
        latency_class="interactive", timeout_ms=20_000, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=3_600),
    ),
    CapabilitySpec(
        tool_id="query_knowledge_graph", title="Knowledge graph query",
        latency_class="interactive", first_event_slo_ms=100, timeout_ms=15_000, exposed_in=_ALL_SURFACES,
        supports_streaming=True, progress_interval_ms=1_000,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=30),
    ),
    CapabilitySpec(
        tool_id="retrieve_context", title="Research step context retrieval",
        summary="Retrieve compact result references from a prior research-plan step.",
        latency_class="instant", first_event_slo_ms=50, timeout_ms=1_000,
        exposed_in=["research"],
    ),
    CapabilitySpec(
        tool_id="run_dcf_workflow", title="DCF workflow", execution_kind="workflow",
        latency_class="background", execution_lane="background",
        timeout_ms=600_000, idempotent=False,
        supports_streaming=True, progress_interval_ms=2_000,
        exposed_in=_ALL_SURFACES, plannable=True, supports_parallel=False,
        allowed_tools=["run_dcf_workflow"],
        accepts=["company_profile", "financial_analysis", "evidence_bundle"],
        produces=["valuation_result"], requires_any=["company_profile", "financial_analysis"],
        max_tool_calls=1, model_tier="specialist",
    ),
    CapabilitySpec(
        tool_id="run_deck_workflow", title="Deck workflow", execution_kind="workflow",
        latency_class="background", execution_lane="background",
        timeout_ms=600_000, idempotent=False,
        supports_streaming=True, progress_interval_ms=2_000,
        exposed_in=_ALL_SURFACES, plannable=True, supports_parallel=False,
        allowed_tools=["run_deck_workflow"],
        accepts=["evidence_bundle", "company_profile", "industry_analysis", "financial_analysis", "valuation_result", "memo"],
        produces=["deck"], requires_any=["evidence_bundle", "company_profile", "memo", "valuation_result"],
        max_tool_calls=1, model_tier="specialist",
    ),
    CapabilitySpec(
        tool_id="run_memo_workflow", title="Memo workflow", execution_kind="workflow",
        latency_class="background", execution_lane="background",
        timeout_ms=300_000, idempotent=False,
        supports_streaming=True, progress_interval_ms=2_000,
        exposed_in=_ALL_SURFACES, plannable=True, supports_parallel=False,
        allowed_tools=["run_memo_workflow"],
        accepts=["evidence_bundle", "company_profile", "industry_analysis", "financial_analysis", "valuation_result"],
        produces=["memo"], requires_any=["evidence_bundle", "company_profile", "financial_analysis"],
        max_tool_calls=1, model_tier="specialist",
    ),
    CapabilitySpec(
        tool_id="company_research", title="Company research", execution_kind="agent",
        summary="Research company fundamentals, developments, strategy, and risks.",
        latency_class="background", execution_lane="background", timeout_ms=300_000,
        exposed_in=["case"], plannable=True,
        allowed_tools=["query_knowledge_graph", "search_documents", "search_web", "retrieve_tool_result", "fetch_sec_filing"],
        accepts=["company", "document", "evidence_pack"], produces=["company_profile", "evidence_bundle"],
        max_tool_calls=4,
    ),
    CapabilitySpec(
        tool_id="industry_research", title="Industry and market research", execution_kind="agent",
        summary="Research market structure, competitors, TAM, demand, and macro drivers.",
        latency_class="background", execution_lane="background", timeout_ms=300_000,
        exposed_in=["case"], plannable=True,
        allowed_tools=["query_knowledge_graph", "search_documents", "search_web", "retrieve_tool_result"],
        accepts=["company", "market", "evidence_pack"], produces=["industry_analysis", "evidence_bundle"],
        max_tool_calls=4,
    ),
    CapabilitySpec(
        tool_id="financial_analysis", title="Financial analysis", execution_kind="agent",
        summary="Analyze statements, performance, earnings quality, and operating drivers.",
        latency_class="background", execution_lane="background", timeout_ms=300_000,
        exposed_in=["case"], plannable=True,
        allowed_tools=["query_knowledge_graph", "search_documents", "search_web", "retrieve_tool_result", "get_company_financials", "render_financial_chart", "calculator", "execute_python"],
        accepts=["financial_statements", "company_profile", "evidence_pack"], produces=["financial_analysis", "evidence_bundle"],
        max_tool_calls=4,
    ),
    CapabilitySpec(
        tool_id="document_analysis", title="Document and contract analysis", execution_kind="agent",
        summary="Analyze filings, contracts, tables, and uploaded documents with citations.",
        latency_class="background", execution_lane="background", timeout_ms=300_000,
        exposed_in=["case"], plannable=True,
        allowed_tools=["search_documents", "query_knowledge_graph"],
        accepts=["document", "evidence_pack"], produces=["document_analysis", "evidence_bundle"],
        max_tool_calls=4,
    ),
)


CURRENT_TOOL_SPECS = CURRENT_CAPABILITY_SPECS
CURRENT_CAPABILITY_REGISTRY = CapabilityRegistry(CURRENT_CAPABILITY_SPECS)
CURRENT_CAPABILITY_REGISTRY.validate_capability_links()


def build_current_capability_registry() -> CapabilityRegistry:
    return CURRENT_CAPABILITY_REGISTRY


def build_current_tool_registry() -> CapabilityRegistry:
    """Compatibility name for universal execution middleware."""
    return build_current_capability_registry()


def capability_ids_for_surface(surface: str) -> list[str]:
    registry = build_current_capability_registry()
    return [
        spec.capability_id
        for spec in registry.specs_for_surface(surface)
        if spec.execution_kind != "agent"
    ]


def select_capability_tools(surface: str, available_tools: Iterable[Any]) -> list[Any]:
    """Generate caller tool exposure from catalog and implementation map."""
    by_name = {
        str(getattr(tool, "name", getattr(tool, "__name__", ""))): tool
        for tool in available_tools
    }
    capability_ids = capability_ids_for_surface(surface)
    missing = [capability_id for capability_id in capability_ids if capability_id not in by_name]
    if missing:
        raise ValueError(f"Missing {surface} capability implementations: {missing}")
    return [by_name[capability_id] for capability_id in capability_ids]


def conversational_tool_ids(*, include_workflows: bool = True) -> list[str]:
    """Capabilities available to bounded conversational ReAct."""
    registry = build_current_capability_registry()
    return [
        capability_id
        for capability_id in capability_ids_for_surface("chat")
        if include_workflows or registry.get(capability_id).execution_kind != "workflow"
    ]


def artifact_renderer_for_result_schema(result_schema: str, output: str) -> str | None:
    """Return renderer tool declared for a durable result schema and output type."""
    for spec in CURRENT_CAPABILITY_SPECS:
        metadata = spec.metadata or {}
        if (
            str(metadata.get("consumes_result_schema") or "") == result_schema
            and output in {str(value) for value in (spec.produces or metadata.get("produces") or [])}
        ):
            return spec.tool_id
    return None


def artifact_render_request_for_results(
    results: list[tuple[str, str]],
    output: str,
) -> dict[str, object] | None:
    """Build declared renderer invocation from available durable result references.

    Prefer renderers with larger declared input cardinality. This lets a
    comparison renderer consume several compatible results instead of creating
    a chart for only one of them.
    """
    candidates: list[tuple[int, CapabilitySpec, list[str]]] = []
    for spec in CURRENT_CAPABILITY_SPECS:
        metadata = spec.metadata or {}
        if output not in {str(value) for value in (spec.produces or metadata.get("produces") or [])}:
            continue
        consumed_schema = str(metadata.get("consumes_result_schema") or "")
        if not consumed_schema:
            continue
        result_ids = [result_id for result_id, schema in results if schema == consumed_schema]
        minimum = int(metadata.get("minimum_input_results") or 1)
        if len(result_ids) < minimum:
            continue
        candidates.append((minimum, spec, result_ids))

    if not candidates:
        return None

    _minimum, spec, result_ids = max(candidates, key=lambda candidate: candidate[0])
    if spec.input_schema_ref == "tool_result_refs.v1":
        args: dict[str, object] = {"input_result_ids": result_ids}
    else:
        args = {"input_result_id": result_ids[-1]}
    return {"tool_id": spec.tool_id, "args": args}


def unsupported_requested_outputs(
    requested_outputs: list[str],
    allowed_tools: list[str] | None,
) -> list[str]:
    """Find artifact outputs unavailable under current tool allowlist."""
    # Reports and tables can be delivered as grounded chat text. Artifact-only
    # outputs require a producer in selected tool set.
    required = {str(output) for output in requested_outputs} - {"answer", "report", "table"}
    if not required:
        return []
    allowed = set(allowed_tools) if allowed_tools is not None else None
    produced: set[str] = set()
    for spec in CURRENT_CAPABILITY_SPECS:
        if allowed is not None and spec.tool_id not in allowed:
            continue
        produced.update(str(output) for output in (spec.produces or spec.metadata.get("produces") or []))
    return sorted(required - produced)

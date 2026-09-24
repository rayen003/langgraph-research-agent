from agent_project.domain.cases import (
    EntityRef,
    ResearchCase,
    SourceRef,
    ValuationMethodSpec,
    default_valuation_methods,
)
from agent_project.domain.execution import TaskResult
from agent_project.domain.objects import AgentObject, ObjectReference, walk_object_dependencies


def test_research_case_search_card_is_compact() -> None:
    case = ResearchCase(
        object_id="case_META_2026",
        title="Meta valuation case",
        summary="Assess Meta ahead of valuation class.",
        target_entity=EntityRef(kind="company", name="Meta Platforms", ticker="META"),
        listed_status="listed",
        payload={"large": "x" * 20_000},
        tags=["valuation", "internet"],
        schema_ref="domain.cases.ResearchCase",
        search_text="Meta META valuation internet advertising AI capex",
        confidence=0.7,
    )

    card = case.search_card()

    assert card == {
        "object_id": "case_META_2026",
        "object_type": "research_case",
        "title": "Meta valuation case",
        "summary": "Assess Meta ahead of valuation class.",
        "schema_ref": "domain.cases.ResearchCase",
        "schema_version": "0.1",
        "search_text": "Meta META valuation internet advertising AI capex",
        "status": "draft",
        "tags": ["valuation", "internet"],
        "entities": [{"kind": "company", "name": "Meta Platforms", "ticker": "META"}],
        "dependency_count": 0,
        "confidence": 0.7,
        "quality": {},
        "updated_at": case.updated_at,
    }
    assert "payload" not in card


def test_workspace_object_payload_preserves_retrievable_envelope() -> None:
    case = ResearchCase(
        object_id="case_AAPL_2026",
        title="Apple strategic review",
        summary="Case root for Apple analysis.",
        target_entity=EntityRef(kind="company", name="Apple", ticker="AAPL"),
        listed_status="listed",
        references=[
            ObjectReference(
                object_id="uploaded_document:apple_10k",
                object_type="uploaded_document",
                relation="uses_source",
                sequence=1,
            ),
        ],
        artifact_paths=["runs/thread_1/report.md"],
        kg_node_ids=["AAPL"],
        source_refs=[
            SourceRef(
                source_id="api:fmp:aapl:income_statement:fy2025",
                source_type="api",
                title="AAPL FY2025 income statement",
                provider="FMP",
                period="FY2025",
                license_status="approved",
            )
        ],
        created_by="case_orchestrator",
        schema_ref="domain.cases.ResearchCase",
    )

    workspace = case.to_workspace_object(session_id="session_1", thread_id="thread_1")

    assert workspace["object_id"] == "case_AAPL_2026"
    assert workspace["object_type"] == "research_case"
    assert workspace["title"] == "Apple strategic review"
    assert workspace["summary"] == "Case root for Apple analysis."
    assert workspace["schema_ref"] == "domain.cases.ResearchCase"
    assert workspace["schema_version"] == "0.1"
    assert workspace["entity_refs"][0]["ticker"] == "AAPL"
    assert workspace["source_refs"][0]["provider"] == "FMP"
    assert workspace["created_by"] == "case_orchestrator"
    assert workspace["source_object_ids"] == ["uploaded_document:apple_10k"]
    assert workspace["artifact_paths"] == ["runs/thread_1/report.md"]
    assert workspace["kg_node_ids"] == ["AAPL"]
    assert workspace["payload"]["object_type"] == "research_case"
    assert workspace["payload"]["target_entity"]["ticker"] == "AAPL"


def test_dependency_walk_is_deterministic_and_cycle_safe() -> None:
    root = AgentObject(
        object_id="task:valuation",
        object_type="case_task",
        title="Run valuation",
        summary="Run selected valuation methods.",
        references=[
            ObjectReference(object_id="task:tam", object_type="case_task", relation="depends_on", sequence=2),
            ObjectReference(object_id="task:data", object_type="case_task", relation="depends_on", sequence=1),
        ],
    )
    data = AgentObject(
        object_id="task:data",
        object_type="case_task",
        title="Collect data",
        summary="Fetch fundamentals.",
        references=[
            ObjectReference(object_id="task:valuation", object_type="case_task", relation="chronological_after", sequence=1),
        ],
    )
    tam = AgentObject(
        object_id="task:tam",
        object_type="case_task",
        title="Build TAM",
        summary="Size market.",
    )

    walked = walk_object_dependencies(root, {obj.object_id: obj for obj in [root, data, tam]})

    assert [obj.object_id for obj in walked] == ["task:data", "task:tam"]


def test_task_result_carries_execution_handoff_fields() -> None:
    result = TaskResult(
        case_id="case_META_2026",
        task_id="task:data",
        capability_id="financial_analysis",
        status="completed",
        summary="FY2025 revenue and margin extracted.",
        output_object_version_ids=["financials:aapl:v1"],
        source_refs=[{"citation_id": "AAPL-10K-2025"}],
        discovered_requirements=[{"type": "latest_quarter"}],
    )

    assert result.capability_id == "financial_analysis"
    assert result.output_object_version_ids == ["financials:aapl:v1"]
    assert result.source_refs[0]["citation_id"] == "AAPL-10K-2025"


def test_valuation_methods_cover_dcf_ddm_and_comps_without_dcf_state() -> None:
    methods = {method.method_id: method for method in default_valuation_methods()}

    assert set(methods) >= {"dcf_fcff", "ddm", "trading_comps"}
    assert methods["dcf_fcff"].method_family == "intrinsic"
    assert "dividend_per_share" in methods["ddm"].required_inputs
    assert "peer_set" in methods["trading_comps"].required_inputs
    assert ValuationMethodSpec.model_validate(methods["ddm"].model_dump()).method_id == "ddm"

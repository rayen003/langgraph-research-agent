import pytest


def _task(task_id: str, dependencies: list[str] | None = None, capability: str = "company_research") -> dict:
    return {
        "task_id": task_id,
        "capability_id": capability,
        "objective": task_id,
        "dependency_task_ids": dependencies or [],
    }


def test_case_plan_rejects_cycles() -> None:
    from domain.execution import CasePlan

    with pytest.raises(ValueError, match="cycle"):
        CasePlan(case_id="case-1", objective="research", tasks=[_task("a", ["b"]), _task("b", ["a"])])


def test_ready_tasks_support_parallel_fanout_and_dependency_join() -> None:
    from domain.execution import CasePlan, ready_tasks

    plan = CasePlan(case_id="case-1", objective="memo", tasks=[
        _task("company"),
        _task("industry", capability="industry_research"),
        _task("memo", ["company", "industry"], capability="run_memo_workflow"),
    ])
    assert [task.task_id for task in ready_tasks(plan, {})] == ["company", "industry"]
    results = {
        "company": {"status": "completed"},
        "industry": {"status": "completed"},
    }
    assert [task.task_id for task in ready_tasks(plan, results)] == ["memo"]


def test_memo_and_deck_capabilities_do_not_require_dcf() -> None:
    from tool_catalog import build_current_capability_registry

    registry = build_current_capability_registry()
    memo = registry.get("run_memo_workflow")
    deck = registry.get("run_deck_workflow")
    assert "valuation_result" not in memo.requires_all
    assert "valuation_result" not in deck.requires_all
    assert "evidence_bundle" in memo.requires_any
    assert "memo" in deck.requires_any


def test_case_task_result_reducer_can_reset_previous_case() -> None:
    from domain.execution import merge_case_task_results

    assert merge_case_task_results(
        {"old-task": {"status": "completed"}},
        {"__reset__": True},
    ) == {}


def test_capability_requirements_validate_declared_dag_inputs() -> None:
    from domain.execution import TaskSpec
    from tool_catalog import build_current_capability_registry

    registry = build_current_capability_registry()
    research = TaskSpec(
        task_id="research", capability_id="company_research", objective="Research",
        required_output_types=["evidence_bundle"],
    )
    memo = TaskSpec(
        task_id="memo", capability_id="run_memo_workflow", objective="Memo",
        dependency_task_ids=["research"], required_output_types=["memo"],
    )
    registry.validate_dependencies([research, memo])

    invalid_memo = memo.model_copy(update={"dependency_task_ids": []})
    with pytest.raises(ValueError, match="requires one of"):
        registry.validate_dependencies([research, invalid_memo])


def test_single_registry_generates_consistent_workflow_exposure() -> None:
    from tool_catalog import build_current_capability_registry, capability_ids_for_surface

    registry = build_current_capability_registry()
    expected = {"run_dcf_workflow", "run_deck_workflow", "run_memo_workflow"}

    assert expected <= set(capability_ids_for_surface("chat"))
    assert expected <= set(capability_ids_for_surface("research"))
    assert expected <= set(capability_ids_for_surface("case"))
    assert expected <= {card["capability_id"] for card in registry.cards(plannable_only=True)}


def test_tool_names_are_compatibility_aliases_not_parallel_contracts() -> None:
    from domain.tool_execution import CapabilitySpec, ToolSpec
    from tool_runtime import CapabilityRegistry, ToolRegistry

    assert ToolSpec is CapabilitySpec
    assert ToolRegistry is CapabilityRegistry


def test_generated_surfaces_resolve_workflow_implementations() -> None:
    from case_orchestration import _tool_registry
    from graphs.conversational import CHAT_TOOLS
    from graphs.research import TOOLS

    expected = {"run_dcf_workflow", "run_deck_workflow", "run_memo_workflow"}

    assert expected <= {tool.name for tool in CHAT_TOOLS}
    assert expected <= {tool.name for tool in TOOLS}
    assert expected <= set(_tool_registry())


def test_generated_surface_fails_when_declared_implementation_is_missing() -> None:
    from tool_catalog import select_capability_tools

    with pytest.raises(ValueError, match="Missing chat capability implementations"):
        select_capability_tools("chat", [])

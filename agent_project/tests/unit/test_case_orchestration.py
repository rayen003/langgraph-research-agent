import json

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Send


class _Planner:
    def invoke(self, _messages):
        return AIMessage(content=json.dumps({
            "objective": "Research Apple and write memo",
            "deliverables": ["memo"],
            "tasks": [
                {
                    "task_id": "company",
                    "capability_id": "company_research",
                    "objective": "Research Apple",
                    "dependency_task_ids": [],
                    "required_output_types": ["evidence_bundle"],
                },
                {
                    "task_id": "industry",
                    "capability_id": "industry_research",
                    "objective": "Research smartphone market",
                    "dependency_task_ids": [],
                    "required_output_types": ["industry_analysis"],
                },
                {
                    "task_id": "memo",
                    "capability_id": "run_memo_workflow",
                    "objective": "Write memo",
                    "dependency_task_ids": ["company", "industry"],
                    "required_output_types": ["memo"],
                },
            ],
        }))


class _FlatPlanner:
    def invoke(self, _messages):
        return AIMessage(content=json.dumps({
            "objective": "Fetch Apple price and latest headline",
            "deliverables": ["short answer"],
            "tasks": [
                {
                    "task_id": "price",
                    "capability_id": "company_research",
                    "objective": "Fetch price",
                    "dependency_task_ids": [],
                    "required_output_types": ["evidence_bundle"],
                },
                {
                    "task_id": "news",
                    "capability_id": "company_research",
                    "objective": "Fetch latest headline",
                    "dependency_task_ids": [],
                    "required_output_types": ["evidence_bundle"],
                },
            ],
        }))


def test_case_planner_and_scheduler_fan_out_without_forcing_dcf(monkeypatch):
    import case_orchestration

    monkeypatch.setattr(case_orchestration, "upsert_workspace_object", lambda obj, **_kwargs: {**obj, "version_id": "case:v1"})
    monkeypatch.setattr(case_orchestration, "emit_ui_event", lambda _event: None)
    state = {
        "messages": [HumanMessage(content="Research Apple and write an investment memo")],
        "thread_id": "thread-1",
        "session_id": "session-1",
        "route_decision": {"route_level": "case", "selected_case_type": "equity_research"},
    }
    planned = case_orchestration.plan_case(state, planner_llm=_Planner())
    assert planned["case_status"] == "planned"
    first_state = {**state, **planned}
    prepared = case_orchestration.prepare_case_dispatch_node(first_state)
    sends = case_orchestration.dispatch_case_tasks({**first_state, **prepared})

    assert isinstance(sends, list)
    assert all(isinstance(send, Send) for send in sends)
    assert [send.arg["task_packet"]["task"]["task_id"] for send in sends] == ["company", "industry"]
    assert sends[0].arg["task_packet"]["task"]["execution_policy"]["max_tool_calls"] == 4
    assert "run_dcf_workflow" not in [task["capability_id"] for task in planned["case_plan"]["tasks"]]

    results = {
        "company": {"task_id": "company", "status": "completed", "output_object_version_ids": ["company:v1"]},
        "industry": {"task_id": "industry", "status": "completed", "output_object_version_ids": ["industry:v1"]},
    }
    next_state = {**state, **planned, "case_task_results": results}
    next_prepared = case_orchestration.prepare_case_dispatch_node(next_state)
    next_sends = case_orchestration.dispatch_case_tasks({**next_state, **next_prepared})
    assert len(next_sends) == 1
    assert next_sends[0].arg["task_packet"]["task"]["task_id"] == "memo"
    assert len(next_sends[0].arg["task_packet"]["dependency_results"]) == 2


def test_flat_case_plan_downgrades_without_creating_case_object(monkeypatch):
    import case_orchestration

    def fail_if_persisted(*_args, **_kwargs):
        raise AssertionError("flat work must not create ResearchCase object")

    events: list[dict] = []
    monkeypatch.setattr(case_orchestration, "upsert_workspace_object", fail_if_persisted)
    monkeypatch.setattr(case_orchestration, "emit_ui_event", events.append)
    state = {
        "messages": [HumanMessage(content="Fetch Apple price and latest headline")],
        "thread_id": "thread-flat",
        "session_id": "session-flat",
        "route_decision": {
            "route_level": "case",
            "selected_case_type": "equity_research",
            "max_tool_calls": 2,
        },
    }

    planned = case_orchestration.plan_case(state, planner_llm=_FlatPlanner())

    assert planned["case_status"] == "downgraded_to_small_task"
    assert planned["case_id"] is None
    assert planned["case_plan"] is None
    assert planned["route_decision"]["route_level"] == "small_task"
    assert planned["route_decision"]["playbook_id"] is None
    assert planned["route_decision"]["selected_case_type"] is None
    assert planned["selected_playbook"] is None
    assert planned["tool_policy"] == {
        "allowed_tools": None,
        "max_tool_calls": 4,
        "route_level": "small_task",
        "playbook_id": None,
    }
    assert planned["current_turn"]["delegation"]["status"] == "not_required"
    assert events[-1]["stage"] == "case_dag_guard"
    assert events[-1]["next_node"] == "chat"

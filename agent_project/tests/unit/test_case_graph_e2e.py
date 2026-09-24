from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage


def _route() -> dict:
    return {
        "route_level": "case",
        "playbook_id": None,
        "selected_workflow": None,
        "selected_case_type": "equity_research",
        "confidence": 0.95,
        "latency_class": "background",
        "max_tool_calls": 8,
        "creates_objects": True,
        "needs_confirmation": False,
        "object_policy": "case",
        "reason": "multiple deliverables",
    }


class _Router:
    def invoke(self, _messages):
        import json
        return AIMessage(content=json.dumps(_route()))


def test_main_graph_executes_parallel_case_and_dependency_join(monkeypatch):
    import case_orchestration
    import file as main_graph
    from domain.execution import TaskResult

    plan = {
        "case_id": "case:e2e",
        "objective": "Research and memo",
        "deliverables": ["memo"],
        "tasks": [
            {"task_id": "company", "capability_id": "company_research", "objective": "Company", "dependency_task_ids": []},
            {"task_id": "industry", "capability_id": "industry_research", "objective": "Industry", "dependency_task_ids": []},
            {"task_id": "memo", "capability_id": "run_memo_workflow", "objective": "Memo", "dependency_task_ids": ["company", "industry"]},
        ],
    }
    monkeypatch.setattr(main_graph, "intent_llm", _Router())
    monkeypatch.setattr(main_graph, "record_route_decision", lambda **_kwargs: {})
    monkeypatch.setattr(main_graph, "build_memory_context", lambda _state: {"retrieved_object_cards": [], "expanded_dependencies": [], "source_refs": []})
    monkeypatch.setattr(main_graph, "build_evidence_pack", lambda _state: {"query": "", "fact_refs": [], "chunk_refs": []})
    monkeypatch.setattr(main_graph, "plan_case", lambda _state: {
        "case_id": "case:e2e", "case_plan": plan, "case_task_results": {"__reset__": True}, "pending_task_packets": [],
    })

    def fake_execute(packet, worker_llm=None):
        return TaskResult(
            task_id=packet.task.task_id,
            case_id=packet.case_id,
            capability_id=packet.task.capability_id,
            status="completed",
            summary=f"done:{packet.task.task_id}",
            output_object_version_ids=[f"result:{packet.task.task_id}:v1"],
        )

    monkeypatch.setattr(case_orchestration, "execute_task", fake_execute)
    monkeypatch.setattr(case_orchestration, "synthesize_case", lambda state, planner_llm=None: {
        "messages": [AIMessage(content="final memo")],
        "case_status": "complete",
    })
    result = main_graph.graph.compile().invoke({
        "messages": [HumanMessage(id="message-case", content="Research Apple and produce a memo")],
        "mode": "auto",
        "thread_id": f"thread-{uuid4().hex[:8]}",
        "session_id": "session-e2e",
    })

    assert result["case_status"] == "complete"
    assert set(result["case_task_results"]) == {"company", "industry", "memo"}
    assert result["case_task_results"]["memo"]["status"] == "completed"
    assert result["messages"][-1].content == "final memo"

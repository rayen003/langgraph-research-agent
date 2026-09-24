import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage


class _FakeRouterLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    def invoke(self, _messages):
        return AIMessage(content=json.dumps(self.payload))


class _ScenarioRouterLLM:
    def __init__(self, routes_by_marker: dict[str, dict]):
        self.routes_by_marker = routes_by_marker

    def invoke(self, messages):
        prompt = messages[0].content
        for marker, payload in self.routes_by_marker.items():
            if marker in prompt:
                return AIMessage(content=json.dumps(payload))
        raise AssertionError(f"missing router eval marker in prompt: {prompt[:200]}")


def _route_payload(
    route_level: str,
    *,
    playbook_id: str | None = None,
    selected_workflow: str | None = None,
    selected_case_type: str | None = None,
    max_tool_calls: int = 0,
) -> dict:
    return {
        "route_level": route_level,
        "playbook_id": playbook_id,
        "selected_workflow": selected_workflow,
        "selected_case_type": selected_case_type,
        "confidence": 0.9,
        "latency_class": "short",
        "max_tool_calls": max_tool_calls,
        "creates_objects": route_level in {"workflow", "case"},
        "needs_confirmation": False,
        "object_policy": "artifact" if route_level == "workflow" else "case" if route_level == "case" else "none",
        "reason": f"{route_level} test route",
    }


def test_execution_step_contract_supplies_stable_defaults() -> None:
    from execution_trace import make_execution_step

    event = make_execution_step(
        stage="route_execution",
        route_level="tool",
        playbook_id="latest_company_news",
        next_node="chat",
    )

    assert event["type"] == "execution_step"
    assert event["stage"] == "route_execution"
    assert event["node"] == "route_execution"
    assert event["allowed_tools"] == []
    assert event["tool_calls"] == []
    assert event["object_ids"] == []
    assert event["citation_ids"] == []
    assert {
        "route_level",
        "playbook_id",
        "selected_workflow",
        "selected_case_type",
        "latency_class",
        "status",
        "reason",
        "next_node",
    } <= set(event)


def test_playbook_registry_loads_micro_playbook() -> None:
    from agent_project.playbooks.registry import PlaybookRegistry

    registry = PlaybookRegistry()
    packet = registry.load_packet("latest_company_news")

    assert packet["id"] == "latest_company_news"
    assert packet["kind"] == "micro"
    assert packet["allowed_tools"] == ["query_knowledge_graph", "search_web", "retrieve_tool_result"]
    assert packet["tool_budget"]["max_calls"] == 2
    assert "instructions" in packet


def test_semantic_router_returns_route_decision(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(
        main_graph,
        "intent_llm",
        _FakeRouterLLM(_route_payload("tool", playbook_id="latest_company_news", max_tool_calls=2)),
    )
    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    result = main_graph.semantic_router_node({
        "messages": [HumanMessage(content="latest news for Apple")],
        "mode": "auto",
        "session_id": "s1",
    })

    assert result["resolved_intent"] == "chat"
    assert result["route_decision"]["route_level"] == "tool"
    assert result["route_decision"]["playbook_id"] == "latest_company_news"
    assert [event["stage"] for event in events if event.get("type") == "execution_step"] == ["semantic_router"]
    agent_trace("Semantic router: latest Apple news", events, result)


def test_runtime_rejects_playbook_that_cannot_produce_requested_chart(monkeypatch) -> None:
    import file as main_graph

    payload = {
        **_route_payload("workflow", playbook_id="deck", selected_workflow="deck", max_tool_calls=1),
        "requested_outputs": ["answer", "chart"],
        "reason": "financial performance and plot",
    }
    monkeypatch.setattr(main_graph, "intent_llm", _FakeRouterLLM(payload))
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda _event: None)
    state = {
        "messages": [HumanMessage(content="Find Apple's financial performance this year and plot it")],
        "mode": "auto",
        "session_id": "s1",
    }
    state.update(main_graph.semantic_router_node(state))
    loaded = main_graph.load_playbook_node(state)

    assert loaded["selected_playbook"]["id"] == "base"
    assert loaded["route_decision"]["route_level"] == "small_task"
    assert loaded["route_decision"]["playbook_id"] is None
    assert loaded["route_decision"]["selected_workflow"] is None
    assert loaded["route_decision"]["selected_case_type"] is None
    assert loaded["tool_policy"]["allowed_tools"] is None
    assert loaded["tool_policy"]["max_tool_calls"] >= 6


@pytest.mark.parametrize(
    ("user_text", "route_payload", "expected_intent", "expected_next_node"),
    [
        ("what is WACC?", _route_payload("direct"), "chat", "chat"),
        (
            "latest Apple news",
            _route_payload("tool", playbook_id="latest_company_news", max_tool_calls=2),
            "chat",
            "chat",
        ),
        (
                "research Apple's latest iPhone demand",
                _route_payload("small_task", playbook_id="source_acquisition", max_tool_calls=4),
                "research",
                "chat",
        ),
        (
            "value Apple using DCF",
            _route_payload("workflow", playbook_id="dcf_fcff", selected_workflow="dcf"),
            "chat",
            "workflow_context_review",
        ),
        (
            "build a listed-company equity research case for Apple",
            _route_payload("case", selected_case_type="listed_company_equity_research"),
            "chat",
            "plan_case",
        ),
    ],
)
def test_router_eval_matrix_routes_common_scenarios(
    monkeypatch,
    agent_trace,
    user_text,
    route_payload,
    expected_intent,
    expected_next_node,
) -> None:
    import file as main_graph

    events: list[dict] = []
    monkeypatch.setattr(main_graph, "intent_llm", _ScenarioRouterLLM({user_text: route_payload}))
    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    state = {
        "messages": [HumanMessage(content=user_text)],
        "mode": "auto",
        "session_id": "s1",
    }
    state.update(main_graph.semantic_router_node(state))

    assert state["resolved_intent"] == expected_intent
    assert main_graph.route_execution(state) == expected_next_node
    assert [event["stage"] for event in events if event.get("type") == "execution_step"] == [
        "semantic_router",
        "route_execution",
    ]
    assert all("node" in event for event in events if event.get("type") == "execution_step")
    assert all("latency_class" in event for event in events if event.get("type") == "execution_step")
    agent_trace(f"Router eval: {user_text}", events, {"state": state, "next_node": expected_next_node})


def test_workflow_route_uses_selected_workflow_not_regex(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(
        main_graph,
        "intent_llm",
        _FakeRouterLLM(_route_payload("workflow", playbook_id="dcf_fcff", selected_workflow="dcf")),
    )
    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)
    state = {
        "messages": [HumanMessage(content="please value Apple")],
        "mode": "auto",
        "session_id": "s1",
    }
    state.update(main_graph.semantic_router_node(state))

    assert main_graph.route_execution(state) == "workflow_context_review"
    assert main_graph._workflow_setup_id(state) == "dcf"
    agent_trace("Workflow route: DCF selected without regex", events, state)


def test_route_execution_covers_direct_tool_small_task_workflow_and_case(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    assert main_graph.route_execution({"route_decision": _route_payload("direct")}) == "chat"
    assert main_graph.route_execution({"route_decision": _route_payload("tool", playbook_id="latest_company_news")}) == "chat"
    assert main_graph.route_execution({"route_decision": _route_payload("small_task")}) == "chat"
    assert main_graph.route_execution({"route_decision": _route_payload("workflow", selected_workflow="dcf")}) == "workflow_context_review"
    assert main_graph.route_execution({
        "route_decision": _route_payload("case", selected_case_type="listed_company_equity_research")
    }) == "plan_case"

    routed = [event for event in events if event.get("type") == "execution_step" and event.get("stage") == "route_execution"]
    assert [event["next_node"] for event in routed] == [
        "chat",
        "chat",
        "chat",
        "workflow_context_review",
        "plan_case",
    ]
    agent_trace("Route execution matrix", events, {"routes": [event for event in events if event.get("stage") == "route_execution"]})


def test_lane_router_collapses_route_aliases(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    assert main_graph.route_lane({"route_decision": _route_payload("direct")}) == "answer_controller"
    assert main_graph.route_lane({"route_decision": _route_payload("tool")}) == "answer_controller"
    assert main_graph.route_lane({"route_decision": _route_payload("small_task")}) == "work_controller"
    assert main_graph.route_lane({"route_decision": _route_payload("workflow", selected_workflow="dcf")}) == "work_controller"
    assert main_graph.route_lane({
        "route_decision": _route_payload("case", selected_case_type="listed_company_equity_research")
    }) == "work_controller"

    routed = [event for event in events if event.get("type") == "execution_step" and event.get("stage") == "route_lane"]
    assert [event["next_node"] for event in routed] == [
        "answer_controller",
        "answer_controller",
        "work_controller",
        "work_controller",
        "work_controller",
    ]
    agent_trace("Lane router matrix", events, {"routes": routed})


def test_work_controller_routes_work_aliases(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    assert main_graph.route_work({"route_decision": _route_payload("small_task")}) == "chat"
    assert main_graph.route_work({"route_decision": _route_payload("workflow", selected_workflow="dcf")}) == "workflow_context_review"
    assert main_graph.route_work({
        "route_decision": _route_payload("case", selected_case_type="listed_company_equity_research")
    }) == "plan_case"

    routed = [event for event in events if event.get("type") == "execution_step" and event.get("stage") == "work_controller"]
    assert [event["next_node"] for event in routed] == [
        "chat",
        "workflow_context_review",
        "plan_case",
    ]
    agent_trace("Work controller matrix", events, {"routes": routed})


def test_load_playbook_and_policy_emit_execution_steps(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []
    state = {"route_decision": _route_payload("tool", playbook_id="latest_company_news", max_tool_calls=2)}

    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    loaded = main_graph.load_playbook_node(state)
    state.update(loaded)
    main_graph.apply_runtime_policy_node(state)

    execution_steps = [event for event in events if event.get("type") == "execution_step"]
    assert [(event["stage"], event.get("playbook_id")) for event in execution_steps] == [
        ("load_playbook", "latest_company_news"),
        ("apply_runtime_policy", "latest_company_news"),
    ]
    assert loaded["tool_policy"] == {
        "allowed_tools": ["query_knowledge_graph", "search_web", "retrieve_tool_result"],
        "max_tool_calls": 2,
        "route_level": "tool",
        "playbook_id": "latest_company_news",
    }
    agent_trace("Playbook load + runtime policy", events, {"selected_playbook": loaded["selected_playbook"], "tool_policy": loaded["tool_policy"]})


def test_case_route_enters_dynamic_planner(monkeypatch, agent_trace) -> None:
    import file as main_graph
    events: list[dict] = []

    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    state = {"route_decision": _route_payload("case", selected_case_type="listed_company_equity_research")}
    assert main_graph.route_work(state) == "plan_case"
    agent_trace("Case route planner", events, {"next_node": "plan_case"})


def test_flat_case_guard_routes_to_bounded_chat_loop() -> None:
    import file as main_graph

    assert main_graph.route_after_case_plan({
        "case_status": "downgraded_to_small_task",
    }) == "chat"
    assert main_graph.route_after_case_plan({"case_status": "planned"}) == "prepare_case_dispatch"


def test_base_small_task_keeps_bounded_general_tool_access(monkeypatch) -> None:
    import file as main_graph
    from tool_catalog import conversational_tool_ids

    loaded = main_graph.load_playbook_node({
        "route_decision": _route_payload("small_task", max_tool_calls=0),
    })

    assert loaded["selected_playbook"]["id"] == "base"
    assert loaded["tool_policy"] == {
        "allowed_tools": conversational_tool_ids(),
        "max_tool_calls": 6,
        "route_level": "small_task",
        "playbook_id": "base",
    }


def test_chat_dynamic_tool_policy_selects_only_allowed_tools(agent_trace) -> None:
    from execution_trace import make_execution_step
    import graphs.conversational as conversational

    tools = conversational._chat_tools_for_policy({
        "allowed_tools": ["query_knowledge_graph", "search_web"],
    })

    assert [tool.name for tool in tools] == ["search_web", "query_knowledge_graph"]
    agent_trace("Dynamic chat tool binding", [
        make_execution_step(
            "dynamic_tool_binding",
            route_level="tool",
            playbook_id="latest_company_news",
            allowed_tools=[tool.name for tool in tools],
        )
    ], {"bound_tools": [tool.name for tool in tools]})


def test_chat_tool_execution_blocks_disallowed_tool() -> None:
    import graphs.conversational as conversational

    response = AIMessage(
        content="",
        tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "call_1"}],
    )

    messages = conversational._invoke_chat_tools(response, allowed_tool_names={"search_web"})

    assert len(messages) == 1
    assert "blocked by execution policy" in messages[0].content


def test_chat_tool_execution_enforces_budget_before_call() -> None:
    import graphs.conversational as conversational

    response = AIMessage(
        content="",
        tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "call_1"}],
    )

    messages = conversational._invoke_chat_tools(
        response,
        allowed_tool_names={"calculator"},
        remaining_tool_calls=0,
    )

    assert len(messages) == 1
    assert "Capability call budget exhausted" in messages[0].content


def test_load_playbook_node_falls_back_to_base_for_missing_playbook() -> None:
    import file as main_graph
    from tool_catalog import conversational_tool_ids

    result = main_graph.load_playbook_node({
        "route_decision": {
            **_route_payload("direct", playbook_id="missing"),
            "latency_class": "instant",
        }
    })

    assert result["selected_playbook"]["id"] == "base"
    assert result["tool_policy"]["allowed_tools"] == conversational_tool_ids()
    assert result["tool_policy"]["max_tool_calls"] == 2
    assert {"run_dcf_workflow", "run_deck_workflow", "run_memo_workflow"} <= set(
        result["tool_policy"]["allowed_tools"]
    )


def test_router_failure_falls_back_to_bounded_tool_capable_loop(monkeypatch) -> None:
    import file as main_graph

    class _BrokenRouter:
        def invoke(self, _messages):
            raise TimeoutError("router unavailable")

    monkeypatch.setattr(main_graph, "intent_llm", _BrokenRouter())
    routed = main_graph.semantic_router_node({
        "messages": [HumanMessage(content="Handle this request")],
        "mode": "auto",
    })
    loaded = main_graph.load_playbook_node(routed)

    assert routed["route_decision"]["route_level"] == "small_task"
    assert routed["route_decision"]["max_tool_calls"] == 4
    assert loaded["tool_policy"]["allowed_tools"]
    assert not any(name.startswith("run_") for name in loaded["tool_policy"]["allowed_tools"])


def test_chart_output_reserves_source_and_renderer_tool_budget() -> None:
    import file as main_graph

    loaded = main_graph.load_playbook_node({
        "route_decision": {
            **_route_payload("small_task", max_tool_calls=1),
            "requested_outputs": ["chart"],
        },
    })

    assert loaded["tool_policy"]["max_tool_calls"] >= 2

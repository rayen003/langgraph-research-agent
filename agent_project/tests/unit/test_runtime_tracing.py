"""Runtime trace contract tests."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langchain_core.messages import AIMessage

import server
import tracing
import utils


def test_interactive_run_waits_for_sse_subscriber() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        run = server.RunState("thread-stream", loop, "Build memo", "chat")
        run.stream_expected = True
        released = False

        async def work() -> None:
            nonlocal released
            await server._await_stream_subscriber(run)
            released = True

        task = asyncio.create_task(work())
        await asyncio.sleep(0)
        assert released is False
        run.stream_connected.set()
        await task
        assert released is True

    asyncio.run(scenario())


def test_background_run_does_not_wait_for_subscriber() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        run = server.RunState("thread-background", loop, "Research", "auto")
        await server._await_stream_subscriber(run)

    asyncio.run(scenario())


def test_sse_stream_releases_run_before_event_replay(monkeypatch) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        run = server.RunState("thread-sse", loop, "Build memo", "chat")
        run.stream_expected = True
        monkeypatch.setitem(server._run_registry, run.thread_id, run)
        monkeypatch.setattr(server, "get_job", lambda _thread_id: {"thread_id": run.thread_id, "status": "classifying"})
        monkeypatch.setattr(server, "list_job_events", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(server, "_persist_event", lambda _thread_id, event: event)

        response = await server.stream_events(run.thread_id, after_id=0)
        iterator = response.body_iterator
        first_chunk = await anext(iterator)

        assert str(first_chunk).startswith(":")
        assert len(str(first_chunk)) >= 2048
        assert run.stream_connected.is_set()
        await iterator.aclose()
        server._run_registry.pop(run.thread_id, None)

    asyncio.run(scenario())


def test_traced_node_emits_linked_start_and_completion(monkeypatch):
    events = []
    monkeypatch.setattr(tracing, "emit_trace_span", events.append)
    tracing.set_trace_context("trace-test", "run-root")

    wrapped = tracing.traced_node("semantic_router", lambda state: {"value": state["value"] + 1})
    result = wrapped({"value": 4})

    assert result == {"value": 5}
    assert [event["status"] for event in events] == ["started", "completed"]
    assert events[0]["span_id"] == events[1]["span_id"]
    assert events[0]["parent_span_id"] == "run-root"
    assert events[1]["duration_ms"] >= 0


def test_model_callback_emits_duration_and_usage(monkeypatch):
    events = []
    monkeypatch.setattr(tracing, "emit_trace_span", events.append)
    tracing.set_trace_context("trace-model", "node-router")
    callback = tracing.TraceCallbackHandler("trace-model", "run-root")
    run_id = uuid4()

    callback.on_chat_model_start(
        {"name": "ChatOpenAI"},
        [[HumanMessage(content="route this")]],
        run_id=run_id,
        invocation_params={"model_name": "gpt-test", "stream": False},
    )
    callback.on_llm_end(
        SimpleNamespace(llm_output={"model_name": "gpt-test", "token_usage": {"total_tokens": 17}}),
        run_id=run_id,
    )

    assert [event["status"] for event in events] == ["started", "completed"]
    assert events[0]["parent_span_id"] == "node-router"
    assert events[1]["metadata"]["model"] == "gpt-test"
    assert events[1]["metadata"]["token_usage"]["total_tokens"] == 17


def test_track_tool_emits_activity_and_trace(monkeypatch):
    events = []
    monkeypatch.setattr(utils, "emit_ui_event", events.append)
    monkeypatch.setattr(tracing, "emit_ui_event", events.append, raising=False)
    tracing.set_trace_context("trace-tool", "node-chat")

    with utils.track_tool(name="search_web", scope="chat", step_id="chat") as span:
        span["summary"] = "six results"

    activities = [event for event in events if event.get("type") == "activity"]
    traces = [event for event in events if event.get("type") == "trace_span"]
    assert [event["status"] for event in activities] == ["started", "completed"]
    assert [event["status"] for event in traces] == ["started", "completed"]
    assert traces[0]["parent_span_id"] == "node-chat"
    assert traces[1]["metadata"]["summary"] == "six results"


def test_trace_endpoint_merges_span_lifecycle(monkeypatch):
    monkeypatch.setattr(server, "get_job", lambda _thread_id: {"thread_id": "thread-1"})
    monkeypatch.setattr(server, "list_job_events", lambda *_args, **_kwargs: [
        {
            "type": "trace_span",
            "trace_id": "trace-old",
            "span_id": "old-node",
            "category": "node",
            "name": "chat",
            "status": "completed",
            "started_at": 1.0,
            "ended_at": 2.0,
            "duration_ms": 1000,
        },
        {
            "type": "trace_span",
            "trace_id": "trace-1",
            "span_id": "node-1",
            "category": "node",
            "name": "semantic_router",
            "status": "started",
            "started_at": 10.0,
            "metadata": {"route": "pending"},
        },
        {
            "type": "trace_span",
            "trace_id": "trace-1",
            "span_id": "node-1",
            "category": "node",
            "name": "semantic_router",
            "status": "completed",
            "ended_at": 12.5,
            "duration_ms": 2500,
            "metadata": {"route": "tool"},
        },
    ])

    result = asyncio.run(server.get_run_trace("thread-1", None))

    assert result["trace_id"] == "trace-1"
    assert all(span["trace_id"] == "trace-1" for span in result["spans"])
    assert result["spans"] == [{
        "type": "trace_span",
        "trace_id": "trace-1",
        "span_id": "node-1",
        "category": "node",
        "name": "semantic_router",
        "status": "completed",
        "started_at": 10.0,
        "ended_at": 12.5,
        "duration_ms": 2500,
        "metadata": {"route": "tool"},
    }]


def test_create_run_returns_trace_identity(monkeypatch):
    events = []

    async def _noop_task(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "list_job_events", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "upsert_job", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_send_event", lambda _rs, event: events.append(event))
    monkeypatch.setattr(server, "_run_agent_task", _noop_task)

    result = asyncio.run(server.create_run(server.RunRequest(query="latest Apple news")))
    server._run_registry.pop(result.thread_id, None)

    assert result.trace_id and result.trace_id.startswith("trace_")
    assert result.root_span_id and result.root_span_id.startswith("run_")
    assert events[0]["status"] == "started"
    assert events[0]["trace_id"] == result.trace_id
    assert events[0]["span_id"] == result.root_span_id


def test_parent_graph_emits_node_waterfall(monkeypatch):
    import json
    import file as main_graph
    import graphs.conversational as conversational

    class _Router:
        def invoke(self, _messages):
            return AIMessage(content=json.dumps({
                "route_level": "direct",
                "playbook_id": None,
                "selected_workflow": None,
                "selected_case_type": None,
                "confidence": 0.99,
                "latency_class": "instant",
                "max_tool_calls": 0,
                "creates_objects": False,
                "needs_confirmation": False,
                "object_policy": "none",
                "reason": "trace test",
            }))

    class _Chat:
        def invoke(self, _history):
            return AIMessage(content="answer")

    events = []
    monkeypatch.setattr(main_graph, "intent_llm", _Router())
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: _Chat())
    monkeypatch.setattr(conversational, "emit_ui_event", events.append)
    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)
    monkeypatch.setattr(utils, "_ui_event_handler_ctx", utils.contextvars.ContextVar("test_ui_handler", default=events.append))
    monkeypatch.setattr(utils, "_ui_event_handler_fallback", events.append)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)
    trace_id = f"trace-{uuid4().hex[:8]}"
    root_span_id = "run-root"
    tracing.set_trace_context(trace_id, root_span_id)

    main_graph.app.invoke(
        {
            "messages": [HumanMessage(content="what is WACC?")],
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "mode": "chat",
            "session_id": f"session-{trace_id}",
        },
        config={"configurable": {"thread_id": f"thread-{trace_id}"}},
    )

    node_spans = {
        event["name"]
        for event in events
        if event.get("type") == "trace_span" and event.get("category") in {"node", "memory"}
    }
    assert {
        "semantic_router",
        "load_playbook",
        "apply_runtime_policy",
        "build_memory_context",
        "answer_controller",
        "chat",
        "persist_turn_object",
    } <= node_spans

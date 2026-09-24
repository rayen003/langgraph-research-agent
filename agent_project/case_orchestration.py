"""Dynamic case planner, LangGraph fan-out scheduler, and isolated workers."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.types import Send

from capability_dispatch import get_capability_dispatcher
from domain.execution import CasePlan, TaskPacket, TaskResult, ready_tasks
from evidence_memory import format_evidence_pack_prompt
from execution_trace import make_execution_step
from storage import upsert_workspace_object
from tool_catalog import build_current_capability_registry
from tool_runtime import unwrap_tool_result_message
from utils import emit_ui_event


PLANNER_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1")
WORKER_MODEL = os.getenv("SUBAGENT_MODEL", "gpt-4o-mini")

_PLAN_PROMPT = """You are finance case orchestrator. Create smallest valid task DAG for requested deliverables.
Return only JSON: {"objective":str,"deliverables":[str],"tasks":[TaskSpec]}.
TaskSpec fields: task_id, capability_id, objective, dependency_task_ids, input_object_version_ids,
required_output_types, context_query, execution_policy, acceptance_criteria.

Rules:
- Use only supplied capabilities.
- Explicit case DAG is justified only when at least one task consumes another task's output.
- If requested work is a flat set of independent tool calls, keep every dependency list empty; runtime will downgrade it to a small task.
- Tasks represent meaningful dependency or parallel workstream, not sections of prose.
- Never require DCF for memo or deck unless user asks for valuation or valuation is essential to stated objective.
- Memo and deck can consume research, document analysis, financial analysis, valuation, or prior artifacts.
- Run independent research tasks in parallel.
- Dependencies must point to task IDs in same plan.
- Reuse supplied evidence/object versions when sufficient.
- Maximum 10 tasks. Prefer 2-5.
- Every requested deliverable must be produced by a task.
"""

_WORKER_PROMPT = """You are delegated finance subagent. Complete one bounded task.
Use only allowed tools and supplied dependency/evidence context. Do not broaden scope.
Return concise final work product. Cite exact citation IDs when evidence provides them.
If blocked, return JSON with status=blocked, summary, and discovered_requirements.
"""


def _json_content(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("\n```", 1)[0]
    return json.loads(text)


def plan_case(state: dict[str, Any], planner_llm: Any | None = None) -> dict[str, Any]:
    registry = build_current_capability_registry()
    current_turn = state.get("current_turn") or {}
    route = current_turn.get("route") or state.get("route_decision") or {}
    case_id = str(state.get("case_id") or f"case:{uuid4().hex[:12]}")
    query = ""
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            query = message.content
            break
    payload = {
        "request": query,
        "selected_case_type": route.get("selected_case_type"),
        "capabilities": registry.cards(plannable_only=True),
        "evidence_pack": state.get("evidence_pack") or {},
        "workspace_objects": (state.get("memory_context") or {}).get("retrieved_object_cards") or [],
    }
    model = planner_llm or ChatOpenAI(model=PLANNER_MODEL, api_key=os.getenv("OPENAI_API_KEY"), timeout=60)
    response = model.invoke([SystemMessage(content=_PLAN_PROMPT), HumanMessage(content=json.dumps(payload, default=str))])
    raw = _json_content(response.content)
    raw["case_id"] = case_id
    raw.setdefault("objective", query)
    plan = CasePlan.model_validate(raw)
    registry.validate_plan_capabilities([task.capability_id for task in plan.tasks])
    initial_types = {"evidence_bundle"} if any(
        (state.get("evidence_pack") or {}).get(key)
        for key in ("fact_refs", "chunk_refs", "entity_refs")
    ) else set()
    registry.validate_dependencies(plan.tasks, initial_types=initial_types)
    if len(plan.tasks) > 10:
        raise ValueError("Case plan exceeds 10-task limit")

    if not any(task.dependency_task_ids for task in plan.tasks):
        max_tool_calls = max(int(route.get("max_tool_calls") or 0), 4)
        downgraded_route = {
            **route,
            "route_level": "small_task",
            "playbook_id": None,
            "selected_case_type": None,
            "latency_class": "medium",
            "max_tool_calls": max_tool_calls,
            "creates_objects": True,
            "needs_confirmation": False,
            "object_policy": "task_result",
            "reason": "Case planner found no cross-task dependencies; using bounded small-task execution.",
        }
        downgraded_policy = {
            "allowed_tools": None,
            "max_tool_calls": max_tool_calls,
            "route_level": "small_task",
            "playbook_id": None,
        }
        emit_ui_event(make_execution_step(
            "case_dag_guard",
            route_level="small_task",
            next_node="chat",
            reason=downgraded_route["reason"],
            proposed_task_count=len(plan.tasks),
        ))
        return {
            "case_id": None,
            "case_plan": None,
            "case_status": "downgraded_to_small_task",
            "pending_task_packets": [],
            "resolved_intent": "research",
            "route_decision": downgraded_route,
            "selected_playbook": None,
            "tool_policy": downgraded_policy,
            "current_turn": {
                **current_turn,
                "route": downgraded_route,
                "playbook": None,
                "execution_policy": downgraded_policy,
                "delegation": {
                    "status": "not_required",
                    "reason": downgraded_route["reason"],
                },
            },
        }

    stored_case = upsert_workspace_object({
        "object_id": case_id,
        "object_type": "research_case",
        "schema_ref": "domain.execution.CasePlan",
        "schema_version": "0.1",
        "title": plan.objective[:160],
        "status": "running",
        "session_id": state.get("session_id"),
        "thread_id": state.get("thread_id"),
        "created_by": "agent:orchestrator",
        "updated_by": "agent:orchestrator",
        "summary": f"{len(plan.tasks)} delegated tasks; deliverables: {', '.join(plan.deliverables)}",
        "search_text": f"{plan.objective} {' '.join(plan.deliverables)}",
        "payload": plan.model_dump(),
    }, action_type="case_planned")
    emit_ui_event(make_execution_step(
        "plan_case", route_level="case", selected_case_type=route.get("selected_case_type"),
        object_ids=[case_id], reason=f"planned {len(plan.tasks)} tasks",
        case_id=case_id, objective=plan.objective,
        tasks=[task.model_dump() for task in plan.tasks],
    ))
    return {
        "case_id": case_id,
        "case_plan": plan.model_dump(),
        "case_status": "planned",
        "case_task_results": {"__reset__": True},
        "pending_task_packets": [],
        "current_turn": {
            **current_turn,
            "delegation": {
                "case_id": case_id,
                "plan_version_id": stored_case.get("version_id"),
                "status": "planned",
                "task_count": len(plan.tasks),
            },
        },
    }


def _result_dependencies(plan: CasePlan, task_id: str, results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    task = next(task for task in plan.tasks if task.task_id == task_id)
    return [results[dependency_id] for dependency_id in task.dependency_task_ids if dependency_id in results]


def _task_packets(state: dict[str, Any]) -> list[TaskPacket]:
    plan = CasePlan.model_validate(state["case_plan"])
    results = state.get("case_task_results") or {}
    ready = ready_tasks(plan, results)
    registry = build_current_capability_registry()
    packets: list[TaskPacket] = []
    for task in ready:
        capability = registry.get(task.capability_id)
        task = task.model_copy(update={
            "execution_policy": {
                "max_tool_calls": capability.max_tool_calls,
                "model_tier": capability.model_tier,
                **task.execution_policy,
            },
        })
        packet = TaskPacket(
            case_id=plan.case_id,
            session_id=str(state.get("session_id") or ""),
            thread_id=str(state.get("thread_id") or ""),
            task=task,
            dependency_results=_result_dependencies(plan, task.task_id, results),
            evidence_pack=state.get("evidence_pack") or {},
            allowed_tools=list(capability.allowed_tools),
        )
        packets.append(packet)
    return packets


def prepare_case_dispatch_node(state: dict[str, Any]) -> dict[str, Any]:
    packets = _task_packets(state)
    results = state.get("case_task_results") or {}
    current_turn = state.get("current_turn") or {}
    delegation = {
        **(current_turn.get("delegation") or {}),
        "status": "running" if packets else "ready_to_synthesize",
        "active_task_ids": [packet.task.task_id for packet in packets],
        "completed_task_ids": [
            task_id for task_id, result in results.items()
            if isinstance(result, dict) and result.get("status") == "completed"
        ],
    }
    return {
        "pending_task_packets": [packet.model_dump() for packet in packets],
        "current_turn": {**current_turn, "delegation": delegation},
    }


def dispatch_case_tasks(state: dict[str, Any]) -> list[Send] | str:
    packets = [TaskPacket.model_validate(packet) for packet in state.get("pending_task_packets") or []]
    if not packets:
        return "synthesize_case"
    return [Send("execute_case_task", {"task_packet": packet.model_dump()}) for packet in packets]


def _tool_registry() -> dict[str, Any]:
    from tools import ALL_TOOLS  # noqa: PLC0415
    from tool_catalog import select_capability_tools  # noqa: PLC0415

    return {tool.name: tool for tool in select_capability_tools("case", ALL_TOOLS)}


def _evidence_source_refs(evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    refs = list(evidence_pack.get("chunk_refs") or [])
    for fact in evidence_pack.get("fact_refs") or []:
        refs.extend(fact.get("source_refs") or [])
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = str(ref.get("citation_id") or ref.get("source_id") or ref.get("evidence_id") or ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def execute_task(packet: TaskPacket, worker_llm: Any | None = None) -> TaskResult:
    from documents import _session_ctx  # noqa: PLC0415

    _session_ctx.set(packet.session_id)
    tools = _tool_registry()
    dispatcher = get_capability_dispatcher()
    selected_tools = [tools[name] for name in packet.allowed_tools if name in tools]
    model = worker_llm or ChatOpenAI(model=WORKER_MODEL, api_key=os.getenv("OPENAI_API_KEY"), timeout=60)
    bound = model.bind_tools(selected_tools) if selected_tools else model
    context = {
        "task": packet.task.model_dump(),
        "dependency_results": packet.dependency_results,
    }
    history = [
        SystemMessage(content=_WORKER_PROMPT + format_evidence_pack_prompt(packet.evidence_pack)),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)),
    ]
    tool_calls = 0
    max_calls = min(int(packet.task.execution_policy.get("max_tool_calls", len(selected_tools) or 0)), 8)
    final = ""
    try:
        for _ in range(5):
            response = bound.invoke(history)
            history.append(response)
            if not response.tool_calls:
                final = str(response.content or "")
                break
            normalized_calls = [{
                **call,
                "id": str(call.get("id") or uuid4().hex),
                "args": call.get("args") or {},
                "type": "tool_call",
            } for call in response.tool_calls]
            wrapped_results = dispatcher.invoke(
                normalized_calls,
                surface="case",
                context={
                    "thread_id": packet.thread_id,
                    "session_id": packet.session_id,
                    "tool_execution_context": {
                        "scope": "workflow",
                        "step_id": packet.task.task_id,
                    },
                },
                allowed_capability_ids=set(packet.allowed_tools),
                max_calls=max(max_calls - tool_calls, 0),
            )
            tool_calls += sum(message.status != "error" for message in wrapped_results)
            history.extend(unwrap_tool_result_message(message) for message in wrapped_results)
        if not final:
            final = "Task stopped after execution limit."
        status = "completed"
        discovered: list[dict[str, Any]] = []
        try:
            structured = _json_content(final)
            if structured.get("status") in {"blocked", "needs_input", "needs_approval", "failed"}:
                status = structured["status"]
            final = str(structured.get("summary") or final)
            discovered = structured.get("discovered_requirements") or []
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        final = f"{type(exc).__name__}: {exc}"
        discovered = []

    source_refs = _evidence_source_refs(packet.evidence_pack)
    stored = upsert_workspace_object({
        "object_id": f"task_result:{packet.case_id}:{packet.task.task_id}",
        "object_type": "task_result",
        "schema_ref": "domain.execution.TaskResult",
        "schema_version": "0.1",
        "title": packet.task.objective[:160],
        "status": status,
        "session_id": packet.session_id,
        "thread_id": packet.thread_id,
        "case_id": packet.case_id,
        "task_id": packet.task.task_id,
        "created_by": f"agent:{packet.task.capability_id}",
        "updated_by": f"agent:{packet.task.capability_id}",
        "source_version_ids": [
            version_id
            for result in packet.dependency_results
            for version_id in result.get("output_object_version_ids") or []
        ],
        "source_refs": source_refs,
        "summary": final,
        "search_text": f"{packet.task.objective} {final}",
        "payload": {"task": packet.task.model_dump(), "result": final},
    }, action_type="task_completed" if status == "completed" else "task_stopped")
    return TaskResult(
        task_id=packet.task.task_id,
        case_id=packet.case_id,
        capability_id=packet.task.capability_id,
        status=status,
        summary=final,
        output_object_version_ids=[str(stored.get("version_id"))] if stored.get("version_id") else [],
        source_refs=source_refs,
        discovered_requirements=discovered,
        metrics={"tool_calls": tool_calls, "model_tier": "worker"},
    )


def execute_case_task_node(state: dict[str, Any]) -> dict[str, Any]:
    packet = TaskPacket.model_validate(state["task_packet"])
    result = execute_task(packet)
    emit_ui_event(make_execution_step(
        "execute_case_task", route_level="case", object_ids=result.output_object_version_ids,
        status=result.status, reason=f"{result.task_id}: {result.summary[:120]}", task_id=result.task_id,
    ))
    return {"case_task_results": {result.task_id: result.model_dump()}}


def collect_case_results_node(state: dict[str, Any]) -> dict[str, Any]:
    results = state.get("case_task_results") or {}
    emit_ui_event(make_execution_step(
        "collect_case_results", route_level="case", status="completed",
        reason=f"collected {len(results)} task results",
    ))
    return {}


def synthesize_case(state: dict[str, Any], planner_llm: Any | None = None) -> dict[str, Any]:
    plan = CasePlan.model_validate(state["case_plan"])
    results = state.get("case_task_results") or {}
    model = planner_llm or ChatOpenAI(model=PLANNER_MODEL, api_key=os.getenv("OPENAI_API_KEY"), timeout=60)
    prompt = {
        "objective": plan.objective,
        "deliverables": plan.deliverables,
        "case_task_results": list(results.values()),
    }
    response = model.invoke([
        SystemMessage(content="Synthesize case results into requested finance deliverables. State missing or failed work. Preserve citations."),
        HumanMessage(content=json.dumps(prompt, ensure_ascii=False, default=str)),
    ])
    content = str(response.content or "")
    status = "complete" if len(results) == len(plan.tasks) and all(r.get("status") == "completed" for r in results.values()) else "partial"
    stored = upsert_workspace_object({
        "object_id": plan.case_id,
        "object_type": "research_case",
        "schema_ref": "domain.execution.CasePlan",
        "schema_version": "0.1",
        "title": plan.objective[:160],
        "status": status,
        "session_id": state.get("session_id"),
        "thread_id": state.get("thread_id"),
        "created_by": "agent:orchestrator",
        "updated_by": "agent:orchestrator",
        "source_version_ids": [version for result in results.values() for version in result.get("output_object_version_ids") or []],
        "summary": content[:500],
        "search_text": f"{plan.objective} {content}",
        "payload": {"plan": plan.model_dump(), "results": results, "synthesis": content},
    }, action_type="case_synthesized")
    emit_ui_event(make_execution_step(
        "synthesize_case", route_level="case", status=status,
        object_ids=[str(stored.get("version_id") or plan.case_id)], reason=f"case {status}",
    ))
    current_turn = state.get("current_turn") or {}
    return {
        "messages": [AIMessage(content=content)],
        "case_status": status,
        "pending_task_packets": [],
        "current_turn": {
            **current_turn,
            "delegation": {
                **(current_turn.get("delegation") or {}),
                "status": status,
                "active_task_ids": [],
            },
            "response": {
                "type": "case_synthesis",
                "case_id": plan.case_id,
                "case_version_id": stored.get("version_id"),
            },
        },
    }


def synthesize_case_node(state: dict[str, Any]) -> dict[str, Any]:
    return synthesize_case(state)

"""Parent graph: intent_node routes queries to the research or conversational subgraph."""

import json
import hashlib
import logging
import os
import re
from typing import Annotated, TypedDict
from uuid import uuid4

import dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
try:
    from langgraph.types import interrupt
except ImportError:  # LangGraph >=1.0 style
    from langgraph.types import Interrupt

    def interrupt(payload: dict):  # type: ignore[no-redef]
        raise Interrupt(payload)
from rich.panel import Panel

import agent_log
from checkpointing import durable_checkpointer
from execution_trace import make_execution_step
from evidence_memory import build_evidence_pack
from memory_context import build_memory_context
from memory_writer import persist_turn_object
from playbooks.registry import PlaybookNotFoundError, PlaybookRegistry
from routing import RouteDecision, fallback_route, parse_route_decision, resolved_intent_for
from storage import record_route_decision
from tracing import traced_node
from turn_context import build_turn_context_snapshot
from tool_catalog import conversational_tool_ids, unsupported_requested_outputs
from case_orchestration import (
    collect_case_results_node,
    dispatch_case_tasks,
    execute_case_task_node,
    plan_case,
    prepare_case_dispatch_node,
    synthesize_case_node,
)
from domain.execution import merge_case_task_results
from utils import console, emit_ui_event, format_plan, get_run_dir, set_thread_id
from graphs.research import (
    plan_node,
    review_plan_node,
    execute_one_step_node,
    route_after_step,
    synthesize_node,
    update_memory_node,
)
from graphs.conversational import chat_node

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    thread_id: str
    trace_id: str
    root_span_id: str
    workspace_id: str
    user_id: str | None
    task_id: str | None
    task_context: dict | None
    current_turn: dict | None
    pending_task_packets: list[dict]
    case_id: str | None
    case_plan: dict | None
    case_status: str | None
    case_task_results: Annotated[dict[str, dict], merge_case_task_results]
    evidence_pack: dict | None
    # Routing
    mode: str           # "auto" | "research" | "chat" — user's selected mode
    resolved_intent: str | None  # "research" | "chat" — set by intent_node
    route_decision: dict | None
    selected_playbook: dict | None
    tool_policy: dict | None
    memory_context: dict | None
    turn_context: dict | None
    # Research subgraph fields
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]
    research_context_version_id: str | None
    plan_version_id: str | None
    approved_plan_version_id: str | None
    step_result_version_ids: dict[str, str]
    report_version_id: str | None
    report_path: str | None
    # Shared memory (persists across turns in the same LangGraph thread)
    session_memory: str
    # RAG session scope — used to filter uploaded documents
    session_id: str
    # User workflow preferences (validation gates, workflow-specific HITL)
    user_settings: dict
    # Chat workflow setup gate (thin context before workflow dispatch)
    workflow_context_draft: dict | None
    approved_workflow_context: dict | None
    workflow_setup_cancelled: bool


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

# Strong controller chooses scope and deliverables. Simple current-news path
# still bypasses an additional synthesis round after this decision.
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1")
intent_llm = ChatOpenAI(
    model=ORCHESTRATOR_MODEL,
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=15,
    max_retries=0,
)

_ROUTER_PROMPT = (
    "Choose the cheapest sufficient execution route for the latest user request.\n\n"
    "Return ONLY JSON matching this schema:\n"
    "{"
    "\"route_level\":\"direct|tool|small_task|workflow|case\","
    "\"playbook_id\": string|null,"
    "\"selected_workflow\": \"dcf|deck|memo|comparison|document_analysis\"|null,"
    "\"selected_case_type\": string|null,"
    "\"confidence\": 0.0-1.0,"
    "\"latency_class\":\"instant|short|medium|long|background\","
    "\"max_tool_calls\": integer,"
    "\"creates_objects\": boolean,"
    "\"needs_confirmation\": boolean,"
    "\"propose_task\": boolean,"
    "\"object_policy\":\"none|ephemeral|task_result|artifact|case\","
    "\"speech_act\":\"answer|context_recall|fresh_lookup|continue|create_artifact|research_project\","
    "\"context_reference\":\"none|previous_user_turn|previous_assistant_turn|recent_thread|workspace\","
    "\"data_requirement\":\"none|thread|workspace|fresh_external\","
    "\"requested_outputs\":[\"answer|chart|table|file|report|pdf|memo|deck|valuation\"],"
    "\"reason\": string"
    "}\n\n"
    "Execution levels:\n"
    "- direct: answer from context, no tools.\n"
    "- tool: one independent tool batch, executed concurrently, no durable object by default.\n"
    "- small_task: bounded multi-round tools and synthesis, without an explicit task DAG.\n"
    "- workflow: DCF, deck, memo, comparison, or document-analysis artifact.\n"
    "- case: dependency-bearing ResearchCase where at least one task consumes another task's output.\n\n"
    "Do not escalate simple current-data questions into workflow or case. "
    "Set propose_task=true only for substantial tracked work with a deadline, human review, "
    "or a sustained research mandate. Ordinary answers, lookups, charts and isolated workflows "
    "do not require a tracked task. If active_task exists, set propose_task=false. "
    "Use tool for multiple independent lookups that can run in one batch. "
    "Do not select case merely because several independent calls or deliverables exist. "
    "Reserve case DAGs for explicit logical or chronological dependencies between tasks. "
    "Use micro playbooks for fast tool answers. "
    "Use workflow only for explicit artifact/workflow requests. "
    "A routine written company comparison or analyst report is a small_task, not comparison workflow. "
    "Select comparison workflow only when user explicitly requests a reusable comparison artifact/workspace workflow. "
    "A standalone chart or plot is a small_task, not a deck. Select deck only when user requests slides, a presentation, PowerPoint, or PPTX. "
    "Use case only for explicit long-running project or research-case requests. "
    "Interpret latest_user_turn in Current turn context as current request. "
    "Use prior turns to resolve references, follow-ups, corrections, and recall requests. "
    "Text mentioned only in prior turns is context, not a new instruction. "
    "Choose fresh_external only when current request needs current outside data. "
    "List every explicit user deliverable in requested_outputs. Requests for graphs, plots, or visualizations require chart. "
    "Requests for a written document, comparison write-up, or analyst report require report, not chart. "
    "Use pdf only when user explicitly requests PDF/export/download. A report can be delivered as substantive chat text. "
    "Use small_task when requested output depends on one or more prior tool results. "
    "If the user asks for a deck, slides, presentation, PowerPoint, or PPTX, "
    "select workflow=deck even when the source material is a DCF, memo, or prior analysis."
)

_BASE_PLAYBOOK = {
    "id": "base",
    "kind": "base",
    "version": "0.1",
    "title": "Base Policy",
    "summary": "Default low-ceremony agent behavior.",
    "route_levels": ["direct", "tool", "small_task", "workflow", "case"],
    "allowed_tools": [],
    "allowed_workflows": [],
    "tool_budget": {"max_calls": 0},
    "object_policy": {"creates_objects": False, "default_object_type": None},
    "required_outputs": {},
    "validators": [],
    "stop_conditions": ["answer complete"],
    "instructions": "Answer at the selected route level. Do not escalate without router approval.",
}


def _latest_human_message(messages: list[BaseMessage]) -> HumanMessage | None:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message
    return None


def _turn_identity(state: AgentState) -> tuple[str, str, str | None]:
    messages = state.get("messages", [])
    latest = _latest_human_message(messages)
    thread_id = str(state.get("thread_id") or get_run_dir().name)
    message_id = str(latest.id) if latest is not None and latest.id else None
    content = latest.content if latest is not None and isinstance(latest.content, str) else ""
    human_count = sum(isinstance(message, HumanMessage) for message in messages)
    seed = f"{thread_id}\n{message_id or ''}\n{human_count}\n{content}"
    turn_id = f"turn:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"
    return thread_id, turn_id, message_id


def _current_route_payload(state: AgentState) -> dict:
    current_turn = state.get("current_turn") or {}
    return current_turn.get("route") or state.get("route_decision") or {}


def _updated_current_turn(state: AgentState, **updates) -> dict:
    return {**(state.get("current_turn") or {}), **updates}


def _persist_current_route(state: AgentState, decision: dict) -> None:
    current_turn = state.get("current_turn") or {}
    turn_id = current_turn.get("turn_id")
    thread_id = state.get("thread_id") or get_run_dir().name
    if not turn_id:
        return
    try:
        record_route_decision(
            route_id=f"route:{thread_id}:{turn_id}",
            turn_id=str(turn_id),
            thread_id=str(thread_id),
            session_id=state.get("session_id"),
            user_id=state.get("user_id"),
            decision=decision,
            model=ORCHESTRATOR_MODEL,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist route decision for turn %s", turn_id)


def build_turn_context_node(state: AgentState) -> dict:
    snapshot = build_turn_context_snapshot(state).model_dump()
    thread_id, _turn_id, _user_message_id = _turn_identity(state)
    emit_ui_event(make_execution_step(
        "build_turn_context",
        object_ids=[card["object_id"] for card in snapshot["workspace_candidates"]],
        reason=(
            f"recent_messages={len(snapshot['recent_messages'])}; "
            f"workspace_candidates={len(snapshot['workspace_candidates'])}"
        ),
    ))
    return {"thread_id": thread_id, "turn_context": snapshot}


def semantic_router_node(state: AgentState) -> dict:
    mode = state.get("mode") or "auto"
    turn_context = state.get("turn_context") or build_turn_context_snapshot(state).model_dump()

    try:
        playbook_cards = PlaybookRegistry().list_cards()
    except Exception:  # noqa: BLE001
        playbook_cards = []

    prompt = (
        f"{_ROUTER_PROMPT}\n\n"
        f"UI mode: {mode}\n"
        f"Available playbooks:\n{json.dumps(playbook_cards, ensure_ascii=False)}\n\n"
        f"Current turn context:\n{json.dumps(turn_context, ensure_ascii=False, default=str)}"
        f"\n\nRetrieved memory context:\n{json.dumps(state.get('memory_context') or {}, ensure_ascii=False, default=str)}"
        f"\n\nRetrieved evidence context:\n{json.dumps(state.get('evidence_pack') or {}, ensure_ascii=False, default=str)}"
    )
    try:
        response = intent_llm.invoke([HumanMessage(content=prompt)])
        decision = parse_route_decision(response.content if isinstance(response.content, str) else "")
    except Exception as exc:  # noqa: BLE001
        decision = fallback_route(f"semantic router failed: {type(exc).__name__}")

    if mode == "research" and decision.route_level in {"direct", "tool"}:
        decision = decision.model_copy(update={
            "route_level": "small_task",
            "playbook_id": decision.playbook_id or "source_acquisition",
            "max_tool_calls": max(decision.max_tool_calls, 6),
            "object_policy": "task_result",
            "reason": f"research mode requested; {decision.reason}",
        })

    intent = resolved_intent_for(decision)
    agent_log.intent_classified(intent, mode)
    agent_log.route_selected(
        level=decision.route_level,
        playbook_id=decision.playbook_id,
        outputs=list(decision.requested_outputs),
        max_tool_calls=decision.max_tool_calls,
        reason=decision.reason,
    )
    emit_ui_event(make_execution_step(
        "semantic_router",
        route_level=decision.route_level,
        playbook_id=decision.playbook_id,
        selected_workflow=decision.selected_workflow,
        selected_case_type=decision.selected_case_type,
        confidence=decision.confidence,
        latency_class=decision.latency_class,
        max_tool_calls=decision.max_tool_calls,
        creates_objects=decision.creates_objects,
        needs_confirmation=decision.needs_confirmation,
        object_policy=decision.object_policy,
        speech_act=decision.speech_act,
        context_reference=decision.context_reference,
        data_requirement=decision.data_requirement,
        reason=decision.reason,
    ))
    route_payload = decision.model_dump()
    thread_id, turn_id, user_message_id = _turn_identity(state)
    current_turn = {
        "turn_id": turn_id,
        "user_message_id": user_message_id,
        "route": route_payload,
        "playbook": None,
        "execution_policy": None,
        "memory_selection": state.get("memory_context") or {},
        "evidence_selection": state.get("evidence_pack") or {},
        "response": None,
        "goal": turn_context.get("latest_user_turn") or "",
        "required_outputs": list(decision.requested_outputs),
        "result_refs": [],
        "artifact_refs": [],
        "artifact_paths": [],
    }
    route_state = {**state, "thread_id": thread_id, "current_turn": current_turn}
    _persist_current_route(route_state, route_payload)
    emit_ui_event({"type": "route_decision", **route_payload})
    emit_ui_event({"type": "intent_classified", "intent": intent, "mode": mode})
    return {
        "thread_id": thread_id,
        "resolved_intent": intent,
        "route_decision": route_payload,
        "current_turn": current_turn,
        "turn_context": turn_context,
    }


intent_node = semantic_router_node


def propose_task_node(state: AgentState) -> dict:
    """Offer tracked work before tool execution, without creating a task yet."""
    route = _current_route_payload(state)
    if state.get("task_id") or not route.get("propose_task"):
        return {}
    goal = (state.get("turn_context") or {}).get("latest_user_turn") or ""
    decision = interrupt({
        "type": "workflow_context_review",
        "workflow_id": "tracked_task",
        "title": "Track this work?",
        "context": {"workflow_id": "tracked_task", "goal": goal,
                    "requested_outputs": route.get("requested_outputs") or [],
                    "reason": route.get("reason") or ""},
        "controls": [],
    })
    task_context = (decision or {}).get("task_context")
    if task_context:
        return {"task_id": task_context["task_id"], "task_context": task_context,
                "turn_context": {**(state.get("turn_context") or {}), "active_task": task_context}}
    return {}


def load_playbook_node(state: AgentState) -> dict:
    route_payload = _current_route_payload(state) or fallback_route().model_dump()
    decision = RouteDecision.model_validate(route_payload)
    registry = PlaybookRegistry()
    packet = _BASE_PLAYBOOK
    if decision.playbook_id:
        try:
            packet = registry.load_packet(decision.playbook_id)
        except PlaybookNotFoundError:
            packet = _BASE_PLAYBOOK
    route_levels = list(packet.get("route_levels") or [])
    if packet.get("id") != "base" and route_levels and decision.route_level not in route_levels:
        if len(route_levels) == 1 and route_levels[0] in {"direct", "tool", "small_task"}:
            decision = decision.model_copy(update={
                "route_level": route_levels[0],
                "reason": (
                    f"runtime aligned route with playbook {packet.get('id')}; "
                    f"{decision.reason}"
                ),
            })
    allowed_tools = packet.get("allowed_tools") or []
    if packet.get("id") == "base" and decision.route_level in {"direct", "tool", "small_task"}:
        # Valid controller decisions can compose atomic tools and workflows.
        # Failed controller decisions fall back to atomic capabilities only.
        allowed_tools = conversational_tool_ids(include_workflows=decision.confidence > 0.0)
    unsupported_outputs = unsupported_requested_outputs(
        list(decision.requested_outputs),
        allowed_tools,
    )
    if unsupported_outputs and packet.get("id") != "base":
        prior_playbook_id = packet.get("id")
        packet = _BASE_PLAYBOOK
        allowed_tools = None
        decision = decision.model_copy(update={
            # A rejected workflow cannot remain selected after its playbook
            # fails the requested-output contract. Dispatch must follow the
            # repaired policy, not stale workflow metadata.
            "route_level": "small_task",
            "playbook_id": None,
            "selected_workflow": None,
            "selected_case_type": None,
            "latency_class": "medium" if decision.latency_class in {"instant", "short"} else decision.latency_class,
            "max_tool_calls": max(decision.max_tool_calls, 6),
            "creates_objects": True,
            "object_policy": "artifact",
            "reason": (
                f"runtime rejected playbook {prior_playbook_id}: cannot produce "
                f"{', '.join(unsupported_outputs)}; {decision.reason}"
            ),
        })
        emit_ui_event(make_execution_step(
            "repair_route_capability",
            route_level=decision.route_level,
            playbook_id=prior_playbook_id,
            status="completed",
            max_tool_calls=decision.max_tool_calls,
            creates_objects=decision.creates_objects,
            object_policy=decision.object_policy,
            reason=f"unsupported_outputs={','.join(unsupported_outputs)}",
        ))
    tool_budget = packet.get("tool_budget") or {}
    if decision.max_tool_calls and (
        not tool_budget.get("max_calls") or decision.max_tool_calls < int(tool_budget.get("max_calls") or 0)
    ):
        tool_budget = {**tool_budget, "max_calls": decision.max_tool_calls}
    default_max_calls = {"direct": 2, "tool": 4, "small_task": 6}.get(decision.route_level, 0)
    artifact_outputs = {"chart", "file", "pdf", "memo", "deck", "valuation"}
    artifact_minimum_calls = 3 if artifact_outputs.intersection(decision.requested_outputs) else 0
    tool_policy = {
        "allowed_tools": allowed_tools,
        "max_tool_calls": max(
            int(tool_budget.get("max_calls") or decision.max_tool_calls or default_max_calls),
            artifact_minimum_calls,
        ),
        "route_level": decision.route_level,
        "playbook_id": packet.get("id"),
    }
    emit_ui_event(make_execution_step(
        "load_playbook",
        playbook_id=packet.get("id"),
        playbook_kind=packet.get("kind"),
        route_level=decision.route_level,
        allowed_tools=allowed_tools or [],
        latency_class=decision.latency_class,
        max_tool_calls=tool_policy["max_tool_calls"],
        creates_objects=decision.creates_objects,
        object_policy=decision.object_policy,
    ))
    agent_log.playbook_loaded(
        packet.get("id") or "base",
        packet.get("kind"),
        len(allowed_tools or []),
        tool_policy["max_tool_calls"],
    )
    return {
        "resolved_intent": resolved_intent_for(decision),
        "route_decision": decision.model_dump(),
        "selected_playbook": packet,
        "tool_policy": tool_policy,
        "current_turn": _updated_current_turn(
            state,
            route=decision.model_dump(),
            playbook=packet,
            execution_policy=tool_policy,
        ),
    }


def apply_runtime_policy_node(state: AgentState) -> dict:
    route_payload = _current_route_payload(state) or fallback_route().model_dump()
    decision = RouteDecision.model_validate(route_payload)
    playbook = state.get("selected_playbook") or _BASE_PLAYBOOK
    route_levels = set(playbook.get("route_levels") or [])
    if route_levels and decision.route_level not in route_levels and playbook.get("id") != "base":
        decision = fallback_route("playbook route level mismatch")
    tool_policy = state.get("tool_policy") or {}
    emit_ui_event(make_execution_step(
        "apply_runtime_policy",
        playbook_id=playbook.get("id"),
        playbook_kind=playbook.get("kind"),
        route_level=decision.route_level,
        allowed_tools=tool_policy.get("allowed_tools") or [],
        latency_class=decision.latency_class,
        max_tool_calls=tool_policy.get("max_tool_calls"),
        creates_objects=decision.creates_objects,
        needs_confirmation=decision.needs_confirmation,
        object_policy=decision.object_policy,
    ))
    route_payload = decision.model_dump()
    current_turn = _updated_current_turn(
        state,
        route=route_payload,
        execution_policy=tool_policy,
    )
    _persist_current_route({**state, "current_turn": current_turn}, route_payload)
    return {"route_decision": route_payload, "current_turn": current_turn}


def build_memory_context_node(state: AgentState) -> dict:
    # Ignore prior turn's route. This node now runs before current controller
    # decision so memory can influence intent, scope, and coreference.
    pre_route_state = {**state, "current_turn": None, "route_decision": None}
    context = build_memory_context(pre_route_state)
    evidence_pack = build_evidence_pack(pre_route_state)
    object_ids = [
        str(card.get("object_id"))
        for card in context.get("retrieved_object_cards", [])
        if card.get("object_id")
    ]
    object_ids.extend(
        str(card.get("object_id"))
        for card in context.get("expanded_dependencies", [])
        if card.get("object_id")
    )
    emit_ui_event(make_execution_step(
        "build_memory_context",
        route_level=None,
        playbook_id=None,
        object_ids=object_ids,
        citation_ids=[
            str(source.get("source_id"))
            for source in context.get("source_refs", [])
            if isinstance(source, dict) and source.get("source_id")
        ],
        reason=f"memory_policy={context.get('memory_policy')}",
    ))
    return {
        "memory_context": context,
        "evidence_pack": evidence_pack,
    }


_WORKFLOW_PATTERNS: dict[str, re.Pattern[str]] = {
    "dcf": re.compile(
        r"\b(dcf|discounted\s+cash\s+flow|intrinsic\s+value|valuation\s+model|value\s+(?:it|this|[A-Z]{1,6}))\b",
        re.IGNORECASE,
    ),
    "document_analysis": re.compile(
        r"\b(analy[sz]e|summari[sz]e|extract|review)\b.*\b(document|pdf|filing|presentation|transcript|upload)\b",
        re.IGNORECASE,
    ),
    "deck": re.compile(
        r"\b(deck|slides?|presentation|powerpoint|pptx)\b",
        re.IGNORECASE,
    ),
    "memo": re.compile(
        r"\b(memo|investment memo|ic memo|committee memo|write[-\s]?up)\b",
        re.IGNORECASE,
    ),
    "comparison": re.compile(
        r"\b(compare|comparison|versus| vs\.? |peer set|side[-\s]?by[-\s]?side)\b",
        re.IGNORECASE,
    ),
}

_WORKFLOW_TITLES = {
    "dcf": "Setup DCF",
    "document_analysis": "Setup document analysis",
    "deck": "Setup deck",
    "memo": "Setup memo",
    "comparison": "Setup comparison",
}

def _latest_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else ""
    return ""


def _infer_company_from_text(text: str) -> str:
    text = " ".join((text or "").strip().split())
    if not text:
        return ""

    patterns = [
        r"\b(?:for|on|of)\s+(.+)$",
        r"\b(?:run|create|generate|build)\s+(?:a\s+)?(?:dcf|valuation)\s+(?:analysis\s+)?(?:for\s+|on\s+)?(.+)$",
        r"\b(?:dcf|valuation|intrinsic\s+value|value)\s+(?:analysis\s+)?(?:for\s+|on\s+)?(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            candidate = re.sub(r"\b(?:please|thanks|full|quick|analysis|model)\b", "", candidate, flags=re.IGNORECASE)
            candidate = candidate.strip(" .,!?:;\"'")
            return " ".join(candidate.split())
    return ""


def _is_ambiguous_company_ref(company_name: str) -> bool:
    normalized = " ".join((company_name or "").lower().split())
    return normalized in {
        "",
        "company",
        "the company",
        "this company",
        "that company",
        "said company",
        "same company",
        "it",
    }


def _company_from_memory_context(memory_context: dict | None) -> str:
    if not isinstance(memory_context, dict):
        return ""
    cards = [
        *(memory_context.get("retrieved_object_cards") or []),
        *(memory_context.get("expanded_dependencies") or []),
    ]
    for card in cards:
        if not isinstance(card, dict):
            continue
        for entity in card.get("entity_refs") or []:
            if isinstance(entity, dict) and entity.get("kind") == "company" and entity.get("name"):
                return str(entity["name"])
    for card in cards:
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or "")
        title = re.sub(r"\b(company brief|brief|overview|profile|dcf run|valuation)\b", "", title, flags=re.IGNORECASE)
        title = " ".join(title.strip(" .:-").split())
        if title:
            return title
    return ""


def _ready_docs(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        from documents import list_docs  # noqa: PLC0415
        return [doc for doc in list_docs(session_id) if doc.get("status") == "ready"]
    except Exception:  # noqa: BLE001
        return []


def _workspace_objects(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        from storage import list_workspace_objects  # noqa: PLC0415
        return list_workspace_objects(session_id=session_id, limit=50)
    except Exception:  # noqa: BLE001
        return []


def _focus_areas_from_text(text: str) -> list[str]:
    checks = [
        ("downside risk", r"\b(downside|bear|risk|stress)\b"),
        ("earnings quality", r"\b(earnings quality|quality of earnings|cash conversion)\b"),
        ("growth", r"\b(growth|revenue|top line)\b"),
        ("margins", r"\b(margin|fcff|free cash flow|profitability)\b"),
        ("capital returns", r"\b(buyback|dividend|capital return)\b"),
    ]
    return [label for label, pattern in checks if re.search(pattern, text, re.IGNORECASE)]


def _detect_workflow_id(text: str) -> str | None:
    latest = text or ""
    for workflow_id in ("dcf", "document_analysis", "deck", "memo", "comparison"):
        if _WORKFLOW_PATTERNS[workflow_id].search(latest):
            return workflow_id
    return None


def _default_output_requirements(workflow_id: str) -> dict:
    if workflow_id == "deck":
        return {"audience": "client", "format": "deck_input", "depth": "full"}
    if workflow_id == "memo":
        return {"audience": "investment_committee", "format": "memo", "depth": "full"}
    return {"audience": "analyst", "format": "report", "depth": "full"}


def _selected_artifacts_for_workflow(objects: list[dict], workflow_id: str) -> list[str]:
    if workflow_id == "deck":
        preferred = {"document_analysis", "dcf_run", "memo"}
    elif workflow_id == "memo":
        preferred = {"document_analysis", "dcf_run", "comparison"}
    elif workflow_id == "comparison":
        preferred = {"dcf_run", "document_analysis"}
    else:
        preferred = set()
    return [
        str(obj.get("object_id"))
        for obj in objects
        if obj.get("object_id") and obj.get("object_type") in preferred
    ][:5]


def _build_workflow_context(state: AgentState, workflow_id: str) -> dict:
    messages = state.get("messages", [])
    latest = _latest_user_text(messages)
    session_id = state.get("session_id") or ""
    docs = _ready_docs(session_id)
    objects = _workspace_objects(session_id)
    company_name = _infer_company_from_text(latest)
    if _is_ambiguous_company_ref(company_name):
        company_name = _company_from_memory_context(state.get("memory_context")) or company_name
    if workflow_id == "dcf" and not company_name:
        for doc in docs:
            if doc.get("company"):
                company_name = str(doc["company"])
                break
    selected_docs = [
        str(doc.get("doc_id"))
        for doc in docs
        if doc.get("doc_id") and workflow_id in {"document_analysis", "dcf", "memo", "deck"}
    ]
    selected_artifacts = _selected_artifacts_for_workflow(objects, workflow_id)
    focus = _focus_areas_from_text(latest)
    context = {
        "workflow_id": workflow_id,
        "session_id": session_id,
        "triggering_message_id": f"latest:{hashlib.sha1(latest.encode('utf-8')).hexdigest()[:12]}",
        "triggering_text": latest,
        "user_intent": latest,
        "focus_areas": focus,
        "selected_doc_ids": selected_docs,
        "selected_artifact_ids": selected_artifacts,
        "selected_kg_node_ids": [],
        "output_requirements": _default_output_requirements(workflow_id),
        "available_documents": [
            {
                "doc_id": doc.get("doc_id"),
                "filename": doc.get("filename"),
                "ticker": doc.get("ticker"),
                "company": doc.get("company"),
                "doc_type": doc.get("doc_type"),
                "fiscal_period": doc.get("fiscal_period"),
            }
            for doc in docs
        ],
        "available_artifacts": [
            {
                "object_id": obj.get("object_id"),
                "version_id": obj.get("version_id"),
                "object_type": obj.get("object_type"),
                "title": obj.get("title"),
                "summary": obj.get("summary"),
                "status": obj.get("status"),
            }
            for obj in objects
        ],
    }
    if state.get("memory_context"):
        context["memory_context"] = state["memory_context"]
    if workflow_id == "dcf":
        context["company_name"] = company_name
        context["horizon_years"] = 5
        context["primary_entity"] = {"kind": "company", "name": company_name}
    elif workflow_id == "document_analysis" and docs:
        first_doc = docs[0]
        context["primary_entity"] = {
            "kind": "document",
            "name": first_doc.get("filename") or "Uploaded document",
            "doc_id": first_doc.get("doc_id"),
        }
        context["constraints"] = {"source_policy": "docs_only", "citation_required": True}
    return context


def _workflow_setup_id(state: AgentState) -> str | None:
    route_payload = _current_route_payload(state)
    if route_payload.get("route_level") == "workflow" and route_payload.get("selected_workflow"):
        workflow_id = str(route_payload["selected_workflow"])
        latest = _latest_user_text(state.get("messages", []))
        approved = state.get("approved_workflow_context") or {}
        if approved.get("workflow_id") == workflow_id and approved.get("triggering_text") == latest:
            return None
        return workflow_id

    if state.get("resolved_intent") != "chat":
        return None
    latest = _latest_user_text(state.get("messages", []))
    if not latest or latest.startswith("[DCF_APPROVED]:") or latest.startswith("[DECK_COMPLETE]:"):
        return None
    workflow_id = _detect_workflow_id(latest)
    if not workflow_id:
        return None
    approved = state.get("approved_workflow_context") or {}
    if approved.get("workflow_id") == workflow_id and approved.get("triggering_text") == latest:
        return None
    return workflow_id


def route_intent(state: AgentState) -> str:
    intent = state.get("resolved_intent") or "chat"
    if intent == "chat" and _workflow_setup_id(state):
        return "workflow_context_review"
    return intent


def _next_execution_node(state: AgentState) -> str:
    route_payload = _current_route_payload(state)
    level = route_payload.get("route_level")
    if level == "workflow":
        return "workflow_context_review"
    if level == "case":
        return "plan_case"
    if level == "small_task":
        return "chat"
    if state.get("resolved_intent") == "research":
        return "planning"
    return "chat"


def route_execution(state: AgentState) -> str:
    route_payload = _current_route_payload(state)
    next_node = _next_execution_node(state)
    legacy_next = "research" if next_node == "planning" else next_node
    emit_ui_event(make_execution_step(
        "route_execution",
        route_level=route_payload.get("route_level") or "direct",
        playbook_id=route_payload.get("playbook_id"),
        selected_workflow=route_payload.get("selected_workflow"),
        selected_case_type=route_payload.get("selected_case_type"),
        next_node=legacy_next,
        latency_class=route_payload.get("latency_class"),
        max_tool_calls=route_payload.get("max_tool_calls"),
        creates_objects=route_payload.get("creates_objects"),
        needs_confirmation=route_payload.get("needs_confirmation"),
        object_policy=route_payload.get("object_policy"),
    ))
    return legacy_next


def route_lane(state: AgentState) -> str:
    route_payload = _current_route_payload(state)
    level = route_payload.get("route_level")
    next_node = "work_controller" if level in {"small_task", "workflow", "case"} else "answer_controller"
    emit_ui_event(make_execution_step(
        "route_lane",
        route_level=level or "direct",
        playbook_id=route_payload.get("playbook_id"),
        selected_workflow=route_payload.get("selected_workflow"),
        selected_case_type=route_payload.get("selected_case_type"),
        next_node=next_node,
        latency_class=route_payload.get("latency_class"),
        max_tool_calls=route_payload.get("max_tool_calls"),
        creates_objects=route_payload.get("creates_objects"),
        needs_confirmation=route_payload.get("needs_confirmation"),
        object_policy=route_payload.get("object_policy"),
    ))
    return next_node


def answer_controller_node(state: AgentState) -> dict:
    route_payload = _current_route_payload(state)
    emit_ui_event(make_execution_step(
        "answer_controller",
        route_level=route_payload.get("route_level") or "direct",
        playbook_id=route_payload.get("playbook_id"),
        next_node="chat",
        latency_class=route_payload.get("latency_class"),
        max_tool_calls=route_payload.get("max_tool_calls"),
        object_policy=route_payload.get("object_policy"),
    ))
    return {}


def persist_turn_object_node(state: AgentState) -> dict:
    route_payload = _current_route_payload(state)
    stored = persist_turn_object(state)
    if stored:
        emit_ui_event(make_execution_step(
            "persist_memory_object",
            route_level=route_payload.get("route_level"),
            playbook_id=route_payload.get("playbook_id"),
            object_ids=[stored.get("object_id")],
            reason="live LLM object extraction",
        ))
        emit_ui_event({"type": "memory_object_written", "object": stored})
    return {}


def work_controller_node(state: AgentState) -> dict:
    return {}


def route_work(state: AgentState) -> str:
    route_payload = _current_route_payload(state)
    next_node = _next_execution_node(state)
    emit_ui_event(make_execution_step(
        "work_controller",
        route_level=route_payload.get("route_level") or "small_task",
        playbook_id=route_payload.get("playbook_id"),
        selected_workflow=route_payload.get("selected_workflow"),
        selected_case_type=route_payload.get("selected_case_type"),
        next_node=next_node,
        latency_class=route_payload.get("latency_class"),
        max_tool_calls=route_payload.get("max_tool_calls"),
        creates_objects=route_payload.get("creates_objects"),
        needs_confirmation=route_payload.get("needs_confirmation"),
        object_policy=route_payload.get("object_policy"),
    ))
    return next_node


def plan_case_node(state: AgentState) -> dict:
    return plan_case(state)


def route_after_case_plan(state: AgentState) -> str:
    return "chat" if state.get("case_status") == "downgraded_to_small_task" else "prepare_case_dispatch"


def workflow_context_review_node(state: AgentState) -> dict:
    workflow_id = _workflow_setup_id(state) or "dcf"
    draft = _build_workflow_context(state, workflow_id)
    controls = [
        {
            "field": "selected_doc_ids",
            "label": "Sources",
            "type": "multi_select",
            "options": [
                {
                    "value": doc.get("doc_id"),
                    "label": doc.get("filename") or "Uploaded document",
                    "selected": doc.get("doc_id") in set(draft["selected_doc_ids"]),
                }
                for doc in draft.get("available_documents", [])
                if doc.get("doc_id")
            ],
        },
        {
            "field": "selected_artifact_ids",
            "label": "Workspace objects",
            "type": "multi_select",
            "options": [
                {
                    "value": obj.get("object_id"),
                    "label": obj.get("title") or obj.get("object_id"),
                    "selected": obj.get("object_id") in set(draft["selected_artifact_ids"]),
                }
                for obj in draft.get("available_artifacts", [])
                if obj.get("object_id")
            ],
        },
        {
            "field": "focus_areas",
            "label": "Focus",
            "type": "chips",
            "options": ["downside risk", "earnings quality", "growth", "margins", "capital returns"],
        },
        {
            "field": "output_requirements.depth",
            "label": "Depth",
            "type": "single_select",
            "options": ["full", "quick"],
        },
        {
            "field": "output_requirements.audience",
            "label": "Audience",
            "type": "single_select",
            "options": ["analyst", "investment_committee", "client"],
        },
        {
            "field": "output_requirements.format",
            "label": "Format",
            "type": "single_select",
            "options": ["report", "memo", "deck_input"],
        },
    ]
    payload = {
        "type": "workflow_context_review",
        "workflow_id": workflow_id,
        "title": _WORKFLOW_TITLES.get(workflow_id, "Workflow setup"),
        "context": draft,
        "controls": controls,
    }
    decision = interrupt(payload)
    action = str((decision or {}).get("action") or "approve").lower()
    if action in {"cancel", "reject"} or not (decision or {}).get("approved", True):
        return {
            "workflow_setup_cancelled": True,
            "workflow_context_draft": draft,
            "approved_workflow_context": None,
            "messages": [AIMessage(content=f"{_WORKFLOW_TITLES.get(workflow_id, 'Workflow setup')} cancelled.")],
        }
    context = (decision or {}).get("context") or draft
    context["workflow_id"] = workflow_id
    context.setdefault("triggering_text", draft.get("triggering_text", ""))
    return {
        "workflow_setup_cancelled": False,
        "workflow_context_draft": draft,
        "approved_workflow_context": context,
    }


def route_after_review(state: AgentState) -> str:
    return "execute_one_step" if state.get("approved") else END


# ---------------------------------------------------------------------------
# Graph: START → turn context/memory → controller → playbook/policy → execution
# ---------------------------------------------------------------------------

graph = StateGraph(AgentState)

graph.add_node("build_turn_context", traced_node("build_turn_context", build_turn_context_node, category="memory"))
graph.add_node("semantic_router", traced_node("semantic_router", semantic_router_node))
graph.add_node("propose_task", propose_task_node)
graph.add_node("load_playbook", traced_node("load_playbook", load_playbook_node))
graph.add_node("apply_runtime_policy", traced_node("apply_runtime_policy", apply_runtime_policy_node))
graph.add_node("build_memory_context", traced_node("build_memory_context", build_memory_context_node, category="memory"))
graph.add_node("answer_controller", traced_node("answer_controller", answer_controller_node))
graph.add_node("work_controller", traced_node("work_controller", work_controller_node))
graph.add_node("planning", traced_node("planning", plan_node))
graph.add_node("review_plan", traced_node("review_plan", review_plan_node))
graph.add_node("execute_one_step", traced_node("execute_one_step", execute_one_step_node))
graph.add_node("synthesize", traced_node("synthesize", synthesize_node))
graph.add_node("update_memory", traced_node("update_memory", update_memory_node, category="memory"))
graph.add_node("chat", traced_node("chat", chat_node))
graph.add_node("persist_turn_object", traced_node("persist_turn_object", persist_turn_object_node, category="memory"))
graph.add_node("workflow_context_review", traced_node("workflow_context_review", workflow_context_review_node))
graph.add_node("plan_case", traced_node("plan_case", plan_case_node))
graph.add_node("prepare_case_dispatch", traced_node("prepare_case_dispatch", prepare_case_dispatch_node))
graph.add_node("execute_case_task", traced_node("execute_case_task", execute_case_task_node))
graph.add_node("collect_case_results", traced_node("collect_case_results", collect_case_results_node))
graph.add_node("synthesize_case", traced_node("synthesize_case", synthesize_case_node))

graph.add_edge(START, "build_turn_context")
graph.add_edge("build_turn_context", "build_memory_context")
graph.add_edge("build_memory_context", "semantic_router")
graph.add_edge("semantic_router", "propose_task")
graph.add_edge("propose_task", "load_playbook")
graph.add_edge("load_playbook", "apply_runtime_policy")
graph.add_conditional_edges("apply_runtime_policy", route_lane, {
    "answer_controller": "answer_controller",
    "work_controller": "work_controller",
})
graph.add_edge("answer_controller", "chat")
graph.add_conditional_edges("work_controller", route_work, {
    "chat": "chat",
    "planning": "planning",
    "workflow_context_review": "workflow_context_review",
    "plan_case": "plan_case",
})
graph.add_conditional_edges(
    "workflow_context_review",
    lambda state: END if state.get("workflow_setup_cancelled") else "chat",
    {"chat": "chat", END: END},
)
graph.add_edge("planning", "review_plan")
graph.add_conditional_edges("review_plan", route_after_review, {"execute_one_step": "execute_one_step", END: END})
graph.add_conditional_edges("execute_one_step", route_after_step, {"execute_one_step": "execute_one_step", "synthesize": "synthesize"})
graph.add_edge("synthesize", "update_memory")
graph.add_edge("update_memory", END)
graph.add_edge("chat", "persist_turn_object")
graph.add_edge("persist_turn_object", END)
graph.add_conditional_edges(
    "plan_case",
    route_after_case_plan,
    {
        "chat": "chat",
        "prepare_case_dispatch": "prepare_case_dispatch",
    },
)
graph.add_conditional_edges("prepare_case_dispatch", dispatch_case_tasks, ["execute_case_task", "synthesize_case"])
graph.add_edge("execute_case_task", "collect_case_results")
graph.add_edge("collect_case_results", "prepare_case_dispatch")
graph.add_edge("synthesize_case", END)

# Runtime app used by the FastAPI server (keeps existing memory behavior).
app = graph.compile(checkpointer=durable_checkpointer("main_agent"))

# Studio/LangGraph API app must not provide a custom checkpointer; the
# platform/runtime manages persistence itself.
studio_app = graph.compile()


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_agent(query: str, mode: str = "auto") -> None:
    thread_id = f"thread_{uuid4().hex[:8]}"
    set_thread_id(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    agent_log.run_start(thread_id, query, mode)

    first = app.invoke(
        {"messages": [HumanMessage(content=query)], "mode": mode, "resolved_intent": None},
        config=config,
    )
    resolved = first.get("resolved_intent", "research")
    agent_log.intent_classified(resolved, mode)

    if resolved == "chat":
        # Chat response is already emitted via events; print last message
        msgs = first.get("messages", [])
        if msgs:
            last = msgs[-1]
            content = last.content if hasattr(last, "content") else ""
            console.print(Panel(str(content), title="💬 Chat Response", border_style="cyan"))
        return

    # Research: handle HITL
    interrupts = first.get("__interrupt__", ())
    if not interrupts:
        return

    plan_payload = interrupts[0].value
    console.print()
    format_plan(plan_payload.get("plan", {}))
    user_input = input("\nAction? [yes/no/edit_plan]: ").strip().lower()
    feedback = input("Optional feedback (enter to skip): ").strip() or None

    if user_input in {"", "yes", "y"}:
        resume_value = {"action": "yes", "feedback": feedback}
    elif user_input in {"edit", "edit_plan"}:
        edited = input("Paste modified plan JSON (single line): ").strip()
        try:
            resume_value = {"action": "edit_plan", "feedback": feedback, "plan": json.loads(edited)}
        except json.JSONDecodeError:
            print("Invalid JSON; stopping.")
            return
    else:
        resume_value = {"action": "no", "feedback": feedback}

    from lg_compat import Command  # noqa: PLC0415
    result = app.invoke(
        Command(resume=resume_value),
        config=config,
    )
    msgs = result.get("messages", [])
    if msgs:
        last = msgs[-1]
        content = last.content if hasattr(last, "content") else ""
        if content:
            console.print(Panel(str(content), title="📄 Final Report", border_style="green", padding=(1, 2)))


if __name__ == "__main__":
    run_agent("What are the latest news about Apple, and give me the price from the last 5 years?")

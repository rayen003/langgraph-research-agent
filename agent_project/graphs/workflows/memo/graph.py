"""Investment memo LangGraph with typed inputs, HITL review, and durable output."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from checkpointing import durable_checkpointer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from utils import get_run_dir
from tracing import traced_node
from workflow_activity import emit_workflow, emit_workflow_step

from .persistence import persist_memo_context, persist_memo_draft, persist_memo_result
from .state import MemoBrief, MemoDraft, MemoOutput, MemoSource, MemoState


def validate_inputs_node(state: MemoState) -> dict[str, Any]:
    parent_step_id = state.get("parent_step_id") or "workflow_memo"
    emit_workflow(workflow="memo", parent_step_id=parent_step_id, status="start")
    emit_workflow_step(
        workflow="memo", step="validate_inputs", status="start", parent_step_id=parent_step_id,
        payload={"summary_line": "Checking brief and sources"},
    )
    brief = MemoBrief.model_validate(state.get("brief") or {})
    sources = [MemoSource.model_validate(source) for source in state.get("sources") or []]
    if not sources:
        raise ValueError("Memo workflow requires at least one source")
    normalized = {**state, "brief": brief.model_dump(), "sources": [source.model_dump() for source in sources]}
    context = persist_memo_context(normalized)
    emit_workflow_step(
        workflow="memo", step="validate_inputs", status="complete", parent_step_id=parent_step_id,
        payload={
            "summary_line": f"{len(sources)} source{'s' if len(sources) != 1 else ''} selected",
            "detail": {
                "inputs": {"company_name": brief.company_name, "audience": brief.audience},
                "outputs": {"source_count": len(sources)},
                "object_refs": [context.get("version_id")] if context.get("version_id") else [],
            },
        },
    )
    return {
        "brief": brief.model_dump(),
        "sources": [source.model_dump() for source in sources],
        "context_version_id": context.get("version_id"),
    }


def generate_draft_node(state: MemoState) -> dict[str, Any]:
    parent_step_id = state.get("parent_step_id") or "workflow_memo"
    emit_workflow_step(
        workflow="memo", step="generate_draft", status="start", parent_step_id=parent_step_id,
        payload={"summary_line": "Drafting memo"},
    )
    brief = MemoBrief.model_validate(state["brief"])
    sources = [MemoSource.model_validate(source) for source in state["sources"]]
    model = ChatOpenAI(
        model=os.getenv("MEMO_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=90,
    ).with_structured_output(MemoDraft, method="function_calling")
    payload = {
        "brief": brief.model_dump(),
        "sources": [source.model_dump() for source in sources],
        "rules": [
            "Use only supplied source content and summaries.",
            "Separate evidence, inference, and missing information.",
            "Preserve supplied citation IDs in relevant sections.",
            "Do not invent valuation outputs, prices, dates, or financial metrics.",
            "Include every required section and decision-ready recommendation.",
        ],
    }
    draft = model.invoke([
        SystemMessage(content="You are institutional investment memo analyst. Return requested structured memo draft."),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ])
    if not isinstance(draft, MemoDraft):
        draft = MemoDraft.model_validate(draft)
    emit_workflow_step(
        workflow="memo", step="generate_draft", status="complete", parent_step_id=parent_step_id,
        payload={
            "summary_line": f"Drafted {len(draft.sections)} sections",
            "detail": {
                "outputs": {"title": draft.title, "section_count": len(draft.sections)},
                "evidence_refs": list(draft.source_refs),
                "metrics": {"confidence": draft.confidence},
            },
        },
    )
    return {"draft": draft.model_dump(), "draft_approved": False}


def review_draft_node(state: MemoState) -> dict[str, Any]:
    brief = MemoBrief.model_validate(state["brief"])
    parent_step_id = state.get("parent_step_id") or "workflow_memo"
    if brief.hitl_mode == "disabled":
        decision: dict[str, Any] = {"action": "approve", "actor_id": "system:auto"}
    else:
        emit_workflow_step(
            workflow="memo", step="review_draft", status="awaiting_input", parent_step_id=parent_step_id,
            payload={
                "summary_line": "Draft ready for review",
                "review_ref": {"type": "memo_draft_review", "workflow_id": "memo"},
            },
        )
        emit_workflow(
            workflow="memo", parent_step_id=parent_step_id, status="awaiting_input",
            payload={
                "summary_line": "Draft ready for review",
                "review_ref": {"type": "memo_draft_review", "workflow_id": "memo"},
            },
        )
        decision = interrupt({
            "type": "memo_draft_review",
            "workflow_id": "memo",
            "draft": state["draft"],
            "sources": state.get("sources") or [],
            "context_version_id": state.get("context_version_id"),
            "options": ["approve", "edit", "reject"],
        })
    action = str((decision or {}).get("action") or "approve").lower()
    if action == "reject":
        emit_workflow_step(
            workflow="memo", step="review_draft", status="rejected", parent_step_id=parent_step_id,
            payload={"summary_line": "Draft rejected", "error": str(decision.get("feedback") or "Draft rejected")},
        )
        emit_workflow(
            workflow="memo", parent_step_id=parent_step_id, status="rejected",
            payload={"summary_line": "Memo stopped after review", "error": str(decision.get("feedback") or "Draft rejected")},
        )
        return {"draft_approved": False, "review_feedback": str(decision.get("feedback") or "")}
    draft = state["draft"]
    if action == "edit":
        draft = MemoDraft.model_validate(decision.get("draft") or draft).model_dump()
    actor_id = str(decision.get("actor_id") or "user")
    persisted = persist_memo_draft({**state, "draft": draft}, actor_id=actor_id, decision=action)
    emit_workflow_step(
        workflow="memo", step="review_draft", status="edited" if action == "edit" else "approved",
        parent_step_id=parent_step_id,
        payload={
            "summary_line": "Edits approved" if action == "edit" else "Draft approved",
            "detail": {
                "outputs": {"decision": action, "actor_id": actor_id},
                "object_refs": [persisted.get("version_id")] if persisted.get("version_id") else [],
            },
        },
    )
    return {
        "draft": draft,
        "draft_approved": True,
        "review_feedback": str(decision.get("feedback") or ""),
        "approved_draft_version_id": persisted.get("version_id"),
    }


def route_after_review(state: MemoState) -> str:
    return "finalize_memo" if state.get("draft_approved") else END


def _markdown(draft: MemoDraft) -> str:
    parts = [f"# {draft.title}", "", "## Executive summary", draft.executive_summary]
    for title, body in draft.sections.items():
        parts.extend(["", f"## {title}", body])
    parts.extend(["", "## Recommendation", draft.recommendation])
    if draft.limitations:
        parts.extend(["", "## Limitations", *[f"- {item}" for item in draft.limitations]])
    return "\n".join(parts).strip() + "\n"


def finalize_memo_node(state: MemoState) -> dict[str, Any]:
    parent_step_id = state.get("parent_step_id") or "workflow_memo"
    emit_workflow_step(
        workflow="memo", step="finalize_memo", status="start", parent_step_id=parent_step_id,
        payload={"summary_line": "Persisting approved memo"},
    )
    draft = MemoDraft.model_validate(state["draft"])
    thread_id = str(state["thread_id"])
    output_dir = get_run_dir() / "memos"
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "memo.md"
    markdown = _markdown(draft)
    markdown_path.write_text(markdown, encoding="utf-8")
    source_object_ids = list(dict.fromkeys(
        str(source["object_id"]) for source in state.get("sources") or [] if source.get("object_id")
    ))
    source_version_ids = list(dict.fromkeys(
        str(source["version_id"]) for source in state.get("sources") or [] if source.get("version_id")
    ))
    output = MemoOutput(
        status="complete",
        memo_id=f"memo:{thread_id}",
        title=draft.title,
        markdown=markdown,
        markdown_path=str(markdown_path),
        context_version_id=state.get("context_version_id"),
        approved_draft_version_id=state.get("approved_draft_version_id"),
        source_object_ids=source_object_ids,
        source_version_ids=source_version_ids,
        source_refs=draft.source_refs,
        confidence=draft.confidence,
        limitations=draft.limitations,
    ).model_dump()
    stored = persist_memo_result(state, output)
    output["memo_version_id"] = stored.get("version_id")
    emit_workflow_step(
        workflow="memo", step="finalize_memo", status="complete", parent_step_id=parent_step_id,
        payload={
            "summary_line": "Memo saved",
            "detail": {
                "outputs": {"title": draft.title, "status": "complete"},
                "artifact_refs": [str(markdown_path)],
                "object_refs": [stored.get("version_id")] if stored.get("version_id") else [],
                "evidence_refs": source_version_ids,
            },
        },
    )
    emit_workflow(
        workflow="memo", parent_step_id=parent_step_id, status="complete",
        payload={
            "summary_line": f"Memo completed · {draft.title}",
            "detail": {
                "artifact_refs": [str(markdown_path)],
                "object_refs": [stored.get("version_id")] if stored.get("version_id") else [],
            },
        },
    )
    return {"output": output, "memo_version_id": stored.get("version_id")}


_graph = StateGraph(MemoState)
_graph.add_node("validate_inputs", traced_node("memo.validate_inputs", validate_inputs_node))
_graph.add_node("generate_draft", traced_node("memo.generate_draft", generate_draft_node))
_graph.add_node("review_draft", traced_node("memo.review_draft", review_draft_node))
_graph.add_node("finalize_memo", traced_node("memo.finalize", finalize_memo_node))
_graph.add_edge(START, "validate_inputs")
_graph.add_edge("validate_inputs", "generate_draft")
_graph.add_edge("generate_draft", "review_draft")
_graph.add_conditional_edges("review_draft", route_after_review, {"finalize_memo": "finalize_memo", END: END})
_graph.add_edge("finalize_memo", END)

memo_workflow_app = _graph.compile(checkpointer=durable_checkpointer("memo_workflow"))


def run_memo_workflow_sync(
    *, sources: list[dict[str, Any]], brief: dict[str, Any], session_id: str = "", parent_step_id: str = "workflow_memo",
) -> dict[str, Any]:
    thread_id = get_run_dir().name
    initial: MemoState = {
        "thread_id": thread_id,
        "session_id": session_id,
        "parent_step_id": parent_step_id,
        "sources": sources,
        "brief": brief,
    }
    config = {"configurable": {"thread_id": f"{thread_id}_memo"}, "recursion_limit": 30}
    result = memo_workflow_app.invoke(initial, config=config)
    graph_state = memo_workflow_app.get_state(config)
    if graph_state.next:
        payload: dict[str, Any] = {}
        for task in graph_state.tasks or []:
            for item in getattr(task, "interrupts", None) or []:
                if isinstance(getattr(item, "value", None), dict):
                    payload = item.value
                    break
        return {"__memo_hitl__": True, "workflow": "memo", **payload}
    if not result.get("output"):
        return {"__memo_rejected__": True, "workflow": "memo", "status": "rejected", "feedback": result.get("review_feedback")}
    return MemoOutput.model_validate(result["output"]).model_dump()

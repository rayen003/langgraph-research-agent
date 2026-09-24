"""
FastAPI server for the agent backend.

Endpoints
---------
POST   /runs                             create & start a new agent run
GET    /runs/{thread_id}/events          SSE stream of execution events
POST   /runs/{thread_id}/decision        HITL approve / reject the plan
GET    /runs/{thread_id}/plan            latest plan JSON for the thread
GET    /runs/{thread_id}/report          final markdown report (if complete)
GET    /runs/{thread_id}/dcf-report.md   DCF valuation report (markdown)
GET    /runs/{thread_id}/dcf-report.pdf  DCF valuation report (PDF)
GET    /runs/{thread_id}/decks/{filename} download generated deck PPTX
GET    /runs/{thread_id}/deck-output      deck JSON snapshot for slide preview
GET    /artifacts/{thread_id}/{filename} serve generated artifact files
GET    /sources/fmp/{ticker}              authenticated FMP source data proxy
GET    /jobs                             list all runs as job summaries
POST   /workflows/dcf/runs               create & start deterministic DCF workflow
POST   /workflows/dcf/runs/{thread_id}/assumptions-decision
                                         approve/edit optional assumption review
POST   /runs/{thread_id}/dcf-decision      approve/edit/reject DCF assumptions review
POST   /runs/{thread_id}/deck-decision      approve/edit/reject deck outline review
GET    /workflows/dcf/runs/{thread_id}/result
                                         get persisted DCF workflow result JSON
GET    /health                           liveness check
"""

import os

# Before chromadb (or anything that imports it) — silences broken PostHog telemetry.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import asyncio
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

import agent_log
import dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from typing import Any
from pydantic import BaseModel, Field
import requests

dotenv.load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

import lg_compat  # noqa: F401 — validates langgraph version at startup, not mid-request

from plan_store import save_plan as save_plan_to_store  # noqa: E402
from storage import (  # noqa: E402
    append_job_event,
    delete_workspace_object,
    get_job,
    get_document_version,
    get_report as get_stored_report,
    get_session_layout,
    get_session_memory,
    get_workspace_object,
    get_workspace_object_version,
    list_object_actions,
    list_route_records,
    list_workspace_object_versions,
    list_workspace_objects,
    list_job_events,
    list_document_versions,
    list_jobs as list_stored_jobs,
    mark_stale_running_jobs,
    replace_session_layout,
    update_job,
    upsert_job,
    upsert_workspace_object,
)
from collaboration_store import (  # noqa: E402
    add_membership,
    create_assignment,
    create_approval,
    create_channel,
    create_message,
    create_object_comment,
    create_suggestion,
    decide_approval,
    decide_suggestion,
    ensure_workspace,
    list_actors,
    list_assignments,
    list_channels,
    list_messages,
    list_notifications,
    list_object_approvals,
    list_object_comments,
    list_object_suggestions,
    mark_notification_read,
    require_workspace_role,
    resolve_comment,
    update_actor_status,
    update_assignment,
    upsert_actor,
)
from tracing import (  # noqa: E402
    TraceCallbackHandler,
    emit_trace_span,
    make_trace_span,
    set_trace_context,
)

AGENT_DIR = Path(__file__).parent
RUNS_DIR = AGENT_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# Console: rich, level-colored, human-scannable. File: plain, grep-friendly.
# (rich shares the agent_log console palette so structured + stdlib logs match.)
from rich.logging import RichHandler  # noqa: E402

_console_handler = RichHandler(
    rich_tracebacks=True,
    show_path=False,          # [name] in the message is enough; full paths are noise
    omit_repeated_times=True,  # collapse identical HH:MM:SS prefixes
    markup=False,
)
_console_handler.setFormatter(logging.Formatter("[%(name)s] %(message)s", datefmt="%H:%M:%S"))

_file_handler = logging.FileHandler(RUNS_DIR / "server.log", encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
mark_stale_running_jobs()


POLL_INTERVAL_SECONDS = 0.5
STREAM_SUBSCRIBER_GRACE_SECONDS = 0.75


# ---------------------------------------------------------------------------
# Run state registry
# ---------------------------------------------------------------------------

class RunState:
    __slots__ = (
        "thread_id", "loop", "event_queue", "stream_connected", "stream_expected", "hitl_future",
        "status", "query", "mode", "intent", "created_at", "session_id",
        "dcf_hitl_payload", "deck_hitl_payload", "memo_hitl_payload", "workflow_context_payload",
        "collaboration_channel_id", "collaboration_workspace_id", "collaboration_actor_id", "collaboration_replied",
        "collaboration_object_version_ids", "collaboration_assignment_id", "collaboration_task_context",
        "trace_id", "root_span_id", "trace_started_at",
    )

    def __init__(self, thread_id: str, loop: asyncio.AbstractEventLoop, query: str, mode: str, session_id: str = "") -> None:
        self.thread_id = thread_id
        self.loop = loop
        self.event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.stream_connected = asyncio.Event()
        self.stream_expected = False
        self.hitl_future: asyncio.Future | None = None
        self.status = "classifying"
        self.query = query
        self.mode = mode
        self.intent: str | None = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.session_id = session_id
        self.dcf_hitl_payload: dict | None = None
        self.deck_hitl_payload: dict | None = None
        self.memo_hitl_payload: dict | None = None
        self.workflow_context_payload: dict | None = None
        self.collaboration_channel_id: str | None = None
        self.collaboration_workspace_id: str | None = None
        self.collaboration_actor_id: str | None = None
        self.collaboration_replied = False
        self.collaboration_object_version_ids: list[str] = []
        self.collaboration_assignment_id: str | None = None
        self.collaboration_task_context: dict[str, Any] | None = None
        self.trace_id = f"trace_{uuid4().hex[:16]}"
        self.root_span_id = f"run_{uuid4().hex[:12]}"
        self.trace_started_at = time.time()


_run_registry: dict[str, RunState] = {}

_RUNNING_STATUSES = {
    "classifying",
    "planning",
    "awaiting_approval",
    "awaiting_assumptions",
    "awaiting_workflow_context",
    "awaiting_outline_review",
    "awaiting_memo_review",
    "workflow_running",
    "executing",
    "synthesizing",
    "chat_responding",
}


def _persist_event(thread_id: str, event: dict) -> dict:
    return append_job_event(thread_id, event)


def _send_event(rs: RunState, event: dict) -> None:
    persisted = _persist_event(rs.thread_id, event)
    rs.event_queue.put_nowait(persisted)


async def _await_stream_subscriber(rs: RunState) -> None:
    """Give interactive clients time to subscribe before expensive work starts.

    Prevents first workflow activities becoming one replay burst. Timeout keeps
    background, collaboration, and API-only runs independent from UI presence.
    """
    if not rs.stream_expected or rs.stream_connected.is_set():
        return
    try:
        await asyncio.wait_for(
            rs.stream_connected.wait(),
            timeout=STREAM_SUBSCRIBER_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        return


def _format_sse_event(event: dict) -> str:
    event_id = event.get("event_id")
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _load_latest_plan(thread_id: str) -> tuple[dict, Path] | tuple[None, None]:
    plan_path = _latest_plan_path(thread_id)
    if plan_path is None:
        return None, None
    try:
        return json.loads(plan_path.read_text(encoding="utf-8")), plan_path
    except (json.JSONDecodeError, OSError):
        return None, None


def _build_context_stack_from_plan(plan: dict) -> list[dict]:
    context_stack: list[dict] = []
    for step in plan.get("steps", []):
        if step.get("status") != "completed":
            continue
        result = (step.get("result") or "").strip()
        summary = result.replace("\n", " ")[:220]
        if len(result) > 220:
            summary += "..."
        context_stack.append({
            "step_id": step.get("id"),
            "summary": summary,
            "tool_result_ids": step.get("tool_result_ids") or [],
        })
    return context_stack


def _prepare_plan_for_resume(plan: dict) -> dict:
    for step in plan.get("steps", []):
        if step.get("status") == "completed":
            continue
        step["status"] = "pending"
        step["result"] = None
        step["tool_result_ids"] = []
    plan["status"] = "approved"
    return plan


async def _resume_research_task(thread_id: str, session_id: str = "") -> None:
    from graphs.research import execute_one_step_node, route_after_step, synthesize_node, update_memory_node  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    job = get_job(thread_id)
    if not job:
        return

    rs = _run_registry.get(thread_id)
    if rs is None:
        rs = RunState(thread_id, loop, job["query"], job["mode"], session_id or job.get("session_id") or "")
        rs.intent = job.get("intent") or "research"
        _run_registry[thread_id] = rs

    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))
    session_id = session_id or rs.session_id or job.get("session_id") or ""

    try:
        plan, plan_path = _load_latest_plan(thread_id)
        if not plan or not plan_path:
            raise RuntimeError(f"No persisted plan found for '{thread_id}'.")

        plan = _prepare_plan_for_resume(plan)
        save_plan_to_store(thread_id, plan)

        rs.status = "executing"
        update_job(thread_id, status="executing", intent="research")
        _send_event(rs, {"type": "execution_started", "resumed": True})

        state = {
            "plan": plan,
            "objective": plan.get("query") or job["query"],
            "review_feedback": None,
            "context_stack": _build_context_stack_from_plan(plan),
            "session_id": session_id,
            "session_memory": get_session_memory(session_id),
        }

        # Execute steps one at a time via the new per-step node so each step
        # is a real LangGraph node invocation → checkpointing, streaming.
        while True:
            executed = await asyncio.to_thread(execute_one_step_node, state)
            state.update(executed)
            if route_after_step(state) != "execute_one_step":
                break

        synthesized = await asyncio.to_thread(synthesize_node, state)
        state.update(synthesized)

        memory = await asyncio.to_thread(update_memory_node, state)
        state.update(memory)

        rs.status = "complete"
        update_job(thread_id, status="complete")
        _send_event(rs, {"type": "run_complete"})

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Resume task failed:\n%s", tb)
        rs.status = "error"
        update_job(thread_id, status="error", error=f"{type(exc).__name__}: {exc}")
        _send_event(rs, {"type": "error", "message": f"{type(exc).__name__}: {exc}\n\n{tb}"})
    finally:
        rs.event_queue.put_nowait(None)
        _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)


def _should_auto_resume(job: dict) -> bool:
    if job.get("status") != "interrupted" or job.get("intent") != "research":
        return False
    plan, _ = _load_latest_plan(job["thread_id"])
    if not plan:
        return False
    if plan.get("status") not in {"approved", "in_progress"}:
        return False
    return any(step.get("status") != "completed" for step in plan.get("steps", []))


async def _handle_dcf_hitl(rs: RunState, thread_id: str, dcf_data: dict) -> dict | None:
    """Common DCF HITL handler used by both chat and research paths.

    Creates a future, waits for the user to approve/reject, and returns
    the assumption overrides (or None if rejected).  Callers decide how
    to resume execution (new ainvoke vs Command(resume)).
    """
    rs.hitl_future = rs.loop.create_future()
    decision = await rs.hitl_future

    if not decision.get("approved"):
        rs.status = "rejected"
        update_job(thread_id, status=rs.status)
        _send_event(rs, {"type": "assumptions_rejected", "workflow": "dcf"})
        return None

    overrides = decision.get("assumptions_overrides") or {}
    _send_event(
        rs,
        {
            "type": "assumptions_submitted",
            "workflow": "dcf",
            "overrides_applied": bool(overrides),
        },
    )
    return overrides


async def _handle_deck_hitl(rs: RunState, thread_id: str) -> dict | None:
    """Wait for deck outline approval, resume the paused deck graph, return payload."""
    from pathlib import Path  # noqa: PLC0415

    from graphs.workflows.deck import deck_workflow_app  # noqa: PLC0415
    from lg_compat import Command  # noqa: PLC0415
    from utils import get_run_dir, set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs.hitl_future = rs.loop.create_future()
    decision = await rs.hitl_future

    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))

    def _invoke_deck_resume(resume_payload: dict) -> dict:
        # run_in_executor drops contextvars — bind the chat thread before disk I/O.
        set_thread_id(thread_id)
        deck_config = {"configurable": {"thread_id": f"{get_run_dir().name}_deck"}}
        return deck_workflow_app.invoke(Command(resume=resume_payload), config=deck_config)

    if not decision.get("approved"):
        feedback = decision.get("feedback") or ""
        _send_event(
            rs,
            {
                "type": "deck_outline_rejected",
                "workflow": "deck",
                "feedback": feedback,
            },
        )
        try:
            await rs.loop.run_in_executor(
                None,
                lambda: _invoke_deck_resume({"action": "reject", "feedback": feedback or None}),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Deck reject resume failed for thread=%s", thread_id, exc_info=True)
        finally:
            set_ui_event_handler(None)
        return None

    action = str(decision.get("action") or "approve").lower()
    resume_payload: dict = {"action": action}
    outline = decision.get("outline")
    if action == "edit" and isinstance(outline, dict):
        resume_payload["outline"] = outline
    feedback = decision.get("feedback")
    if feedback:
        resume_payload["feedback"] = feedback

    rs.status = "workflow_running"
    update_job(thread_id, status=rs.status)
    _send_event(
        rs,
        {
            "type": "deck_outline_submitted",
            "workflow": "deck",
            "action": action,
        },
    )

    try:
        result = await rs.loop.run_in_executor(
            None,
            lambda: _invoke_deck_resume(resume_payload),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Deck resume failed for thread=%s: %s", thread_id, exc, exc_info=True)
        rs.status = "error"
        update_job(thread_id, status=rs.status)
        _send_event(rs, {"type": "error", "workflow": "deck", "message": str(exc)})
        return None
    finally:
        set_ui_event_handler(None)

    if result.get("outline_approved") is False:
        return None

    deck_output_path = result.get("deck_output_path")
    if deck_output_path:
        return json.loads(Path(deck_output_path).read_text(encoding="utf-8"))

    logger.warning("Deck resume finished without deck_output_path for thread=%s", thread_id)
    return None


async def _handle_memo_hitl(rs: RunState, thread_id: str) -> dict | None:
    """Wait for memo draft decision and resume paused memo graph."""
    from graphs.workflows.memo import memo_workflow_app  # noqa: PLC0415
    from lg_compat import Command  # noqa: PLC0415
    from utils import get_run_dir, set_thread_id  # noqa: PLC0415

    rs.hitl_future = rs.loop.create_future()
    decision = await rs.hitl_future
    action = "approve" if decision.get("approved", True) else "reject"
    action = str(decision.get("action") or action).lower()
    resume_payload: dict[str, Any] = {
        "action": action,
        "actor_id": str(decision.get("actor_id") or "user"),
    }
    if decision.get("feedback"):
        resume_payload["feedback"] = decision["feedback"]
    if action == "edit" and isinstance(decision.get("draft"), dict):
        resume_payload["draft"] = decision["draft"]

    _send_event(rs, {
        "type": "memo_draft_rejected" if action == "reject" else "memo_draft_submitted",
        "workflow": "memo",
        "action": action,
    })

    set_thread_id(thread_id)
    config = {"configurable": {"thread_id": f"{get_run_dir().name}_memo"}, "recursion_limit": 30}
    result = await rs.loop.run_in_executor(
        None, lambda: memo_workflow_app.invoke(Command(resume=resume_payload), config=config),
    )
    return result.get("output") if action != "reject" else None


def _has_pending_collaboration_review(rs: RunState) -> bool:
    return bool(
        rs.dcf_hitl_payload
        or rs.deck_hitl_payload
        or rs.memo_hitl_payload
        or rs.workflow_context_payload
    )


def _complete_collaboration_assignment(
    rs: RunState,
    content: str,
    *,
    artifact_paths: list[str] | None = None,
    output_version_ids: list[str] | None = None,
) -> None:
    """Persist one final channel response and link it to tracked assignment."""
    if (
        not rs.collaboration_workspace_id
        or rs.collaboration_replied
    ):
        return
    task_context = rs.collaboration_task_context or {}
    result_object = upsert_workspace_object({
        "object_id": f"task_result:{rs.thread_id}",
        "object_type": "task_result",
        "schema_ref": "workspace.collaboration_task_result",
        "schema_version": "0.1",
        "title": str(task_context.get("title") or rs.query.splitlines()[0])[:140],
        "status": "complete",
        "session_id": rs.session_id,
        "thread_id": rs.thread_id,
        "task_id": rs.collaboration_assignment_id,
        "run_id": rs.thread_id,
        "source_message_id": task_context.get("source_message_id"),
        "created_by": rs.collaboration_actor_id or "agent:research",
        "updated_by": rs.collaboration_actor_id or "agent:research",
        "source_version_ids": rs.collaboration_object_version_ids,
        "artifact_paths": artifact_paths or [],
        "summary": " ".join(content.split())[:300],
        "search_text": f"{rs.query} {content}",
        "payload": {
            "assignment": task_context.get("goal") or rs.query,
            "result": content,
            "channel_id": rs.collaboration_channel_id,
        },
    }, action_type="agent_assignment_completed")
    result_version = result_object.get("version_id")
    outputs = [str(value) for value in (output_version_ids or []) if value]
    if result_version:
        outputs.append(str(result_version))
    outputs = list(dict.fromkeys(outputs))
    if rs.collaboration_assignment_id:
        update_assignment(
            rs.collaboration_assignment_id,
            "completed",
            rs.collaboration_actor_id or "agent:research",
            thread_id=rs.thread_id,
            output_object_version_ids=outputs,
        )
    if rs.collaboration_channel_id:
        create_message(
            rs.collaboration_channel_id,
            rs.collaboration_workspace_id,
            rs.collaboration_actor_id or "agent:research",
            {
                "body": content,
                "mentions": [],
                "object_version_ids": [*rs.collaboration_object_version_ids, *outputs],
            },
        )
    rs.collaboration_replied = True


def _make_event_bridge(rs: RunState):
    """Return a sync callback safe to call from any thread."""
    def bridge(event: dict) -> None:
        # Intercept intent_classified to update RunState.intent
        if event.get("type") == "intent_classified":
            rs.intent = event.get("intent")
            rs.status = "planning" if rs.intent == "research" else "chat_responding"
            update_job(rs.thread_id, status=rs.status, intent=rs.intent)
        elif event.get("type") == "dcf_assumptions_review":
            # Store full HITL snapshot on RunState — restored on fast-path re-run.
            rs.dcf_hitl_payload = {
                "ticker": event.get("ticker", "?"),
                "horizon_years": event.get("horizon_years", 5),
                "assumptions": event.get("assumptions", {}),
                "assumption_provenance": event.get("assumption_provenance", {}),
                "assumption_memo": event.get("assumption_memo"),
                "memo_proposals": event.get("memo_proposals", {}),
                "evidence_items": event.get("evidence_items", []),
                "scenarios": event.get("scenarios", []),
                "company_state": event.get("company_state"),
                "thesis": event.get("thesis"),
                "features": event.get("features", {}),
                "fundamentals": event.get("fundamentals", {}),
                "profile": event.get("profile", "default"),
                "profile_meta": event.get("profile_meta", {}),
                "wacc_components": event.get("wacc_components", {}),
            }
            rs.status = "awaiting_assumptions"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "deck_outline_review":
            rs.deck_hitl_payload = {
                "deck_title": event.get("deck_title", ""),
                "hitl_mode": event.get("hitl_mode", "partial"),
                "outline": event.get("outline", {}),
                "blocks_preview": event.get("blocks_preview", []),
                "slide_count": event.get("slide_count", 0),
            }
            rs.status = "awaiting_outline_review"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "memo_draft_review":
            rs.memo_hitl_payload = {
                "draft": event.get("draft", {}),
                "sources": event.get("sources", []),
                "context_version_id": event.get("context_version_id"),
            }
            rs.status = "awaiting_memo_review"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "workflow_context_review":
            rs.workflow_context_payload = {
                "workflow_id": event.get("workflow_id", ""),
                "title": event.get("title", "Workflow setup"),
                "context": event.get("context", {}),
                "controls": event.get("controls", []),
            }
            rs.status = "awaiting_workflow_context"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "synthesis_start":
            rs.status = "synthesizing"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "synthesis_complete":
            rs.status = "complete"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "chat_complete":
            if not _has_pending_collaboration_review(rs):
                _complete_collaboration_assignment(
                    rs,
                    str(event.get("content") or ""),
                    artifact_paths=event.get("artifact_paths")
                    if isinstance(event.get("artifact_paths"), list)
                    else [],
                )
            _sync_text_workspace_object(
                thread_id=rs.thread_id,
                session_id=rs.session_id,
                query=rs.query,
                content=str(event.get("content") or ""),
                artifact_paths=event.get("artifact_paths") if isinstance(event.get("artifact_paths"), list) else [],
            )
            # Skip marking complete if workflow HITL is pending — keep SSE alive
            if not rs.dcf_hitl_payload and not rs.deck_hitl_payload and not rs.memo_hitl_payload and not rs.workflow_context_payload:
                rs.status = "complete"
                update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "execution_started":
            rs.status = "executing"
            update_job(rs.thread_id, status=rs.status)

        if rs.loop.is_running():
            persisted = _persist_event(rs.thread_id, event)
            rs.loop.call_soon_threadsafe(rs.event_queue.put_nowait, persisted)
    return bridge


async def _run_agent_task(
    thread_id: str,
    query: str,
    mode: str,
    session_id: str = "",
    user_settings: dict[str, Any] | None = None,
) -> None:
    from file import app as agent_graph  # noqa: PLC0415
    from langchain_core.messages import HumanMessage  # noqa: PLC0415
    from lg_compat import Command  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs = _run_registry[thread_id]
    set_trace_context(rs.trace_id, rs.root_span_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": [TraceCallbackHandler(rs.trace_id, rs.root_span_id)],
    }
    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))
    session_memory = get_session_memory(session_id)
    _run_t = agent_log.run_start(thread_id, query, mode)

    try:
        await _await_stream_subscriber(rs)
        # Phase 1 — intent + (plan for research | chat for conversational)
        result = await agent_graph.ainvoke(
            {
                "messages": [HumanMessage(content=query)],
                "trace_id": rs.trace_id,
                "root_span_id": rs.root_span_id,
                "mode": mode,
                "resolved_intent": None,
                "session_id": session_id,
                "session_memory": session_memory,
                "user_settings": user_settings or {},
                "task_id": rs.collaboration_assignment_id,
                "task_context": rs.collaboration_task_context,
            },
            config=config,
        )

        interrupts = result.get("__interrupt__", ())

        if interrupts:
            first_interrupt = interrupts[0].value if hasattr(interrupts[0], "value") else {}
        else:
            first_interrupt = {}

        while isinstance(first_interrupt, dict) and first_interrupt.get("type") == "workflow_context_review":
            setup_payload = first_interrupt
            rs.status = "awaiting_workflow_context"
            update_job(thread_id, status=rs.status)
            rs.hitl_future = rs.loop.create_future()
            _send_event(rs, setup_payload)
            decision = await rs.hitl_future
            rs.workflow_context_payload = None
            if setup_payload.get("workflow_id") == "tracked_task" and decision.get("approved", True):
                workspace_id = rs.collaboration_workspace_id or "workspace:default"
                ensure_workspace(workspace_id)
                assignment = create_assignment(workspace_id, {
                    "title": query[:160], "description": query,
                    "assigned_to": "agent:research", "thread_id": thread_id,
                    "channel_id": rs.collaboration_channel_id,
                    "object_version_ids": rs.collaboration_object_version_ids,
                }, "human:local")
                rs.collaboration_workspace_id = workspace_id
                rs.collaboration_assignment_id = assignment["assignment_id"]
                rs.collaboration_actor_id = "agent:research"
                rs.collaboration_task_context = {
                    "task_id": assignment["assignment_id"], "title": assignment["title"],
                    "goal": query, "assigned_to": "agent:research",
                    "input_object_version_ids": rs.collaboration_object_version_ids,
                }
                update_assignment(assignment["assignment_id"], "working", "agent:research")
                decision["task_context"] = rs.collaboration_task_context
            if not decision.get("approved", True):
                decision = {"action": "cancel", "approved": False, "context": setup_payload.get("context", {})}
            rs.status = "chat_responding"
            update_job(thread_id, status=rs.status)
            result = await agent_graph.ainvoke(Command(resume=decision), config=config)
            interrupts = result.get("__interrupt__", ())
            first_interrupt = interrupts[0].value if interrupts and hasattr(interrupts[0], "value") else {}

        # Check for DCF HITL — payload set by event bridge when dcf_assumptions_review fires
        # (chat mode: tool emitted event, chat_node broke its ReAct loop).
        if rs.dcf_hitl_payload:
            dcf_data = rs.dcf_hitl_payload
            rs.dcf_hitl_payload = None  # consumed
            overrides = await _handle_dcf_hitl(rs, thread_id, dcf_data)
            if overrides is None:
                rs.event_queue.put_nowait(None)
                return

            # User approved — resume the interrupted DCF graph natively.
            # Do not inject a hidden [DCF_APPROVED] chat turn or run the
            # valuation-only graph; the interrupted graph already has the full
            # state, including net_debt, scenarios, evidence, and provenance.
            from tools import resume_dcf_workflow_after_hitl  # noqa: PLC0415
            from utils import list_artifact_paths  # noqa: PLC0415

            resume_payload = {"action": "approve"}
            if overrides:
                resume_payload = {"action": "edit", "assumptions": overrides}
            rs.status = "chat_responding"
            update_job(thread_id, status=rs.status)
            _payload, _pointer, report = await rs.loop.run_in_executor(
                None,
                lambda: resume_dcf_workflow_after_hitl(
                    resume_payload=resume_payload,
                    thread_id=thread_id,
                    args={
                        "ticker": dcf_data.get("ticker", "?"),
                        "horizon_years": dcf_data.get("horizon_years", 5),
                        "resume_payload": resume_payload,
                    },
                ),
            )
            complete_event: dict = {"type": "chat_complete", "content": report}
            artifact_paths = list_artifact_paths()
            if artifact_paths:
                complete_event["artifact_paths"] = artifact_paths
            _send_event(rs, complete_event)
            _sync_dcf_workspace_object(
                thread_id,
                session_id,
                result_path=_payload.get("result_path"),
            )
            _complete_collaboration_assignment(
                rs,
                report,
                artifact_paths=artifact_paths,
            )
            update_job(thread_id, status="complete")
            _send_event(rs, {"type": "run_complete"})
            rs.event_queue.put_nowait(None)
            return

        if rs.deck_hitl_payload:
            rs.deck_hitl_payload = None  # consumed
            deck_result = await _handle_deck_hitl(rs, thread_id)
            if deck_result is None:
                rs.status = "complete"
                update_job(thread_id, status=rs.status)
                _send_event(rs, {"type": "run_complete", "workflow": "deck", "status": "rejected"})
                rs.event_queue.put_nowait(None)
                return

            rs.status = "chat_responding"
            update_job(thread_id, status=rs.status)
            complete_payload = {
                "deck_title": deck_result.get("brief", {}).get("title"),
                "pptx_path": deck_result.get("pptx_path"),
                "deck_output_path": deck_result.get("deck_output_path")
                if "deck_output_path" in deck_result
                else None,
                "slide_count": len(deck_result.get("slides") or []),
                "result_version_id": deck_result.get("result_version_id"),
            }
            if not complete_payload.get("deck_output_path"):
                run_dir = _runs_dir_for(thread_id)
                candidate = run_dir / "decks" / "deck_output.json"
                if candidate.exists():
                    complete_payload["deck_output_path"] = str(candidate)

            complete_message = f"[DECK_COMPLETE]:{json.dumps(complete_payload, ensure_ascii=False)}"
            set_ui_event_handler(_make_event_bridge(rs))
            await agent_graph.ainvoke(
                {
                    "messages": [HumanMessage(content=complete_message)],
                    "mode": mode,
                    "resolved_intent": "chat",
                    "session_id": session_id,
                    "session_memory": session_memory,
                },
                config=config,
            )
            rel_pptx = None
            pptx_abs = complete_payload.get("pptx_path")
            if pptx_abs:
                from utils import relative_run_path  # noqa: PLC0415

                rel_pptx = relative_run_path(pptx_abs)
            if not rel_pptx:
                from utils import list_deck_artifact_paths  # noqa: PLC0415

                deck_paths = list_deck_artifact_paths(_runs_dir_for(thread_id))
                rel_pptx = deck_paths[0] if deck_paths else None
            _sync_deck_workspace_object(
                thread_id=thread_id,
                session_id=session_id,
                deck_title=complete_payload.get("deck_title"),
                pptx_path=rel_pptx or pptx_abs,
                slide_count=complete_payload.get("slide_count"),
                result_version_id=complete_payload.get("result_version_id"),
            )
            update_job(thread_id, status="complete")
            _send_event(
                rs,
                {
                    "type": "run_complete",
                    "workflow": "deck",
                    "pptx_path": rel_pptx or pptx_abs,
                    "artifact_paths": [rel_pptx] if rel_pptx else [],
                },
            )
            rs.event_queue.put_nowait(None)
            return

        if rs.memo_hitl_payload:
            rs.memo_hitl_payload = None
            memo_result = await _handle_memo_hitl(rs, thread_id)
            if memo_result is None:
                update_job(thread_id, status="complete")
                _send_event(rs, {"type": "run_complete", "workflow": "memo", "status": "rejected"})
                rs.event_queue.put_nowait(None)
                return
            _send_event(rs, {
                "type": "chat_complete",
                "content": memo_result.get("markdown") or "",
                "artifact_paths": [memo_result["markdown_path"]] if memo_result.get("markdown_path") else [],
            })
            _complete_collaboration_assignment(
                rs,
                memo_result.get("markdown") or "",
                artifact_paths=[memo_result["markdown_path"]] if memo_result.get("markdown_path") else [],
                output_version_ids=[memo_result["memo_version_id"]]
                if memo_result.get("memo_version_id")
                else [],
            )
            update_job(thread_id, status="complete")
            _send_event(rs, {
                "type": "run_complete",
                "workflow": "memo",
                "memo_id": memo_result.get("memo_id"),
                "memo_version_id": memo_result.get("memo_version_id"),
            })
            rs.event_queue.put_nowait(None)
            return

        # No interrupt → chat run or plan-less completion
        if not interrupts:
            _sync_dcf_workspace_object(thread_id, session_id)
            update_job(thread_id, status="complete")
            _send_event(rs, {"type": "run_complete"})
            rs.event_queue.put_nowait(None)
            return

        # Interrupt → research HITL flow
        plan = interrupts[0].value.get("plan", {})
        save_plan_to_store(thread_id, plan)
        rs.status = "awaiting_approval"
        update_job(thread_id, status=rs.status)
        rs.hitl_future = rs.loop.create_future()
        _send_event(rs, {"type": "plan_ready", "plan": plan})

        decision = await rs.hitl_future

        if not decision.get("approved"):
            rs.status = "rejected"
            update_job(thread_id, status=rs.status)
            _send_event(rs, {"type": "rejected"})
            rs.event_queue.put_nowait(None)
            return

        rs.status = "executing"
        update_job(thread_id, status=rs.status)
        _send_event(rs, {"type": "execution_started"})

        # Phase 2 — execute + synthesize (with DCF HITL support)
        # The graph may hit DCF review interrupts while executing steps.
        # Loop: invoke → check for interrupts → handle → resume → repeat.
        resume_value = {"action": "yes", "feedback": None}
        while True:
            result = await agent_graph.ainvoke(
                Command(resume=resume_value),
                config=config,
            )
            interrupts = result.get("__interrupt__", ())
            if not interrupts:
                break  # done — graph reached END

            # Check if this interrupt is a DCF assumption review
            value = interrupts[0].value if hasattr(interrupts[0], "value") else {}
            if isinstance(value, dict) and value.get("type") == "dcf_review":
                overrides = await _handle_dcf_hitl(rs, thread_id, value)
                if overrides is None:
                    rs.event_queue.put_nowait(None)
                    return
                resume_value = {"approved": True, "assumption_overrides": overrides}
                continue

            # Unknown interrupt — shouldn't happen in Phase 2, but surface it
            logger.warning("Unexpected interrupt in Phase 2: %s", value)
            break

        update_job(thread_id, status="complete")
        _sync_dcf_workspace_object(thread_id, session_id)
        _sync_report_workspace_object(thread_id, session_id, query)
        _send_event(rs, {"type": "run_complete"})
        agent_log.run_done(thread_id, _run_t, "done")

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Agent task failed:\n%s", tb)
        rs.status = "error"
        update_job(thread_id, status=rs.status, error=f"{type(exc).__name__}: {exc}")
        if rs.collaboration_assignment_id:
            update_assignment(
                rs.collaboration_assignment_id,
                "blocked",
                rs.collaboration_actor_id or "agent:research",
                thread_id=rs.thread_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        _send_event(rs, {"type": "error", "message": f"{type(exc).__name__}: {exc}\n\n{tb}"})
        agent_log.run_done(thread_id, _run_t, "error")
    finally:
        trace_ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=rs.trace_id,
            span_id=rs.root_span_id,
            category="run",
            name="agent_run",
            status="error" if rs.status == "error" else "completed",
            started_at=rs.trace_started_at,
            ended_at=trace_ended_at,
            duration_ms=(trace_ended_at - rs.trace_started_at) * 1000,
            metadata={"mode": mode, "session_id": session_id, "final_status": rs.status},
            error="agent run failed" if rs.status == "error" else None,
        ))
        rs.event_queue.put_nowait(None)  # sentinel — closes SSE stream
        _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)


# ---------------------------------------------------------------------------
# Amend a previous user message (LangGraph checkpoint fork)
# ---------------------------------------------------------------------------


async def _find_amend_checkpoint(
    agent_graph: Any,
    config: dict,
    original_content: str,
) -> tuple[dict | None, int]:
    """Find the checkpoint to fork from when amending a user message.

    Walks the thread's state history newest → oldest. Locates the most
    recent ``HumanMessage`` whose ``.content`` exactly matches
    ``original_content``, then finds a snapshot whose ``messages`` length
    is exactly the index of that message (i.e. the state right before
    it was added). Invoking from that checkpoint with a new
    ``HumanMessage`` recreates the conversation from that point forward.

    Returns ``(target_config, target_msg_index)`` or ``(None, -1)`` when
    no matching message is found.
    """
    target_index = -1
    snapshots: list[Any] = []
    async for snap in agent_graph.aget_state_history(config):
        snapshots.append(snap)
        msgs = (snap.values or {}).get("messages") or []
        if target_index == -1:
            # Latest snapshot — scan its messages for the most recent match.
            for i in range(len(msgs) - 1, -1, -1):
                msg = msgs[i]
                content = getattr(msg, "content", None)
                msg_type = getattr(msg, "type", None) or msg.__class__.__name__.lower()
                if (msg_type in {"human", "humanmessage"}
                    or msg.__class__.__name__ == "HumanMessage") \
                   and content == original_content:
                    target_index = i
                    break
            if target_index == -1:
                return None, -1
        # Among the collected snapshots, find one whose state has exactly
        # target_index messages — that is, the state *before* the target
        # message was appended.
        if len(msgs) == target_index:
            return snap.config, target_index
    return None, -1


async def _run_amended_agent_task(
    thread_id: str,
    original_content: str,
    new_content: str,
    mode: str,
    session_id: str = "",
) -> None:
    """Re-invoke the agent graph at a forked checkpoint with an amended user message."""
    from file import app as agent_graph  # noqa: PLC0415
    from langchain_core.messages import HumanMessage  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs = _run_registry[thread_id]
    set_trace_context(rs.trace_id, rs.root_span_id)
    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))
    session_memory = get_session_memory(session_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": [TraceCallbackHandler(rs.trace_id, rs.root_span_id)],
    }
    _run_t = agent_log.run_start(thread_id, new_content, mode)

    try:
        await _await_stream_subscriber(rs)
        target_config, target_index = await _find_amend_checkpoint(
            agent_graph, config, original_content,
        )
        if target_config is None:
            raise RuntimeError(
                f"Could not locate user message to amend in thread '{thread_id}'. "
                "The original content may no longer match (server restart?)."
            )

        # Tell frontend to drop messages from the rewind point and reset UI.
        _send_event(rs, {
            "type": "chat_amended",
            "message_index": target_index,
            "new_content": new_content,
        })

        result = await agent_graph.ainvoke(
            {
                "messages": [HumanMessage(content=new_content)],
                "trace_id": rs.trace_id,
                "root_span_id": rs.root_span_id,
                "mode": mode,
                "resolved_intent": None,
                "session_id": session_id,
                "session_memory": session_memory,
            },
            config={**target_config, "callbacks": config["callbacks"]},
        )

        # Mirror the simple completion path from _run_agent_task. The amended
        # turn is conversational by design — research/DCF HITL flows are not
        # supported here yet (would require replaying the full interrupt loop).
        interrupts = result.get("__interrupt__", ())
        if interrupts:
            logger.warning("Amend produced an unsupported interrupt; ignoring.")

        update_job(thread_id, status="complete")
        _send_event(rs, {"type": "run_complete"})
        agent_log.run_done(thread_id, _run_t, "done")

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Amend task failed:\n%s", tb)
        rs.status = "error"
        update_job(thread_id, status=rs.status, error=f"{type(exc).__name__}: {exc}")
        _send_event(rs, {"type": "error", "message": f"{type(exc).__name__}: {exc}\n\n{tb}"})
        agent_log.run_done(thread_id, _run_t, "error")
    finally:
        trace_ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=rs.trace_id,
            span_id=rs.root_span_id,
            category="run",
            name="agent_run",
            status="error" if rs.status == "error" else "completed",
            started_at=rs.trace_started_at,
            ended_at=trace_ended_at,
            duration_ms=(trace_ended_at - rs.trace_started_at) * 1000,
            metadata={"mode": mode, "session_id": session_id, "amended": True, "final_status": rs.status},
            error="amended agent run failed" if rs.status == "error" else None,
        ))
        rs.event_queue.put_nowait(None)
        if rs.status not in _RUNNING_STATUSES:
            _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)


async def _run_dcf_workflow_task(thread_id: str, request: "DCFRunRequest") -> None:
    from graphs.workflows.dcf import dcf_workflow_app  # noqa: PLC0415
    from lg_compat import Command  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs = _run_registry[thread_id]
    set_trace_context(rs.trace_id, rs.root_span_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": [TraceCallbackHandler(rs.trace_id, rs.root_span_id)],
    }
    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))

    initial_state = {
        "ticker": request.ticker,
        "horizon_years": request.horizon_years,
        "session_id": request.session_id or "",
        "assumption_review_mode": request.assumption_review_mode,
        "allow_external_assumptions": request.allow_external_assumptions,
        "assumption_overrides": request.assumption_overrides or {},
        "assumptions": {},
        "assumption_provenance": {},
        "assumptions_approved": False,
        "fundamentals": {},
        "assumption_conflicts": [],
        "profile": "default",
        "profile_meta": {},
        "assumption_flags": [],
        "valuation_flags": [],
        "confidence_label": "medium",
        "market_snapshot": {},
        "projected_fcff": [],
        "valuation": {},
        "sensitivity_table": [],
        "result_path": None,
        "parent_step_id": "workflow_dcf",
        "features": {},
        "wacc_components": {},
        "evidence_pack": {},
        "company_state": None,
        "assumption_memo": None,
        "confidence_breakdown": None,
        "wacc_sanity": None,
        "implied_growth": None,
        "implied_margin": None,
        "thesis": None,
        "analysis_iteration": 0,
        "critique": None,
        "previous_valuation": None,
        "scenarios": [],
        "scenario_results": [],
    }

    try:
        await _await_stream_subscriber(rs)
        rs.status = "workflow_running"
        update_job(thread_id, status=rs.status, intent="workflow_dcf")
        # Workflow-started activity is emitted from inside normalize_input_node
        # via the unified contract (kind="workflow", status="started").
        result = await dcf_workflow_app.ainvoke(initial_state, config=config)
        interrupts = result.get("__interrupt__", ())

        if interrupts:
            payload = interrupts[0].value if hasattr(interrupts[0], "value") else {}
            assumptions = payload.get("assumptions") if isinstance(payload, dict) else {}
            assumption_provenance = payload.get("assumption_provenance") if isinstance(payload, dict) else {}
            rs.status = "awaiting_assumptions"
            update_job(thread_id, status=rs.status)
            rs.hitl_future = rs.loop.create_future()
            _send_event(
                rs,
                {
                    "type": "assumptions_ready",
                    "workflow": "dcf",
                    "assumptions": assumptions or {},
                    "assumption_provenance": assumption_provenance or {},
                },
            )

            decision = await rs.hitl_future
            if not decision.get("approved", True):
                rs.status = "rejected"
                update_job(thread_id, status=rs.status)
                _send_event(rs, {"type": "assumptions_rejected", "workflow": "dcf"})
                rs.event_queue.put_nowait(None)
                return

            overrides = decision.get("assumptions_overrides") or {}
            resume_payload: dict = {"action": "approve"}
            if overrides:
                resume_payload = {"action": "edit", "assumptions": overrides}

            rs.status = "workflow_running"
            update_job(thread_id, status=rs.status)
            _send_event(
                rs,
                {
                    "type": "assumptions_submitted",
                    "workflow": "dcf",
                    "overrides_applied": bool(overrides),
                },
            )
            result = await dcf_workflow_app.ainvoke(Command(resume=resume_payload), config=config)

        result_path = result.get("result_path")
        _sync_dcf_workspace_object(thread_id, request.session_id or "")
        rs.status = "complete"
        update_job(thread_id, status=rs.status)
        _send_event(
            rs,
            {
                "type": "run_complete",
                "workflow": "dcf",
                "result_path": result_path,
            },
        )

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("DCF workflow task failed:\n%s", tb)
        rs.status = "error"
        update_job(thread_id, status=rs.status, error=f"{type(exc).__name__}: {exc}")
        _send_event(
            rs,
            {
                "type": "error",
                "workflow": "dcf",
                "message": f"{type(exc).__name__}: {exc}\n\n{tb}",
            },
        )
    finally:
        trace_ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=rs.trace_id,
            span_id=rs.root_span_id,
            category="run",
            name="dcf_workflow_run",
            status="error" if rs.status == "error" else "completed",
            started_at=rs.trace_started_at,
            ended_at=trace_ended_at,
            duration_ms=(trace_ended_at - rs.trace_started_at) * 1000,
            metadata={"ticker": request.ticker, "final_status": rs.status},
            error="DCF workflow run failed" if rs.status == "error" else None,
        ))
        rs.event_queue.put_nowait(None)
        if rs.status not in _RUNNING_STATUSES:
            _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _runs_dir_for(thread_id: str) -> Path:
    return RUNS_DIR / thread_id


def _artifacts_dir_for(thread_id: str) -> Path:
    return _runs_dir_for(thread_id) / "artifacts"


def _latest_plan_path(thread_id: str) -> Path | None:
    plans_dir = _runs_dir_for(thread_id) / "plans"
    if not plans_dir.exists():
        return None
    files = sorted(plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _report_path(thread_id: str) -> Path:
    return _runs_dir_for(thread_id) / "final_report.md"


def _workflow_result_path(thread_id: str, filename: str) -> Path:
    return _runs_dir_for(thread_id) / filename


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"${number:,.2f}"


def _sync_dcf_workspace_object(
    thread_id: str,
    session_id: str = "",
    *,
    result_path: str | Path | None = None,
) -> dict[str, Any] | None:
    path = Path(result_path) if result_path else _workflow_result_path(thread_id, "dcf_output.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read DCF workspace object payload thread=%s", thread_id, exc_info=True)
        return None

    persisted_version_id = payload.get("result_version_id")
    if persisted_version_id:
        current = get_workspace_object(f"dcf_run:{thread_id}")
        if current and current.get("version_id") == persisted_version_id:
            return current

    ticker = str(payload.get("ticker") or "DCF").upper()
    valuation = payload.get("valuation") if isinstance(payload.get("valuation"), dict) else {}
    implied = valuation.get("implied_share_price") if valuation else payload.get("implied_share_price")
    current = valuation.get("current_price") if valuation else payload.get("current_price")
    confidence = payload.get("confidence_label") or payload.get("confidence")
    summary_parts = [f"Implied {_money(implied)}"]
    if current is not None:
        summary_parts.append(f"spot {_money(current)}")
    if confidence:
        summary_parts.append(f"confidence {confidence}")

    return upsert_workspace_object({
        "object_id": f"dcf_run:{thread_id}",
        "object_type": "dcf_run",
        "schema_ref": "domain.cases.ValuationRunResult",
        "schema_version": "0.1",
        "title": f"{ticker} DCF run",
        "status": "complete",
        "session_id": session_id or payload.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": payload.get("kg_run_id") or thread_id,
        "created_by": "dcf_workflow",
        "updated_by": "dcf_workflow",
        "source_message_id": None,
        "source_object_ids": [],
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
        "source_refs": [],
        "kg_node_ids": [payload.get("kg_run_id")] if payload.get("kg_run_id") else [],
        "artifact_paths": [
            f"/runs/{thread_id}/dcf-report.md",
            f"/runs/{thread_id}/dcf-report.pdf?inline=1",
            f"/workflows/dcf/runs/{thread_id}/result",
        ],
        "summary": " · ".join(summary_parts),
        "search_text": f"{ticker} DCF valuation FCFF implied share price WACC terminal growth",
        "tags": [ticker.lower(), "dcf", "valuation"],
        "confidence": payload.get("effective_confidence") if isinstance(payload.get("effective_confidence"), (int, float)) else None,
        "quality": {
            "validation_status": payload.get("model_validity"),
            "warnings": payload.get("valuation_flags") or [],
            "blocking_gaps": [payload.get("invalidation_reason")] if payload.get("invalidation_reason") else [],
        },
        "payload": {
            "ticker": ticker,
            "kg_run_id": payload.get("kg_run_id"),
            "implied_share_price": implied,
            "current_price": current,
            "confidence_label": confidence,
            "model_validity": payload.get("model_validity"),
            "result_path": str(path),
        },
    })


def _object_type_from_query(query: str, content: str = "") -> str | None:
    q = (query or "").lower()
    c = (content or "").lower()
    if "deck" in q or "presentation" in q or "slides" in q:
        return "deck"
    if "memo" in q or "investment committee" in q or "ic memo" in q:
        return "memo"
    if "compare" in q or "comparison" in q or " vs " in q or "versus" in q:
        return "comparison"
    if "[doc:" in content or "document" in q or "pdf" in q or "uploaded" in q or "citations" in c:
        return "document_analysis"
    return None


def _sync_text_workspace_object(
    *,
    thread_id: str,
    session_id: str,
    query: str,
    content: str,
    artifact_paths: list[str] | None = None,
) -> dict[str, Any] | None:
    if _workflow_result_path(thread_id, "dcf_output.json").exists():
        return None
    object_type = _object_type_from_query(query, content)
    if not object_type or object_type == "deck":
        return None
    title_seed = " ".join(query.strip().split())[:80] or object_type.replace("_", " ").title()
    summary = " ".join(content.strip().split())[:220] if content else None
    return upsert_workspace_object({
        "object_id": f"{object_type}:{thread_id}",
        "object_type": object_type,
        "schema_ref": f"workspace.{object_type}",
        "schema_version": "0.1",
        "title": title_seed,
        "status": "complete",
        "session_id": session_id,
        "thread_id": thread_id,
        "created_by": "chat_workflow",
        "updated_by": "chat_workflow",
        "source_message_id": None,
        "source_object_ids": [],
        "entity_refs": [],
        "source_refs": [],
        "kg_node_ids": [],
        "artifact_paths": artifact_paths or [],
        "summary": summary,
        "search_text": f"{query} {summary or ''}".strip(),
        "tags": [object_type],
        "quality": {},
        "payload": {
            "query": query,
            "content": content,
        },
    })


def _sync_report_workspace_object(thread_id: str, session_id: str, query: str) -> dict[str, Any] | None:
    current = get_workspace_object(f"research_report:{thread_id}")
    if current:
        return current
    path = _report_path(thread_id)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _sync_text_workspace_object(
        thread_id=thread_id,
        session_id=session_id,
        query=query,
        content=content,
        artifact_paths=[f"/runs/{thread_id}/report"],
    )


def _sync_deck_workspace_object(
    *,
    thread_id: str,
    session_id: str,
    deck_title: str | None,
    pptx_path: str | None,
    slide_count: int | None = None,
    result_version_id: str | None = None,
) -> dict[str, Any] | None:
    if result_version_id:
        current = get_workspace_object(f"deck:{thread_id}")
        if current and current.get("version_id") == result_version_id:
            return current
    title = deck_title or "Presentation deck"
    artifacts: list[str] = []
    if pptx_path:
        filename = Path(str(pptx_path)).name
        artifacts.append(f"/runs/{thread_id}/decks/{filename}")
    return upsert_workspace_object({
        "object_id": f"deck:{thread_id}",
        "object_type": "deck",
        "schema_ref": "workspace.deck",
        "schema_version": "0.1",
        "title": title,
        "status": "complete",
        "session_id": session_id,
        "thread_id": thread_id,
        "run_id": thread_id,
        "created_by": "deck_workflow",
        "updated_by": "deck_workflow",
        "source_message_id": None,
        "source_object_ids": [],
        "entity_refs": [],
        "source_refs": [],
        "kg_node_ids": [],
        "artifact_paths": artifacts,
        "summary": f"{slide_count or 0} slides" if slide_count is not None else None,
        "search_text": f"{title} deck presentation slides",
        "tags": ["deck", "presentation"],
        "quality": {},
        "payload": {
            "deck_title": deck_title,
            "pptx_path": pptx_path,
            "slide_count": slide_count,
        },
    })


def _sync_uploaded_document_workspace_object(info: dict[str, Any]) -> dict[str, Any] | None:
    doc_id = str(info.get("doc_id") or "")
    if not doc_id:
        return None

    filename = str(info.get("filename") or "Uploaded document")
    status = str(info.get("status") or "processing")
    stage = info.get("stage")
    page_count = int(info.get("page_count") or 0)
    chunk_count = int(info.get("chunk_count") or 0)
    company = info.get("company")
    ticker = info.get("ticker")
    fiscal_period = info.get("fiscal_period")

    summary_parts: list[str] = []
    if company:
        summary_parts.append(str(company))
    if ticker:
        summary_parts.append(str(ticker).upper())
    if fiscal_period:
        summary_parts.append(str(fiscal_period))
    if page_count:
        summary_parts.append(f"{page_count} pg")
    elif stage:
        summary_parts.append(str(stage))

    return upsert_workspace_object({
        "object_id": f"uploaded_document:{doc_id}",
        "object_type": "uploaded_document",
        "schema_ref": "workspace.uploaded_document",
        "schema_version": "0.1",
        "title": filename,
        "status": status,
        "session_id": info.get("session_id") or "",
        "thread_id": None,
        "created_by": "document_ingest",
        "updated_by": "document_ingest",
        "source_message_id": None,
        "source_object_ids": [],
        "entity_refs": [
            {"kind": "company", "name": str(company), "ticker": str(ticker).upper() if ticker else None}
        ] if company or ticker else [],
        "source_refs": [{
            "source_id": f"doc:{doc_id}",
            "source_type": "document",
            "title": filename,
            "object_id": f"uploaded_document:{doc_id}",
            "period": fiscal_period,
            "license_status": "unknown",
        }],
        "kg_node_ids": [f"doc:{doc_id}"],
        "artifact_paths": [f"/documents/{doc_id}/file"],
        "summary": " · ".join(summary_parts) if summary_parts else (f"{chunk_count} chunks" if chunk_count else None),
        "search_text": " ".join(str(part) for part in [filename, company, ticker, fiscal_period, stage] if part),
        "tags": [tag for tag in ["uploaded_document", str(ticker).lower() if ticker else ""] if tag],
        "quality": {"ingest_stage": stage, "chunk_count": chunk_count, "page_count": page_count},
        "payload": {
            "doc_id": doc_id,
            "filename": filename,
            "status": status,
            "stage": stage,
            "chunk_count": chunk_count,
            "page_count": page_count,
            "company": company,
            "ticker": ticker,
            "doc_type": info.get("doc_type"),
            "fiscal_period": fiscal_period,
        },
    })


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str


class RunRequest(BaseModel):
    query: str
    mode: str = "auto"
    thread_id: str | None = None       # optional: reuse thread for multi-turn chat
    session_id: str | None = None      # used to scope RAG document search
    user_settings: dict[str, Any] = Field(default_factory=dict)


class RunCreatedResponse(BaseModel):
    thread_id: str
    # Highest event_id at the moment this run was created. Clients should pass
    # this back as ?after_id= when opening the SSE stream so prior turns in
    # the same chat thread don't get replayed as live events.
    start_event_id: int = 0
    trace_id: str | None = None
    root_span_id: str | None = None


class DecisionRequest(BaseModel):
    approved: bool


class AmendRequest(BaseModel):
    """Amend a previously-sent user message in a chat thread.

    The backend locates the most recent ``HumanMessage`` matching
    ``original_content`` in the LangGraph thread state, rewinds the
    checkpoint to just before that message, and re-invokes the graph
    with ``new_content``. Old messages after the rewind point are
    discarded by the frontend.
    """
    original_content: str
    new_content: str
    mode: str = "auto"
    session_id: str | None = None


class DCFRunRequest(BaseModel):
    ticker: str
    horizon_years: int = 5
    assumption_review_mode: bool = False
    allow_external_assumptions: bool = True
    assumption_overrides: dict[str, float] | None = None
    thread_id: str | None = None
    session_id: str | None = None


class AssumptionsDecisionRequest(BaseModel):
    approved: bool = True
    assumptions_overrides: dict[str, float] | None = None


class PlanStep(BaseModel):
    id: str
    description: str
    depends_on: list[str] = []
    status: str
    result: str | None = None
    tool_result_ids: list[str] = []


class PlanResponse(BaseModel):
    plan_id: str
    query: str
    status: str
    created_at: str
    steps: list[PlanStep]


class JobSummary(BaseModel):
    thread_id: str
    query: str
    status: str
    mode: str
    intent: str | None
    created_at: str


class WorkspaceObject(BaseModel):
    object_id: str
    object_type: str
    schema_ref: str | None = None
    schema_version: str = "0.1"
    title: str
    status: str
    session_id: str | None = None
    thread_id: str | None = None
    case_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    source_message_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    source_object_ids: list[str] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    kg_node_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    search_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    visibility: str = "session"
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    version_id: str | None = None
    version_number: int = 0


class WorkspaceObjectAction(BaseModel):
    action_id: int
    object_id: str
    action_type: str
    actor_id: str
    previous_version_id: str | None = None
    resulting_version_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class WorkspaceObjectCreate(BaseModel):
    object_id: str | None = None
    object_type: str
    schema_ref: str | None = None
    schema_version: str = "0.1"
    title: str
    status: str = "complete"
    session_id: str | None = None
    thread_id: str | None = None
    case_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    source_message_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    source_object_ids: list[str] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    kg_node_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    search_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    visibility: str = "session"
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agent Backend",
    description="FastAPI server wrapping the LangGraph research agent.",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def resume_interrupted_research_jobs() -> None:
    loop = asyncio.get_running_loop()
    for job in list_stored_jobs():
        if not _should_auto_resume(job):
            continue
        thread_id = job["thread_id"]
        if thread_id in _run_registry:
            continue
        rs = RunState(
            thread_id,
            loop,
            job["query"],
            job["mode"],
            job.get("session_id") or "",
        )
        rs.intent = job.get("intent") or "research"
        rs.status = "executing"
        _run_registry[thread_id] = rs
        asyncio.create_task(_resume_research_task(thread_id, rs.session_id))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _latest_event_id_for(thread_id: str) -> int:
    """Highest persisted event_id for this thread (0 if none).

    Used as the per-turn boundary marker: clients that reuse a thread_id
    across multiple turns (chat continuity) pass this back as ?after_id=
    when opening SSE so prior turns aren't replayed as live events.
    """
    prior = list_job_events(thread_id, after_id=0, limit=1_000_000)
    return max((int(e.get("event_id") or 0) for e in prior), default=0)


@app.post("/runs", response_model=RunCreatedResponse)
async def create_run(body: RunRequest) -> RunCreatedResponse:
    thread_id = body.thread_id or f"thread_{uuid4().hex[:8]}"
    session_id = body.session_id or ""

    # Refuse if the same thread is already running
    existing = _run_registry.get(thread_id)
    if existing and existing.status in _RUNNING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is already running (status={existing.status}).",
        )

    start_event_id = _latest_event_id_for(thread_id)

    loop = asyncio.get_running_loop()
    rs = RunState(thread_id, loop, body.query, body.mode, session_id)
    rs.stream_expected = True
    _run_registry[thread_id] = rs
    upsert_job(
        thread_id=thread_id,
        query=body.query,
        mode=body.mode,
        status=rs.status,
        session_id=session_id,
    )
    _send_event(rs, make_trace_span(
        trace_id=rs.trace_id,
        span_id=rs.root_span_id,
        category="run",
        name="agent_run",
        status="started",
        started_at=rs.trace_started_at,
        metadata={"mode": body.mode, "session_id": session_id},
    ))
    asyncio.create_task(_run_agent_task(
        thread_id,
        body.query,
        body.mode,
        session_id,
        body.user_settings,
    ))
    return RunCreatedResponse(
        thread_id=thread_id,
        start_event_id=start_event_id,
        trace_id=rs.trace_id,
        root_span_id=rs.root_span_id,
    )


@app.post("/runs/{thread_id}/amend", response_model=RunCreatedResponse)
async def amend_message(thread_id: str, body: AmendRequest) -> RunCreatedResponse:
    """Amend a previously-sent user message, re-running the chat from that point.

    Refuses to run when the thread is currently active. Returns the same
    ``thread_id`` and a fresh ``start_event_id`` so the frontend can re-open
    its SSE stream from that point.
    """
    existing = _run_registry.get(thread_id)
    if existing and existing.status in _RUNNING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is currently running (status={existing.status}).",
        )

    if not body.original_content or not body.new_content:
        raise HTTPException(status_code=400, detail="original_content and new_content are required")

    session_id = body.session_id or (existing.session_id if existing else "")
    start_event_id = _latest_event_id_for(thread_id)

    loop = asyncio.get_running_loop()
    rs = RunState(thread_id, loop, body.new_content, body.mode, session_id)
    rs.stream_expected = True
    _run_registry[thread_id] = rs
    upsert_job(
        thread_id=thread_id,
        query=body.new_content,
        mode=body.mode,
        status=rs.status,
        session_id=session_id,
    )
    _send_event(rs, make_trace_span(
        trace_id=rs.trace_id,
        span_id=rs.root_span_id,
        category="run",
        name="agent_run",
        status="started",
        started_at=rs.trace_started_at,
        metadata={"mode": body.mode, "session_id": session_id, "amended": True},
    ))
    asyncio.create_task(
        _run_amended_agent_task(
            thread_id, body.original_content, body.new_content,
            body.mode, session_id,
        )
    )
    return RunCreatedResponse(
        thread_id=thread_id,
        start_event_id=start_event_id,
        trace_id=rs.trace_id,
        root_span_id=rs.root_span_id,
    )


@app.post("/workflows/dcf/runs", response_model=RunCreatedResponse)
async def create_dcf_run(body: DCFRunRequest) -> RunCreatedResponse:
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    thread_id = body.thread_id or f"workflow_dcf_{uuid4().hex[:8]}"
    session_id = body.session_id or ""
    existing = _run_registry.get(thread_id)
    if existing and existing.status in _RUNNING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is already running (status={existing.status}).",
        )

    loop = asyncio.get_running_loop()
    rs = RunState(thread_id, loop, f"DCF valuation for {ticker}", "workflow_dcf", session_id)
    rs.stream_expected = True
    rs.status = "workflow_running"
    rs.intent = "workflow_dcf"
    _run_registry[thread_id] = rs
    upsert_job(
        thread_id=thread_id,
        query=rs.query,
        mode=rs.mode,
        status=rs.status,
        session_id=session_id,
        intent=rs.intent,
    )
    start_event_id = _latest_event_id_for(thread_id)
    _send_event(rs, make_trace_span(
        trace_id=rs.trace_id,
        span_id=rs.root_span_id,
        category="run",
        name="dcf_workflow_run",
        status="started",
        started_at=rs.trace_started_at,
        metadata={"ticker": ticker, "session_id": session_id},
    ))
    asyncio.create_task(_run_dcf_workflow_task(thread_id, body))
    return RunCreatedResponse(
        thread_id=thread_id,
        start_event_id=start_event_id,
        trace_id=rs.trace_id,
        root_span_id=rs.root_span_id,
    )


@app.get("/runs/{thread_id}/events")
async def stream_events(
    thread_id: str,
    after_id: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    rs = _run_registry.get(thread_id)
    stored_job = get_job(thread_id)
    if rs is None and stored_job is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")

    replay_after = after_id
    if replay_after is None and last_event_id:
        try:
            replay_after = int(last_event_id)
        except ValueError:
            replay_after = None

    async def generate():
        last_seen = replay_after or 0
        last_ping = time.time()
        stream_started_at = time.time()
        transport_span_id = f"transport_{uuid4().hex[:12]}"
        trace_id = rs.trace_id if rs is not None else thread_id
        root_span_id = rs.root_span_id if rs is not None else None
        replay_events_sent = 0
        queue_events_sent = 0
        db_poll_events_sent = 0
        db_poll_rounds = 0

        try:
            # Open transport before releasing graph execution. Padding defeats
            # proxy/browser minimum-buffer thresholds without visible events.
            rs_live = _run_registry.get(thread_id)
            if rs_live is not None:
                rs_live.stream_connected.set()
            yield ":" + (" " * 2048) + "\n\n"
            _persist_event(thread_id, make_trace_span(
                trace_id=trace_id,
                span_id=transport_span_id,
                parent_span_id=root_span_id,
                category="transport",
                name="sse_connection",
                status="started",
                started_at=stream_started_at,
            ))
            # Initial durable replay so reconnects never miss persisted events.
            events = list_job_events(thread_id, last_seen)
            if events:
                for event in events:
                    last_seen = int(event.get("event_id") or last_seen)
                    yield _format_sse_event(event)
                    replay_events_sent += 1

            while True:
                rs_live = _run_registry.get(thread_id)
                if rs_live is not None:
                    try:
                        event = await asyncio.wait_for(
                            rs_live.event_queue.get(),
                            timeout=POLL_INTERVAL_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        event = None
                    except asyncio.CancelledError:
                        return

                    if isinstance(event, dict):
                        event_id = int(event.get("event_id") or 0)
                        # Skip duplicates that may have been replayed from SQLite.
                        if event_id > last_seen:
                            last_seen = event_id
                            yield _format_sse_event(event)
                            queue_events_sent += 1
                        continue

                try:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    return

                db_poll_rounds += 1
                events = list_job_events(thread_id, last_seen)
                if events:
                    for event in events:
                        last_seen = int(event.get("event_id") or last_seen)
                        yield _format_sse_event(event)
                        db_poll_events_sent += 1
                    continue

                job = get_job(thread_id)
                active = thread_id in _run_registry
                if not active and (job is None or job.get("status") not in _RUNNING_STATUSES):
                    yield 'data: {"type":"done"}\n\n'
                    return

                if time.time() - last_ping >= 25:
                    last_ping = time.time()
                    yield 'data: {"type":"ping"}\n\n'
        finally:
            stream_ended_at = time.time()
            elapsed_ms = int((stream_ended_at - stream_started_at) * 1000)
            _persist_event(thread_id, make_trace_span(
                trace_id=trace_id,
                span_id=transport_span_id,
                parent_span_id=root_span_id,
                category="transport",
                name="sse_connection",
                status="completed",
                started_at=stream_started_at,
                ended_at=stream_ended_at,
                duration_ms=elapsed_ms,
                metadata={
                    "replay_events": replay_events_sent,
                    "queue_events": queue_events_sent,
                    "db_poll_events": db_poll_events_sent,
                    "db_poll_rounds": db_poll_rounds,
                },
            ))
            logger.info(
                "SSE stream closed thread_id=%s elapsed_ms=%d replay_events=%d queue_events=%d db_poll_events=%d db_poll_rounds=%d",
                thread_id,
                elapsed_ms,
                replay_events_sent,
                queue_events_sent,
                db_poll_events_sent,
                db_poll_rounds,
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/runs/{thread_id}/trace")
async def get_run_trace(
    thread_id: str,
    trace_id: str | None = Query(default=None),
) -> dict[str, Any]:
    if get_job(thread_id) is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    span_events = [
        event for event in list_job_events(thread_id, 0, limit=10_000)
        if event.get("type") == "trace_span"
    ]
    selected_trace_id = trace_id
    if selected_trace_id is None and span_events:
        selected_trace_id = str(span_events[-1].get("trace_id") or "") or None
    if selected_trace_id:
        span_events = [event for event in span_events if event.get("trace_id") == selected_trace_id]

    merged: dict[str, dict[str, Any]] = {}
    for event in span_events:
        span_id = str(event.get("span_id") or "")
        if not span_id:
            continue
        prior = merged.get(span_id, {})
        merged[span_id] = {
            **prior,
            **event,
            "started_at": prior.get("started_at", event.get("started_at")),
            "metadata": {**(prior.get("metadata") or {}), **(event.get("metadata") or {})},
        }
    spans = sorted(merged.values(), key=lambda item: float(item.get("started_at") or 0))
    return {
        "thread_id": thread_id,
        "trace_id": selected_trace_id,
        "spans": spans,
    }


@app.post("/runs/{thread_id}/decision")
async def submit_decision(thread_id: str, body: DecisionRequest) -> dict:
    rs = _run_registry.get(thread_id)
    if rs is None:
        job = get_job(thread_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        if job.get("status") != "awaiting_approval":
            raise HTTPException(status_code=409, detail=f"Thread '{thread_id}' is not awaiting approval.")
        if not body.approved:
            update_job(thread_id, status="rejected")
            append_job_event(thread_id, {"type": "rejected"})
            return {"ok": True}

        loop = asyncio.get_running_loop()
        rs = RunState(thread_id, loop, job["query"], job["mode"], job.get("session_id") or "")
        rs.intent = job.get("intent") or "research"
        rs.status = "executing"
        _run_registry[thread_id] = rs
        asyncio.create_task(_resume_research_task(thread_id, rs.session_id))
        return {"ok": True}
    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result({"approved": body.approved})
    return {"ok": True}


@app.post("/workflows/dcf/runs/{thread_id}/assumptions-decision")
async def submit_dcf_assumptions_decision(
    thread_id: str,
    body: AssumptionsDecisionRequest,
) -> dict:
    rs = _run_registry.get(thread_id)
    if rs is None:
        job = get_job(thread_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        if job.get("status") != "awaiting_assumptions":
            raise HTTPException(
                status_code=409,
                detail=f"Thread '{thread_id}' is not awaiting assumptions review.",
            )
        # The worker process is expected to be alive while awaiting assumptions.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Thread '{thread_id}' is awaiting assumptions but has no active worker. "
                "Restart the workflow run."
            ),
        )

    if rs.status != "awaiting_assumptions":
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is not awaiting assumptions review.",
        )

    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result(
            {
                "approved": body.approved,
                "assumptions_overrides": body.assumptions_overrides or {},
            }
        )
    return {"ok": True}


class DcfDecisionRequest(BaseModel):
    approved: bool = True
    assumptions_overrides: dict[str, float] | None = None


class DeckDecisionRequest(BaseModel):
    approved: bool = True
    action: str = "approve"
    outline: dict | None = None
    feedback: str | None = None


class MemoDecisionRequest(BaseModel):
    approved: bool = True
    action: str = "approve"
    draft: dict[str, Any] | None = None
    feedback: str | None = None


class WorkflowContextDecisionRequest(BaseModel):
    approved: bool = True
    action: str = "approve"
    context: dict[str, Any] | None = None


class DcfContinueRequest(BaseModel):
    action: str = "approve"
    assumptions: dict[str, float] | None = None


@app.post("/runs/{thread_id}/dcf-decision")
async def submit_dcf_decision(
    thread_id: str,
    body: DcfDecisionRequest,
) -> dict:
    """Submit user decision on DCF assumptions review (approve/edit)."""
    rs = _run_registry.get(thread_id)
    if rs is None or rs.status != "awaiting_assumptions":
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is not awaiting assumptions review.",
        )

    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result(
            {
                "approved": body.approved,
                "assumptions_overrides": body.assumptions_overrides or {},
            }
        )
    return {"ok": True}


@app.post("/runs/{thread_id}/workflow-context-decision")
async def submit_workflow_context_decision(
    thread_id: str,
    body: WorkflowContextDecisionRequest,
) -> dict:
    """Submit user decision on workflow setup review."""
    rs = _run_registry.get(thread_id)
    if rs is None or rs.status != "awaiting_workflow_context":
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is not awaiting workflow setup review.",
        )

    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result(
            {
                "approved": body.approved,
                "action": "cancel" if not body.approved else body.action,
                "context": body.context or {},
            }
        )
    return {"ok": True}


@app.post("/runs/{thread_id}/deck-decision")
async def submit_deck_decision(
    thread_id: str,
    body: DeckDecisionRequest,
) -> dict:
    """Submit user decision on deck outline review (approve/edit/reject)."""
    rs = _run_registry.get(thread_id)
    if rs is None or rs.status != "awaiting_outline_review":
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is not awaiting deck outline review.",
        )

    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result(
            {
                "approved": body.approved,
                "action": "reject" if not body.approved else body.action,
                "outline": body.outline,
                "feedback": body.feedback,
            }
        )
    return {"ok": True}


@app.post("/runs/{thread_id}/memo-decision")
async def submit_memo_decision(thread_id: str, body: MemoDecisionRequest) -> dict:
    """Submit user decision on memo draft review."""
    rs = _run_registry.get(thread_id)
    if rs is None or rs.status != "awaiting_memo_review":
        raise HTTPException(
            status_code=409,
            detail=f"Thread '{thread_id}' is not awaiting memo review.",
        )
    if rs.hitl_future and not rs.hitl_future.done():
        rs.hitl_future.set_result({
            "approved": body.approved,
            "action": "reject" if not body.approved else body.action,
            "draft": body.draft,
            "feedback": body.feedback,
        })
    return {"ok": True}


@app.post("/runs/{thread_id}/dcf-continue")
async def continue_dcf_after_review(thread_id: str, body: DcfContinueRequest) -> dict:
    """Resume DCF graph from assumption review interrupt."""
    from graphs.workflows.dcf import dcf_workflow_app  # noqa: PLC0415
    from lg_compat import Command  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    # Create or reuse a run state for SSE streaming of valuation events
    loop = asyncio.get_running_loop()
    rs = _run_registry.get(thread_id)
    if rs is None:
        job = get_job(thread_id)
        query = job["query"] if job else "DCF valuation"
        rs = RunState(thread_id, loop, query, "chat", job.get("session_id") or "" if job else "")
        rs.status = "workflow_running"
        _run_registry[thread_id] = rs

    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))
    config = {"configurable": {"thread_id": thread_id}}

    if body.action == "edit" and body.assumptions:
        resume_cmd = Command(resume={"action": "edit", "assumptions": body.assumptions})
    else:
        resume_cmd = Command(resume={"action": "approve"})

    rs.status = "workflow_running"
    try:
        await loop.run_in_executor(None, lambda: dcf_workflow_app.invoke(resume_cmd, config=config))
        rs.status = "complete"
        update_job(thread_id, status="complete")
        _send_event(rs, {"type": "run_complete"})
    except Exception as exc:  # noqa: BLE001
        import traceback
        logger.error("DCF resume failed:\n%s", traceback.format_exc())
        rs.status = "error"
        _send_event(rs, {"type": "error", "message": str(exc)})
    finally:
        rs.event_queue.put_nowait(None)
        _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)
    return {"ok": True}


@app.get("/runs/{thread_id}/plan", response_model=PlanResponse)
def get_plan(thread_id: str) -> PlanResponse:
    plan_path = _latest_plan_path(thread_id)
    if plan_path is None:
        raise HTTPException(status_code=404, detail=f"No plan found for thread '{thread_id}'")
    try:
        data = json.loads(plan_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read plan: {exc}") from exc
    return PlanResponse(**data)


@app.get("/runs/{thread_id}/report")
def get_report(thread_id: str) -> dict:
    path = _report_path(thread_id)
    if not path.exists():
        stored = get_stored_report(thread_id)
        if stored:
            return {"thread_id": thread_id, "content": stored["content"]}
        raise HTTPException(status_code=404, detail=f"No report found for thread '{thread_id}'")
    return {"thread_id": thread_id, "content": path.read_text(encoding="utf-8")}


@app.get("/runs/{thread_id}/dcf-report.md")
def get_dcf_report_markdown(thread_id: str) -> Response:
    """Download the DCF valuation report as markdown."""
    from report_export import load_dcf_report_markdown  # noqa: PLC0415

    run_dir = _runs_dir_for(thread_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"No run found for thread '{thread_id}'")
    try:
        markdown, base_name, _ = load_dcf_report_markdown(run_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}-report.md"',
        },
    )


@app.get("/runs/{thread_id}/dcf-report.pdf")
def get_dcf_report_pdf(
    thread_id: str,
    inline: bool = Query(default=False),
) -> Response:
    """Render the DCF valuation report as a formatted PDF.

    ``inline=true`` serves it with ``Content-Disposition: inline`` so the
    browser renders it in a new tab (used by the "Open report" action on the
    DCF node); the default ``attachment`` keeps the download behaviour for the
    report card's download button.
    """
    from report_export import load_dcf_report_markdown, render_report_pdf  # noqa: PLC0415

    run_dir = _runs_dir_for(thread_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"No run found for thread '{thread_id}'")
    try:
        markdown, base_name, png_path = load_dcf_report_markdown(run_dir)
        pdf_bytes = render_report_pdf(markdown, sensitivity_png=png_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    disposition = "inline" if inline else "attachment"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{base_name}-report.pdf"',
        },
    )


@app.get("/workflows/dcf/runs/{thread_id}/result")
def get_dcf_result(thread_id: str) -> dict:
    result_path = _workflow_result_path(thread_id, "dcf_output.json")
    if not result_path.exists():
        raise HTTPException(status_code=404, detail=f"No DCF workflow result found for thread '{thread_id}'")
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt DCF result payload: {exc}") from exc


@app.get("/runs/{thread_id}/decks/{filename}")
def get_deck_pptx(thread_id: str, filename: str) -> FileResponse:
    """Download a generated deck PPTX from ``runs/<thread>/decks/``."""
    from utils import resolve_deck_pptx_path  # noqa: PLC0415

    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid deck filename.")
    deck_path = resolve_deck_pptx_path(thread_id, safe_name)
    if deck_path is None or not deck_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Deck '{safe_name}' not found for thread '{thread_id}'",
        )
    return FileResponse(
        deck_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/runs/{thread_id}/deck-output")
def get_deck_output(thread_id: str) -> dict:
    """Return ``deck_output.json`` for in-app slide preview."""
    from utils import resolve_deck_output_path  # noqa: PLC0415

    output_path = resolve_deck_output_path(thread_id)
    if output_path is None or not output_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No deck output found for thread '{thread_id}'",
        )
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read deck output: {exc}") from exc
    pptx_path = payload.get("pptx_path")
    rel_pptx = None
    if pptx_path:
        try:
            rel_pptx = str(Path(str(pptx_path)).resolve().relative_to(_runs_dir_for(thread_id).resolve()))
        except ValueError:
            rel_pptx = f"decks/{Path(str(pptx_path)).name}"
    payload["pptx_relpath"] = rel_pptx
    payload["pptx_filename"] = Path(str(pptx_path)).name if pptx_path else None
    return payload


@app.get("/artifacts/{thread_id}/{filename}")
def get_artifact(thread_id: str, filename: str) -> FileResponse:
    artifact_path = _artifacts_dir_for(thread_id) / filename
    if not artifact_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{filename}' not found for thread '{thread_id}'",
        )
    return FileResponse(artifact_path)


@app.get("/sources/fmp/{ticker}")
def get_fmp_source_data(ticker: str, field: str | None = Query(default=None)) -> dict:
    """Return raw FMP source data using the server-side API key.

    Report links must not include API keys, so DCF references point here for
    FMP-backed assumptions. The response intentionally exposes the upstream
    endpoint names, but never the configured API key.
    """
    symbol = ticker.strip().upper()
    if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid ticker")

    api_key = os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="FMP_API_KEY is not configured on the server")

    endpoints = {
        "profile": f"profile?symbol={symbol}",
        "income_statement": f"income-statement?symbol={symbol}&period=annual&limit=5",
        "balance_sheet": f"balance-sheet-statement?symbol={symbol}&period=annual&limit=5",
        "cash_flow": f"cash-flow-statement?symbol={symbol}&period=annual&limit=5",
    }
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, path in endpoints.items():
        url = f"https://financialmodelingprep.com/stable/{path}"
        try:
            response = requests.get(url, params={"apikey": api_key}, timeout=12)
            response.raise_for_status()
            data[name] = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("FMP source proxy failed ticker=%s endpoint=%s error=%s", symbol, name, exc)
            errors[name] = str(exc)

    if not data:
        raise HTTPException(status_code=502, detail={"message": "FMP source fetch failed", "errors": errors})

    return {
        "provider": "financialmodelingprep",
        "ticker": symbol,
        "field": field,
        "endpoints": list(data.keys()),
        "data": data,
        "errors": errors,
    }


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs() -> list[JobSummary]:
    """Return all runs as job summaries, newest first."""
    persisted = {job["thread_id"]: job for job in list_stored_jobs()}
    for rs in _run_registry.values():
        persisted[rs.thread_id] = {
            "thread_id": rs.thread_id,
            "query": rs.query,
            "status": rs.status,
            "mode": rs.mode,
            "intent": rs.intent,
            "created_at": rs.created_at,
        }
    jobs = [JobSummary(**job) for job in persisted.values()]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


@app.get("/threads/{thread_id}/routes", response_model=list[dict[str, Any]])
def list_thread_routes(
    thread_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Return durable route decisions in chronological turn order."""
    return list_route_records(thread_id=thread_id, limit=limit)


@app.get("/workspace/objects", response_model=list[WorkspaceObject])
def list_workspace_objects_endpoint(
    session_id: str | None = Query(default=None),
    object_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[WorkspaceObject]:
    return [
        WorkspaceObject(**obj)
        for obj in list_workspace_objects(
            session_id=session_id,
            object_type=object_type,
            limit=limit,
        )
    ]


@app.get("/workspace/objects/{object_id}", response_model=WorkspaceObject)
def get_workspace_object_endpoint(object_id: str) -> WorkspaceObject:
    obj = get_workspace_object(object_id)
    if not obj:
        raise HTTPException(status_code=404, detail=f"Workspace object '{object_id}' not found")
    return WorkspaceObject(**obj)


@app.get("/workspace/objects/{object_id}/versions", response_model=list[WorkspaceObject])
def list_workspace_object_versions_endpoint(object_id: str) -> list[WorkspaceObject]:
    versions = list_workspace_object_versions(object_id)
    if not versions and not get_workspace_object(object_id):
        raise HTTPException(status_code=404, detail=f"Workspace object '{object_id}' not found")
    return [WorkspaceObject(**version) for version in versions]


@app.get("/workspace/objects/{object_id}/versions/{version_number}", response_model=WorkspaceObject)
def get_workspace_object_version_endpoint(object_id: str, version_number: int) -> WorkspaceObject:
    version = get_workspace_object_version(object_id, version_number)
    if not version:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace object '{object_id}' version {version_number} not found",
        )
    return WorkspaceObject(**version)


@app.get("/workspace/objects/{object_id}/actions", response_model=list[WorkspaceObjectAction])
def list_workspace_object_actions_endpoint(object_id: str) -> list[WorkspaceObjectAction]:
    if not get_workspace_object(object_id):
        raise HTTPException(status_code=404, detail=f"Workspace object '{object_id}' not found")
    return [WorkspaceObjectAction(**action) for action in list_object_actions(object_id)]


class ActorCreateRequest(BaseModel):
    actor_id: str | None = None
    kind: str = "human"
    display_name: str
    handle: str
    avatar_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    status: str = "available"


class ChannelCreateRequest(BaseModel):
    name: str
    kind: str = "channel"
    topic: str = ""
    object_id: str | None = None
    case_id: str | None = None
    actor_id: str = "human:local"


class CollaborationMessageRequest(BaseModel):
    body: str
    actor_id: str = "human:local"
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    object_version_ids: list[str] = Field(default_factory=list)
    parent_message_id: str | None = None


class ObjectCommentRequest(BaseModel):
    workspace_id: str
    object_version_id: str
    body: str
    actor_id: str = "human:local"
    block_id: str | None = None


class ApprovalCreateRequest(BaseModel):
    workspace_id: str
    object_version_id: str
    assigned_to: str
    actor_id: str = "human:local"
    note: str = ""


class MembershipRequest(BaseModel):
    actor_id: str
    role: str = "viewer"


class SuggestionCreateRequest(BaseModel):
    workspace_id: str
    base_version_id: str
    patch: dict[str, Any]
    actor_id: str = "human:local"
    block_id: str | None = None
    rationale: str = ""


class CollaborationDecisionRequest(BaseModel):
    decision: str
    actor_id: str = "human:local"
    note: str = ""


class ReadStateRequest(BaseModel):
    read: bool = True
    actor_id: str = "human:local"


class PresenceRequest(BaseModel):
    status: str = "available"


class AssignmentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    assigned_to: str
    actor_id: str = "human:local"
    case_id: str | None = None
    object_version_ids: list[str] = Field(default_factory=list)
    due_at: str | None = None
    channel_id: str | None = None
    source_message_id: str | None = None
    start_now: bool = False


class AssignmentStatusRequest(BaseModel):
    status: str
    actor_id: str = "human:local"


@app.post("/collaboration/workspaces/{workspace_id}", response_model=dict[str, Any])
def bootstrap_collaboration_workspace(workspace_id: str, name: str = "Finance workspace") -> dict[str, Any]:
    return ensure_workspace(workspace_id, name=name)


@app.get("/collaboration/workspaces/{workspace_id}/actors", response_model=list[dict[str, Any]])
def collaboration_actor_directory(workspace_id: str) -> list[dict[str, Any]]:
    ensure_workspace(workspace_id)
    return list_actors(workspace_id)


@app.post("/collaboration/actors", response_model=dict[str, Any])
def create_collaboration_actor(body: ActorCreateRequest) -> dict[str, Any]:
    return upsert_actor(body.model_dump(exclude_none=True))


@app.post("/collaboration/workspaces/{workspace_id}/memberships", response_model=dict[str, Any])
def create_collaboration_membership(workspace_id: str, body: MembershipRequest) -> dict[str, Any]:
    try:
        return add_membership(workspace_id, body.actor_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/collaboration/actors/{actor_id}/presence", response_model=dict[str, Any])
def update_collaboration_presence(actor_id: str, body: PresenceRequest) -> dict[str, Any]:
    if body.status not in {"available", "working", "waiting", "blocked", "offline"}:
        raise HTTPException(status_code=422, detail="Invalid presence status")
    result = update_actor_status(actor_id, body.status)
    if not result:
        raise HTTPException(status_code=404, detail="Actor not found")
    return result


@app.get("/collaboration/workspaces/{workspace_id}/assignments", response_model=list[dict[str, Any]])
def collaboration_assignments(workspace_id: str, actor_id: str | None = None) -> list[dict[str, Any]]:
    return list_assignments(workspace_id, actor_id)


async def _start_collaboration_assignment(assignment: dict[str, Any]) -> dict[str, Any]:
    """Launch a confirmed task through the shared main graph."""
    workspace_id = assignment["workspace_id"]
    actor_id = assignment["assigned_to"]
    channel_id = assignment.get("channel_id")
    refs = assignment.get("object_version_ids") or []
    thread_id = assignment.get("thread_id") or f"thread_{uuid4().hex[:8]}"
    query = assignment.get("description") or assignment["title"]
    loop = asyncio.get_running_loop()
    rs = RunState(thread_id, loop, query, "auto", workspace_id)
    rs.collaboration_channel_id = channel_id
    rs.collaboration_workspace_id = workspace_id
    rs.collaboration_actor_id = actor_id
    rs.collaboration_object_version_ids = list(refs)
    rs.collaboration_assignment_id = assignment["assignment_id"]
    rs.collaboration_task_context = {
        "task_id": assignment["assignment_id"],
        "title": assignment["title"],
        "goal": assignment["description"],
        "status": "working",
        "assigned_by": assignment["assigned_by"],
        "assigned_to": assignment["assigned_to"],
        "channel_id": channel_id,
        "source_message_id": assignment.get("source_message_id"),
        "input_object_version_ids": list(refs),
    }
    _run_registry[thread_id] = rs
    upsert_job(thread_id=thread_id, query=query, mode="auto", status=rs.status, session_id=workspace_id)
    assignment = update_assignment(
        assignment["assignment_id"], "working", actor_id, thread_id=thread_id,
    ) or assignment
    asyncio.create_task(_run_agent_task(thread_id, query, "auto", workspace_id, {}))
    return assignment


@app.post("/collaboration/workspaces/{workspace_id}/assignments", response_model=dict[str, Any])
async def create_collaboration_assignment(workspace_id: str, body: AssignmentCreateRequest) -> dict[str, Any]:
    try:
        require_workspace_role(workspace_id, body.actor_id, "commenter")
        require_workspace_role(workspace_id, body.assigned_to, "viewer")
        if body.channel_id and not any(channel["channel_id"] == body.channel_id for channel in list_channels(workspace_id)):
            raise HTTPException(status_code=422, detail="Channel does not belong to workspace")
        assignment = create_assignment(workspace_id, body.model_dump(exclude={"actor_id", "start_now"}), body.actor_id)
        if body.start_now and body.assigned_to.startswith("agent:"):
            return await _start_collaboration_assignment(assignment)
        return assignment
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.patch("/collaboration/assignments/{assignment_id}", response_model=dict[str, Any])
def update_collaboration_assignment(assignment_id: str, body: AssignmentStatusRequest) -> dict[str, Any]:
    if body.status not in {"open", "working", "blocked", "completed", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid assignment status")
    try:
        result = update_assignment(assignment_id, body.status, body.actor_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return result


@app.get("/collaboration/workspaces/{workspace_id}/channels", response_model=list[dict[str, Any]])
def collaboration_channels(workspace_id: str) -> list[dict[str, Any]]:
    ensure_workspace(workspace_id)
    return list_channels(workspace_id)


@app.post("/collaboration/workspaces/{workspace_id}/channels", response_model=dict[str, Any])
def create_collaboration_channel(workspace_id: str, body: ChannelCreateRequest) -> dict[str, Any]:
    try:
        return create_channel(workspace_id, body.model_dump(exclude={"actor_id"}), body.actor_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/collaboration/channels/{channel_id}/messages", response_model=list[dict[str, Any]])
def collaboration_messages(channel_id: str) -> list[dict[str, Any]]:
    return list_messages(channel_id)


@app.get("/collaboration/channels/{channel_id}/events")
async def collaboration_channel_events(channel_id: str) -> StreamingResponse:
    async def generate():
        seen: set[str] = set()
        last_ping = time.time()
        while True:
            try:
                for message in list_messages(channel_id):
                    message_id = str(message["message_id"])
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    yield f"data: {json.dumps({'type': 'collaboration_message', 'message': message}, ensure_ascii=False)}\n\n"
                if time.time() - last_ping >= 20:
                    last_ping = time.time()
                    yield 'data: {"type":"ping"}\n\n'
                await asyncio.sleep(0.75)
            except asyncio.CancelledError:
                return
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/collaboration/workspaces/{workspace_id}/channels/{channel_id}/messages", response_model=dict[str, Any])
async def create_collaboration_message(workspace_id: str, channel_id: str, body: CollaborationMessageRequest) -> dict[str, Any]:
    try:
        require_workspace_role(workspace_id, body.actor_id, "commenter")
        message = create_message(channel_id, workspace_id, body.actor_id, body.model_dump(exclude={"actor_id"}))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    actionable = next((mention for mention in body.mentions if mention.get("kind") == "agent" and mention.get("requested_action") in {"execute", "review"}), None)
    if actionable:
        thread_id = f"thread_{uuid4().hex[:8]}"
        actor_id = str(actionable.get("target_id") or "agent:research")
        refs = body.object_version_ids or actionable.get("context_refs") or []
        assignment = create_assignment(workspace_id, {
            "title": body.body[:160],
            "description": body.body,
            "assigned_to": actor_id,
            "object_version_ids": refs,
            "channel_id": channel_id,
            "source_message_id": message["message_id"],
            "thread_id": thread_id,
        }, body.actor_id)
        assignment = await _start_collaboration_assignment(assignment)
        message["assignment"] = assignment
    human_action = next((mention for mention in body.mentions if mention.get("kind") == "human" and mention.get("requested_action") in {"execute", "review"}), None)
    if human_action:
        assignment = create_assignment(workspace_id, {
            "title": body.body[:160], "description": body.body,
            "assigned_to": human_action["target_id"], "object_version_ids": body.object_version_ids,
        }, body.actor_id)
        message["human_assignment"] = assignment
    return message


@app.get("/workspace/objects/{object_id}/comments", response_model=list[dict[str, Any]])
def workspace_object_comments(object_id: str) -> list[dict[str, Any]]:
    return list_object_comments(object_id)


@app.post("/workspace/objects/{object_id}/comments", response_model=dict[str, Any])
def create_workspace_object_comment(object_id: str, body: ObjectCommentRequest) -> dict[str, Any]:
    try:
        require_workspace_role(body.workspace_id, body.actor_id, "commenter")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return create_object_comment(body.workspace_id, object_id, body.model_dump(exclude={"workspace_id", "actor_id"}), body.actor_id)


@app.patch("/workspace/comments/{comment_id}", response_model=dict[str, Any])
def update_workspace_object_comment(comment_id: str, body: ReadStateRequest) -> dict[str, Any]:
    try:
        result = resolve_comment(comment_id, body.read, body.actor_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Comment not found")
    return result


@app.get("/workspace/objects/{object_id}/suggestions", response_model=list[dict[str, Any]])
def workspace_object_suggestions(object_id: str) -> list[dict[str, Any]]:
    return list_object_suggestions(object_id)


@app.post("/workspace/objects/{object_id}/suggestions", response_model=dict[str, Any])
def create_workspace_object_suggestion(object_id: str, body: SuggestionCreateRequest) -> dict[str, Any]:
    try:
        require_workspace_role(body.workspace_id, body.actor_id, "reviewer")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return create_suggestion(body.workspace_id, object_id, body.model_dump(exclude={"workspace_id", "actor_id"}), body.actor_id)


@app.post("/workspace/suggestions/{suggestion_id}/decision", response_model=dict[str, Any])
def decide_workspace_object_suggestion(suggestion_id: str, body: CollaborationDecisionRequest) -> dict[str, Any]:
    try:
        result = decide_suggestion(suggestion_id, body.decision, body.actor_id)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=403 if isinstance(exc, PermissionError) else 422, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return result


@app.post("/workspace/objects/{object_id}/approvals", response_model=dict[str, Any])
def request_workspace_object_approval(object_id: str, body: ApprovalCreateRequest) -> dict[str, Any]:
    try:
        require_workspace_role(body.workspace_id, body.actor_id, "editor")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return create_approval(body.workspace_id, object_id, body.model_dump(exclude={"workspace_id", "actor_id"}), body.actor_id)


@app.get("/workspace/objects/{object_id}/approvals", response_model=list[dict[str, Any]])
def workspace_object_approvals(object_id: str) -> list[dict[str, Any]]:
    return list_object_approvals(object_id)


@app.post("/workspace/approvals/{approval_id}/decision", response_model=dict[str, Any])
def decide_workspace_object_approval(approval_id: str, body: CollaborationDecisionRequest) -> dict[str, Any]:
    try:
        result = decide_approval(approval_id, body.decision, body.actor_id, body.note)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=403 if isinstance(exc, PermissionError) else 422, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Approval not found")
    return result


@app.get("/collaboration/actors/{actor_id}/notifications", response_model=list[dict[str, Any]])
def collaboration_notifications(actor_id: str) -> list[dict[str, Any]]:
    return list_notifications(actor_id)


@app.patch("/collaboration/notifications/{notification_id}", response_model=dict[str, Any])
def update_collaboration_notification(notification_id: str, body: ReadStateRequest) -> dict[str, Any]:
    result = mark_notification_read(notification_id, body.read, body.actor_id)
    if not result:
        raise HTTPException(status_code=404, detail="Notification not found")
    return result


@app.post("/workspace/objects", response_model=WorkspaceObject)
def create_workspace_object_endpoint(body: WorkspaceObjectCreate) -> WorkspaceObject:
    object_id = body.object_id or f"{body.object_type}:{uuid4().hex[:12]}"
    obj = upsert_workspace_object({
        "object_id": object_id,
        "object_type": body.object_type,
        "schema_ref": body.schema_ref,
        "schema_version": body.schema_version,
        "title": body.title,
        "status": body.status,
        "session_id": body.session_id,
        "thread_id": body.thread_id,
        "case_id": body.case_id,
        "task_id": body.task_id,
        "run_id": body.run_id,
        "source_message_id": body.source_message_id,
        "created_by": body.created_by,
        "updated_by": body.updated_by,
        "source_object_ids": body.source_object_ids,
        "source_version_ids": body.source_version_ids,
        "entity_refs": body.entity_refs,
        "source_refs": body.source_refs,
        "kg_node_ids": body.kg_node_ids,
        "artifact_paths": body.artifact_paths,
        "search_text": body.search_text,
        "tags": body.tags,
        "confidence": body.confidence,
        "quality": body.quality,
        "visibility": body.visibility,
        "summary": body.summary,
        "payload": body.payload,
    })
    return WorkspaceObject(**obj)


# ---------------------------------------------------------------------------
# Document endpoints (RAG)
# ---------------------------------------------------------------------------

class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    session_id: str
    status: str           # "processing" | "ready" | "error"
    # Fine-grained ingest progress for the upload card (in-memory only):
    # uploading → parsing → chunking → embedding → ready | error.
    stage: str | None = None
    chunk_count: int = 0
    page_count: int = 0
    error: str | None = None
    created_at: float
    # Entity metadata extracted at upload (gpt-4o-mini). Surfaced to the UI so
    # the doc card + planner can show "Meta Platforms (META) · earnings_call".
    company: str | None = None
    ticker: str | None = None
    doc_type: str | None = None
    fiscal_period: str | None = None


class DocumentCitationResponse(BaseModel):
    citation_id: str
    doc_id: str
    document_version_id: str
    filename: str
    page: int | str | None = None
    chunk_index: int
    citation_label: str
    company: str | None = None
    ticker: str | None = None
    doc_type: str | None = None
    fiscal_period: str | None = None
    text: str
    previous_text: str = ""
    next_text: str = ""
    tables: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceQuery(BaseModel):
    session_id: str
    query: str
    include_chunks: bool = True
    limit: int = Field(default=8, ge=1, le=20)


@app.post("/documents", response_model=DocumentInfo)
async def upload_document(
    file: UploadFile = File(...),
    session_id: str = Form(...),
) -> DocumentInfo:
    from documents import ingest_document, register_document  # noqa: PLC0415

    doc_id = f"doc_{uuid4().hex[:12]}"
    file_bytes = await file.read()
    filename = file.filename or "upload"

    entry: dict = {
        "doc_id": doc_id,
        "filename": filename,
        "session_id": session_id,
        "status": "processing",
        "stage": "queued",
        "chunk_count": 0,
        "page_count": 0,
        "error": None,
        "created_at": time.time(),
    }
    register_document(entry)
    _sync_uploaded_document_workspace_object(entry)

    # Run parsing + embedding in a thread pool so we don't block the event loop
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, ingest_document, file_bytes, filename, session_id, doc_id)

    return DocumentInfo(**entry)


@app.get("/documents/{doc_id}/status", response_model=DocumentInfo)
def document_status(doc_id: str) -> DocumentInfo:
    from documents import _doc_registry  # noqa: PLC0415
    info = _doc_registry.get(doc_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    _sync_uploaded_document_workspace_object(info)
    return DocumentInfo(**info)


@app.get("/documents", response_model=list[DocumentInfo])
def list_documents(session_id: str) -> list[DocumentInfo]:
    from documents import list_docs  # noqa: PLC0415
    docs = list_docs(session_id)
    for doc in docs:
        _sync_uploaded_document_workspace_object(doc)
    return [DocumentInfo(**d) for d in docs]


@app.get("/documents/citations/{citation_id}", response_model=DocumentCitationResponse)
def get_document_citation_source(citation_id: str) -> DocumentCitationResponse:
    from documents import get_document_citation  # noqa: PLC0415

    try:
        citation = get_document_citation(citation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DocumentCitationResponse(**citation)


@app.get("/documents/{doc_id}/versions", response_model=list[dict[str, Any]])
def get_document_versions(doc_id: str) -> list[dict[str, Any]]:
    versions = list_document_versions(doc_id)
    if not versions:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return versions


@app.get("/document-versions/{version_id}", response_model=dict[str, Any])
def get_document_version_endpoint(version_id: str) -> dict[str, Any]:
    version = get_document_version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail=f"Document version '{version_id}' not found")
    return version


@app.post("/memory/evidence", response_model=dict[str, Any])
def retrieve_evidence(body: EvidenceQuery) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage  # noqa: PLC0415
    from evidence_memory import build_evidence_pack  # noqa: PLC0415

    return build_evidence_pack(
        {
            "messages": [HumanMessage(content=body.query)],
            "session_id": body.session_id,
            "route_decision": {"route_level": "small_task" if body.include_chunks else "direct"},
        },
        include_chunks=body.include_chunks,
        limit=body.limit,
    )


@app.get("/documents/{doc_id}/file")
def get_document_file(doc_id: str) -> FileResponse:
    from documents import _doc_registry  # noqa: PLC0415
    info = _doc_registry.get(doc_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    upload_path = info.get("upload_path")
    if not upload_path or not Path(upload_path).exists():
        raise HTTPException(status_code=404, detail="File not available")
    return FileResponse(
        upload_path,
        filename=info["filename"],
        headers={"Content-Disposition": f'inline; filename="{info["filename"]}"'},
    )


@app.delete("/documents/{doc_id}")
def remove_document(doc_id: str) -> dict:
    from documents import _doc_registry, delete_document  # noqa: PLC0415
    if doc_id not in _doc_registry:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    delete_document(doc_id)
    delete_workspace_object(f"uploaded_document:{doc_id}")
    return {"deleted": doc_id}


# ---------------------------------------------------------------------------
# Session sidebar layout (groups, pin, order — no message history)
# ---------------------------------------------------------------------------

class SessionGroupModel(BaseModel):
    id: str
    name: str
    color: str
    collapsed: bool = False
    sort_order: int = 0
    created_at: str


class SessionLayoutItemModel(BaseModel):
    session_id: str
    title_override: str | None = None
    pinned: bool = False
    group_id: str | None = None
    sort_order: int = 0
    updated_at: str | None = None


class SessionLayoutPayload(BaseModel):
    groups: list[SessionGroupModel] = Field(default_factory=list)
    sessions: list[SessionLayoutItemModel] = Field(default_factory=list)


@app.get("/sessions/layout", response_model=SessionLayoutPayload)
def read_session_layout() -> SessionLayoutPayload:
    data = get_session_layout()
    return SessionLayoutPayload(**data)


@app.put("/sessions/layout", response_model=SessionLayoutPayload)
def write_session_layout(body: SessionLayoutPayload) -> SessionLayoutPayload:
    data = replace_session_layout(
        groups=[g.model_dump() for g in body.groups],
        sessions=[s.model_dump() for s in body.sessions],
    )
    return SessionLayoutPayload(**data)


# ---------------------------------------------------------------------------
# Knowledge Graph endpoints
# ---------------------------------------------------------------------------

class KGNodeUpsert(BaseModel):
    ticker: str
    node_type: str
    field: str
    value: Any
    confidence: float = 1.0
    source: str = "user_stated"
    run_id: str | None = None


class KGNodePatch(BaseModel):
    value: Any | None = None
    confidence: float | None = None


class KGEdgeCreate(BaseModel):
    src_id: str
    tgt_id: str
    relation: str
    confidence: float = 1.0
    source: str = "user_stated"


class KGQueryRequest(BaseModel):
    question: str
    ticker: str | None = None


class KGCompareChatRequest(BaseModel):
    """Side-chat over an assembled cross-run comparison artifact."""
    question: str
    diff: dict[str, Any]
    history: list[dict[str, str]] | None = None


@app.get("/kg/{session_id}")
async def kg_full(session_id: str) -> dict[str, Any]:
    """Return all KG nodes + edges (cross-session, full ticker corpus).

    The session_id param is kept for URL compatibility but is NOT used as a
    filter. The KG is a global knowledge base about tickers — a DCF rerun that
    targets a new session must not make the old session's nodes disappear from
    the graph panel. Callers (KnowledgePanel) already filter client-side by
    ticker / node_type / source.
    """
    from storage import list_kg_nodes, list_kg_edges  # noqa: PLC0415
    nodes = list_kg_nodes()   # all sessions, all tickers
    edges = list_kg_edges()   # all sessions
    return {"nodes": nodes, "edges": edges}


@app.get("/kg/{session_id}/subgraph/{ticker}")
async def kg_subgraph(session_id: str, ticker: str) -> dict[str, Any]:
    """Cross-session ticker subgraph.

    Ignores session_id for the same reason as kg_full — DCF reruns in new
    sessions must not hide prior run artifacts when the panel refreshes.
    """
    from storage import list_kg_nodes, list_kg_edges  # noqa: PLC0415
    nodes = list_kg_nodes(ticker=ticker.upper())   # all sessions for ticker
    node_ids = {n["id"] for n in nodes}
    all_edges = list_kg_edges()                    # all sessions
    edges = [e for e in all_edges if e["src_id"] in node_ids or e["tgt_id"] in node_ids]
    return {"nodes": nodes, "edges": edges}


@app.post("/kg/{session_id}/nodes")
async def kg_create_node(session_id: str, body: KGNodeUpsert) -> dict[str, Any]:
    """User-driven node creation. Default source='user_stated', confidence=1.0."""
    from kg import get_cache  # noqa: PLC0415
    cache = get_cache()
    node = cache.put(
        ticker=body.ticker.upper(),
        node_type=body.node_type,
        field=body.field,
        value=body.value,
        source=body.source,
        confidence=body.confidence,
        run_id=body.run_id,
        session_id=session_id,
        respect_user_lock=False,  # user explicitly creating, allow overwrite
    )
    return {"node": node}


@app.patch("/kg/{session_id}/nodes/{node_id}")
async def kg_patch_node(session_id: str, node_id: str, body: KGNodePatch) -> dict[str, Any]:
    """Edit value/confidence on an existing node. Always becomes 'user_stated'."""
    from kg import get_cache  # noqa: PLC0415
    from storage import get_kg_node  # noqa: PLC0415
    existing = get_kg_node(node_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    cache = get_cache()
    new_value = body.value if body.value is not None else existing["value"]
    new_conf = body.confidence if body.confidence is not None else 1.0
    node = cache.put(
        ticker=existing["ticker"],
        node_type=existing["node_type"],
        field=existing["field"],
        value=new_value,
        source="user_stated",  # any user edit → user_stated
        confidence=new_conf,
        run_id=existing.get("run_id"),
        session_id=session_id,
        respect_user_lock=False,
    )
    return {"node": node}


@app.delete("/kg/{session_id}/nodes/{node_id}")
async def kg_delete_node(session_id: str, node_id: str) -> dict[str, str]:
    """Delete a node and any edges touching it."""
    from kg import get_cache  # noqa: PLC0415
    from storage import delete_kg_node  # noqa: PLC0415
    delete_kg_node(node_id)
    cache = get_cache()
    cache.invalidate(node_id)
    return {"deleted": node_id}


@app.post("/kg/{session_id}/edges")
async def kg_create_edge(session_id: str, body: KGEdgeCreate) -> dict[str, Any]:
    from kg import get_cache  # noqa: PLC0415
    cache = get_cache()
    edge = cache.add_edge(
        src_id=body.src_id,
        tgt_id=body.tgt_id,
        relation=body.relation,
        session_id=session_id,
        confidence=body.confidence,
        source=body.source,
    )
    return {"edge": edge}


@app.delete("/kg/{session_id}/edges/{edge_id:path}")
async def kg_delete_edge(session_id: str, edge_id: str) -> dict[str, str]:
    from kg import get_cache  # noqa: PLC0415
    cache = get_cache()
    cache.remove_edge(edge_id)
    return {"deleted": edge_id}


@app.get("/kg/{session_id}/traversal/{run_id}")
async def kg_traversal(session_id: str, run_id: str) -> dict[str, Any]:
    """Replay the KG access path for a past DCF run."""
    from storage import list_kg_traversals  # noqa: PLC0415
    return {"run_id": run_id, "traversal": list_kg_traversals(run_id)}


@app.post("/kg/{session_id}/query")
async def kg_query(session_id: str, body: KGQueryRequest) -> dict[str, Any]:
    """Natural-language query against the KG — multi-hop deep-research engine.

    Same engine the ``query_knowledge_graph`` tool uses (single code path); the
    manual panel benefits from the same hop-by-hop reasoning. Returns answer +
    traversal subgraph (+ ``hops`` log).
    """
    from kg.deep_research import run_deep_research  # noqa: PLC0415
    import anyio  # noqa: PLC0415
    # run_deep_research is sync (LLM .invoke) — run off the event loop.
    return await anyio.to_thread.run_sync(
        lambda: run_deep_research(
            question=body.question,
            ticker=(body.ticker.upper() if body.ticker else None),
            session_id=session_id,
        )
    )


@app.post("/kg/{session_id}/compare-chat")
async def kg_compare_chat(session_id: str, body: KGCompareChatRequest) -> dict[str, Any]:
    """Side-chat over an assembled cross-run comparison. LLM reasons over the
    structured diff the frontend built (bounded context, no graph traversal)."""
    from kg.compare import discuss_comparison  # noqa: PLC0415
    import anyio  # noqa: PLC0415
    # discuss_comparison is sync (LLM .invoke) — run off the event loop.
    return await anyio.to_thread.run_sync(
        lambda: discuss_comparison(body.diff, body.question, body.history)
    )


# ---------------------------------------------------------------------------
# KG Audit endpoints
# ---------------------------------------------------------------------------


class KGAuditRequest(BaseModel):
    ticker: str | None = None
    tickers: list[str] | None = None  # audit a specific subset; None/empty = all
    checks: list[str] | None = None  # cross_source, staleness, orphan, entity_coherence, hallucination
    sample_size: int = 5
    auto_fix: bool = True


@app.post("/kg/audit")
async def kg_audit_run(body: KGAuditRequest) -> dict[str, Any]:
    """Run quality audit on the Knowledge Graph.

    Deterministic checks (no LLM): cross_source, staleness, orphan, entity_coherence.
    LLM spot-check: hallucination (re-extracts from source chunks, compares).

    All findings written to a separate kg_audit_log table.
    """
    from kg import run_audit  # noqa: PLC0415
    import anyio  # noqa: PLC0415

    # A specific subset of tickers → audit each and merge; otherwise a single
    # ticker (or None = whole graph). Keeps the per-ticker run_audit contract.
    targets: list[str | None]
    if body.tickers:
        targets = [t.upper() for t in body.tickers if t]
    else:
        targets = [body.ticker]

    def _run_all() -> list[Any]:
        out: list[Any] = []
        for tk in targets:
            out.extend(run_audit(
                ticker=tk,
                checks=body.checks,
                sample_size=body.sample_size,
                auto_fix=body.auto_fix,
            ))
        return out

    findings = await anyio.to_thread.run_sync(_run_all)
    by_severity = {}
    by_check = {}
    for f in findings:
        d = f.to_dict()
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_check[f.check_type] = by_check.get(f.check_type, 0) + 1
    return {
        "total_findings": len(findings),
        "by_severity": by_severity,
        "by_check": by_check,
        "findings": [f.to_dict() for f in findings],
    }


@app.get("/kg/audit/findings")
async def kg_audit_findings(
    ticker: str | None = None,
    severity: str | None = None,
    check_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Retrieve previously logged audit findings."""
    from kg import get_audit_findings  # noqa: PLC0415
    findings = get_audit_findings(
        ticker=ticker,
        severity=severity,
        check_type=check_type,
        limit=limit,
    )
    return {"findings": findings}


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)

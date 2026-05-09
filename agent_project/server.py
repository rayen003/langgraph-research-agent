"""
FastAPI server for the agent backend.

Endpoints
---------
POST   /runs                             create & start a new agent run
GET    /runs/{thread_id}/events          SSE stream of execution events
POST   /runs/{thread_id}/decision        HITL approve / reject the plan
GET    /runs/{thread_id}/plan            latest plan JSON for the thread
GET    /runs/{thread_id}/report          final markdown report (if complete)
GET    /artifacts/{thread_id}/{filename} serve generated artifact files
GET    /jobs                             list all runs as job summaries
POST   /workflows/dcf/runs               create & start deterministic DCF workflow
POST   /workflows/dcf/runs/{thread_id}/assumptions-decision
                                         approve/edit optional assumption review
GET    /workflows/dcf/runs/{thread_id}/result
                                         get persisted DCF workflow result JSON
GET    /health                           liveness check
"""

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

import dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

dotenv.load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

from storage import (  # noqa: E402
    append_job_event,
    get_job,
    get_report as get_stored_report,
    get_session_memory,
    list_job_events,
    list_jobs as list_stored_jobs,
    mark_stale_running_jobs,
    sync_job_steps,
    update_job,
    upsert_job,
)

AGENT_DIR = Path(__file__).parent
RUNS_DIR = AGENT_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(RUNS_DIR / "server.log", encoding="utf-8"),
    ],
)
mark_stale_running_jobs()


POLL_INTERVAL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Run state registry
# ---------------------------------------------------------------------------

class RunState:
    __slots__ = (
        "thread_id", "loop", "event_queue", "hitl_future",
        "status", "query", "mode", "intent", "created_at", "session_id",
        "dcf_hitl_payload",
    )

    def __init__(self, thread_id: str, loop: asyncio.AbstractEventLoop, query: str, mode: str, session_id: str = "") -> None:
        self.thread_id = thread_id
        self.loop = loop
        self.event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.hitl_future: asyncio.Future | None = None
        self.status = "classifying"
        self.query = query
        self.mode = mode
        self.intent: str | None = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.session_id = session_id
        self.dcf_hitl_payload: dict | None = None


_run_registry: dict[str, RunState] = {}

_RUNNING_STATUSES = {
    "classifying",
    "planning",
    "awaiting_approval",
    "awaiting_assumptions",
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
    from graphs.research import execute_plan_node, synthesize_node, update_memory_node  # noqa: PLC0415
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
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        sync_job_steps(thread_id, plan)

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

        executed = await asyncio.to_thread(execute_plan_node, state)
        state.update(executed)

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


def _make_event_bridge(rs: RunState):
    """Return a sync callback safe to call from any thread."""
    def bridge(event: dict) -> None:
        # Intercept intent_classified to update RunState.intent
        if event.get("type") == "intent_classified":
            rs.intent = event.get("intent")
            rs.status = "planning" if rs.intent == "research" else "chat_responding"
            update_job(rs.thread_id, status=rs.status, intent=rs.intent)
        elif event.get("type") == "dcf_assumptions_review":
            # Store HITL payload directly on RunState — safe from any thread
            # since we're only writing and _run_agent_task reads it after ainvoke.
            rs.dcf_hitl_payload = {
                "ticker": event.get("ticker", "?"),
                "horizon_years": event.get("horizon_years", 5),
                "assumptions": event.get("assumptions", {}),
                "assumption_provenance": event.get("assumption_provenance", {}),
                "memo_proposals": event.get("memo_proposals", {}),
                "evidence_items": event.get("evidence_items", []),
            }
            rs.status = "awaiting_assumptions"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "synthesis_start":
            rs.status = "synthesizing"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "synthesis_complete":
            rs.status = "complete"
            update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "chat_complete":
            # Skip marking complete if DCF HITL is pending — keep SSE alive
            if not rs.dcf_hitl_payload:
                rs.status = "complete"
                update_job(rs.thread_id, status=rs.status)
        elif event.get("type") == "execution_started":
            rs.status = "executing"
            update_job(rs.thread_id, status=rs.status)

        if rs.loop.is_running():
            persisted = _persist_event(rs.thread_id, event)
            rs.loop.call_soon_threadsafe(rs.event_queue.put_nowait, persisted)
    return bridge


async def _run_agent_task(thread_id: str, query: str, mode: str, session_id: str = "") -> None:
    from file import app as agent_graph  # noqa: PLC0415
    from langchain_core.messages import HumanMessage  # noqa: PLC0415
    from langgraph.types import Command  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs = _run_registry[thread_id]
    config = {"configurable": {"thread_id": thread_id}}
    set_thread_id(thread_id)
    set_ui_event_handler(_make_event_bridge(rs))
    session_memory = get_session_memory(session_id)

    try:
        # Phase 1 — intent + (plan for research | chat for conversational)
        result = await agent_graph.ainvoke(
            {
                "messages": [HumanMessage(content=query)],
                "mode": mode,
                "resolved_intent": None,
                "session_id": session_id,
                "session_memory": session_memory,
            },
            config=config,
        )

        interrupts = result.get("__interrupt__", ())

        # Check for DCF HITL — payload set by event bridge when dcf_assumptions_review fires
        if rs.dcf_hitl_payload:
            dcf_hitl = rs.dcf_hitl_payload
            rs.hitl_future = rs.loop.create_future()
            # Event already emitted by run_dcf_workflow tool and bridge set status
            decision = await rs.hitl_future
            rs.dcf_hitl_payload = None  # clear for next run

            if not decision.get("approved"):
                rs.status = "rejected"
                update_job(thread_id, status=rs.status)
                _send_event(rs, {"type": "assumptions_rejected", "workflow": "dcf"})
                rs.event_queue.put_nowait(None)
                return

            # User approved — resume DCF workflow with overrides if any
            overrides = decision.get("assumptions_overrides") or {}
            _send_event(
                rs,
                {
                    "type": "assumptions_submitted",
                    "workflow": "dcf",
                    "overrides_applied": bool(overrides),
                },
            )

            # Re-invoke chat with approval message to trigger DCF completion
            rs.status = "chat_responding"
            update_job(thread_id, status=rs.status)
            approval_payload = {
                "ticker": dcf_hitl.get("ticker", "?"),
                "horizon_years": dcf_hitl.get("horizon_years", 5),
                "all_assumptions": overrides or dcf_hitl.get("assumptions", {}),
            }
            approval_message = f"[DCF_APPROVED]:{json.dumps(approval_payload)}"

            await agent_graph.ainvoke(
                {
                    "messages": [HumanMessage(content=approval_message)],
                    "mode": mode,
                    "resolved_intent": "chat",
                    "session_id": session_id,
                    "session_memory": session_memory,
                },
                config=config,
            )
            update_job(thread_id, status="complete")
            _send_event(rs, {"type": "run_complete"})
            rs.event_queue.put_nowait(None)
            return

        # No interrupt → chat run or plan-less completion
        if not interrupts:
            update_job(thread_id, status="complete")
            _send_event(rs, {"type": "run_complete"})
            rs.event_queue.put_nowait(None)
            return

        # Interrupt → research HITL flow
        plan = interrupts[0].value.get("plan", {})
        sync_job_steps(thread_id, plan)
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

        # Phase 2 — execute + synthesize
        await agent_graph.ainvoke(
            Command(resume={"action": "yes", "feedback": None}),
            config=config,
        )

        update_job(thread_id, status="complete")
        _send_event(rs, {"type": "run_complete"})

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Agent task failed:\n%s", tb)
        rs.status = "error"
        update_job(thread_id, status=rs.status, error=f"{type(exc).__name__}: {exc}")
        _send_event(rs, {"type": "error", "message": f"{type(exc).__name__}: {exc}\n\n{tb}"})
    finally:
        rs.event_queue.put_nowait(None)  # sentinel — closes SSE stream
        if rs.status not in _RUNNING_STATUSES:
            _run_registry.pop(thread_id, None)
        set_ui_event_handler(None)


async def _run_dcf_workflow_task(thread_id: str, request: "DCFRunRequest") -> None:
    from graphs.workflows.dcf import dcf_workflow_app  # noqa: PLC0415
    from langgraph.types import Command  # noqa: PLC0415
    from utils import set_thread_id, set_ui_event_handler  # noqa: PLC0415

    rs = _run_registry[thread_id]
    config = {"configurable": {"thread_id": thread_id}}
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
    }

    try:
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
        update_job(thread_id, status="complete")
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


class RunCreatedResponse(BaseModel):
    thread_id: str


class DecisionRequest(BaseModel):
    approved: bool


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

    loop = asyncio.get_running_loop()
    rs = RunState(thread_id, loop, body.query, body.mode, session_id)
    _run_registry[thread_id] = rs
    upsert_job(
        thread_id=thread_id,
        query=body.query,
        mode=body.mode,
        status=rs.status,
        session_id=session_id,
    )
    asyncio.create_task(_run_agent_task(thread_id, body.query, body.mode, session_id))
    return RunCreatedResponse(thread_id=thread_id)


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
    asyncio.create_task(_run_dcf_workflow_task(thread_id, body))
    return RunCreatedResponse(thread_id=thread_id)


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
        replay_events_sent = 0
        queue_events_sent = 0
        db_poll_events_sent = 0
        db_poll_rounds = 0

        try:
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
            elapsed_ms = int((time.time() - stream_started_at) * 1000)
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.post("/runs/{thread_id}/dcf-continue")
async def continue_dcf_after_review(thread_id: str, body: DcfContinueRequest) -> dict:
    """Resume DCF graph from assumption review interrupt."""
    from graphs.workflows.dcf import dcf_workflow_app  # noqa: PLC0415
    from langgraph.types import Command  # noqa: PLC0415
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


@app.get("/workflows/dcf/runs/{thread_id}/result")
def get_dcf_result(thread_id: str) -> dict:
    result_path = _workflow_result_path(thread_id, "dcf_output.json")
    if not result_path.exists():
        raise HTTPException(status_code=404, detail=f"No DCF workflow result found for thread '{thread_id}'")
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt DCF result payload: {exc}") from exc


@app.get("/artifacts/{thread_id}/{filename}")
def get_artifact(thread_id: str, filename: str) -> FileResponse:
    artifact_path = _artifacts_dir_for(thread_id) / filename
    if not artifact_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{filename}' not found for thread '{thread_id}'",
        )
    return FileResponse(artifact_path)


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


# ---------------------------------------------------------------------------
# Document endpoints (RAG)
# ---------------------------------------------------------------------------

class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    session_id: str
    status: str           # "processing" | "ready" | "error"
    chunk_count: int = 0
    page_count: int = 0
    error: str | None = None
    created_at: float


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
        "chunk_count": 0,
        "page_count": 0,
        "error": None,
        "created_at": time.time(),
    }
    register_document(entry)

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
    return DocumentInfo(**info)


@app.get("/documents", response_model=list[DocumentInfo])
def list_documents(session_id: str) -> list[DocumentInfo]:
    from documents import list_docs  # noqa: PLC0415
    return [DocumentInfo(**d) for d in list_docs(session_id)]


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
    return {"deleted": doc_id}


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)

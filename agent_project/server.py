"""FastAPI backend for the research agent and React UI."""

import asyncio
import json
import queue as queue_module
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from agent import app as agent_graph  # noqa: E402
from utils import set_thread_id, set_ui_event_handler  # noqa: E402

dotenv.load_dotenv(Path(__file__).parent / ".env")

AGENT_DIR = Path(__file__).parent
RUNS_DIR = AGENT_DIR / "runs"


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


class HealthResponse(BaseModel):
    status: str


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


class CreateRunRequest(BaseModel):
    query: str


class CreateRunResponse(BaseModel):
    thread_id: str
    plan: PlanResponse


class ResumeRunRequest(BaseModel):
    action: Literal["yes", "no", "edit_plan"] = "yes"
    feedback: str | None = None
    plan: dict | None = None


class OkResponse(BaseModel):
    ok: bool


@dataclass
class RunSession:
    thread_id: str
    query: str
    config: dict
    queue: queue_module.Queue[dict] = field(default_factory=queue_module.Queue)
    status: str = "reviewing"


RUN_SESSIONS: dict[str, RunSession] = {}


def _require_session(thread_id: str) -> RunSession:
    session = RUN_SESSIONS.get(thread_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Unknown thread_id '{thread_id}'")
    return session


def _enqueue_event(thread_id: str, event: dict) -> None:
    session = RUN_SESSIONS.get(thread_id)
    if session:
        session.queue.put(event)


def _run_execution(thread_id: str, resume_value: dict) -> None:
    session = _require_session(thread_id)

    def handler(event: dict) -> None:
        _enqueue_event(thread_id, event)

    def worker() -> None:
        set_thread_id(thread_id)
        set_ui_event_handler(handler)
        session.status = "executing"
        try:
            for _ in agent_graph.stream(
                Command(resume=resume_value),
                config=session.config,
                stream_mode=["updates", "messages"],
                version="v2",
            ):
                pass
            session.status = "completed"
            _enqueue_event(thread_id, {"type": "run_complete"})
        except Exception as exc:  # noqa: BLE001
            session.status = "failed"
            _enqueue_event(thread_id, {"type": "run_error", "error": str(exc)})
        finally:
            set_ui_event_handler(None)

    threading.Thread(target=worker, daemon=True).start()


app = FastAPI(
    title="Research Agent Backend",
    description="FastAPI backend wrapping the LangGraph research agent.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Legacy Chainlit client probes (close old tabs / previews pointing at :8000)
# ---------------------------------------------------------------------------
# A browser tab that still loads the Chainlit app will request Socket.IO and
# /project/translations. This API is SSE-only; these stubs avoid 404 spam in logs.


@app.get("/project/translations", include_in_schema=False)
def legacy_chainlit_translations(language: str = "en-US") -> dict:
    return {}


@app.api_route("/ws/socket.io/", methods=["GET", "POST"], include_in_schema=False)
@app.api_route("/socket.io/", methods=["GET", "POST"], include_in_schema=False)
def legacy_socketio_disabled() -> PlainTextResponse:
    """Engine.IO clients expect 200; empty body stops some polling loops quietly."""
    return PlainTextResponse("", status_code=200)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/runs", response_model=CreateRunResponse)
def create_run(payload: CreateRunRequest) -> CreateRunResponse:
    thread_id = f"thread_{uuid4().hex[:8]}"
    set_thread_id(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    first = agent_graph.invoke({"messages": [HumanMessage(content=payload.query)]}, config=config)
    interrupts = first.get("__interrupt__", ())
    if not interrupts:
        raise HTTPException(status_code=500, detail="Agent did not pause for plan review.")

    plan_payload = interrupts[0].value
    plan = plan_payload.get("plan")
    if not plan:
        raise HTTPException(status_code=500, detail="Planner did not return a plan.")

    RUN_SESSIONS[thread_id] = RunSession(
        thread_id=thread_id,
        query=payload.query,
        config=config,
    )
    return CreateRunResponse(thread_id=thread_id, plan=PlanResponse(**plan))


@app.post("/runs/{thread_id}/resume", response_model=OkResponse)
def resume_run(thread_id: str, payload: ResumeRunRequest) -> OkResponse:
    session = _require_session(thread_id)
    if session.status == "executing":
        raise HTTPException(status_code=409, detail="Run is already executing.")

    resume_value = {"action": payload.action, "feedback": payload.feedback}
    if payload.action == "edit_plan":
        if not payload.plan:
            raise HTTPException(status_code=400, detail="Edited plan payload is required.")
        resume_value["plan"] = payload.plan

    _run_execution(thread_id, resume_value)
    return OkResponse(ok=True)


@app.get("/runs/{thread_id}/events")
async def stream_run_events(thread_id: str) -> StreamingResponse:
    session = _require_session(thread_id)

    async def event_stream():
        yield "event: ready\ndata: {}\n\n"
        while True:
            try:
                event = await asyncio.to_thread(session.queue.get, True, 1.0)
            except queue_module.Empty:
                yield ": keep-alive\n\n"
                continue

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") in {"run_complete", "run_error"}:
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/artifacts/{thread_id}/{filename}")
def get_artifact(thread_id: str, filename: str) -> FileResponse:
    artifact_path = _artifacts_dir_for(thread_id) / filename
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found for thread '{thread_id}'")
    return FileResponse(artifact_path)


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


@app.get("/runs/{thread_id}/status", response_model=OkResponse)
def get_run_status(thread_id: str) -> OkResponse:
    _require_session(thread_id)
    return OkResponse(ok=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

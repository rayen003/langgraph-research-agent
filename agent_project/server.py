"""
FastAPI server for the agent backend.

Learning map
------------
/health                            → basic route, JSON response
/artifacts/{thread_id}/{filename}  → path params, FileResponse, 404 handling
/runs/{thread_id}/plan             → path params, reading JSON from disk, Pydantic response model
/copilotkit                        → CopilotKit LangGraph adapter (mounted, not hand-written)
"""

import json
import sys
from pathlib import Path

import dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

dotenv.load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Path helpers — same logic as utils.py but self-contained for the server
# ---------------------------------------------------------------------------

AGENT_DIR = Path(__file__).parent
RUNS_DIR = AGENT_DIR / "runs"


def _runs_dir_for(thread_id: str) -> Path:
    return RUNS_DIR / thread_id


def _artifacts_dir_for(thread_id: str) -> Path:
    return _runs_dir_for(thread_id) / "artifacts"


def _latest_plan_path(thread_id: str) -> Path | None:
    """Return the most recently modified plan JSON for a thread, or None."""
    plans_dir = _runs_dir_for(thread_id) / "plans"
    if not plans_dir.exists():
        return None
    files = sorted(plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Pydantic models for response typing
# FastAPI uses these to validate and document responses automatically.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agent Backend",
    description="FastAPI server wrapping the LangGraph research agent.",
    version="0.1.0",
)

# CORS: allow the React dev server (port 5173 for Vite) to call this backend.
# In production you'd restrict this to your actual domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoint 1: /health
#
# FastAPI concept: basic route + automatic JSON serialisation from a Pydantic
# model.  Visiting http://localhost:8000/health returns {"status": "ok"}.
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


# ---------------------------------------------------------------------------
# Endpoint 2: /artifacts/{thread_id}/{filename}
#
# FastAPI concept:
#   - Path parameters: {thread_id} and {filename} are extracted from the URL
#     and passed as typed function arguments.
#   - FileResponse: streams a file from disk to the browser without loading it
#     entirely into memory.
#   - HTTPException: raising this sends the correct HTTP status code (404 here)
#     instead of a 500.
#
# The React frontend uses this as an <img src> URL so matplotlib plots render
# inside the report viewer.
# ---------------------------------------------------------------------------

@app.get("/artifacts/{thread_id}/{filename}")
def get_artifact(thread_id: str, filename: str) -> FileResponse:
    artifact_path = _artifacts_dir_for(thread_id) / filename
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found for thread '{thread_id}'")
    # FileResponse infers the correct Content-Type (image/png, etc.) from the
    # file extension automatically.
    return FileResponse(artifact_path)


# ---------------------------------------------------------------------------
# Endpoint 3: /runs/{thread_id}/plan
#
# FastAPI concept:
#   - Returning a Pydantic model automatically validates the data and documents
#     it in /docs (Swagger UI).
#   - Reading JSON from disk and deserialising into a typed model.
#   - 404 when the thread or plan doesn't exist yet.
#
# The frontend calls this on page load to hydrate the progress stepper with the
# latest plan state before real-time events start flowing.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Endpoint 4 (coming next): /copilotkit
#
# This is where CopilotKit's LangGraph adapter will be mounted.  It handles
# SSE streaming, interrupt/resume, and shared state sync automatically.
# We'll add it once the static endpoints are verified working.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

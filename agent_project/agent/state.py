"""Agent state and plan data models."""

from datetime import datetime, timezone
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    result: str | None = None
    tool_result_ids: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:12]}")
    query: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: Literal["draft", "approved", "in_progress", "completed"] = "draft"
    steps: list[PlanStep]


class PlanDraft(BaseModel):
    """Structured planner output. Kept small so the model favors fewer, denser steps."""

    steps: list[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description=(
            "Ordered high-level steps. Prefer 2–4; use 5 only when the task has clearly "
            "separate sub-goals (e.g. unrelated comparisons). Each step may use multiple "
            "tool rounds (search, retrieve, python, etc.) — do not split one logical unit across steps."
        ),
    )


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]   # Append-only per plan; reset on new plan
    session_memory: str          # Persists across plans within a chat session

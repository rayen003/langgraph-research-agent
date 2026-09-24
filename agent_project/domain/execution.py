"""Typed contracts for dynamic case planning and delegated task execution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TaskStatus = Literal["pending", "running", "completed", "failed", "blocked", "needs_input", "needs_approval"]


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    dependency_task_ids: list[str] = Field(default_factory=list)
    input_object_version_ids: list[str] = Field(default_factory=list)
    required_output_types: list[str] = Field(default_factory=list)
    context_query: str = ""
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)


class CasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    deliverables: list[str] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dag(self) -> "CasePlan":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_id values must be unique")
        known = set(task_ids)
        for task in self.tasks:
            unknown = set(task.dependency_task_ids) - known
            if unknown:
                raise ValueError(f"task {task.task_id} has unknown dependencies: {sorted(unknown)}")
            if task.task_id in task.dependency_task_ids:
                raise ValueError(f"task {task.task_id} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {task.task_id: task.dependency_task_ids for task in self.tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in dependencies[task_id]:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class TaskPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    session_id: str = ""
    thread_id: str = ""
    task: TaskSpec
    dependency_results: list[dict[str, Any]] = Field(default_factory=list)
    evidence_pack: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    playbook_id: str | None = None


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    case_id: str
    capability_id: str
    status: TaskStatus
    summary: str = ""
    output_object_version_ids: list[str] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    discovered_requirements: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


def ready_tasks(plan: CasePlan, results: dict[str, dict[str, Any]]) -> list[TaskSpec]:
    """Return pending tasks whose dependencies completed successfully."""
    completed = {
        task_id
        for task_id, result in results.items()
        if isinstance(result, dict) and result.get("status") == "completed"
    }
    terminal = {task_id for task_id, result in results.items() if isinstance(result, dict)}
    return [
        task
        for task in plan.tasks
        if task.task_id not in terminal and set(task.dependency_task_ids) <= completed
    ]


def merge_case_task_results(
    current: dict[str, dict[str, Any]] | None,
    updates: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """LangGraph reducer: merge parallel task results by stable task ID."""
    updates = dict(updates or {})
    if updates.pop("__reset__", False):
        return updates
    return {**(current or {}), **updates}

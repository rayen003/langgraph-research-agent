"""Single seam for plan + report persistence.

Every plan mutation flows through here.  Callers never think about disk vs
SQLite — one call does both.  Tool results remain disk-only (large payloads,
read-on-demand) and are handled by ``utils.persist_tool_result``.
"""

from __future__ import annotations

import json
from pathlib import Path

from storage import store_report as _store_report, sync_job_steps as _sync_job_steps, update_job_step as _update_job_step
from utils import get_run_dir


def save_plan(thread_id: str, plan: dict) -> str:
    """Persist the full plan to disk AND sync every step to SQLite job_steps.

    Returns the absolute path of the written plan file.
    """
    # Disk
    plans_dir = get_run_dir() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan['plan_id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))

    # SQLite
    _sync_job_steps(thread_id, plan)

    return str(path)


def update_step(
    thread_id: str,
    plan: dict,
    step_id: str,
    *,
    status: str | None = None,
    result: str | None = None,
    tool_result_ids: list[str] | None = None,
) -> None:
    """Mutate a step in *plan*, persist the plan to disk, and update SQLite.

    ``plan`` is mutated in-place (the step's ``status``, ``result``, and/or
    ``tool_result_ids`` are set) and then written back.
    """
    # Mutate the step in the plan dict
    matched = False
    for step in plan.get("steps", []):
        if step.get("id") == step_id:
            if status is not None:
                step["status"] = status
            if result is not None:
                step["result"] = result
            if tool_result_ids is not None:
                step["tool_result_ids"] = tool_result_ids
            matched = True
            break

    if not matched:
        return

    # Persist plan to disk
    plans_dir = get_run_dir() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan['plan_id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))

    # Persist step to SQLite
    _update_job_step(
        thread_id,
        step_id,
        status=status,
        result=result,
        tool_result_ids=tool_result_ids,
    )


def save_report(
    thread_id: str,
    session_id: str,
    objective: str,
    content: str,
) -> str:
    """Write the final report markdown to disk AND store it in SQLite.

    Returns the absolute path of the report file.
    """
    # Disk
    path = get_run_dir() / "final_report.md"
    path.write_text(content, encoding="utf-8")

    # SQLite
    _store_report(
        thread_id=thread_id,
        session_id=session_id,
        objective=objective,
        content=content,
        report_path=str(path),
    )

    return str(path)

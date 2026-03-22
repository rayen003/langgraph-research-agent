"""Filesystem persistence helpers for runs, plans, tool results, and artifacts."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rich.console import Console

console = Console()
BASE_DIR = Path(__file__).parent.parent  # agent_project/
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

_current_thread_id: str | None = None


def set_thread_id(thread_id: str) -> Path:
    """Set the active thread and create its run directory structure."""
    global _current_thread_id  # noqa: PLW0603
    _current_thread_id = thread_id
    run_dir = RUNS_DIR / thread_id
    (run_dir / "tool_results").mkdir(parents=True, exist_ok=True)
    (run_dir / "context_items").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    return run_dir


def get_run_dir() -> Path:
    if _current_thread_id is None:
        fallback = RUNS_DIR / "_default"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return RUNS_DIR / _current_thread_id


def save_plan(plan: dict) -> str:
    plans_dir = get_run_dir() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan['plan_id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return str(path)


def get_artifacts_dir() -> Path:
    artifacts_dir = get_run_dir() / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return artifacts_dir


def save_artifact_file(remote_path: str, content: bytes) -> str:
    """Persist a downloaded artifact into the active run directory."""
    artifacts_dir = get_artifacts_dir()
    safe_name = remote_path.strip("/").replace("/", "_") or f"artifact_{uuid4().hex[:8]}"
    destination = artifacts_dir / safe_name
    if destination.exists():
        destination = artifacts_dir / f"{destination.stem}_{uuid4().hex[:6]}{destination.suffix}"
    destination.write_bytes(content)
    return str(destination)


def list_artifact_paths() -> list[str]:
    """List artifacts relative to the run directory for markdown linking."""
    run_dir = get_run_dir()
    artifacts_dir = get_artifacts_dir()
    return sorted(
        str(path.relative_to(run_dir))
        for path in artifacts_dir.iterdir()
        if path.is_file()
    )


def save_final_report(markdown: str) -> str:
    path = get_run_dir() / "final_report.md"
    path.write_text(markdown, encoding="utf-8")
    return str(path)


def persist_tool_result(tool_name: str, args: dict, result: str, summary: str) -> str:
    result_id = f"{tool_name}_{uuid4().hex[:12]}"
    tool_dir = get_run_dir() / "tool_results"
    tool_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool_result_id": result_id,
        "tool_name": tool_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "summary": summary,
        "result": result,
    }
    file_path = tool_dir / f"{result_id}.json"
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return json.dumps(
        {
            "tool_result_id": result_id,
            "tool_name": tool_name,
            "summary": summary,
            "stored_at": str(file_path),
            "hint": "Call retrieve_tool_result with this tool_result_id to read the full content.",
        },
        ensure_ascii=False,
    )


def persist_context_item(
    title: str,
    content: str,
    kind: str,
    step_id: str | None = None,
    tool_result_ids: list[str] | None = None,
) -> dict:
    """Persist full context content to disk and return stack metadata pointer."""
    item_id = f"{kind}_{uuid4().hex[:12]}"
    ctx_dir = get_run_dir() / "context_items"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "context_item_id": item_id,
        "kind": kind,
        "title": title,
        "step_id": step_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "content": content,
        "tool_result_ids": tool_result_ids or [],
    }
    file_path = ctx_dir / f"{item_id}.json"
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return {
        "context_item_id": item_id,
        "kind": kind,
        "title": title,
        "step_id": step_id,
        "stored_at": str(file_path),
    }


def has_pending_steps(plan: dict | None) -> bool:
    if not plan:
        return False
    return any(step["status"] == "pending" for step in plan["steps"])


def get_next_pending_step(plan: dict) -> dict | None:
    for step in plan["steps"]:
        if step["status"] == "pending":
            return step
    return None


def mark_step(plan: dict, step_id: str, status: str, result: str | None = None) -> dict:
    for step in plan["steps"]:
        if step["id"] == step_id:
            step["status"] = status
            if result is not None:
                step["result"] = result
            break
    return plan

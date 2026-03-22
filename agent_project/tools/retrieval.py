"""Retrieval tools: retrieve_context and retrieve_tool_result."""

import json

from langchain_core.tools import tool

from utils.persistence import get_run_dir


@tool
def retrieve_context(step_id: str) -> str:
    """Retrieve a prior step's summary and tool-result pointers from the saved plan."""
    plans_dir = get_run_dir() / "plans"
    if not plans_dir.exists():
        return json.dumps({"step_id": step_id, "matches": []})
    plan_files = sorted(plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for plan_file in plan_files:
        try:
            payload = json.loads(plan_file.read_text())
        except json.JSONDecodeError:
            continue
        for step in payload.get("steps", []):
            if step.get("id") == step_id:
                return json.dumps(
                    {
                        "step_id": step_id,
                        "matches": [
                            {
                                "step_id": step.get("id"),
                                "description": step.get("description"),
                                "status": step.get("status"),
                                "result": step.get("result"),
                                "tool_result_ids": step.get("tool_result_ids", []),
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
    return json.dumps({"step_id": step_id, "matches": []}, ensure_ascii=False)


@tool
def retrieve_tool_result(tool_result_id: str) -> str:
    """Read the full content of a previously stored tool result by its tool_result_id."""
    tool_dir = get_run_dir() / "tool_results"
    file_path = tool_dir / f"{tool_result_id}.json"
    if not file_path.exists():
        return json.dumps({"error": f"No result found for id '{tool_result_id}'", "tool_result_id": tool_result_id})
    try:
        payload = json.loads(file_path.read_text())
    except json.JSONDecodeError:
        return json.dumps({"error": "Corrupt file", "tool_result_id": tool_result_id})
    return json.dumps(payload, ensure_ascii=False)

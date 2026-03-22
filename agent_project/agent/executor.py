"""Step executor: LLM ↔ tool loop and report post-processing helpers."""

import json
import re
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent.prompts import STATIC_SYSTEM_PROMPT, build_step_message
from tools import MAX_SEARCHES_PER_STEP, MAX_TOOL_ROUNDS, TOOLS_BY_NAME, agent_llm
from utils.events import emit_ui_event
from utils.formatting import format_tool_call, format_tool_error, format_tool_result


def _normalize_tool_args(args: dict) -> dict:
    """Handle malformed tool args from some models (e.g. {"parameters": {}})."""
    if not isinstance(args, dict):
        return {}
    if isinstance(args.get("parameters"), dict) and len(args) == 1:
        return args["parameters"]
    return args


def _format_context_stack(context_stack: list[dict]) -> str:
    """Format the append-only context stack for prompt injection."""
    if not context_stack:
        return "none"
    lines: list[str] = []
    for entry in context_stack:
        step_id = entry.get("step_id", "?")
        summary = (entry.get("summary") or "").replace("\n", " ")
        tool_ids = entry.get("tool_result_ids", [])
        tool_line = ", ".join(tool_ids) if tool_ids else "none"
        lines.append(f"- {step_id}: {summary}\n  tool_result_ids: {tool_line}")
    return "\n".join(lines)


def execute_step(
    plan: dict,
    step: dict,
    objective: str,
    review_feedback: str | None,
    plan_trajectory: str,
    previous_step: str,
    next_step: str,
    context_stack: list[dict],
) -> tuple[str, list[str]]:
    """Run one plan step: LLM ↔ tool loop until the model stops calling tools.
    Returns (result_text, tool_result_ids).
    """
    context_stack_formatted = _format_context_stack(context_stack)
    step_message = build_step_message(
        objective, step, review_feedback,
        plan_trajectory, previous_step, next_step,
        context_stack_formatted,
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=STATIC_SYSTEM_PROMPT),
        HumanMessage(content=step_message),
    ]

    search_count = 0
    tool_result_ids: set[str] = set()

    for _ in range(MAX_TOOL_ROUNDS):
        accumulated: AIMessageChunk | None = None
        reasoning_tokens: list[str] = []
        for chunk in agent_llm.stream(messages):
            if not isinstance(chunk, AIMessageChunk):
                continue
            accumulated = chunk if accumulated is None else accumulated + chunk
            if isinstance(chunk.content, str) and chunk.content:
                reasoning_tokens.append(chunk.content)

        if accumulated is None:
            break

        response = AIMessage(
            content=accumulated.content if isinstance(accumulated.content, str) else "",
            tool_calls=getattr(accumulated, "tool_calls", []) or [],
            id=getattr(accumulated, "id", None),
        )
        messages.append(response)

        reasoning_text = "".join(reasoning_tokens).strip()
        if reasoning_text and response.tool_calls:
            emit_ui_event({
                "type": "step_reasoning",
                "step_id": step["id"],
                "text": reasoning_text,
            })

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            tool_fn = TOOLS_BY_NAME.get(tc["name"])
            args = _normalize_tool_args(tc.get("args", {}))

            if tc["name"] == "search_web" and search_count >= MAX_SEARCHES_PER_STEP:
                result = json.dumps({
                    "error": f"Search budget exhausted ({MAX_SEARCHES_PER_STEP} calls). "
                             "Use retrieve_tool_result to read full content from earlier searches.",
                })
                format_tool_error(tc["name"], f"budget exhausted ({search_count}/{MAX_SEARCHES_PER_STEP})")
                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
                continue

            format_tool_call(tc["name"], args)
            emit_ui_event({
                "type": "tool_call_start",
                "step_id": step["id"],
                "tool_name": tc["name"],
                "args_preview": json.dumps(args, ensure_ascii=False)[:150],
            })
            if not tool_fn:
                result = json.dumps({"error": f"unknown tool: {tc['name']}"})
                format_tool_error(tc["name"], "unknown tool")
                emit_ui_event({"type": "tool_error", "step_id": step["id"], "tool_name": tc["name"], "error": "unknown tool"})
            else:
                try:
                    result = tool_fn.invoke(args)
                    format_tool_result(result)
                    if tc["name"] == "search_web":
                        search_count += 1
                    evt_summary = ""
                    evt_tool_result_id = ""
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict):
                            if parsed.get("tool_result_id"):
                                tool_result_ids.add(parsed["tool_result_id"])
                                evt_tool_result_id = parsed["tool_result_id"]
                            evt_summary = parsed.get("summary", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    emit_ui_event({
                        "type": "tool_call_end",
                        "step_id": step["id"],
                        "tool_name": tc["name"],
                        "summary": evt_summary,
                        "tool_result_id": evt_tool_result_id,
                    })
                except Exception as e:  # noqa: BLE001
                    result = json.dumps({"error": str(e), "hint": "Provide required args (e.g. query for search_web)"})
                    format_tool_error(tc["name"], str(e))
                    emit_ui_event({"type": "tool_error", "step_id": step["id"], "tool_name": tc["name"], "error": str(e)})
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    result_text = last_ai.content if last_ai and isinstance(last_ai.content, str) else "Step completed."
    return result_text, list(tool_result_ids)


def _clean_step_output(text: str) -> str:
    """Remove conversational CTAs and closing-marker lines from step/synthesis output."""
    blocked_phrases = ("if you'd like", "if you\u2019d like", "let me know")
    closing_lines = {
        "end of report.",
        "end of report",
        "--- end of report ---",
        "end.",
        "[artifacts]",
        "[artifact]",
        "[chart]",
    }
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        lower = line.strip().lower()
        if lower in closing_lines:
            continue
        if any(phrase in lower for phrase in blocked_phrases):
            continue
        cleaned.append(line)
    while cleaned and cleaned[-1].strip() in {"---", "___", "***"}:
        cleaned.pop()
    return "\n".join(cleaned).strip() or "Step completed."


def _build_artifact_markdown(artifact_paths: list[str]) -> str:
    if not artifact_paths:
        return ""
    lines: list[str] = []
    image_suffixes = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
    for artifact_path in artifact_paths:
        if Path(artifact_path).suffix.lower() in image_suffixes:
            lines.append(f"![{Path(artifact_path).stem}]({artifact_path})")
    return "\n".join(lines)


def _merge_artifacts_into_report(markdown: str, artifact_paths: list[str]) -> str:
    """Insert artifact markdown at an explicit marker or before the Limitations section."""
    artifact_markdown = _build_artifact_markdown(artifact_paths)
    if not artifact_markdown:
        return markdown

    if "[ARTIFACTS]" in markdown:
        return markdown.replace("[ARTIFACTS]", artifact_markdown, 1)

    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        normalized = line.strip().lstrip("#").strip().lower().rstrip(":")
        if normalized.startswith("limitations"):
            before = "\n".join(lines[:idx]).rstrip()
            after = "\n".join(lines[idx:]).lstrip()
            return f"{before}\n\n{artifact_markdown}\n\n{after}".strip()

    return f"{markdown.rstrip()}\n\n{artifact_markdown}"


def _clean_report_wording(text: str) -> str:
    """Normalize awkward artifact phrasing in final reports."""
    cleaned = text
    replacements = [
        ("provided as an artifact", "included below"),
        ("provided as a chart", "included below"),
        ("included as an artifact for reference", "included below for reference"),
        ("included as an artifact", "included below"),
        ("displayed as an artifact", "displayed below"),
        ("shown as an artifact", "shown below"),
        ("artifact to accompany this report", "chart accompanying this report"),
        ("artifacts to accompany this report", "charts accompanying this report"),
    ]
    for old, new in replacements:
        cleaned = re.sub(re.escape(old), new, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bartifacts\b", "charts", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bartifact\b", "chart", cleaned, flags=re.IGNORECASE)
    return cleaned

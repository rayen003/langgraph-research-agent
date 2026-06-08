"""Shared utilities for persistence and streaming-friendly rich formatting."""

import contextlib
import contextvars
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from langchain_core.messages import BaseMessage
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from activity import (
    ACTIVITY_EVENT_TYPE,
    ActivityKind,
    ActivityScope,
    ActivityStatus,
    make_activity,
)

console = Console()
BASE_DIR = Path(__file__).parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

TOOL_OUTPUT_MAX_LEN = 1000
TOOL_CALL_ARGS_MAX_LEN = 180

_thread_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "thread_id",
    default=None,
)

# ---------------------------------------------------------------------------
# UI event hooks (consumed by Chainlit / other frontends)
# ---------------------------------------------------------------------------

_ui_event_handler_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "ui_event_handler",
    default=None,
)

# Process-wide fallback. ContextVars do NOT propagate into threads spawned by
# ``loop.run_in_executor`` or LangGraph's internal sync-node executor, so when a
# nested invocation (e.g. the post-HITL fast path inside ``_direct_dcf_approval``
# called via ``agent_graph.ainvoke`` → threadpool) reads the ctx, it gets
# ``None`` and every ``emit_ui_event`` is silently dropped. This caused workflow
# substeps to vanish from the right sidebar after HITL approval. The fallback
# mirrors the most-recently-registered handler so any thread can still emit.
_ui_event_handler_fallback: Any = None

_dcf_hitl_payload_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "dcf_hitl_payload",
    default=None,
)

_deck_hitl_payload_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "deck_hitl_payload",
    default=None,
)


def set_ui_event_handler(handler: Any) -> None:
    """Register a callback that receives fine-grained execution events.

    Sets BOTH the contextvar (for normal async-only paths) AND the process-wide
    fallback (so nested invocations dispatched through executor threads — where
    contextvars do not propagate — still find the active handler).

    Passing ``None`` clears the contextvar always; it clears the fallback only
    when it matches the previously-registered handler (avoiding a concurrent
    SSE stream wiping another stream's handler on its own ``finally``).
    """
    global _ui_event_handler_fallback
    prev = _ui_event_handler_ctx.get()
    _ui_event_handler_ctx.set(handler)
    if handler is not None:
        _ui_event_handler_fallback = handler
    elif _ui_event_handler_fallback is prev:
        _ui_event_handler_fallback = None


def set_dcf_hitl_payload(payload: dict | None) -> None:
    """Store DCF HITL payload for inter-thread coordination."""
    _dcf_hitl_payload_ctx.set(payload)


def get_dcf_hitl_payload() -> dict | None:
    """Retrieve stored DCF HITL payload."""
    return _dcf_hitl_payload_ctx.get()


def set_deck_hitl_payload(payload: dict | None) -> None:
    """Store deck-workflow HITL payload (outline review) for inter-thread access."""
    _deck_hitl_payload_ctx.set(payload)


def get_deck_hitl_payload() -> dict | None:
    """Retrieve stored deck HITL payload."""
    return _deck_hitl_payload_ctx.get()


def emit_ui_event(event: dict) -> None:
    """Fire an event to the registered UI handler, if any.

    Falls back to the process-wide handler when the contextvar is None — the
    common case for nested sync nodes inside ``agent_graph.ainvoke`` and any
    work pushed through ``loop.run_in_executor``.
    """
    handler = _ui_event_handler_ctx.get() or _ui_event_handler_fallback
    if handler is not None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Activity events — unified contract for tool/workflow/node telemetry.
#
# These helpers are additive: they emit the new `type="activity"` envelope
# defined in agent_project/activity.py. Legacy `tool_call_start/end/error`
# events are still emitted by call sites during the migration window so
# the frontend renders identically in both old and new modes.
# ---------------------------------------------------------------------------


def emit_activity(
    *,
    activity_id: str,
    kind: ActivityKind,
    name: str,
    scope: ActivityScope,
    status: ActivityStatus,
    parent_activity_id: str | None = None,
    display_label: str | None = None,
    step_id: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    summary: str | None = None,
    args_preview: str | None = None,
    confidence_label: str | None = None,
    flag_count: int | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Fire a normalized activity event over the UI bus.

    Prefer the `track_tool` / `track_workflow_step` context managers — they
    handle id generation, lifecycle states, and timing automatically.
    """
    payload = make_activity(
        activity_id=activity_id,
        kind=kind,
        name=name,
        scope=scope,
        status=status,
        parent_activity_id=parent_activity_id,
        display_label=display_label,
        step_id=step_id,
        started_at=started_at,
        ended_at=ended_at,
        summary=summary,
        args_preview=args_preview,
        confidence_label=confidence_label,
        flag_count=flag_count,
        error=error,
        meta=meta,
    )
    emit_ui_event(payload)


@contextlib.contextmanager
def track_tool(
    *,
    name: str,
    scope: ActivityScope,
    step_id: str | None = None,
    args_preview: str | None = None,
    parent_activity_id: str | None = None,
    display_label: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Track a single tool invocation as a unified activity span.

    Emits `started` on entry and either `completed` or `error` on exit.
    Yields a mutable dict the caller can populate with `summary`, `meta`,
    or other fields before the closing event is sent.

    Usage:
        with track_tool(name="search_web", scope="research", step_id=step["id"],
                        args_preview=preview) as span:
            result = tool_fn.invoke(args)
            span["summary"] = parsed_summary
    """
    activity_id = f"tool_{uuid4().hex[:12]}"
    started_at = time.time()
    span: dict[str, Any] = {
        "summary": "",
        "meta": None,
        "args_preview": args_preview or "",
    }

    emit_activity(
        activity_id=activity_id,
        kind="tool",
        name=name,
        scope=scope,
        status="started",
        step_id=step_id,
        started_at=started_at,
        args_preview=args_preview,
        parent_activity_id=parent_activity_id,
        display_label=display_label,
    )

    try:
        yield span
    except Exception as exc:  # noqa: BLE001
        emit_activity(
            activity_id=activity_id,
            kind="tool",
            name=name,
            scope=scope,
            status="error",
            step_id=step_id,
            started_at=started_at,
            ended_at=time.time(),
            error=str(exc),
            args_preview=span.get("args_preview"),
            parent_activity_id=parent_activity_id,
            display_label=display_label,
        )
        raise
    else:
        emit_activity(
            activity_id=activity_id,
            kind="tool",
            name=name,
            scope=scope,
            status="completed",
            step_id=step_id,
            started_at=started_at,
            ended_at=time.time(),
            summary=span.get("summary") or "",
            args_preview=span.get("args_preview"),
            meta=span.get("meta"),
            parent_activity_id=parent_activity_id,
            display_label=display_label,
        )


@contextlib.contextmanager
def track_workflow_step(
    *,
    workflow: str,
    step: str,
    parent_activity_id: str | None = None,
    parent_step_id: str | None = None,
    summary: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Track a single workflow substep as a unified activity span.

    The `name` is encoded as `workflow:<workflow>:<step>` to match the
    existing `getToolDisplay` lookup table in `lib/toolLabels.ts`. This
    keeps the human-readable label working without UI changes.
    """
    activity_id = f"wf_{uuid4().hex[:12]}"
    started_at = time.time()
    span: dict[str, Any] = {"summary": summary or "", "meta": None}

    emit_activity(
        activity_id=activity_id,
        kind="workflow_step",
        name=f"workflow:{workflow}:{step}",
        scope="workflow",
        status="started",
        step_id=parent_step_id,
        parent_activity_id=parent_activity_id,
        started_at=started_at,
        summary=summary,
    )

    try:
        yield span
    except Exception as exc:  # noqa: BLE001
        emit_activity(
            activity_id=activity_id,
            kind="workflow_step",
            name=f"workflow:{workflow}:{step}",
            scope="workflow",
            status="error",
            step_id=parent_step_id,
            parent_activity_id=parent_activity_id,
            started_at=started_at,
            ended_at=time.time(),
            error=str(exc),
        )
        raise
    else:
        emit_activity(
            activity_id=activity_id,
            kind="workflow_step",
            name=f"workflow:{workflow}:{step}",
            scope="workflow",
            status="completed",
            step_id=parent_step_id,
            parent_activity_id=parent_activity_id,
            started_at=started_at,
            ended_at=time.time(),
            summary=span.get("summary") or "",
            meta=span.get("meta"),
        )


def set_thread_id(thread_id: str) -> Path:
    """Set the active thread and create its run directory structure."""
    _thread_id_ctx.set(thread_id)
    run_dir = RUNS_DIR / thread_id
    (run_dir / "tool_results").mkdir(parents=True, exist_ok=True)
    (run_dir / "context_items").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    return run_dir


def get_run_dir() -> Path:
    current_thread_id = _thread_id_ctx.get()
    if current_thread_id is None:
        fallback = RUNS_DIR / "_default"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return RUNS_DIR / current_thread_id


def get_artifacts_dir() -> Path:
    artifacts_dir = get_run_dir() / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return artifacts_dir


def save_artifact_file(remote_path: str, content: bytes) -> str:
    """Persist a downloaded sandbox artifact into the active run directory."""
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
    return sorted(str(path.relative_to(run_dir)) for path in artifacts_dir.iterdir() if path.is_file())


def relative_run_path(path: str | Path | None) -> str | None:
    """Return a run-relative path (e.g. ``decks/foo.pptx``) for API linking."""
    if not path:
        return None
    p = Path(path)
    run_dir = get_run_dir()
    try:
        return str(p.resolve().relative_to(run_dir.resolve()))
    except ValueError:
        parts = p.parts
        if "decks" in parts:
            idx = parts.index("decks")
            return str(Path(*parts[idx:]))
        return None


def find_latest_deck_pptx(run_dir: Path | None = None) -> Path | None:
    """Return the newest PPTX under ``runs/<thread>/decks/``."""
    base = run_dir or get_run_dir()
    decks_dir = base / "decks"
    if not decks_dir.is_dir():
        return None
    candidates = list(decks_dir.glob("*.pptx"))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def list_deck_artifact_paths(run_dir: Path | None = None) -> list[str]:
    """Deck PPTX paths relative to the run directory."""
    base = run_dir or get_run_dir()
    latest = find_latest_deck_pptx(base)
    if latest is None:
        return []
    try:
        return [str(latest.relative_to(base))]
    except ValueError:
        return [f"decks/{latest.name}"]


def run_dir_for_thread(thread_id: str) -> Path:
    """Absolute run directory for a chat/workflow thread id."""
    return RUNS_DIR / thread_id


def _adopt_deck_dir_from_fallback(run_dir: Path) -> Path:
    """Copy deck artifacts from ``runs/_default/decks`` into ``run_dir/decks``."""
    fallback = RUNS_DIR / "_default" / "decks"
    if not fallback.is_dir():
        return run_dir / "decks"
    dest = run_dir / "decks"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("deck_output.json",):
        src = fallback / name
        if src.is_file() and not (dest / name).exists():
            shutil.copy2(src, dest / name)
    for src in fallback.glob("*.pptx"):
        dest_file = dest / src.name
        if not dest_file.exists():
            shutil.copy2(src, dest_file)
    return dest


def resolve_deck_pptx_path(thread_id: str, filename: str) -> Path | None:
    """Locate a deck PPTX for API download; adopts from ``_default`` when needed."""
    safe_name = Path(filename).name
    if safe_name != filename:
        return None
    run_dir = run_dir_for_thread(thread_id)
    primary = run_dir / "decks" / safe_name
    if primary.exists():
        return primary

    fallback = RUNS_DIR / "_default" / "decks" / safe_name
    if fallback.exists():
        adopted = _adopt_deck_dir_from_fallback(run_dir) / safe_name
        if adopted.exists():
            return adopted
        return fallback

    latest = find_latest_deck_pptx(run_dir)
    if latest is not None and latest.name == safe_name:
        return latest
    return None


def resolve_deck_output_path(thread_id: str) -> Path | None:
    """Locate ``deck_output.json`` for slide preview."""
    run_dir = run_dir_for_thread(thread_id)
    primary = run_dir / "decks" / "deck_output.json"
    if primary.exists():
        return primary

    fallback = RUNS_DIR / "_default" / "decks" / "deck_output.json"
    if fallback.exists():
        adopted = _adopt_deck_dir_from_fallback(run_dir) / "deck_output.json"
        if adopted.exists():
            return adopted
        return fallback
    return None


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


def format_tool_call(tool_name: str, args: dict) -> None:
    """Print a structured tool-call trace line via agent_log."""
    import agent_log  # noqa: PLC0415
    args_str = json.dumps(args, ensure_ascii=False)
    if len(args_str) > TOOL_CALL_ARGS_MAX_LEN:
        args_str = args_str[:TOOL_CALL_ARGS_MAX_LEN] + "…"
    agent_log.tool_call(tool_name, args_str, depth=1)


def format_tool_result(result_str: str) -> None:
    """Extract a human-readable summary from a tool result and log it."""
    import agent_log  # noqa: PLC0415
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        payload = None

    if isinstance(payload, dict):
        if "result" in payload:
            raw = payload.get("result", "")
            snippet = str(raw).strip().replace("\n", " ")[:160]
            agent_log.info(f"↳ [green]retrieved[/green] {snippet}{'…' if len(str(raw)) > 160 else ''}", depth=2)
            return
        summary = payload.get("summary", "")
        stored = payload.get("stored_at", "")
        if summary:
            agent_log.info(f"↳ {summary}" + (f"  [dim]{stored}[/dim]" if stored else ""), depth=2)
        return

    short = str(result_str).strip().replace("\n", " ")[:160]
    agent_log.info(f"↳ {short}{'…' if len(result_str) > 160 else ''}", depth=2)


def format_tool_error(tool_name: str, error: str) -> None:
    """Print a tool error trace line via agent_log."""
    import agent_log  # noqa: PLC0415
    agent_log.tool_error(tool_name, error, depth=1)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _safe_json_loads(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw.startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_tool_pointer(payload: dict) -> str | None:
    required = {"tool_result_id", "tool_name", "summary", "stored_at"}
    if not required.issubset(payload.keys()):
        return None
    lines = [
        f"Tool: {payload.get('tool_name', 'unknown')}",
        f"Summary: {payload.get('summary', '')}",
        f"Result ID: {payload.get('tool_result_id', '')}",
        f"Stored At: {payload.get('stored_at', '')}",
    ]
    hint = payload.get("retrieval_hint")
    if hint:
        lines.append(f"Hint: {hint}")
    return "\n".join(lines)


def format_message_content(message: BaseMessage | dict) -> str:
    """Convert message content to a compact, streaming-friendly display string."""
    parts: list[str] = []
    tool_calls_processed = False

    content = message.get("content", "") if isinstance(message, dict) else message.content
    parsed_pointer = _safe_json_loads(content)
    if parsed_pointer:
        tool_pointer_str = _format_tool_pointer(parsed_pointer)
        if tool_pointer_str:
            return tool_pointer_str

    if isinstance(content, str):
        parts.append(_truncate(content, TOOL_OUTPUT_MAX_LEN))
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type == "text":
                parts.append(_truncate(str(item.get("text", "")), TOOL_OUTPUT_MAX_LEN))
            elif item_type in {"tool_use", "tool_call"}:
                args = item.get("input", item.get("args", {}))
                args_str = _truncate(json.dumps(args, ensure_ascii=False), TOOL_CALL_ARGS_MAX_LEN)
                parts.append(f"Tool Call: {item.get('name', 'unknown')}")
                parts.append(f"Args: {args_str}")
                parts.append(f"ID: {item.get('id', 'N/A')}")
                tool_calls_processed = True
            else:
                parts.append(_truncate(str(item), TOOL_OUTPUT_MAX_LEN))
    else:
        parts.append(_truncate(str(content), TOOL_OUTPUT_MAX_LEN))

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    if not tool_calls_processed and tool_calls:
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                parts.append(f"Tool Call: {tool_call}")
                continue
            args_str = _truncate(
                json.dumps(tool_call.get("args", {}), ensure_ascii=False),
                TOOL_CALL_ARGS_MAX_LEN,
            )
            parts.append(f"Tool Call: {tool_call.get('name', 'unknown')}")
            parts.append(f"Args: {args_str}")
            parts.append(f"ID: {tool_call.get('id', 'N/A')}")

    return "\n".join(p for p in parts if p).strip()


def format_messages(messages: list[BaseMessage] | BaseMessage | list[dict] | dict) -> None:
    """Format and display message(s) with role-aware styling."""
    if not isinstance(messages, list):
        messages = [messages]
    for message in messages:
        if isinstance(message, dict):
            msg_type = (message.get("role", "message") or "message").capitalize()
        else:
            msg_type = message.__class__.__name__.replace("Message", "")

        title = "📝 " + msg_type
        border_style = "white"
        if msg_type == "Human":
            title = "🧑 Human"
            border_style = "blue"
        elif msg_type in {"Ai", "AI", "Assistant"}:
            title = "🤖 Assistant"
            border_style = "green"
        elif msg_type == "Tool":
            title = "🔧 Tool Output"
            border_style = "yellow"
        elif msg_type == "System":
            title = "⚙️ System"
            border_style = "magenta"
        console.print(Panel(format_message_content(message), title=title, border_style=border_style))


def format_message(messages: list[BaseMessage] | BaseMessage | list[dict] | dict) -> None:
    """Backward-compatible alias."""
    format_messages(messages)


_STEP_STATUS_COLOR = {
    "pending": "yellow",
    "in_progress": "cyan",
    "completed": "green",
    "failed": "red",
}
_STEP_STATUS_ICON = {
    "pending": "⏳",
    "in_progress": "🔄",
    "completed": "✅",
    "failed": "❌",
}
_PLAN_STATUS_COLOR = {
    "draft": "blue",
    "approved": "green",
    "in_progress": "cyan",
    "completed": "green",
}


def format_plan(plan: dict) -> None:
    """Display a plan with Rich formatting, colour-coded by step status."""
    if not plan:
        return

    plan_status = plan.get("status", "draft")
    plan_color = _PLAN_STATUS_COLOR.get(plan_status, "white")

    header = (
        f"[bold]Plan ID:[/bold]  {plan.get('plan_id', 'N/A')}\n"
        f"[bold]Query:[/bold]    {plan.get('query', 'N/A')}\n"
        f"[bold]Status:[/bold]   [{plan_color}]{plan_status.upper()}[/{plan_color}]\n"
        f"[bold]Created:[/bold]  {plan.get('created_at', 'N/A')}"
    )
    console.print(Panel(header, title="📋 Plan", border_style="blue", padding=(0, 2)))

    for step in plan.get("steps", []):
        step_id = step.get("id", "?")
        status = step.get("status", "pending")
        description = step.get("description", "")
        depends_on = step.get("depends_on", [])
        result = step.get("result")

        color = _STEP_STATUS_COLOR.get(status, "white")
        icon = _STEP_STATUS_ICON.get(status, "•")

        lines: list[str] = [
            f"[{color}]{icon}  {status.upper()}[/{color}]",
            f"[white]{description}[/white]",
        ]
        if depends_on:
            lines.append(f"[dim]depends on: {', '.join(depends_on)}[/dim]")
        if result:
            short = result.strip().replace("\n", " ")
            short = short[:200] + ("..." if len(short) > 200 else "")
            lines.append(f"[dim italic]↳ {short}[/dim italic]")

        console.print(Panel(
            "\n".join(lines),
            title=f"[bold]{step_id}[/bold]",
            border_style=color,
            padding=(0, 1),
        ))


def show_prompt(prompt_text: str, title: str = "Prompt", border_style: str = "blue") -> None:
    """Display a prompt in a Rich panel with lightweight syntax highlighting."""
    formatted = Text(prompt_text)
    formatted.highlight_regex(r"<[^>]+>", style="bold blue")
    formatted.highlight_regex(r"##[^#\n]+", style="bold magenta")
    formatted.highlight_regex(r"###[^#\n]+", style="bold cyan")
    console.print(
        Panel(
            formatted,
            title=f"[bold green]{title}[/bold green]",
            border_style=border_style,
            padding=(1, 2),
        )
    )


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


async def stream_agent(agent, query, config=None):
    """Stream graph execution with updates/messages/values and rich formatting."""
    current_state = None
    async for graph_name, stream_mode, event in agent.astream(
        query,
        stream_mode=["updates", "messages", "values"],
        subgraphs=True,
        config=config,
    ):
        if stream_mode == "updates":
            graph_label = graph_name if len(graph_name) > 0 else "root"
            console.print(f"\n[bold cyan]Graph:[/bold cyan] {graph_label}")
            if isinstance(event, dict) and event:
                node, result = list(event.items())[0]
                console.print(f"[bold cyan]Node:[/bold cyan] {node}")
                if isinstance(result, dict):
                    for key, value in result.items():
                        if "messages" in key:
                            format_messages(value)
                            break
        elif stream_mode == "messages":
            msg_chunk, _ = event
            if getattr(msg_chunk, "content", None):
                print(msg_chunk.content, end="", flush=True)
        elif stream_mode == "values":
            current_state = event
    return current_state

"""Rich console formatting helpers."""

import json
from typing import Any

from langchain_core.messages import BaseMessage
from rich.panel import Panel
from rich.text import Text

from utils.persistence import console

TOOL_OUTPUT_MAX_LEN = 1000
TOOL_CALL_ARGS_MAX_LEN = 180


def format_tool_call(tool_name: str, args: dict) -> None:
    args_str = json.dumps(args, ensure_ascii=False)
    if len(args_str) > TOOL_CALL_ARGS_MAX_LEN:
        args_str = args_str[:TOOL_CALL_ARGS_MAX_LEN] + "..."
    console.print(f"  [dim]🔧 {tool_name}({args_str})[/dim]")


def format_tool_result(result_str: str) -> None:
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        short = str(result_str).strip().replace("\n", " ")[:200]
        console.print(f"  [dim]  ↳ {short}{'...' if len(result_str) > 200 else ''}[/dim]")
        return
    if "result" in payload:
        raw = payload.get("result", "")
        if isinstance(raw, str):
            snippet = raw.strip().replace("\n", " ")[:280]
            console.print(f"  [dim]  ↳ [green]Retrieved:[/green] {snippet}{'...' if len(raw) > 280 else ''}[/dim]")
        else:
            console.print(f"  [dim]  ↳ [green]Retrieved:[/green] {str(raw)[:200]}...[/dim]")
        return
    summary = payload.get("summary", "")
    stored = payload.get("stored_at", "")
    console.print(f"  [dim]  ↳ {summary}[/dim]")
    if stored:
        console.print(f"  [dim]    📁 {stored}[/dim]")


def format_tool_error(tool_name: str, error: str) -> None:
    console.print(f"  [red]  ✗ {tool_name} error: {error}[/red]")


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

"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

# Ensure agent_project is importable (tests run from project root).
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "agent_project"))

# Stub OPENAI_API_KEY so ChatOpenAI doesn't raise at import time.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "payloads"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-dcf-e2e",
        action="store_true",
        default=False,
        help="Run live DCF workflow E2E tests and write timestamped artifacts.",
    )
    parser.addoption(
        "--run-live-agent-e2e",
        action="store_true",
        default=False,
        help="Run live LangGraph agent thread E2E tests that call LLM providers.",
    )
    parser.addoption(
        "--agent-trace",
        action="store_true",
        default=False,
        help="Print coloured agent routing/playbook/tool traces from selected unit tests.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: opt-in end-to-end tests that may call LLMs/external APIs",
    )
    config.addinivalue_line(
        "markers",
        "llm: opt-in tests that call LLM providers",
    )
    config.addinivalue_line(
        "markers",
        "contract: deterministic graph-contract tests with scripted model boundaries",
    )


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}. Run `make capture-fixtures` first.")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def aapl_payload() -> dict:
    """Full DCF output payload for AAPL (captured from live run)."""
    return _load_fixture("valid_aapl.json")


@pytest.fixture(scope="session")
def invalid_solver_payload() -> dict:
    """Payload with model_validity=invalid (hand-crafted)."""
    return _load_fixture("invalid_solver.json")


@pytest.fixture
def agent_trace(
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[str, list[dict[str, Any]], dict[str, Any] | None], None]:
    enabled = bool(request.config.getoption("--agent-trace") or os.getenv("AGENT_TRACE"))

    def _print(
        title: str,
        events: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not enabled:
            return

        console = Console(force_terminal=True, color_system="truecolor", width=120, emoji=False)
        table = Table(title="Execution Steps", show_header=True, header_style="bold cyan")
        table.add_column("#", style="dim", width=4)
        table.add_column("Stage", style="bold green")
        table.add_column("Route", style="magenta")
        table.add_column("Playbook", style="yellow")
        table.add_column("Next", style="bold blue")
        table.add_column("Details", style="white", overflow="fold")

        execution_events = [event for event in events if event.get("type") == "execution_step"]
        for idx, event in enumerate(execution_events, 1):
            allowed_tools = event.get("allowed_tools")
            tool_calls = event.get("tool_calls")
            object_ids = event.get("object_ids")
            citation_ids = event.get("citation_ids")
            details = [
                f"latency={event.get('latency_class')}" if event.get("latency_class") else "",
                f"allowed={', '.join(allowed_tools)}" if isinstance(allowed_tools, list) and allowed_tools else "",
                f"calls={', '.join(tool_calls)}" if isinstance(tool_calls, list) and tool_calls else "",
                f"objects={', '.join(object_ids)}" if isinstance(object_ids, list) and object_ids else "",
                f"citations={', '.join(citation_ids)}" if isinstance(citation_ids, list) and citation_ids else "",
                f"status={event.get('status')}" if event.get("status") else "",
                f"reason={event.get('reason')}" if event.get("reason") else "",
            ]
            table.add_row(
                str(idx),
                str(event.get("stage") or ""),
                str(event.get("route_level") or ""),
                str(event.get("playbook_id") or ""),
                str(event.get("next_node") or ""),
                "\n".join(detail for detail in details if detail),
            )

        with capsys.disabled():
            console.print()
            console.print(Panel.fit(title, style="bold white on dark_green"))
            console.print(table)
            if payload is not None:
                payload_json = json.dumps(payload, default=str, indent=2, sort_keys=True)
                console.print(Panel(JSON(payload_json), title="Payload", border_style="cyan"))

    return _print


@pytest.fixture
def agent_thread_trace(
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[str, list[dict[str, Any]]], None]:
    enabled = bool(request.config.getoption("--agent-trace") or os.getenv("AGENT_TRACE"))

    def _short(value: Any, limit: int = 360) -> str:
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _route_table(events: list[dict[str, Any]]) -> Table:
        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("Stage", style="bold green")
        table.add_column("Route", style="magenta")
        table.add_column("Playbook", style="yellow")
        table.add_column("Next", style="bold blue")
        table.add_column("Detail", style="white", overflow="fold")
        for event in events:
            if event.get("type") != "execution_step":
                continue
            details = [
                f"latency={event.get('latency_class')}" if event.get("latency_class") else "",
                f"allowed={', '.join(event.get('allowed_tools') or [])}" if event.get("allowed_tools") else "",
                f"objects={', '.join(event.get('object_ids') or [])}" if event.get("object_ids") else "",
                f"reason={event.get('reason')}" if event.get("reason") else "",
            ]
            table.add_row(
                str(event.get("stage") or ""),
                str(event.get("route_level") or ""),
                str(event.get("playbook_id") or ""),
                str(event.get("next_node") or ""),
                "\n".join(detail for detail in details if detail),
            )
        return table

    def _event_tree(turn: dict[str, Any]) -> Tree:
        tree = Tree("[bold white]Trace[/bold white]")
        input_node = tree.add("[bold cyan]input[/bold cyan]")
        input_node.add(_short(turn.get("input") or ""))

        route_node = tree.add("[bold magenta]routing[/bold magenta]")
        route_events = [e for e in turn.get("events", []) if e.get("type") == "route_decision"]
        if route_events:
            for event in route_events:
                route_node.add(
                    f"{event.get('route_level')} / {event.get('playbook_id')} / "
                    f"{event.get('selected_workflow')} | {event.get('reason')}"
                )
        else:
            route_node.add("[dim]none[/dim]")

        memory_node = tree.add("[bold yellow]memory[/bold yellow]")
        memory_events = [
            e for e in turn.get("events", [])
            if e.get("type") == "execution_step" and e.get("stage") == "build_memory_context"
        ]
        if memory_events:
            for event in memory_events:
                memory_node.add(
                    f"policy={str(event.get('reason') or '').replace('memory_policy=', '')} "
                    f"objects={event.get('object_ids') or []} citations={event.get('citation_ids') or []}"
                )
        else:
            memory_node.add("[dim]none[/dim]")

        tools_node = tree.add("[bold green]tool calls[/bold green]")
        activities = [e for e in turn.get("events", []) if e.get("type") == "activity"]
        if activities:
            for event in activities:
                label = (
                    f"{event.get('kind')}:{event.get('name')} "
                    f"status={event.get('status')}"
                )
                node = tools_node.add(label)
                if event.get("args_preview"):
                    node.add(f"args={event.get('args_preview')}")
                if event.get("summary"):
                    node.add(f"summary={event.get('summary')}")
                if event.get("error"):
                    node.add(f"error={event.get('error')}")
        else:
            tools_node.add("[dim]none[/dim]")

        delegation_node = tree.add("[bold magenta]delegation DAG[/bold magenta]")
        dag = turn.get("dag") or {}
        if dag:
            delegation_node.add(f"case={dag.get('case_id')} status={dag.get('status')}")
            for task in dag.get("tasks") or []:
                delegation_node.add(
                    f"{task.get('task_id')} [{task.get('status')}] "
                    f"capability={task.get('capability_id')} deps={task.get('dependencies') or []}"
                )
        else:
            task_events = [
                event for event in turn.get("events", [])
                if event.get("type") == "execution_step" and event.get("stage") in {
                    "plan_case", "execute_case_task", "collect_case_results", "synthesize_case",
                }
            ]
            if task_events:
                for event in task_events:
                    delegation_node.add(
                        f"{event.get('stage')} status={event.get('status')} "
                        f"objects={event.get('object_ids') or []} reason={event.get('reason') or ''}"
                    )
            else:
                delegation_node.add("[dim]none[/dim]")

        hitl_node = tree.add("[bold red]HITL[/bold red]")
        if turn.get("interrupt"):
            hitl_node.add(f"interrupt={_short(turn['interrupt'])}")
        if turn.get("resume_payload"):
            hitl_node.add(f"resume={_short(turn['resume_payload'])}")
        if not turn.get("interrupt") and not turn.get("resume_payload"):
            hitl_node.add("[dim]none[/dim]")

        objects_node = tree.add("[bold blue]objects[/bold blue]")
        objects = turn.get("objects_written") or []
        if objects:
            for obj in objects:
                objects_node.add(
                    f"{obj.get('object_id')} ({obj.get('object_type')}) "
                    f"title={obj.get('title')}"
                )
        else:
            objects_node.add("[dim]none[/dim]")

        output_node = tree.add("[bold white]output[/bold white]")
        if turn.get("workflow_interrupt"):
            output_node.add(json.dumps(turn["workflow_interrupt"], default=str, ensure_ascii=False))
        else:
            output_node.add(_short(turn.get("output") or ""))
        return tree

    def _print(title: str, turns: list[dict[str, Any]]) -> None:
        if not enabled:
            return
        console = Console(force_terminal=True, color_system="truecolor", width=130, emoji=False)
        with capsys.disabled():
            console.print()
            console.print(Panel.fit(title, style="bold white on dark_green"))
            for idx, turn in enumerate(turns, 1):
                console.print(Panel.fit(f"Turn {idx}", style="bold white on dark_blue"))
                console.print(_event_tree(turn))
                console.print(_route_table(turn.get("events", [])))
                compact_objects = [
                    {
                        key: obj.get(key)
                        for key in (
                            "object_id", "object_type", "version_id", "status", "title",
                            "source_object_ids", "source_version_ids",
                        )
                        if obj.get(key) not in (None, "", [], {})
                    }
                    for obj in turn.get("objects_written") or []
                ]
                route_events = [
                    {
                        key: event.get(key)
                        for key in (
                            "route_level", "playbook_id", "selected_workflow",
                            "selected_case_type", "latency_class", "reason",
                        )
                        if event.get(key) is not None
                    }
                    for event in turn.get("events", [])
                    if event.get("type") == "route_decision"
                ]
                payload = {
                    "input": turn.get("input"),
                    "output": turn.get("output"),
                    "routes": route_events,
                    "dag": turn.get("dag"),
                    "workflow_interrupt": turn.get("workflow_interrupt"),
                    "interrupt": _short(turn.get("interrupt")) if turn.get("interrupt") else None,
                    "resume_payload": turn.get("resume_payload"),
                    "objects_written": compact_objects,
                }
                console.print(Panel(JSON(json.dumps(payload, default=str, ensure_ascii=False, indent=2)), title="Turn Payload", border_style="cyan"))

    return _print

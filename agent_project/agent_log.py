"""
agent_log.py — Structured, color-coded terminal logging for the research agent.

Layout per line:
    HH:MM:SS  {indent}{badge:<12}  {message}  [{duration}]

Hierarchy (depth → indent level):
    depth 0 → RUN/DONE rule, INTENT, CHAT, PLAN, STEP, SYNTH
    depth 1 → TOOL calls (under chat or research step)
    depth 2 → DCF substeps (under run_dcf_workflow tool call)

Color palette:
    RUN/DONE     bold white rule
    INTENT       magenta
    CHAT         cyan
    PLAN         blue
    STEP start   cyan
    STEP done    green
    TOOL call    yellow
    TOOL done    green
    TOOL error   red
    DCF step     magenta
    DCF done     green
    DCF HITL     yellow
    ERROR        bold red
    dim          dim white
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.rule import Rule

_console = Console(highlight=False)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _clip(text: str, n: int = 80) -> str:
    s = str(text).strip().replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


def _row(
    depth: int,
    badge: str,
    badge_style: str,
    msg: str,
    dur: float | None = None,
) -> None:
    """Print one structured log line."""
    ts = f"[dim]{_ts()}[/dim]"
    indent = "  " * depth
    # badge fixed width so columns align
    b = f"[{badge_style}]{badge:<12}[/{badge_style}]"
    d = f"  [dim]{_dur(dur)}[/dim]" if dur is not None else ""
    _console.print(f"{ts}  {indent}{b}  {msg}{d}")


# ── Run lifecycle ─────────────────────────────────────────────────────────────

def run_start(thread_id: str, query: str, mode: str) -> float:
    short = thread_id[-8:]
    _console.print()
    _console.rule(
        f"[bold white] RUN [dim]{short}[/dim]  [white]{_clip(query, 55)}[/white]"
        f"  [dim]{mode}[/dim] [/bold white]",
        style="bright_white",
    )
    return time.time()


def run_done(thread_id: str, started_at: float, status: str = "done") -> None:
    short = thread_id[-8:]
    elapsed = time.time() - started_at
    ok = status == "done"
    style = "bold green" if ok else "bold red"
    label = "DONE" if ok else "ERROR"
    _console.rule(
        f"[{style}] {label} [dim]{short}[/dim]  [dim]{_dur(elapsed)}[/dim] [{style}]",
        style="dim",
    )
    _console.print()


# ── Intent ────────────────────────────────────────────────────────────────────

def intent_classified(intent: str, mode: str, dur: float | None = None) -> None:
    color = "blue" if intent == "research" else "cyan"
    label = f"[bold {color}]{intent}[/bold {color}]"
    mode_str = f"[dim]({mode})[/dim]"
    d = f"  [dim]{_dur(dur)}[/dim]" if dur is not None else ""
    ts = f"[dim]{_ts()}[/dim]"
    b = f"[bold magenta]{'INTENT':<12}[/bold magenta]"
    _console.print(f"{ts}  {b}  → {label}  {mode_str}{d}")


# ── Chat ──────────────────────────────────────────────────────────────────────

def chat_start() -> float:
    _row(0, "CHAT", "bold cyan", "starting")
    return time.time()


def chat_done(summary: str = "", started_at: float | None = None) -> None:
    dur = time.time() - started_at if started_at else None
    msg = f"[dim]{_clip(summary, 55)}[/dim]" if summary else "done"
    _row(0, "CHAT ✓", "bold green", msg, dur)


def chat_hitl(ticker: str) -> None:
    _row(0, "CHAT ⏸", "bold yellow", f"DCF HITL — awaiting review for [bold]{ticker}[/bold]")


# ── Research plan ─────────────────────────────────────────────────────────────

def plan_ready(step_count: int) -> None:
    _row(0, "PLAN", "bold blue", f"[dim]{step_count} step{'s' if step_count != 1 else ''}[/dim]")


def step_start(step_num: int, total: int, step_id: str, description: str) -> float:
    ts = f"[dim]{_ts()}[/dim]"
    nums = f"[bold cyan]STEP {step_num}/{total}[/bold cyan]"
    sid = f"[cyan]{step_id}[/cyan]"
    _console.print(f"{ts}  {nums}  {sid}  [dim]—[/dim]  {_clip(description, 65)}")
    return time.time()


def step_done(step_id: str, result_preview: str, started_at: float) -> None:
    dur = time.time() - started_at
    _row(0, "STEP ✓", "bold green", f"[dim]{step_id}[/dim]  {_clip(result_preview, 55)}", dur)


def step_error(step_id: str, error: str) -> None:
    _row(0, "STEP ✗", "bold red", f"[dim]{step_id}[/dim]  [red]{_clip(error, 70)}[/red]")


def step_hitl_pause(step_id: str, ticker: str) -> None:
    _row(
        0, "STEP ⏸", "bold yellow",
        f"[dim]{step_id}[/dim]  awaiting DCF review  [bold]{ticker}[/bold]",
    )


def step_hitl_resume(step_id: str, ticker: str) -> None:
    _row(
        0, "STEP ▶", "bold cyan",
        f"[dim]{step_id}[/dim]  resumed with DCF overrides  [dim]{ticker}[/dim]",
    )


def step_rejected(step_id: str) -> None:
    _row(0, "STEP ✗", "bold red", f"[dim]{step_id}[/dim]  [red]rejected[/red]")


def synth_start() -> float:
    _row(0, "SYNTH", "bold magenta", "building final report…")
    return time.time()


def synth_done(path: str, started_at: float) -> None:
    _row(0, "SYNTH ✓", "bold green", f"[dim]{path}[/dim]", time.time() - started_at)


# ── Tool calls (depth 1) ──────────────────────────────────────────────────────

def tool_call(tool_name: str, args_preview: str, depth: int = 1) -> float:
    _row(
        depth, "TOOL", "yellow",
        f"[bold yellow]{tool_name}[/bold yellow]  [dim]{_clip(args_preview, 75)}[/dim]",
    )
    return time.time()


def tool_done(
    tool_name: str, summary: str, started_at: float, depth: int = 1
) -> None:
    _row(
        depth, "TOOL ✓", "green",
        f"[green]{tool_name}[/green]  [dim]{_clip(summary, 55)}[/dim]",
        time.time() - started_at,
    )


def tool_error(tool_name: str, error: str, depth: int = 1) -> None:
    _row(
        depth, "TOOL ✗", "bold red",
        f"[red]{tool_name}[/red]  {_clip(error, 75)}",
    )


def tool_budget_exhausted(tool_name: str, count: int, limit: int, depth: int = 1) -> None:
    _row(
        depth, "TOOL ✗", "bold red",
        f"[red]{tool_name}[/red]  [dim]budget exhausted ({count}/{limit})[/dim]",
    )


# ── DCF substeps (depth 2) ────────────────────────────────────────────────────

_DCF_LABELS: dict[str, str] = {
    "normalize_input":      "Resolving ticker & horizon",
    "assemble_evidence":    "Assembling evidence",
    "semantic_synthesis":   "Synthesizing company profile",
    "propose_assumptions":  "Proposing assumptions",
    "assumption_review":    "Reviewing assumptions",
    "collect_market_data":  "Fetching market data",
    "project_cashflows":    "Projecting cash flows",
    "compute_valuation":    "Computing valuation",
    "compute_implied_wacc": "Market-implied WACC check",
    "sensitivity":          "Running sensitivity table",
    "finalize":             "Finalizing result",
}

# timing store keyed by (parent_step_id, step) → start time
_dcf_timings: dict[tuple[str, str], float] = {}


def dcf_step_start(step: str, parent_step_id: str, summary: str = "") -> None:
    _dcf_timings[(parent_step_id, step)] = time.time()
    label = _DCF_LABELS.get(step, step.replace("_", " ").title())
    extra = f"  [dim]{_clip(summary, 45)}[/dim]" if summary else ""
    _row(2, "DCF", "magenta", f"[magenta]{label}[/magenta]{extra}")


def dcf_step_done(
    step: str,
    parent_step_id: str,
    summary: str = "",
    status: str = "completed",
) -> None:
    started = _dcf_timings.pop((parent_step_id, step), None)
    dur = time.time() - started if started is not None else None
    label = _DCF_LABELS.get(step, step.replace("_", " ").title())

    if status in {"awaiting_input"}:
        _row(2, "DCF ⏸", "bold yellow", f"[yellow]{label}[/yellow]  [dim]awaiting HITL[/dim]")
        return
    if status in {"rejected", "error"}:
        _row(2, "DCF ✗", "bold red", f"[red]{label}[/red]  [dim]{_clip(summary, 45)}[/dim]", dur)
        return
    if status == "skipped":
        _row(2, "DCF –", "dim", f"[dim]{label}  skipped[/dim]")
        return

    # completed / approved / edited / fallback
    _row(2, "DCF ✓", "green", f"[dim]{label}[/dim]  [dim]{_clip(summary, 45)}[/dim]", dur)


# ── General ───────────────────────────────────────────────────────────────────

def info(msg: str, depth: int = 0) -> None:
    _console.print(f"[dim]{_ts()}[/dim]  {'  ' * depth}[dim]{msg}[/dim]")


def warning(msg: str, depth: int = 0) -> None:
    _console.print(f"[dim]{_ts()}[/dim]  {'  ' * depth}[bold yellow]⚠  {msg}[/bold yellow]")


def error(msg: str, depth: int = 0) -> None:
    _console.print(f"[dim]{_ts()}[/dim]  {'  ' * depth}[bold red]✗  {msg}[/bold red]")

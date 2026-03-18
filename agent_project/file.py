import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

import dotenv
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from simpleeval import simple_eval

from rich.panel import Panel

from utils import (
    console,
    emit_ui_event,
    format_messages,
    format_plan,
    format_tool_call,
    format_tool_error,
    format_tool_result,
    get_artifacts_dir,
    get_run_dir,
    list_artifact_paths,
    persist_tool_result,
    save_plan,
    save_artifact_file,
    save_final_report,
    set_thread_id,
)

dotenv.load_dotenv()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

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
    steps: list[str]


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]   # Append-only per plan; reset on new plan
    session_memory: str          # Persists across plans within a chat session


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression such as '2 + 3 * 4'."""
    try:
        value = str(simple_eval(expression))
        return persist_tool_result(
            "calculator", {"expression": expression},
            value, f"Calculated '{expression}' = {value}",
        )
    except Exception as exc:  # noqa: BLE001
        err = f"Error: {exc}"
        return persist_tool_result(
            "calculator", {"expression": expression},
            err, f"Calculator failed for '{expression}'",
        )


@tool
def search_web(query: str) -> str:
    """Search the web with Tavily."""
    tavily = TavilySearch(api_key=os.getenv("TAVILY_API_KEY"), max_results=5, topic="general")
    raw = tavily.run(query)
    summary = f"Web search completed for '{query}'."
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            summary = f"Web search for '{query}' returned {len(parsed.get('results', []))} result(s)."
    except (json.JSONDecodeError, TypeError):
        pass
    return persist_tool_result("search_web", {"query": query}, raw, summary)


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


@tool
def execute_python(code: str, output_paths: list[str] | None = None) -> str:
    """Run Python code locally for computation, data fetching, and matplotlib visualizations.

    The code runs with the current Python interpreter. The artifacts directory is available
    as the ARTIFACTS_DIR environment variable — save any output files there so they are
    automatically picked up (e.g. plt.savefig(os.environ['ARTIFACTS_DIR'] + '/plot.png')).

    include paths (relative to ARTIFACTS_DIR) you saved in output_paths to confirm them.
    """
    output_paths = output_paths or []
    artifacts_dir = get_artifacts_dir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    script_path: str | None = None
    stdout = stderr = ""
    exit_code = -1
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            script_path = f.name

        mpl_cache = artifacts_dir / ".mplcache"
        mpl_cache.mkdir(exist_ok=True)
        env = {**os.environ, "ARTIFACTS_DIR": str(artifacts_dir), "MPLCONFIGDIR": str(mpl_cache)}
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SANDBOX_EXEC_TIMEOUT,
            env=env,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        stderr = f"Execution timed out after {SANDBOX_EXEC_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001
        stderr = str(exc)
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:  # noqa: BLE001
                pass

    confirmed_artifacts: list[dict] = []
    for path_str in output_paths:
        p = Path(path_str)
        if not p.is_absolute():
            p = artifacts_dir / p.name
        confirmed_artifacts.append({"path": str(p), "exists": p.exists()})

    result_payload = {
        "exit_code": exit_code,
        "stdout": stdout[:4000],
        "stderr": stderr[:2000] if stderr else "",
        "local_artifacts_dir": str(artifacts_dir),
        "confirmed_artifacts": confirmed_artifacts,
    }
    ok = exit_code == 0
    if stderr and not ok:
        summary = f"Python execution failed (exit {exit_code}). stderr: {stderr[:300]}"
    else:
        summary = f"Python execution {'succeeded' if ok else 'finished with warnings'} (exit {exit_code}). stdout: {stdout[:300]}"

    return persist_tool_result(
        "execute_python",
        {"code": code, "output_paths": output_paths},
        json.dumps(result_payload, ensure_ascii=False),
        summary,
    )


TOOLS = [calculator, search_web, retrieve_context, retrieve_tool_result, execute_python]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

llm = ChatOpenAI(model="gpt-5-nano", api_key=os.getenv("OPENAI_API_KEY"))
agent_llm = llm.bind_tools(TOOLS)

MAX_TOOL_ROUNDS = 10
MAX_SEARCHES_PER_STEP = 3
SHOW_TOKEN_STREAM = False
SANDBOX_EXEC_TIMEOUT = 300


def _normalize_tool_args(args: dict) -> dict:
    """Handle malformed tool args from some models (e.g. {"parameters": {}})."""
    if not isinstance(args, dict):
        return {}
    if isinstance(args.get("parameters"), dict) and len(args) == 1:
        return args["parameters"]
    return args


def _format_context_stack(context_stack: list[dict]) -> str:
    """Format the append-only context stack for prompt injection.
    Each entry: { step_id, summary, tool_result_ids }.
    Full results remain on disk; use retrieve_tool_result(id) when needed.
    """
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


# ---------------------------------------------------------------------------
# Step executor module (pure function, testable in isolation)
# ---------------------------------------------------------------------------

# Static system prompt — never mutated at runtime.
# All dynamic context (objective, plan state, step info, context stack) lives
# in the HumanMessage so this prefix is fully KV-cache eligible.
STATIC_SYSTEM_PROMPT = (
    "You are a goal-directed research agent.\n"
    "Execution mode: CLOSED LOOP. No additional user input arrives during execution.\n"
    "\n"
    "## Identity\n"
    "You execute one step of a multi-step research plan at a time. "
    "You have access to tools for web search, calculation, context retrieval, "
    "Python code execution, and reading prior tool results. "
    "You never break character, never ask for clarification, and never offer optional follow-ups.\n"
    "\n"
    "## Tool rules\n"
    f"- search_web budget: maximum {MAX_SEARCHES_PER_STEP} calls per step. Be precise.\n"
    "- search_web returns a summary + tool_result_id pointer ONLY. "
    "You MUST call retrieve_tool_result(tool_result_id) to read the full content.\n"
    "- Retrieval workflow: search_web → retrieve_tool_result → extract data → produce output.\n"
    "- NEVER call retrieve_tool_result on the same tool_result_id more than once — "
    "within a step OR across steps. Once retrieved, the content is in your context permanently.\n"
    "- retrieve_context gives you a step's summary. That summary is usually sufficient. "
    "Only call retrieve_tool_result on a specific ID from a prior step if the summary "
    "is genuinely missing a concrete value you need (e.g. a URL, a number). "
    "Do not bulk-refetch all prior tool results as a habit.\n"
    "- execute_python runs code locally with full network access. Use it for:\n"
    "  (a) Fetching tabular/structured data directly: pandas.read_csv(url), requests.get(url), etc.\n"
    "  (b) Computation on data retrieved in prior steps (embed as Python literals if needed).\n"
    "  (c) Generating matplotlib plots and saving them.\n"
    "- ARTIFACTS_DIR env var points to the run artifacts folder. Always save files there:\n"
    "  import os; artifacts_dir = os.environ['ARTIFACTS_DIR']\n"
    "  plt.savefig(os.path.join(artifacts_dir, 'plot.png'))\n"
    "  Pass the saved filename in output_paths=['plot.png'].\n"
    "- Prefer execute_python for fetching structured datasets (CSVs, JSON APIs) and for all "
    "numerical modelling or chart generation — do NOT just describe what a chart would look like.\n"
    "- Always end every execute_python script with a print() of a summary dict so results "
    "appear in stdout (e.g. print({'rows': len(df), 'close_last': df.Close.iloc[-1]})).\n"
    "- Pre-installed packages (DO NOT try to install anything — pip is not available):\n"
    "  pandas, matplotlib, numpy, requests, yfinance, pytz\n"
    "  Use these directly; no subprocess install needed.\n"
    "- Stock price data sources:\n"
    "  PREFERRED — Stooq free CSV (no auth): pd.read_csv('https://stooq.com/q/d/l/?s=aapl.us&i=d')\n"
    "    Replace 'aapl' with the ticker (lowercase). Returns Date, Open, High, Low, Close, Volume.\n"
    "  ALTERNATIVE — yfinance: import yfinance as yf; df = yf.download('AAPL', period='5y')\n"
    "  AVOID — Yahoo Finance direct CSV URLs (/v7/finance/download/...) require authentication and return 401.\n"
    "\n"
    "## Output rules\n"
    "- Complete ONLY the current step described in the human message.\n"
    "- If a dependency is listed, call retrieve_context for it before producing output, "
    "then call retrieve_tool_result on any returned tool_result_ids to get the raw data.\n"
    "- Only produce plots, tables, or metrics when the data actually supports them. "
    "If data is insufficient, state the limitation clearly.\n"
    "- If structured data (e.g. full daily price history) is needed, use execute_python "
    "to fetch it directly (pandas.read_csv(url), yfinance, etc.) rather than via search_web.\n"
    "- Return concise, factual output with concrete numbers extracted from tool results.\n"
    "- Do NOT ask the user for choices, confirmation, or follow-up questions.\n"
    "- Never write phrases like 'If you\u2019d like' or 'let me know'.\n"
)


def build_step_message(
    objective: str,
    step: dict,
    review_feedback: str | None,
    plan_trajectory: str,
    previous_step: str,
    next_step: str,
    context_stack_formatted: str,
) -> str:
    """Build the dynamic human message for a single step execution.

    Everything that varies per-step or per-query goes here, keeping the
    system prompt static and fully KV-cache eligible.
    """
    deps = step.get("depends_on", [])
    dep_text = ", ".join(deps) if deps else "none"
    dep_instruction = (
        f"MANDATORY: before producing output, call retrieve_context for each dependency: {dep_text}. "
        "Then call retrieve_tool_result on any returned tool_result_ids to read the raw data.\n\n"
        if deps else ""
    )
    fb_line = f"User feedback on the plan: {review_feedback}\n\n" if review_feedback else ""
    return (
        f"## Objective\n{objective}\n\n"
        f"## Plan (full trajectory)\n{plan_trajectory}\n\n"
        f"## Execution context\n"
        f"Previous step: {previous_step}\n"
        f"Current step:  {step['id']} — {step['description']}\n"
        f"Next step:     {next_step}\n"
        f"Dependencies:  {dep_text}\n\n"
        f"## Context stack (prior step summaries; retrieve full data via retrieve_tool_result)\n"
        f"{context_stack_formatted}\n\n"
        f"{fb_line}"
        f"{dep_instruction}"
        f"Execute the current step: {step['description']}"
    )


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
        objective,
        step,
        review_feedback,
        plan_trajectory,
        previous_step,
        next_step,
        context_stack_formatted,
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=STATIC_SYSTEM_PROMPT),
        HumanMessage(content=step_message),
    ]

    search_count = 0
    tool_result_ids: set[str] = set()

    for _ in range(MAX_TOOL_ROUNDS):
        # Stream the LLM response; accumulate chunks so tool_calls are preserved.
        # Any text tokens emitted before tool calls are the model's "reasoning" and
        # are sent to the UI as a single step_reasoning event once streaming ends.
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

        # Promote to AIMessage so the rest of the loop works unchanged
        response = AIMessage(
            content=accumulated.content if isinstance(accumulated.content, str) else "",
            tool_calls=getattr(accumulated, "tool_calls", []) or [],
            id=getattr(accumulated, "id", None),
        )
        messages.append(response)

        # Emit reasoning text (pre-tool thinking) as a single event
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
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict):
                            if parsed.get("tool_result_id"):
                                tool_result_ids.add(parsed["tool_result_id"])
                            evt_summary = parsed.get("summary", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    emit_ui_event({"type": "tool_call_end", "step_id": step["id"], "tool_name": tc["name"], "summary": evt_summary})
                except Exception as e:
                    result = json.dumps({"error": str(e), "hint": "Provide required args (e.g. query for search_web)"})
                    format_tool_error(tc["name"], str(e))
                    emit_ui_event({"type": "tool_error", "step_id": step["id"], "tool_name": tc["name"], "error": str(e)})
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    result_text = last_ai.content if last_ai and isinstance(last_ai.content, str) else "Step completed."
    return result_text, list(tool_result_ids)


def _clean_step_output(text: str) -> str:
    """Remove conversational CTAs and closing-marker lines from synthesis output."""
    blocked_phrases = ("if you'd like", "if you’d like", "let me know")
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
    # Strip trailing horizontal rules left at the very end
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
    """Insert artifact markdown at an explicit marker or before Limitations."""
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


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def plan_node(state: AgentState) -> dict:
    query = state["messages"][-1].content if state.get("messages") else ""

    memory_section = ""
    prior = state.get("session_memory") or ""
    if prior:
        memory_section = (
            "\n\nPrior research completed in this session (use as context if relevant, "
            "don't repeat work already done):\n"
            f"{prior}\n"
        )

    planner = llm.with_structured_output(PlanDraft)
    draft = planner.invoke([
        HumanMessage(content=(
            "Create a concise execution plan (3-6 steps) for this task. "
            "Return just meaningful steps.\n\n"
            "Important constraints:\n"
            "- Data retrieval: use search_web for news/articles/text. "
            "For bulk structured data (price history, CSVs, JSON APIs), use execute_python "
            "with pandas.read_csv(url) or requests — it has full network access.\n"
            "- execute_python can fetch, compute, AND visualize in a single step — "
            "plan steps that need data + charts as a single execute_python call.\n"
            f"{memory_section}\n"
            "Task:\n" + str(query)
        ))
    ])

    raw_steps = [d.strip() for d in draft.steps if d.strip()]
    steps: list[dict] = []
    for idx, desc in enumerate(raw_steps):
        steps.append(PlanStep(
            id=f"step_{idx + 1}",
            description=desc,
            depends_on=[f"step_{idx}"] if idx > 0 else [],
        ).model_dump())

    plan = Plan(query=str(query), steps=steps).model_dump()
    plan_path = save_plan(plan)
    return {
        "plan": plan,
        "plan_path": plan_path,
        "objective": str(query),
        "approved": False,
        "review_feedback": None,
        "context_stack": [],
    }


def review_plan_node(state: AgentState) -> dict:
    decision = interrupt({
        "action": "review_plan",
        "message": "Approve or modify this plan before execution.",
        "plan_path": state["plan_path"],
        "plan": state["plan"],
        "choices": ["yes", "no", "edit_plan"],
    })
    action = decision.get("action", "no")
    feedback = decision.get("feedback")
    approved = action in {"yes", "edit_plan"}
    plan = decision.get("plan", state["plan"]) if action == "edit_plan" else state["plan"]
    plan_path = state["plan_path"]
    if approved:
        plan["status"] = "approved"
        plan_path = save_plan(plan)
    return {"approved": approved, "plan": plan, "plan_path": plan_path, "review_feedback": feedback}


def execute_plan_node(state: AgentState) -> dict:
    """Loop through all pending steps sequentially.
    Context stack: append-only list of prior step summaries; full results on disk.
    """
    plan = state["plan"]
    if not plan:
        return {"messages": [AIMessage(content="No plan to execute.")]}

    result_messages: list[BaseMessage] = []
    context_stack: list[dict] = list(state.get("context_stack") or [])

    plan["status"] = "in_progress"
    total = sum(1 for s in plan["steps"] if s["status"] == "pending")

    for idx, step in enumerate(plan["steps"]):
        if step["status"] != "pending":
            continue

        step["status"] = "in_progress"
        save_plan(plan)
        done = sum(1 for s in plan["steps"] if s["status"] == "completed")
        console.print(f"\n[bold cyan]▶ Step {done + 1}/{total}:[/bold cyan] {step['id']} — {step['description']}")
        emit_ui_event({
            "type": "step_start",
            "step_id": step["id"],
            "description": step["description"],
            "step_index": idx,
            "total_steps": len(plan["steps"]),
        })

        previous_step = (
            f"{plan['steps'][idx - 1]['id']} — {plan['steps'][idx - 1]['description']}"
            if idx > 0 else "none"
        )
        next_step = (
            f"{plan['steps'][idx + 1]['id']} — {plan['steps'][idx + 1]['description']}"
            if idx + 1 < len(plan["steps"]) else "none"
        )
        plan_trajectory = "\n".join(
            f"- {s['id']} [{s['status']}]: {s['description']}" for s in plan["steps"]
        )

        result_text, tool_result_ids = execute_step(
            plan=plan,
            step=step,
            objective=state.get("objective", ""),
            review_feedback=state.get("review_feedback"),
            plan_trajectory=plan_trajectory,
            previous_step=previous_step,
            next_step=next_step,
            context_stack=context_stack,
        )
        result_text = _clean_step_output(result_text)

        step["status"] = "completed"
        step["result"] = result_text
        step["tool_result_ids"] = tool_result_ids
        save_plan(plan)

        # Append to context stack (never modify prior entries)
        summary = result_text.strip().replace("\n", " ")[:220]
        if len(result_text.strip()) > 220:
            summary += "..."
        context_stack.append({
            "step_id": step["id"],
            "summary": summary,
            "tool_result_ids": tool_result_ids,
        })

        short = result_text.strip().replace("\n", " ")[:400]
        console.print(f"[green]✅ {step['id']} done:[/green] {short}{'...' if len(result_text) > 400 else ''}")
        emit_ui_event({
            "type": "step_complete",
            "step_id": step["id"],
            "result_preview": result_text.strip()[:200],
            "tool_result_ids": tool_result_ids,
        })

        result_messages.append(AIMessage(content=f"[{step['id']}] {result_text}"))

    plan["status"] = "completed"
    save_plan(plan)
    return {
        "plan": plan,
        "messages": result_messages,
        "context_stack": context_stack,
    }


def synthesize_node(state: AgentState) -> dict:
    """Produce a unified final answer from all step results."""
    plan = state.get("plan", {})
    objective = state.get("objective", "")
    artifact_paths = list_artifact_paths()

    step_results = []
    for step in plan.get("steps", []):
        result = step.get("result", "No result.")
        step_results.append(f"### {step['id']}: {step['description']}\n{result}")

    all_findings = "\n\n".join(step_results)

    console.print("\n[bold magenta]📝 Synthesizing final report...[/bold magenta]")
    emit_ui_event({"type": "synthesis_start"})

    synthesis_prompt = (
        "You are writing a final report for the user.\n"
        f"Original question: {objective}\n\n"
        "Below are the completed research steps and their findings.\n"
        "Your job: synthesize ALL of them into ONE coherent, well-structured answer that directly "
        "addresses the original question. Cover every part of the question. Use concrete data from "
        "the findings where available. End with a brief 'Limitations' section.\n"
        f"{'If a chart/plot is relevant, insert the exact standalone line [ARTIFACTS] where it should appear. Use that marker at most once.' if artifact_paths else ''}\n"
        "Do NOT list file paths or mention artifact locations in your text.\n"
        "Do NOT say 'artifact', 'artifacts', or 'chart provided as an artifact'. "
        "If you reference the visual, call it a chart or plot only.\n"
        "Do NOT ask follow-up questions. Do NOT offer optional next steps.\n"
        "Do NOT end the report with 'End of report.', '---', or any closing marker — "
        "just stop after the Limitations section.\n\n"
        f"--- FINDINGS ---\n{all_findings}\n--- END FINDINGS ---"
    )

    final_text = ""
    for chunk in llm.stream([
        SystemMessage(content=synthesis_prompt),
        HumanMessage(content=f"Write the final report answering: {objective}"),
    ]):
        token = chunk.content if hasattr(chunk, "content") and isinstance(chunk.content, str) else ""
        if token:
            final_text += token
            emit_ui_event({"type": "synthesis_token", "token": token})
    final_text = _clean_step_output(final_text)
    if artifact_paths:
        final_text = _clean_report_wording(final_text)
    final_markdown = _merge_artifacts_into_report(final_text, artifact_paths)
    report_path = save_final_report(final_markdown)
    console.print(f"[dim]Markdown report: {report_path}[/dim]")
    emit_ui_event({"type": "synthesis_complete", "content": final_text, "artifact_paths": artifact_paths})

    return {"messages": [AIMessage(content=final_text)]}


def update_memory_node(state: AgentState) -> dict:
    """Compress the completed plan into a compact session memory entry.

    No LLM call — just structured text. The planner is smart enough to
    extract what it needs on subsequent turns.  Bounded to ~2000 chars
    so it never dominates the context window.
    """
    plan = state.get("plan") or {}
    objective = state.get("objective", "")
    prior = state.get("session_memory") or ""

    findings: list[str] = []
    for step in plan.get("steps", []):
        result = (step.get("result") or "").strip()
        if result:
            short = result.replace("\n", " ")[:150]
            findings.append(f"  - {short}")

    entry = f"[{objective}]\n" + ("\n".join(findings) if findings else "  - (no findings)")

    updated = f"{prior}\n\n{entry}".strip() if prior else entry
    if len(updated) > 2000:
        updated = "...\n" + updated[-1900:]

    return {"session_memory": updated}


def route_after_review(state: AgentState) -> str:
    return "execute_plan" if state.get("approved") else END


# ---------------------------------------------------------------------------
# Graph: plan → review → execute_plan → synthesize → update_memory → END
# ---------------------------------------------------------------------------

graph = StateGraph(AgentState)
graph.add_node("plan", plan_node)
graph.add_node("review_plan", review_plan_node)
graph.add_node("execute_plan", execute_plan_node)
graph.add_node("synthesize", synthesize_node)
graph.add_node("update_memory", update_memory_node)

graph.add_edge(START, "plan")
graph.add_edge("plan", "review_plan")
graph.add_conditional_edges("review_plan", route_after_review)
graph.add_edge("execute_plan", "synthesize")
graph.add_edge("synthesize", "update_memory")
graph.add_edge("update_memory", END)

app = graph.compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_agent(query: str) -> None:
    thread_id = f"thread_{uuid4().hex[:8]}"
    set_thread_id(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    console.print(f"[dim]Thread: {thread_id} | Run dir: {get_run_dir()}[/dim]")
    first = app.invoke({"messages": [HumanMessage(content=query)]}, config=config)
    interrupts = first.get("__interrupt__", ())
    if not interrupts:
        return

    plan_payload = interrupts[0].value
    console.print()
    format_plan(plan_payload.get("plan", {}))
    user_input = input("\nAction? [yes/no/edit_plan]: ").strip().lower()
    feedback = input("Optional feedback (enter to skip): ").strip() or None

    if user_input in {"", "yes", "y"}:
        resume_value = {"action": "yes", "feedback": feedback}
    elif user_input in {"edit", "edit_plan"}:
        edited = input("Paste modified plan JSON (single line): ").strip()
        try:
            resume_value = {"action": "edit_plan", "feedback": feedback, "plan": json.loads(edited)}
        except json.JSONDecodeError:
            print("Invalid JSON; stopping.")
            return
    else:
        resume_value = {"action": "no", "feedback": feedback}

    for chunk in app.stream(
        Command(resume=resume_value),
        config=config,
        stream_mode=["updates", "messages"],
        version="v2",
    ):
        if chunk["type"] == "updates":
            for node_name, node_state in chunk["data"].items():
                if not isinstance(node_state, dict):
                    continue
                if node_name == "plan" and node_state.get("plan"):
                    format_plan(node_state["plan"])
                elif node_name == "synthesize" and node_state.get("messages"):
                    final_msg = node_state["messages"][-1]
                    final_content = getattr(final_msg, "content", "") if not isinstance(final_msg, dict) else final_msg.get("content", "")
                    if final_content:
                        console.print(Panel(final_content, title="📄 Final Report", border_style="green", padding=(1, 2)))
                elif "messages" in node_state:
                    pass
        elif chunk["type"] == "messages" and SHOW_TOKEN_STREAM:
            msg_chunk, _ = chunk["data"]
            if msg_chunk.content:
                print(msg_chunk.content, end="", flush=True)
    console.print()


if __name__ == "__main__":
    run_agent(
        "What are the latest news about Apple, and give me the price from the last 5 years?"
    )

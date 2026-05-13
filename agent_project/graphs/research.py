"""Research subgraph nodes — plan, HITL review, execute, synthesize, memory update."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import dotenv
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
try:
    from langgraph.types import interrupt
except ImportError:  # LangGraph >=1.0 style
    from langgraph.types import Interrupt

    def interrupt(payload: dict):  # type: ignore[no-redef]
        raise Interrupt(payload)
from pydantic import BaseModel, Field

from documents import search_documents, _session_ctx
from plan_store import save_plan as save_plan_to_store, update_step as store_update_step, save_report as store_save_report
from storage import set_session_memory
from tools import (
    calculator,
    execute_python,
    fetch_sec_filing,
    retrieve_tool_result,
    run_dcf_workflow,
    search_web,
)
import agent_log
from utils import (
    console,
    emit_ui_event,
    format_tool_call,
    format_tool_error,
    format_tool_result,
    get_dcf_hitl_payload,
    get_run_dir,
    list_artifact_paths,
    track_tool,
)

dotenv.load_dotenv()

# ---------------------------------------------------------------------------
# Constants & LLM
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 6
MAX_SEARCHES_PER_STEP = 3

llm = ChatOpenAI(model="gpt-5-nano", api_key=os.getenv("OPENAI_API_KEY"), timeout=60)

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


# ---------------------------------------------------------------------------
# Tools (canonical definitions in tools.py; research-only tools here)
# ---------------------------------------------------------------------------

from langchain_core.tools import tool as _tool


@_tool
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


TOOLS = [
    calculator,
    search_web,
    retrieve_context,
    retrieve_tool_result,
    execute_python,
    run_dcf_workflow,
    search_documents,
    fetch_sec_filing,
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
agent_llm = llm.bind_tools(TOOLS)


# ---------------------------------------------------------------------------
# Step execution helpers
# ---------------------------------------------------------------------------

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
    "- search_documents: search the user's uploaded files (PDFs, spreadsheets, reports). "
    "ALWAYS call this BEFORE search_web for every step — uploaded documents may already contain the data you need. "
    "Only use search_web when search_documents returns no relevant results.\n"
    f"- search_web budget: maximum {MAX_SEARCHES_PER_STEP} calls per step. Be precise.\n"
    "- search_web returns a summary + tool_result_id pointer ONLY. "
    "You MUST call retrieve_tool_result(tool_result_id) to read the full content.\n"
    "- Retrieval workflow: search_web → retrieve_tool_result → extract data → produce output.\n"
    "- NEVER call retrieve_tool_result on the same tool_result_id more than once.\n"
    "- retrieve_context gives you a step's summary. Only call retrieve_tool_result on a specific ID "
    "if the summary is genuinely missing a concrete value you need.\n"
    "- execute_python runs code locally with full network access. Use it for:\n"
    "  (a) Fetching tabular/structured data directly.\n"
    "  (b) Computation on data retrieved in prior steps.\n"
    "  (c) Generating matplotlib plots and saving them.\n"
    "- ARTIFACTS_DIR env var points to the run artifacts folder. Always save files there:\n"
    "  import os; artifacts_dir = os.environ['ARTIFACTS_DIR']\n"
    "  plt.savefig(os.path.join(artifacts_dir, 'plot.png'))\n"
    "  Pass the saved filename in output_paths=['plot.png'].\n"
    "- Pre-installed packages: pandas, matplotlib, numpy, requests, yfinance, pytz\n"
    "- ALWAYS set a timeout on requests calls: requests.get(url, timeout=15).\n"
    "- fetch_sec_filing: fetch 10-K/10-Q filings from SEC EDGAR for a company. "
    "Use for questions about a company's risks, MD&A, business model, or filings. "
    "Prefer this over search_web for company-specific fundamental research.\n"
    "- run_dcf_workflow runs a deterministic valuation workflow. Use it when the step asks for "
    "a DCF/intrinsic value/sensitivity analysis. It internally gathers evidence from SEC filings, "
    "FMP/yfinance, and uploaded documents; proposes assumptions; and computes valuation. "
    "It can use uploaded session documents inside its evidence assembly. Provide ticker and "
    "horizon_years. The tool result includes a detailed report with assumption provenance, "
    "WACC decomposition, confidence label (high/medium/low), and quality flags. "
    "When confidence != high, you MUST surface the flagged fields and the implied vs spot "
    "price gap in the synthesis instead of presenting the implied price as authoritative.\n"
    "- Stock price / financial data — use the pre-injected get_stock_data() helper:\n"
    "    df = get_stock_data('AAPL', period='5y')  # returns clean DataFrame\n"
    "  NEVER import yfinance directly. NEVER use stooq.com or Yahoo Finance CSV URLs.\n"
    "- Always end every execute_python script with a print() of a summary dict.\n"
    "\n"
    "## Output rules\n"
    "- Complete ONLY the current step described in the human message.\n"
    "- Return concise, factual output with concrete numbers extracted from tool results.\n"
    "- Do NOT ask the user for choices, confirmation, or follow-up questions.\n"
    "- Never write phrases like 'If you’d like' or 'let me know'.\n"
)


def _normalize_tool_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    if isinstance(args.get("parameters"), dict) and len(args) == 1:
        return args["parameters"]
    return args


def _format_context_stack(context_stack: list[dict]) -> str:
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


def build_step_message(
    objective: str,
    step: dict,
    review_feedback: str | None,
    plan_trajectory: str,
    previous_step: str,
    next_step: str,
    context_stack_formatted: str,
) -> str:
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
        f"## Context stack (prior step summaries)\n"
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
    context_stack_formatted = _format_context_stack(context_stack)
    step_message = build_step_message(
        objective, step, review_feedback,
        plan_trajectory, previous_step, next_step, context_stack_formatted,
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
            if tc["name"] == "run_dcf_workflow":
                args.setdefault("parent_step_id", step["id"])

            if tc["name"] == "search_web" and search_count >= MAX_SEARCHES_PER_STEP:
                result = json.dumps({
                    "error": f"Search budget exhausted ({MAX_SEARCHES_PER_STEP} calls). "
                             "Use retrieve_tool_result to read full content from earlier searches.",
                })
                format_tool_error(tc["name"], f"budget exhausted ({search_count}/{MAX_SEARCHES_PER_STEP})")
                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
                continue

            args_preview = json.dumps(args, ensure_ascii=False)[:150]
            format_tool_call(tc["name"], args)
            if not tool_fn:
                result = json.dumps({"error": f"unknown tool: {tc['name']}"})
                format_tool_error(tc["name"], "unknown tool")
                # Emit a started+error span so the unknown tool still
                # appears in the activity log.
                try:
                    with track_tool(
                        name=tc["name"],
                        scope="research",
                        step_id=step["id"],
                        args_preview=args_preview,
                    ):
                        raise RuntimeError("unknown tool")
                except RuntimeError:
                    pass
            else:
                try:
                    with track_tool(
                        name=tc["name"],
                        scope="research",
                        step_id=step["id"],
                        args_preview=args_preview,
                    ) as span:
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
                        span["summary"] = evt_summary
                except Exception as e:
                    result = json.dumps({"error": str(e)})
                    format_tool_error(tc["name"], str(e))
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    result_text = last_ai.content if last_ai and isinstance(last_ai.content, str) else "Step completed."
    if not result_text.strip():
        messages.append(HumanMessage(content="Use the available tool results above to produce this step's final result now. Do not call more tools."))
        response = llm.invoke(messages)
        result_text = response.content if isinstance(response.content, str) else "Step completed."
    if not result_text.strip():
        result_text = "Step completed without a textual result. Check agent_project/runs/server.log for backend details."
    return result_text, list(tool_result_ids)


def _clean_step_output(text: str) -> str:
    blocked_phrases = ("if you'd like", "if you’d like", "let me know")
    closing_lines = {
        "end of report.", "end of report", "--- end of report ---",
        "end.", "[artifacts]", "[artifact]", "[chart]",
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


def plan_node(state: dict) -> dict:
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
            "- For explicit DCF/intrinsic-value valuation tasks, include a step that calls "
            "run_dcf_workflow (deterministic workflow) instead of improvising formulas in plain text.\n"
            "- execute_python can fetch, compute, AND visualize in a single step.\n"
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
    plan_path = save_plan_to_store(get_run_dir().name, plan)
    return {
        "plan": plan,
        "plan_path": plan_path,
        "objective": str(query),
        "approved": False,
        "review_feedback": None,
        "context_stack": [],
    }


def review_plan_node(state: dict) -> dict:
    decision = interrupt({
        "action": "review_plan",
        "message": "Approve or modify this plan before execution.",
        "plan_path": state["plan_path"],
        "plan": state["plan"],
        "choices": ["yes", "no", "edit_plan"],
    })
    # Studio/API resume payloads can be either a string ("yes"/"no"/"edit_plan")
    # or a richer dict. Normalize to one shape before reading fields.
    if isinstance(decision, str):
        decision = {"action": decision}
    elif not isinstance(decision, dict):
        decision = {"action": "no"}

    action = str(decision.get("action", "no")).lower()
    feedback = decision.get("feedback")
    approved = action in {"yes", "edit_plan"}
    plan = decision.get("plan", state["plan"]) if action == "edit_plan" else state["plan"]
    plan_path = state["plan_path"]
    if approved:
        plan["status"] = "approved"
        plan_path = save_plan_to_store(get_run_dir().name, plan)
    return {"approved": approved, "plan": plan, "plan_path": plan_path, "review_feedback": feedback}


def execute_one_step_node(state: dict) -> dict:
    """Execute a single pending step from the plan.

    Called repeatedly by the parent graph via a conditional edge (route_after_step)
    until all steps are completed, at which point execution flows to synthesize.
    Each invocation is a real LangGraph node → checkpoint, stream, resume for free.
    """
    plan = state["plan"]
    if not plan:
        return {"messages": [AIMessage(content="No plan to execute.")]}
    _session_ctx.set(state.get("session_id") or "")

    context_stack: list[dict] = list(state.get("context_stack") or [])

    # ── DCF HITL resume ────────────────────────────────────────────────
    # If a previous invocation paused for DCF assumption review, we are
    # re-entered here after the user approved (or rejected).  The interrupt
    # at the bottom of this function raised; on resume, we come straight
    # back to the top and the awaiting step is still in the plan.
    awaiting = [s for s in plan.get("steps", []) if s.get("status") == "awaiting_assumptions"]
    if awaiting:
        step = awaiting[0]
        dcf_data = step.get("dcf_hitl") or {}
        resume_value = interrupt({
            "type": "dcf_review",
            "step_id": step["id"],
            "ticker": dcf_data.get("ticker", "?"),
            "horizon_years": dcf_data.get("horizon_years", 5),
            "assumptions": dcf_data.get("assumptions", {}),
            "assumption_provenance": dcf_data.get("assumption_provenance", {}),
            "memo_proposals": dcf_data.get("memo_proposals", {}),
            "evidence_items": dcf_data.get("evidence_items", []),
        })

        if not isinstance(resume_value, dict) or not resume_value.get("approved"):
            step["status"] = "failed"
            step["result"] = "DCF assumptions were rejected by the user."
            store_update_step(get_run_dir().name, plan, step["id"], status="failed", result=step["result"])
            agent_log.step_rejected(step["id"])
            return {
                "plan": plan,
                "messages": [AIMessage(content=f"[{step['id']}] DCF assumptions rejected.")],
                "context_stack": context_stack,
            }

        overrides = resume_value.get("assumption_overrides") or {}
        ticker = dcf_data.get("ticker", "?")
        horizon = dcf_data.get("horizon_years", 5)
        agent_log.step_hitl_resume(step["id"], ticker)
        import time as _time; _step_resume_t = _time.time()

        # Re-run the step with the approved overrides — tell the LLM to complete the DCF
        continuation_prompt = (
            f"The DCF assumptions for {ticker} ({horizon}yr horizon) were approved by the user.\n"
            f"Call run_dcf_workflow with ticker='{ticker}', horizon_years={horizon}, "
            f"assumption_review_mode=False, and assumption_overrides={json.dumps(overrides)}.\n"
            f"Then present the valuation result with key findings."
        )

        result_text, tool_result_ids = execute_step(
            plan=plan, step=step,
            objective=state.get("objective", ""),
            review_feedback=continuation_prompt,
            plan_trajectory="",
            previous_step="none",
            next_step="none",
            context_stack=context_stack,
        )
        result_text = _clean_step_output(result_text)

        store_update_step(
            get_run_dir().name, plan, step["id"],
            status="completed", result=result_text, tool_result_ids=tool_result_ids,
        )

        summary = result_text.strip().replace("\n", " ")[:220]
        if len(result_text.strip()) > 220:
            summary += "..."
        context_stack.append({
            "step_id": step["id"],
            "summary": summary,
            "tool_result_ids": tool_result_ids,
        })

        agent_log.step_done(step["id"], result_text.strip(), _step_resume_t)
        emit_ui_event({
            "type": "step_complete",
            "step_id": step["id"],
            "result_preview": result_text.strip()[:200],
            "tool_result_ids": tool_result_ids,
        })

        return {
            "plan": plan,
            "messages": [AIMessage(content=f"[{step['id']}] {result_text}")],
            "context_stack": context_stack,
        }

    # ── Normal execution ───────────────────────────────────────────────

    # Mark plan as in_progress on first entry
    if plan.get("status") != "in_progress":
        plan["status"] = "in_progress"
        save_plan_to_store(get_run_dir().name, plan)

    # Find the first pending step
    pending_steps = [s for s in plan["steps"] if s["status"] == "pending"]
    if not pending_steps:
        # All done — shouldn't happen if route_after_step is correct, but be safe
        plan["status"] = "completed"
        save_plan_to_store(get_run_dir().name, plan)
        return {"plan": plan, "context_stack": context_stack}

    step = pending_steps[0]
    idx = plan["steps"].index(step)
    total = len(pending_steps)
    done_count = sum(1 for s in plan["steps"] if s["status"] == "completed")

    store_update_step(get_run_dir().name, plan, step["id"], status="running")
    _step_start_t = agent_log.step_start(done_count + 1, done_count + total, step["id"], step["description"])
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
        plan=plan, step=step,
        objective=state.get("objective", ""),
        review_feedback=state.get("review_feedback"),
        plan_trajectory=plan_trajectory,
        previous_step=previous_step,
        next_step=next_step,
        context_stack=context_stack,
    )
    result_text = _clean_step_output(result_text)

    # ── DCF HITL detection ───────────────────────────────────────────
    # If the canonical run_dcf_workflow tool emitted a DCF review event
    # (via emit_ui_event + set_dcf_hitl_payload), pause the graph so the
    # user can review assumptions in the same DcfHitlSection used by chat.
    dcf_hitl = get_dcf_hitl_payload()
    if dcf_hitl:
        step["status"] = "awaiting_assumptions"
        step["dcf_hitl"] = dcf_hitl
        save_plan_to_store(get_run_dir().name, plan)
        agent_log.step_hitl_pause(step["id"], dcf_hitl.get("ticker", "?"))
        interrupt({
            "type": "dcf_review",
            "step_id": step["id"],
            "ticker": dcf_hitl.get("ticker", "?"),
            "horizon_years": dcf_hitl.get("horizon_years", 5),
            "assumptions": dcf_hitl.get("assumptions", {}),
            "assumption_provenance": dcf_hitl.get("assumption_provenance", {}),
            "memo_proposals": dcf_hitl.get("memo_proposals", {}),
            "evidence_items": dcf_hitl.get("evidence_items", []),
        })
        # Never reached until resume — on resume, the awaiting check at
        # the top of this function picks up the step.

    store_update_step(
        get_run_dir().name, plan, step["id"],
        status="completed", result=result_text, tool_result_ids=tool_result_ids,
    )

    summary = result_text.strip().replace("\n", " ")[:220]
    if len(result_text.strip()) > 220:
        summary += "..."
    context_stack.append({
        "step_id": step["id"],
        "summary": summary,
        "tool_result_ids": tool_result_ids,
    })

    agent_log.step_done(step["id"], result_text.strip(), _step_start_t)
    emit_ui_event({
        "type": "step_complete",
        "step_id": step["id"],
        "result_preview": result_text.strip()[:200],
        "tool_result_ids": tool_result_ids,
    })

    # If this was the last step, mark plan as completed
    if not any(s["status"] == "pending" for s in plan["steps"]):
        plan["status"] = "completed"
        save_plan_to_store(get_run_dir().name, plan)

    return {
        "plan": plan,
        "messages": [AIMessage(content=f"[{step['id']}] {result_text}")],
        "context_stack": context_stack,
    }


def route_after_step(state: dict) -> str:
    """Return 'execute_one_step' if any step is still pending, else 'synthesize'."""
    plan = state.get("plan") or {}
    has_pending = any(s["status"] == "pending" for s in plan.get("steps", []))
    return "execute_one_step" if has_pending else "synthesize"


def synthesize_node(state: dict) -> dict:
    plan = state.get("plan", {})
    objective = state.get("objective", "")
    artifact_paths = list_artifact_paths()

    step_results = []
    for step in plan.get("steps", []):
        result = step.get("result", "No result.")
        step_results.append(f"### {step['id']}: {step['description']}\n{result}")

    all_findings = "\n\n".join(step_results)

    _synth_t = agent_log.synth_start()
    emit_ui_event({"type": "synthesis_start"})

    synthesis_prompt = (
        "You are writing a final report for the user.\n"
        f"Original question: {objective}\n\n"
        "Synthesize ALL findings into ONE coherent, well-structured answer that directly "
        "addresses the original question. Cover every part of the question. Use concrete data "
        "from the findings where available. End with a brief 'Limitations' section.\n"
        f"{'If a chart/plot is relevant, insert the exact standalone line [ARTIFACTS] where it should appear. Use that marker at most once.' if artifact_paths else ''}\n"
        "Do NOT list file paths or mention artifact locations in your text.\n"
        "Do NOT say 'artifact', 'artifacts', or 'chart provided as an artifact'. "
        "If you reference the visual, call it a chart or plot only.\n"
        "Do NOT ask follow-up questions. Do NOT offer optional next steps.\n"
        "Do NOT end with 'End of report.', '---', or any closing marker.\n\n"
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
    report_path = store_save_report(
        get_run_dir().name,
        state.get("session_id") or "",
        str(objective),
        final_markdown,
    )
    agent_log.synth_done(str(report_path), _synth_t)
    emit_ui_event({"type": "synthesis_complete", "content": final_text, "artifact_paths": artifact_paths})

    return {"messages": [AIMessage(content=final_text)]}


def update_memory_node(state: dict) -> dict:
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

    set_session_memory(state.get("session_id") or "", updated)
    return {"session_memory": updated}

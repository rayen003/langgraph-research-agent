"""LangGraph graph nodes."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import interrupt

from agent.executor import (
    _clean_report_wording,
    _clean_step_output,
    _merge_artifacts_into_report,
    execute_step,
)
from agent.state import AgentState, Plan, PlanDraft, PlanStep
from tools import llm
from utils.events import emit_ui_event
from utils.formatting import format_plan
from utils.persistence import console, list_artifact_paths, save_final_report, save_plan

# Phase A: planner prompt — prefer fewer steps; each step = one LLM session with many tool rounds.
PLANNER_INSTRUCTIONS = """\
Create a concise execution plan for the task below.

## Step budget (important)
- **Default:** **2–4 steps.** One step is OK for a narrow, single-focus task.
- **At most 5 steps** (hard limit). Use 5 only when the task has genuinely separate sub-goals you cannot merge (e.g. unrelated multi-part research).
- **Do not** pad with micro-steps. Each step is a full execution round: the agent can call tools many times inside that step.

## Merge these into ONE step (do not split across steps)
- **Search + read:** `search_web` → `retrieve_tool_result` → synthesize (same step).
- **Known URL + read:** `fetch_url` → `retrieve_tool_result` → extract (same step).
- **Data + chart + summary:** one `execute_python` that fetches, computes, saves plots, prints a summary dict.
- **Prior-step data:** if the next action is “use results from earlier,” that belongs in the step that consumes them, not a separate “retrieve context”-only step unless retrieval is the whole goal.

## Tool hints (for writing step descriptions)
- **News / articles / narrative web:** `search_web`; follow with `retrieve_tool_result` when you need full text from a result id.
- **Specific URL full text:** `fetch_url` then `retrieve_tool_result`.
- **Tabular data, APIs, price history, modelling, plots:** `execute_python` (pandas, requests, yfinance, matplotlib). Network is available inside the sandbox.
- **Math:** `calculator` or inline in `execute_python`.

## Output
Return **meaningful step descriptions** only (what to accomplish, not tool names unless helpful)."""


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
            f"{PLANNER_INSTRUCTIONS}\n"
            f"{memory_section}\n"
            "## Task\n"
            f"{query!s}"
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
    """Loop through all pending steps sequentially."""
    plan = state["plan"]
    if not plan:
        return {"messages": [AIMessage(content="No plan to execute.")]}

    result_messages: list[AIMessage] = []
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


def route_after_review(state: AgentState) -> str:
    return "execute_plan" if state.get("approved") else END

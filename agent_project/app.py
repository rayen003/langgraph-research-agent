"""
Chainlit UI for the plan-then-execute research agent.

Run with:  chainlit run app.py

Custom React elements live in public/elements/:
  PlanCard.jsx   — collapsible plan card with status badge
  StepTracker.jsx — live vertical timeline with expandable tool-call pills
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import chainlit as cl
from langchain_core.messages import HumanMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parent))

from file import app as agent_graph  # noqa: E402
from utils import get_artifacts_dir, set_thread_id, set_ui_event_handler  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _update_element(element: cl.CustomElement, new_props: dict) -> None:
    """Sync props → content and push the update to the frontend."""
    element.props = new_props
    element.content = json.dumps(new_props)
    await element.update()


def _plan_steps_for_display(steps: list[dict]) -> list[dict]:
    return [
        {
            "id": s["id"],
            "description": s["description"],
            "depends_on": s.get("depends_on", []),
        }
        for s in steps
    ]


def _split_report_for_artifacts(content: str) -> tuple[str, str]:
    """Split report text into content before/after inline artifacts."""
    stripped = content.strip()
    if not stripped:
        return "", ""

    for marker in ("[ARTIFACTS]", "[ARTIFACT]", "[CHART]"):
        if marker in stripped:
            before, after = stripped.split(marker, 1)
            return before.rstrip(), after.lstrip()

    lines = stripped.splitlines()
    for idx, line in enumerate(lines):
        normalized = line.strip().lstrip("#").strip().lower().rstrip(":")
        if normalized.startswith("limitations"):
            before = "\n".join(lines[:idx]).rstrip()
            after = "\n".join(lines[idx:]).lstrip()
            return before, after

    return stripped, ""


def _remove_artifact_markers(text: str) -> str:
    """Remove any leaked placeholder markers from visible report text."""
    cleaned = text
    for marker in ("[ARTIFACTS]", "[ARTIFACT]", "[CHART]"):
        cleaned = cleaned.replace(marker, "")
    return cleaned.strip()


def _artifact_image_elements(artifact_paths: list[str]) -> list[cl.Image]:
    """Build inline image elements for generated run artifacts."""
    run_dir = get_artifacts_dir().parent
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    images: list[cl.Image] = []
    for artifact_path in artifact_paths:
        path = Path(artifact_path)
        local_path = path if path.is_absolute() else run_dir / path
        if local_path.is_file() and local_path.suffix.lower() in image_suffixes:
            images.append(cl.Image(name=local_path.stem, path=str(local_path), display="inline"))
    return images


def _initial_tracker_state(steps: list[dict]) -> dict:
    return {
        "overall_status": "running",
        "steps": [
            {
                "id": s["id"],
                "description": s["description"],
                "status": "pending",
                "tool_calls": [],
                "reasoning": "",
            }
            for s in steps
        ],
    }


# ---------------------------------------------------------------------------
# Event processing — mutates tracker_state and pushes element update
# ---------------------------------------------------------------------------

async def _process_event(
    event: dict,
    tracker_state: dict,
    tracker_element: cl.CustomElement,
    plan_element: cl.CustomElement | None,
    ui_state: dict,
) -> None:
    etype = event.get("type")

    if etype == "step_start":
        sid = event["step_id"]
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                s["status"] = "running"
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "step_reasoning":
        sid = event.get("step_id", "")
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                s["reasoning"] = event.get("text", "")
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "tool_call_start":
        sid = event.get("step_id", "")
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                s["tool_calls"].append({
                    "tool_name": event["tool_name"],
                    "status": "running",
                    "summary": "",
                    "args_preview": event.get("args_preview", ""),
                })
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "tool_call_end":
        sid = event.get("step_id", "")
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                for tc in reversed(s["tool_calls"]):
                    if tc["tool_name"] == event["tool_name"] and tc["status"] == "running":
                        tc["status"] = "done"
                        tc["summary"] = event.get("summary", "")
                        break
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "tool_error":
        sid = event.get("step_id", "")
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                for tc in reversed(s["tool_calls"]):
                    if tc["tool_name"] == event["tool_name"] and tc["status"] == "running":
                        tc["status"] = "error"
                        tc["summary"] = event.get("error", "")
                        break
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "step_complete":
        sid = event["step_id"]
        for s in tracker_state["steps"]:
            if s["id"] == sid:
                s["status"] = "completed"
                break
        await _update_element(tracker_element, tracker_state)

    elif etype == "synthesis_start":
        tracker_state["overall_status"] = "synthesizing"
        await _update_element(tracker_element, tracker_state)
        if plan_element:
            plan_element.props["status"] = "running"
            plan_element.content = json.dumps(plan_element.props)
            await plan_element.update()
        report_msg = cl.Message(content="## Final Report\n\n")
        await report_msg.send()
        ui_state["report_msg"] = report_msg

    elif etype == "synthesis_token":
        msg = ui_state.get("report_msg")
        if msg:
            await msg.stream_token(event.get("token", ""))

    elif etype == "synthesis_complete":
        tracker_state["overall_status"] = "complete"
        await _update_element(tracker_element, tracker_state)
        if plan_element:
            plan_element.props["status"] = "completed"
            plan_element.content = json.dumps(plan_element.props)
            await plan_element.update()
        msg = ui_state.get("report_msg")
        if msg:
            content = event.get("content", "")
            artifact_paths = event.get("artifact_paths", []) or []
            if artifact_paths:
                before, after = _split_report_for_artifacts(content)
                before = _remove_artifact_markers(before)
                after = _remove_artifact_markers(after)
                msg.content = f"## Final Report\n\n{before}" if before else "## Final Report"
                await msg.update()

                image_elements = _artifact_image_elements(artifact_paths)
                if image_elements:
                    await cl.Message(content="", elements=image_elements).send()

                if after:
                    await cl.Message(content=after).send()
            else:
                await msg.update()


# ---------------------------------------------------------------------------
# Chainlit lifecycle
# ---------------------------------------------------------------------------

@cl.on_chat_start
async def on_start():
    thread_id = f"thread_{uuid4().hex[:8]}"
    set_thread_id(thread_id)
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("config", {"configurable": {"thread_id": thread_id}})
    await cl.Message(
        content=(
            "Send me a research question and I'll build an execution plan "
            "for your approval before running it."
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    config = cl.user_session.get("config")

    # ── Phase 1: Planning ─────────────────────────────────────────────────
    planning_msg = cl.Message(content="Building plan\u2026")
    await planning_msg.send()

    try:
        result = await agent_graph.ainvoke(
            {"messages": [HumanMessage(content=message.content)]},
            config=config,
        )
    except Exception as exc:
        planning_msg.content = f"Planning failed: {exc}"
        await planning_msg.update()
        return

    interrupts = result.get("__interrupt__", ())
    if not interrupts:
        msgs = result.get("messages", [])
        content = msgs[-1].content if msgs and hasattr(msgs[-1], "content") else "Done."
        planning_msg.content = content
        await planning_msg.update()
        return

    plan = interrupts[0].value.get("plan", {})
    steps = plan.get("steps", [])

    # Replace the "Building plan…" message with the PlanCard custom element
    plan_props = {
        "query": plan.get("query", ""),
        "status": "draft",
        "steps": _plan_steps_for_display(steps),
    }
    plan_element = cl.CustomElement(
        name="PlanCardV3",
        props=plan_props,
        display="inline",
    )
    planning_msg.content = ""
    planning_msg.elements = [plan_element]
    await planning_msg.update()

    # ── HITL: approve / reject ────────────────────────────────────────────
    ask_msg = cl.AskActionMessage(
        content="Review the plan. Do you approve?",
        actions=[
            cl.Action(name="approve", payload={"value": "yes"}, label="Approve"),
            cl.Action(name="reject", payload={"value": "no"}, label="Reject"),
        ],
        timeout=600,
    )
    res = await ask_msg.send()
    await ask_msg.remove()

    if not res or res.get("name") != "approve":
        plan_element.props["status"] = "draft"
        plan_element.content = json.dumps(plan_element.props)
        await plan_element.update()
        await cl.Message(content="Plan rejected. Send a new question to start over.").send()
        return

    # Mark plan approved
    plan_element.props["status"] = "approved"
    plan_element.content = json.dumps(plan_element.props)
    await plan_element.update()

    # ── Phase 2: Execute + Synthesize ─────────────────────────────────────
    # Create live StepTracker
    tracker_state = _initial_tracker_state(steps)
    tracker_element = cl.CustomElement(
        name="StepTrackerV3",
        props=tracker_state,
        display="inline",
    )
    tracker_msg = cl.Message(content="", elements=[tracker_element])
    await tracker_msg.send()

    # Update plan status to running
    plan_element.props["status"] = "running"
    plan_element.content = json.dumps(plan_element.props)
    await plan_element.update()

    # Async event bridge: sync graph thread → async Chainlit loop
    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue[dict] = asyncio.Queue()

    def _bridge(event: dict) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, event)

    set_ui_event_handler(_bridge)

    graph_task = asyncio.ensure_future(
        agent_graph.ainvoke(
            Command(resume={"action": "yes", "feedback": None}),
            config=config,
        )
    )

    ui_state: dict = {"report_msg": None}

    try:
        while not graph_task.done():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            await _process_event(event, tracker_state, tracker_element, plan_element, ui_state)

        # Drain events that arrived between last poll and task completion
        while not event_queue.empty():
            event = event_queue.get_nowait()
            await _process_event(event, tracker_state, tracker_element, plan_element, ui_state)

        graph_result = graph_task.result()

    except Exception as exc:
        await cl.Message(content=f"Execution error: {exc}").send()
        return
    finally:
        set_ui_event_handler(None)

    # Ensure final states
    tracker_state["overall_status"] = "complete"
    await _update_element(tracker_element, tracker_state)

    plan_element.props["status"] = "completed"
    plan_element.content = json.dumps(plan_element.props)
    await plan_element.update()

    # ── Fallback report (if synthesis streaming didn't fire) ─────────────
    if not ui_state.get("report_msg"):
        final_content = ""
        msgs = graph_result.get("messages", [])
        if msgs:
            last = msgs[-1]
            final_content = last.content if hasattr(last, "content") else str(last)
        if final_content:
            artifact_paths = []
            artifacts_dir = get_artifacts_dir()
            if artifacts_dir.exists():
                artifact_paths = [
                    str(path.relative_to(artifacts_dir.parent))
                    for path in sorted(artifacts_dir.iterdir())
                    if path.is_file() and not path.name.startswith(".")
                ]

            before, after = _split_report_for_artifacts(final_content)
            before = _remove_artifact_markers(before)
            after = _remove_artifact_markers(after)
            await cl.Message(content=f"## Final Report\n\n{before or final_content}").send()

            image_elements = _artifact_image_elements(artifact_paths)
            if image_elements:
                await cl.Message(content="", elements=image_elements).send()

            if after:
                await cl.Message(content=after).send()
        else:
            await cl.Message(content="Execution finished but no report was generated.").send()

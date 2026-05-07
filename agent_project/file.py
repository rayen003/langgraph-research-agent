"""Parent graph: intent_node routes queries to the research or conversational subgraph."""

import json
import os
from typing import Annotated, TypedDict
from uuid import uuid4

import dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from rich.panel import Panel

from utils import console, emit_ui_event, format_plan, get_run_dir, set_thread_id
from graphs.research import (
    plan_node,
    review_plan_node,
    execute_plan_node,
    synthesize_node,
    update_memory_node,
)
from graphs.conversational import chat_node

dotenv.load_dotenv()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Routing
    mode: str           # "auto" | "research" | "chat" — user's selected mode
    resolved_intent: str | None  # "research" | "chat" — set by intent_node
    # Research subgraph fields
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]
    # Shared memory (persists across turns in the same LangGraph thread)
    session_memory: str
    # RAG session scope — used to filter uploaded documents
    session_id: str


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

# Fast, cheap model just for classification — keeps latency low
intent_llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY"), timeout=30)

_CLASSIFY_PROMPT = (
    "Classify the user's latest message as 'research' or 'chat'.\n\n"
    "- 'research': requires web search, data fetching, financial analysis, multi-step "
    "investigation, generating a formal report, building models, or creating charts.\n"
    "- 'chat': quick factual question, clarification, follow-up on existing results, "
    "explanation of a concept, conversational message answerable from knowledge alone.\n\n"
    "Consider any prior conversation history for context.\n"
    "Reply with ONLY one word: research   or   chat"
)


def intent_node(state: AgentState) -> dict:
    mode = state.get("mode") or "auto"

    if mode == "research":
        intent = "research"
    elif mode == "chat":
        intent = "chat"
    else:
        # Auto: use fast LLM to classify
        messages = state.get("messages", [])
        # Include last few turns for context-aware classification
        history_str = ""
        for m in messages[-6:]:
            role = m.__class__.__name__.replace("Message", "").lower()
            content = m.content if isinstance(m.content, str) else ""
            history_str += f"{role}: {content[:300]}\n"

        response = intent_llm.invoke([
            HumanMessage(content=f"{_CLASSIFY_PROMPT}\n\n--- Conversation ---\n{history_str}")
        ])
        raw = (response.content or "").strip().lower()
        intent = "research" if "research" in raw else "chat"
        console.print(f"[dim]Intent classified: {intent} (mode=auto)[/dim]")

    emit_ui_event({"type": "intent_classified", "intent": intent, "mode": mode})
    return {"resolved_intent": intent}


def route_intent(state: AgentState) -> str:
    return state.get("resolved_intent") or "chat"


def route_after_review(state: AgentState) -> str:
    return "execute_plan" if state.get("approved") else END


# ---------------------------------------------------------------------------
# Graph: START → intent → [research path | chat path]
# ---------------------------------------------------------------------------

graph = StateGraph(AgentState)

graph.add_node("intent", intent_node)
graph.add_node("plan", plan_node)
graph.add_node("review_plan", review_plan_node)
graph.add_node("execute_plan", execute_plan_node)
graph.add_node("synthesize", synthesize_node)
graph.add_node("update_memory", update_memory_node)
graph.add_node("chat", chat_node)

graph.add_edge(START, "intent")
graph.add_conditional_edges("intent", route_intent, {"research": "plan", "chat": "chat"})
graph.add_edge("plan", "review_plan")
graph.add_conditional_edges("review_plan", route_after_review, {"execute_plan": "execute_plan", END: END})
graph.add_edge("execute_plan", "synthesize")
graph.add_edge("synthesize", "update_memory")
graph.add_edge("update_memory", END)
graph.add_edge("chat", END)

# Runtime app used by the FastAPI server (keeps existing memory behavior).
app = graph.compile(checkpointer=MemorySaver())

# Studio/LangGraph API app must not provide a custom checkpointer; the
# platform/runtime manages persistence itself.
studio_app = graph.compile()


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_agent(query: str, mode: str = "auto") -> None:
    thread_id = f"thread_{uuid4().hex[:8]}"
    set_thread_id(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    console.print(f"[dim]Thread: {thread_id} | Mode: {mode}[/dim]")

    first = app.invoke(
        {"messages": [HumanMessage(content=query)], "mode": mode, "resolved_intent": None},
        config=config,
    )
    resolved = first.get("resolved_intent", "research")
    console.print(f"[dim]Resolved intent: {resolved}[/dim]")

    if resolved == "chat":
        # Chat response is already emitted via events; print last message
        msgs = first.get("messages", [])
        if msgs:
            last = msgs[-1]
            content = last.content if hasattr(last, "content") else ""
            console.print(Panel(str(content), title="💬 Chat Response", border_style="cyan"))
        return

    # Research: handle HITL
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

    result = app.invoke(
        Command(resume=resume_value),
        config=config,
    )
    msgs = result.get("messages", [])
    if msgs:
        last = msgs[-1]
        content = last.content if hasattr(last, "content") else ""
        if content:
            console.print(Panel(str(content), title="📄 Final Report", border_style="green", padding=(1, 2)))


if __name__ == "__main__":
    run_agent("What are the latest news about Apple, and give me the price from the last 5 years?")

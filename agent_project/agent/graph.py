"""Graph assembly, compilation, and CLI runner."""

import json
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from rich.panel import Panel

from agent.nodes import (
    execute_plan_node,
    plan_node,
    review_plan_node,
    route_after_review,
    synthesize_node,
)
from agent.state import AgentState
from memory.session import update_memory_node
from tools import SHOW_TOKEN_STREAM
from utils.formatting import console, format_plan
from utils.persistence import get_run_dir, set_thread_id

# ---------------------------------------------------------------------------
# Graph
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
                    final_content = (
                        getattr(final_msg, "content", "")
                        if not isinstance(final_msg, dict)
                        else final_msg.get("content", "")
                    )
                    if final_content:
                        console.print(Panel(final_content, title="📄 Final Report", border_style="green", padding=(1, 2)))
        elif chunk["type"] == "messages" and SHOW_TOKEN_STREAM:
            msg_chunk, _ = chunk["data"]
            if msg_chunk.content:
                print(msg_chunk.content, end="", flush=True)
    console.print()


if __name__ == "__main__":
    run_agent(
        "What are the latest news about Apple, and give me the price from the last 5 years?"
    )

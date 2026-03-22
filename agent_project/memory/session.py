"""Session memory: compress a completed plan into a compact memory entry."""

from agent.state import AgentState


def update_memory_node(state: AgentState) -> dict:
    """Compress the completed plan into a compact session memory entry.

    No LLM call — just structured text. The planner reads this on the next
    turn to avoid repeating work. Bounded to ~2000 chars.
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

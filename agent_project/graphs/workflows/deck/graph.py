"""Deck workflow — graph wiring + public sync entrypoint.

Flow::

    START → validate_sources → normalize_all → generate_outline
          → outline_review (HITL, mode-aware)
          → [per_slide_generate → assemble_pptx → finalize → END]
          → [END]                                   (when outline rejected)

The outline review is mode-aware via ``brief.hitl_mode``:
  - "disabled" — node auto-approves, no interrupt.
  - "partial"  — interrupt for outline only (default).
  - "full"     — interrupt for outline + (future) per-slide review.

Public API: ``run_deck_workflow_sync(*, sources, brief, session_id, ...)``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph

from utils import get_run_dir

from .assemble import assemble_pptx_node
from .finalize import finalize_node
from .normalize import normalize_all_node, validate_sources_node
from .outline import generate_outline_node
from .review import outline_review_node, route_after_outline_review
from .slides import per_slide_generate_node
from .state import DeckBrief, DeckSource, DeckState

logger = logging.getLogger(__name__)

__all__ = [
    "deck_workflow_app",
    "run_deck_workflow_sync",
]


# ---------------------------------------------------------------------------
# Graph build
# ---------------------------------------------------------------------------

_graph = StateGraph(DeckState)

_graph.add_node("validate_sources", validate_sources_node)
_graph.add_node("normalize_all", normalize_all_node)
_graph.add_node("generate_outline", generate_outline_node)
_graph.add_node("outline_review", outline_review_node)
_graph.add_node("per_slide_generate", per_slide_generate_node)
_graph.add_node("assemble_pptx", assemble_pptx_node)
_graph.add_node("finalize_deck", finalize_node)

_graph.add_edge(START, "validate_sources")
_graph.add_edge("validate_sources", "normalize_all")
_graph.add_edge("normalize_all", "generate_outline")
_graph.add_edge("generate_outline", "outline_review")
_graph.add_conditional_edges(
    "outline_review",
    route_after_outline_review,
    {"per_slide_generate": "per_slide_generate", END: END},
)
_graph.add_edge("per_slide_generate", "assemble_pptx")
_graph.add_edge("assemble_pptx", "finalize_deck")
_graph.add_edge("finalize_deck", END)

deck_workflow_app = _graph.compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Public sync entrypoint
# ---------------------------------------------------------------------------


def _build_initial_state(
    *,
    sources: list[dict | DeckSource],
    brief: DeckBrief | dict,
    session_id: str,
    parent_step_id: str,
) -> DeckState:
    """Normalize public-API inputs into the LangGraph state dict."""
    # Accept Pydantic models or raw dicts for sources.
    source_dicts: list[dict] = []
    for s in sources:
        if hasattr(s, "model_dump"):
            source_dicts.append(s.model_dump())  # type: ignore[attr-defined]
        elif isinstance(s, dict):
            source_dicts.append(s)
        else:
            raise TypeError(f"Source must be dict or DeckSource model, got {type(s).__name__}")

    if isinstance(brief, DeckBrief):
        brief_dict = brief.model_dump()
    elif isinstance(brief, dict):
        # Validate now to fail fast with a clear error.
        brief_dict = DeckBrief.model_validate(brief).model_dump()
    else:
        raise TypeError(f"brief must be dict or DeckBrief, got {type(brief).__name__}")

    return {
        "sources": source_dicts,
        "brief": brief_dict,
        "blocks": [],
        "blocks_by_id": {},
        "outline": {},
        "outline_approved": False,
        "outline_feedback": None,
        "slides": [],
        "artifacts": [],
        "pptx_path": None,
        "pdf_path": None,
        "html_path": None,
        "deck_run_id": None,
        "deck_output_path": None,
        "session_id": session_id,
        "parent_step_id": parent_step_id,
        "hitl_mode": str(brief_dict.get("hitl_mode") or "partial"),
    }


def run_deck_workflow_sync(
    *,
    sources: list[dict | DeckSource],
    brief: DeckBrief | dict,
    session_id: str = "",
    parent_step_id: str = "workflow_deck",
) -> dict:
    """Run the deck workflow synchronously and return the result payload.

    Behavior by HITL mode:
      - ``"disabled"``: runs straight through, returns final payload.
      - ``"partial"`` / ``"full"``: pauses at the outline-review interrupt;
        returns a structured ``__deck_hitl__`` envelope with the outline +
        block inventory.  Caller resumes via LangGraph ``Command(resume=...)``.

    Returns the JSON contents of ``decks/deck_output.json`` on success.
    """
    initial_state = _build_initial_state(
        sources=sources,
        brief=brief,
        session_id=session_id,
        parent_step_id=parent_step_id,
    )
    config = {"configurable": {"thread_id": f"{get_run_dir().name}_deck"}}

    try:
        result = deck_workflow_app.invoke(initial_state, config=config)
    except GraphInterrupt as gi:
        # Legacy LangGraph path — interrupt raised as exception.
        return _build_hitl_envelope_from_exc(gi)

    # Modern LangGraph: invoke() returns when paused; check graph state.
    graph_state = deck_workflow_app.get_state(config)
    if graph_state.next:
        # Graph is paused at an interrupt — outline awaiting review.
        return _build_hitl_envelope_from_state(result, graph_state)

    # Graph terminated. Two outcomes: rejected (no slides) or completed.
    if result.get("outline_approved") is False:
        logger.info("Deck outline rejected — returning reject envelope.")
        return {
            "__deck_rejected__": True,
            "workflow": "deck",
            "status": "rejected",
            "feedback": result.get("outline_feedback"),
            "outline": result.get("outline", {}),
            "message": "Deck outline was rejected by the user. No PPTX was produced.",
        }

    deck_output_path = result.get("deck_output_path")
    if not deck_output_path:
        raise RuntimeError("Deck workflow finished without writing deck_output.json")
    from pathlib import Path  # noqa: PLC0415
    return json.loads(Path(deck_output_path).read_text(encoding="utf-8"))


def _build_hitl_envelope_from_exc(gi: GraphInterrupt) -> dict:
    """Build HITL envelope from a raised GraphInterrupt (legacy LangGraph)."""
    payload: dict[str, Any] = {}
    if gi.args:
        raw = gi.args[0]
        if isinstance(raw, dict):
            payload = raw
    logger.info(
        "Deck interrupted (exception path) — slides=%d",
        len(payload.get("outline", {}).get("slides", [])),
    )
    return {
        "__deck_hitl__": True,
        "workflow": "deck",
        "outline": payload.get("outline", {}),
        "blocks_preview": payload.get("blocks_preview", []),
        "hitl_mode": payload.get("hitl_mode"),
        "message": (
            "Deck outline ready for review. Present these slides to the user "
            "for approve/reject/edit. Resume with Command(resume={'action':'approve'})"
            " or include 'outline' to edit."
        ),
    }


def _build_hitl_envelope_from_state(result: dict, graph_state: Any) -> dict:
    """Build HITL envelope when invoke() returned paused state (modern LangGraph)."""
    # Pull the interrupt payload from the paused task's interrupts list if present.
    interrupt_payload: dict = {}
    for task in (graph_state.tasks or []):
        interrupts = getattr(task, "interrupts", None) or []
        for itr in interrupts:
            val = getattr(itr, "value", None)
            if isinstance(val, dict):
                interrupt_payload = val
                break
        if interrupt_payload:
            break

    outline = interrupt_payload.get("outline") or result.get("outline", {})
    blocks_preview = interrupt_payload.get("blocks_preview") or []
    hitl_mode = interrupt_payload.get("hitl_mode") or result.get("hitl_mode")

    logger.info(
        "Deck interrupted (state path) — paused at %s, slides=%d",
        graph_state.next, len(outline.get("slides", [])),
    )
    return {
        "__deck_hitl__": True,
        "workflow": "deck",
        "outline": outline,
        "blocks_preview": blocks_preview,
        "hitl_mode": hitl_mode,
        "message": (
            "Deck outline ready for review. Present these slides to the user "
            "for approve/reject/edit. Resume with Command(resume={'action':'approve'})"
            " or include 'outline' to edit."
        ),
    }

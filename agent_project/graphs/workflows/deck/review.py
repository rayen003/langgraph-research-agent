"""HITL outline review — three modes routed off ``brief.hitl_mode``.

Modes:
  - ``disabled``: bypass — auto-approve outline, no interrupt.
  - ``partial``:  interrupt for outline approval only (default).
  - ``full``:     interrupt for outline approval AND each slide later
                  (slide-level HITL is handled in ``slides.py``).

This node only handles the OUTLINE gate.  Slide-level HITL is the
``slides.py`` concern.

On interrupt, the payload sent to the UI contains:
  - outline (full ``DeckOutline`` dict)
  - blocks_inventory (compact block summaries for context)
  - mode metadata so the frontend can show 'partial' vs 'full' badging.

The user response shape:
  {"action": "approve" | "reject" | "edit",
   "outline": <optional edited outline dict>,
   "feedback": <optional str>}
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from langgraph.types import interrupt
except ImportError:  # LangGraph >=1.0 style
    from langgraph.types import Interrupt

    def interrupt(payload: dict):  # type: ignore[no-redef]
        raise Interrupt(payload)

from .activity import emit_step, emit_workflow_terminal
from .state import DeckOutline, DeckState

logger = logging.getLogger(__name__)


def outline_review_node(state: DeckState) -> dict:
    """Gate slide rendering on user approval of the outline.

    Three behaviors based on ``state['hitl_mode']``:
      - ``disabled``: auto-approve, emit a 'skipped' substep, continue.
      - ``partial`` / ``full``: call ``interrupt()`` and wait for user input.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    hitl_mode = str(state.get("hitl_mode") or "partial").lower()
    outline_dict = state.get("outline") or {}

    if hitl_mode == "disabled":
        emit_step("outline_review", "skipped", parent_step_id, {
            "summary_line": "HITL disabled — auto-approving outline.",
            "hitl_mode": hitl_mode,
            "slide_count": len(outline_dict.get("slides") or []),
        })
        logger.info("Deck outline auto-approved (hitl_mode=disabled).")
        return {"outline_approved": True, "outline_feedback": None}

    # Build a compact block inventory for UI context (kept lightweight).
    blocks = state.get("blocks") or []
    blocks_preview = [
        {
            "block_id": b["block_id"],
            "kind": b["kind"],
            "title": str(b.get("title") or "")[:100],
            "source_type": b.get("source_type"),
        }
        for b in blocks
    ]

    emit_step("outline_review", "awaiting_input", parent_step_id, {
        "summary_line": (
            f"Awaiting outline approval ({len(outline_dict.get('slides') or [])} slides, "
            f"hitl_mode={hitl_mode})."
        ),
        "hitl_mode": hitl_mode,
        "outline": outline_dict,
        "blocks_preview": blocks_preview,
    })

    decision = interrupt({
        "action": "review_outline",
        "workflow": "deck",
        "hitl_mode": hitl_mode,
        "message": (
            "Approve, edit, or reject the deck outline before per-slide "
            "content generation runs."
        ),
        "outline": outline_dict,
        "blocks_preview": blocks_preview,
        "choices": ["approve", "reject", "edit"],
    })

    action = str(decision.get("action") or "approve").lower()
    feedback = decision.get("feedback")

    if action == "reject":
        emit_step("outline_review", "rejected", parent_step_id, {
            "summary_line": "Outline rejected — deck workflow will halt.",
            "feedback": feedback,
        })
        # Terminate parent span — graph routes to END after this, so the
        # finalize_node 'completed' emit won't fire.  UI needs the close-out.
        emit_workflow_terminal(
            parent_step_id=parent_step_id,
            status="error",
            payload={
                "summary_line": "Deck rejected at outline review",
                "feedback": feedback,
            },
        )
        return {"outline_approved": False, "outline_feedback": feedback}

    if action == "edit":
        edited_outline_raw = decision.get("outline")
        if isinstance(edited_outline_raw, dict):
            try:
                edited = DeckOutline.model_validate(edited_outline_raw)
                emit_step("outline_review", "edited", parent_step_id, {
                    "summary_line": (
                        f"Outline edited and approved ({len(edited.slides)} slides)."
                    ),
                    "slide_count": len(edited.slides),
                    "feedback": feedback,
                })
                return {
                    "outline": edited.model_dump(),
                    "outline_approved": True,
                    "outline_feedback": feedback,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("Edited outline failed validation: %s — falling back to original.", exc)
                emit_step("outline_review", "warning", parent_step_id, {
                    "summary_line": f"Edited outline invalid; using original. ({exc})",
                    "feedback": feedback,
                })
                # Fall through to approve original.

    emit_step("outline_review", "approved", parent_step_id, {
        "summary_line": f"Outline approved as-is ({len(outline_dict.get('slides') or [])} slides).",
        "feedback": feedback,
    })
    return {"outline_approved": True, "outline_feedback": feedback}


def route_after_outline_review(state: DeckState) -> str:
    """Route to per-slide generation if approved, else terminate the workflow."""
    from langgraph.graph import END  # noqa: PLC0415
    return "per_slide_generate" if state.get("outline_approved") else END


__all__ = ["outline_review_node", "route_after_outline_review"]

"""Finalize stage — persist deck snapshot to disk + KG.

Writes ``deck_output.json`` next to the PPTX containing the full snapshot:
brief, sources manifest, blocks, outline, rendered slides, artifact paths.
This is the analogue of ``dcf_output.json`` and is the canonical record of
what was generated.

KG snapshot (Layer 3 — run artifacts, immutable):
  - ``deck_run`` anchor node (deterministic ID from session + title + brief hash).
  - ``deck_slide`` child nodes (one per rendered slide, content-hashed IDs).

KG writes are best-effort: a failure logs a warning and continues — the
deck file on disk is the source of truth, the KG is the searchable index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from utils import get_run_dir

from .activity import emit_step, emit_workflow_terminal

logger = logging.getLogger(__name__)


def finalize_node(state: dict) -> dict:
    """Write deck_output.json + KG deck_run snapshot + terminal activity."""
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    emit_step("finalize_deck", "start", parent_step_id)

    brief = state.get("brief") or {}
    sources = state.get("sources") or []
    blocks = state.get("blocks") or []
    outline = state.get("outline") or {}
    slides = state.get("slides") or []
    pptx_path = state.get("pptx_path")

    # ── Build payload ───────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "workflow": "deck",
        "generated_at": time.time(),
        "brief": brief,
        "sources_manifest": [
            {"index": i, "type": s.get("type"), "summary": _source_summary(s)}
            for i, s in enumerate(sources)
        ],
        "block_count": len(blocks),
        "blocks": blocks,
        "outline": outline,
        "slides": slides,
        "pptx_path": pptx_path,
        "pdf_path": state.get("pdf_path"),
        "html_path": state.get("html_path"),
        "session_id": state.get("session_id", ""),
    }

    # ── Disk write ──────────────────────────────────────────────────────────
    run_dir = Path(get_run_dir())
    decks_dir = run_dir / "decks"
    decks_dir.mkdir(parents=True, exist_ok=True)
    deck_output_path = decks_dir / "deck_output.json"
    deck_output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Deck output written to %s", deck_output_path)

    # ── KG snapshot (best-effort) ───────────────────────────────────────────
    # Resolve DCF run_id if any source is a dcf_output with a KG node ID.
    dcf_run_id: str | None = None
    for src in sources:
        if isinstance(src, dict) and src.get("type") == "dcf_output":
            dcf_run_id = src.get("run_id") or None
            if dcf_run_id:
                break

    deck_run_id = _try_write_kg_snapshot(
        session_id=state.get("session_id", ""),
        brief=brief,
        sources=sources,
        slides=slides,
        pptx_path=pptx_path,
        dcf_run_id=dcf_run_id,
    )

    summary_line = f"Deck written: {Path(pptx_path).name if pptx_path else 'n/a'} ({len(slides)} slides)"
    emit_step("finalize_deck", "complete", parent_step_id, {
        "summary_line": summary_line,
        "deck_output_path": str(deck_output_path),
        "pptx_path": pptx_path,
        "deck_run_id": deck_run_id,
        "slide_count": len(slides),
    })

    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="completed",
        payload={
            "summary_line": summary_line,
            "slide_count": len(slides),
            "pptx_path": pptx_path,
            "pdf_path": state.get("pdf_path"),
            "html_path": state.get("html_path"),
            "deck_output_path": str(deck_output_path),
            "deck_title": brief.get("title"),
        },
    )

    return {
        "deck_output_path": str(deck_output_path),
        "deck_run_id": deck_run_id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_summary(src: dict) -> str:
    """One-line description of a source for the manifest."""
    t = src.get("type", "?")
    if t == "dcf_output":
        ref = src.get("run_id") or src.get("payload_path") or "inline"
        return f"DCF output ({ref})"
    if t == "document":
        ids = src.get("doc_ids") or []
        return f"{len(ids)} document(s)"
    if t == "web":
        return f"{len(src.get('urls') or [])} URL(s)"
    if t == "manual_text":
        return f"manual: {str(src.get('title') or '')[:50]}"
    if t == "kg_subgraph":
        return f"KG anchor: {src.get('anchor_id', '?')}"
    if t == "chart_artifact":
        return f"chart: {Path(str(src.get('path') or '')).name}"
    return t


def _try_write_kg_snapshot(
    *,
    session_id: str,
    brief: dict,
    sources: list[dict],
    slides: list[dict],
    pptx_path: str | None,
    dcf_run_id: str | None = None,
) -> str | None:
    """Best-effort KG write — failure logs warning, does not raise."""
    if not session_id:
        logger.debug("No session_id provided — skipping KG snapshot.")
        return None
    try:
        from kg.cache import get_cache  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("KG module unavailable (%s) — skipping snapshot.", exc)
        return None

    try:
        cache = get_cache()
        brief_hash = hashlib.sha256(
            json.dumps(brief, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:10]
        # Anchor "ticker" — when the deck has a source DCF run, nest it under
        # that company's ticker (parsed from the dcf_run node id, e.g.
        # "META::dcf_run::workflow_dcf::meta" → "META") so the deck groups under
        # the company in the KG instead of becoming its own top-level ticker.
        # Falls back to a session-scoped anchor for DCF-less decks.
        deck_anchor = f"deck::{session_id}"
        if dcf_run_id and "::" in dcf_run_id:
            parsed_ticker = dcf_run_id.split("::", 1)[0].strip()
            if parsed_ticker:
                deck_anchor = parsed_ticker
        deck_run_field = f"run::{brief_hash}"

        # Deck run anchor (Layer 3 — immutable run artifact)
        cache.put(
            ticker=deck_anchor,
            node_type="deck_run",
            field=deck_run_field,
            value={
                "title": brief.get("title"),
                "audience": brief.get("audience"),
                "slide_count": len(slides),
                "source_count": len(sources),
                "source_types": sorted({s.get("type") for s in sources if s.get("type")}),
                "pptx_path": pptx_path,
                "generated_at": time.time(),
            },
            source="workflow_deck",
            confidence=1.0,
            session_id=session_id,
            run_id=brief_hash,
        )

        deck_run_id = f"{deck_anchor}::deck_run::{brief_hash}::{deck_run_field}"

        # One node per slide (run-scoped, content-keyed for stability).
        # Each slide also gets a HAS_SLIDE edge from the deck_run so slides
        # nest under their deck instead of floating as orphans.
        for s in slides:
            slide_id = s.get("slide_id", "")
            if not slide_id:
                continue
            cache.put(
                ticker=deck_anchor,
                node_type="deck_slide",
                field=slide_id,
                value={
                    "layout": s.get("layout"),
                    "title": s.get("title"),
                    "citations": s.get("citations") or [],
                },
                source="workflow_deck",
                confidence=1.0,
                session_id=session_id,
                run_id=brief_hash,
            )
            try:
                cache.add_edge(
                    src_id=deck_run_id,
                    tgt_id=f"{deck_anchor}::deck_slide::{brief_hash}::{slide_id}",
                    relation="HAS_SLIDE",
                    session_id=session_id,
                    confidence=1.0,
                    source="workflow_deck",
                )
            except Exception:
                logger.warning("Failed to write HAS_SLIDE edge — continuing.", exc_info=True)

        logger.info("KG snapshot written: %s + %d slide nodes", deck_run_id, len(slides))

        # ── HAS_DECK edge: dcf_run → deck_run ──────────────────────────────
        if dcf_run_id:
            try:
                cache.add_edge(
                    src_id=dcf_run_id,
                    tgt_id=deck_run_id,
                    relation="HAS_DECK",
                    session_id=session_id,
                    confidence=1.0,
                    source="workflow_deck",
                )
                logger.info("KG edge: %s --HAS_DECK--> %s", dcf_run_id, deck_run_id)
            except Exception:
                logger.warning("Failed to write HAS_DECK edge — continuing.", exc_info=True)

        return deck_run_id
    except Exception:
        logger.warning("KG snapshot write failed — continuing.", exc_info=True)
        return None


__all__ = ["finalize_node"]

"""Normalize stage — parse sources + fan out to adapters.

Two nodes:
  - ``validate_sources_node``: Pydantic-parses raw source dicts into typed
    ``DeckSource`` models.  Loud failure with field-level errors when input
    is malformed.  No I/O.
  - ``normalize_all_node``: iterates validated sources, dispatches each to
    its adapter, concatenates blocks.  Builds the ``blocks_by_id`` lookup
    map used by downstream slide generation.

Adapter failures are isolated — one bad source does not abort the whole
deck.  The failing source emits an ``adapter_failure`` substep and is
skipped; remaining sources continue.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .activity import emit_step, emit_workflow_terminal
from .adapters import get_adapter
from .state import DeckSource, DeckState, NormalizedBlock

logger = logging.getLogger(__name__)


# Re-usable parser — Pydantic resolves the discriminator on every call.
_SOURCE_PARSER: TypeAdapter[DeckSource] = TypeAdapter(DeckSource)


def validate_sources_node(state: DeckState) -> dict:
    """Parse raw source dicts → typed ``DeckSource`` models.

    Stores validated dicts back in ``state['sources']`` (each carries its
    ``type`` discriminator + validated fields).  Raises on any malformed
    source — better to fail at the boundary than render garbage later.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    raw_sources = state.get("sources") or []
    brief = state.get("brief") or {}

    # Bookend the parent workflow span at the first node so the UI shows the
    # deck group while substeps stream in (mirrors DCF's normalize_input_node).
    emit_workflow_terminal(
        parent_step_id=parent_step_id,
        status="started",
        payload={
            "summary_line": str(brief.get("title") or "Deck"),
            "deck_title": brief.get("title"),
            "audience": brief.get("audience"),
            "hitl_mode": brief.get("hitl_mode"),
            "source_count": len(raw_sources),
        },
    )
    emit_step("validate_sources", "start", parent_step_id, {"count": len(raw_sources)})

    if not raw_sources:
        emit_step("validate_sources", "complete", parent_step_id, {
            "summary_line": "No sources provided — nothing to render.",
            "valid_count": 0,
        })
        return {"sources": []}

    validated: list[dict] = []
    errors: list[dict] = []
    for idx, raw in enumerate(raw_sources):
        try:
            parsed = _SOURCE_PARSER.validate_python(raw)
            validated.append(parsed.model_dump())
        except ValidationError as exc:
            errors.append({
                "source_index": idx,
                "raw_type": raw.get("type") if isinstance(raw, dict) else None,
                "errors": exc.errors(),
            })

    if errors:
        # Loud failure: surface every problem at once so the caller can fix
        # one round-trip rather than peeling errors one at a time.
        emit_step("validate_sources", "error", parent_step_id, {
            "summary_line": f"{len(errors)} source(s) failed validation",
            "errors": errors,
        })
        raise ValueError(
            f"Deck source validation failed for {len(errors)} item(s): "
            f"{errors}"
        )

    type_counts: dict[str, int] = {}
    for s in validated:
        type_counts[s["type"]] = type_counts.get(s["type"], 0) + 1
    emit_step("validate_sources", "complete", parent_step_id, {
        "summary_line": f"{len(validated)} source(s) validated: " + ", ".join(
            f"{t}×{c}" for t, c in sorted(type_counts.items())
        ),
        "valid_count": len(validated),
        "type_counts": type_counts,
    })
    return {"sources": validated}


def normalize_all_node(state: DeckState) -> dict:
    """Dispatch each validated source to its adapter → concat blocks.

    Per-source failures are isolated (warning + skip), not graph-aborting.
    Total empty result IS an error — a deck with zero blocks is unrenderable.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_deck"
    sources = state.get("sources") or []
    session_id = state.get("session_id") or ""
    emit_step("normalize_all", "start", parent_step_id, {"source_count": len(sources)})

    blocks: list[NormalizedBlock] = []
    evidence_index: dict[str, dict] = {}
    per_source_summary: list[dict[str, Any]] = []

    for idx, src_dict in enumerate(sources):
        src_type = src_dict.get("type", "<unknown>")
        try:
            # Re-parse to typed instance (state stores dicts only).
            typed_src = _SOURCE_PARSER.validate_python(src_dict)
            adapter = get_adapter(src_type)
            produced = adapter.normalize(typed_src, session_id=session_id)
            blocks.extend(produced)
            # Optional evidence hook — adapters with a source-level evidence
            # corpus expose it for readable citations on the references slide.
            collect = getattr(adapter, "collect_evidence", None)
            if callable(collect):
                try:
                    for eid, item in (collect(typed_src, session_id=session_id) or {}).items():
                        evidence_index.setdefault(eid, item)
                except Exception:  # noqa: BLE001
                    logger.warning("collect_evidence failed for %s (idx=%d) — skipping.", src_type, idx)
            per_source_summary.append({
                "source_index": idx,
                "source_type": src_type,
                "block_count": len(produced),
            })
            logger.info("Adapter %s produced %d blocks (source idx=%d)", src_type, len(produced), idx)
        except NotImplementedError as exc:
            emit_step("adapter_failure", "warning", parent_step_id, {
                "summary_line": f"Source {idx} ({src_type}) — adapter not implemented",
                "source_index": idx,
                "source_type": src_type,
                "error": str(exc),
            })
            logger.warning("Adapter for %s not implemented — skipping source idx=%d", src_type, idx)
            per_source_summary.append({
                "source_index": idx,
                "source_type": src_type,
                "block_count": 0,
                "error": "not_implemented",
            })
        except Exception as exc:  # noqa: BLE001
            emit_step("adapter_failure", "warning", parent_step_id, {
                "summary_line": f"Source {idx} ({src_type}) failed: {exc}",
                "source_index": idx,
                "source_type": src_type,
                "error": str(exc),
            })
            logger.exception("Adapter %s failed for source idx=%d", src_type, idx)
            per_source_summary.append({
                "source_index": idx,
                "source_type": src_type,
                "block_count": 0,
                "error": str(exc),
            })

    if not blocks:
        emit_step("normalize_all", "error", parent_step_id, {
            "summary_line": "No blocks produced — every adapter returned empty or failed.",
            "per_source": per_source_summary,
        })
        raise ValueError("normalize_all_node produced zero blocks; cannot proceed.")

    block_dicts = [b.model_dump() for b in blocks]
    blocks_by_id = {b["block_id"]: b for b in block_dicts}

    kind_counts: dict[str, int] = {}
    for b in block_dicts:
        kind_counts[b["kind"]] = kind_counts.get(b["kind"], 0) + 1

    emit_step("normalize_all", "complete", parent_step_id, {
        "summary_line": f"{len(block_dicts)} block(s) from {len(sources)} source(s): " + ", ".join(
            f"{k}×{v}" for k, v in sorted(kind_counts.items())
        ),
        "block_count": len(block_dicts),
        "kind_counts": kind_counts,
        "per_source": per_source_summary,
    })

    return {"blocks": block_dicts, "blocks_by_id": blocks_by_id, "evidence_index": evidence_index}


__all__ = ["validate_sources_node", "normalize_all_node"]

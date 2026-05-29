"""Normalize LLM-provided run_deck_workflow args into valid deck inputs.

Chat models often pass placeholder strings, partial DCF dicts, or nest ``brief``
inside ``sources``.  Resolve to concrete sources + brief before validation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from utils import get_run_dir

from .state import DeckBrief

logger = logging.getLogger(__name__)

_KNOWN_SOURCE_TYPES = {
    "dcf_output",
    "document",
    "manual_text",
    "web",
    "kg_subgraph",
    "chart_artifact",
}


def is_full_dcf_payload(payload: dict[str, Any] | None) -> bool:
    """True when dict looks like a completed ``dcf_output.json`` payload."""
    if not isinstance(payload, dict):
        return False
    if not payload.get("ticker"):
        return False
    return "valuation" in payload or "assumptions" in payload


def default_dcf_source(*, run_dir: Path | None = None) -> dict[str, str] | None:
    path = (run_dir or get_run_dir()) / "dcf_output.json"
    if not path.exists():
        return None
    return {"type": "dcf_output", "payload_path": str(path)}


def default_sensitivity_source(
    dcf_payload: dict[str, Any] | None,
    *,
    ticker: str = "?",
    run_dir: Path | None = None,
) -> dict[str, str] | None:
    sens_path = (dcf_payload or {}).get("sensitivity_chart")
    if not sens_path:
        # Disk discovery fallback — DCF workflow doesn't always persist the
        # chart path into dcf_output.json. Glob the run's artifacts dir.
        rd = run_dir or get_run_dir()
        artifacts_dir = rd / "artifacts"
        if artifacts_dir.exists():
            candidates = sorted(artifacts_dir.glob("sensitivity_*.png"))
            if candidates:
                sens_path = str(candidates[0])
    if not sens_path or not Path(str(sens_path)).exists():
        return None
    return {
        "type": "chart_artifact",
        "path": str(sens_path),
        "caption": f"{ticker} — WACC × terminal growth sensitivity",
    }


def _coerce_sources(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(raw, list):
        return raw
    return []


def _sanitize_source_dict(item: dict[str, Any]) -> dict[str, Any] | None:
    source_type = item.get("type")
    if source_type not in _KNOWN_SOURCE_TYPES:
        return None

    cleaned = dict(item)
    if source_type == "dcf_output":
        inline = cleaned.get("payload_inline")
        if inline is not None and not is_full_dcf_payload(inline):
            cleaned.pop("payload_inline", None)
        if not cleaned.get("payload_inline") and not cleaned.get("payload_path") and not cleaned.get("run_id"):
            default = default_dcf_source()
            if default:
                cleaned["payload_path"] = default["payload_path"]
    return cleaned


def sanitize_sources(raw_sources: Any) -> list[dict[str, Any]]:
    """Keep only valid source dicts; drop LLM placeholder strings."""
    valid: list[dict[str, Any]] = []
    for item in _coerce_sources(raw_sources):
        if not isinstance(item, dict):
            logger.debug("Dropped non-dict deck source: %r", item)
            continue
        cleaned = _sanitize_source_dict(item)
        if cleaned:
            valid.append(cleaned)
    return valid


def _normalize_brief(raw_brief: Any, *, ticker: str) -> dict[str, Any]:
    if isinstance(raw_brief, str) and raw_brief.strip():
        raw_brief = {"title": raw_brief.strip()}
    if not isinstance(raw_brief, dict):
        raw_brief = {}

    title = str(raw_brief.get("title") or f"{ticker} — DCF Investment Case").strip()
    merged = {
        "title": title,
        "audience": raw_brief.get("audience") or "ic",
        "hitl_mode": raw_brief.get("hitl_mode") or "partial",
        "slide_count_target": raw_brief.get("slide_count_target"),
        "tone": raw_brief.get("tone"),
        "must_cover": raw_brief.get("must_cover") or [],
        # Phase B theme tokens — passed through; assemble.py resolves defaults.
        "density": raw_brief.get("density"),
        "accent": raw_brief.get("accent"),
        "font_scale": raw_brief.get("font_scale"),
    }
    return DeckBrief.model_validate(merged).model_dump()


def resolve_deck_workflow_inputs(
    raw_sources: Any,
    raw_brief: Any,
    *,
    dcf_payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return validated ``(sources, brief)`` with DCF/chart defaults applied."""
    payload = dcf_payload
    if payload is None:
        path = get_run_dir() / "dcf_output.json"
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if is_full_dcf_payload(loaded):
                    payload = loaded
            except (json.JSONDecodeError, OSError):
                payload = None

    ticker = str((payload or {}).get("ticker") or "?").upper()
    sources = sanitize_sources(raw_sources)

    has_dcf = any(s.get("type") == "dcf_output" for s in sources)
    if not has_dcf:
        default = default_dcf_source()
        if default:
            sources.insert(0, default)
            logger.info("Deck inputs: injected default dcf_output source from disk.")

    has_chart = any(s.get("type") == "chart_artifact" for s in sources)
    if not has_chart:
        chart = default_sensitivity_source(payload, ticker=ticker)
        if chart:
            sources.append(chart)

    if not sources:
        raise ValueError(
            "No deck sources available. Run a DCF first or pass valid source dicts "
            "(dcf_output, document, manual_text, chart_artifact, etc.)."
        )

    brief = _normalize_brief(raw_brief, ticker=ticker)
    return sources, brief


__all__ = [
    "default_dcf_source",
    "default_sensitivity_source",
    "is_full_dcf_payload",
    "resolve_deck_workflow_inputs",
    "sanitize_sources",
]

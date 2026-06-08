"""Assumption building: merge defaults, web/doc hints, canonical fundamentals, and user overrides.

This is today's build_assumptions logic extracted as-is. It will evolve
toward the LLM-driven assumption memo in a later phase.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .activity import emit_step
from .state import (
    _ASSUMPTION_FIELDS,
    _PROVENANCE_PASSTHROUGH_KEYS,
    _TIER_A_FIELDS,
    clip_to_field_range,
    filter_user_assumption_overrides,
    normalize_assumption_value,
)
from .wacc import resolve_wacc_from_features

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default assumptions
# ---------------------------------------------------------------------------


def _default_assumptions() -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    assumptions = {
        "revenue_growth": 0.08,
        "fcff_margin": 0.17,
        "wacc": 0.10,
        "terminal_growth": 0.025,
        "tax_rate": 0.21,
        "base_revenue": 10000.0,
        "shares_outstanding": 1000.0,
        "net_debt": 5000.0,
    }
    provenance = {
        key: {
            "source": "default",
            "evidence": "Deterministic workflow default.",
            "confidence": 0.35,
        }
        for key in assumptions
    }
    return assumptions, provenance


# ---------------------------------------------------------------------------
# Regex-based extraction (will be replaced by LLM memo)
# ---------------------------------------------------------------------------


def _extract_candidates_from_text(
    text: str,
    *,
    source: str,
    evidence_ref: str,
) -> dict[str, dict[str, Any]]:
    """Parse assumption candidates from raw text using regex heuristics.

    .. warning::
       This is a transitional implementation. The target architecture
       replaces regex extraction with LLM-driven structured extraction
       from the evidence pack.
    """
    compact = " ".join(str(text).split())
    candidates: dict[str, dict[str, Any]] = {}
    for field, spec in _ASSUMPTION_FIELDS.items():
        for alias in spec["aliases"]:
            pattern = (
                rf"(?i)\b{re.escape(alias)}\b"
                rf"[^-\d%]{{0,80}}"
                rf"(-?\d[\d,]*(?:\.\d+)?)"
                rf"\s*(%|percent|bps|x|million|millions|billion|bn)?"
            )
            match = re.search(pattern, compact)
            if not match:
                continue
            raw_number = float(match.group(1).replace(",", ""))
            if (match.group(2) or "").lower() == "bps":
                raw_number /= 100.0
            value = normalize_assumption_value(
                raw_number, match.group(0), str(spec["kind"]),
            )
            if value is None:
                continue
            clipped = clip_to_field_range(field, value)
            if clipped is None:
                continue
            candidates[field] = {
                "value": clipped,
                "source": source,
                "evidence": compact[max(match.start() - 80, 0): match.end() + 120],
                "reference": evidence_ref,
                "confidence": 0.7 if source == "document" else 0.55,
            }
            break
    return candidates


def _infer_assumptions_from_documents(
    session_id: str,
    ticker: str,
) -> dict[str, dict[str, Any]]:
    """Search uploaded documents for assumption hints."""
    if not session_id:
        return {}
    try:
        from documents import hybrid_search, list_docs  # noqa: PLC0415

        ready_docs = [d for d in list_docs(session_id) if d.get("status") == "ready"]
        if not ready_docs:
            return {}
        query = (
            f"{ticker} DCF valuation assumptions revenue growth FCFF margin WACC "
            "terminal growth tax rate net debt shares outstanding"
        )
        results = hybrid_search(query, session_id, n_results=6)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "DCF document assumptions unavailable session_id=%s error=%s",
            session_id, exc,
        )
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for result in results:
        meta = result.get("metadata") or {}
        filename = meta.get("filename", "uploaded document")
        page = meta.get("page", "?")
        candidates = _extract_candidates_from_text(
            str(result.get("text") or ""),
            source="document",
            evidence_ref=f"{filename} p.{page}",
        )
        for field, candidate in candidates.items():
            if field not in merged:
                merged[field] = candidate
    return merged


def _infer_assumptions_from_web(ticker: str) -> dict[str, dict[str, Any]]:
    """Search the web (Exa) for assumption hints."""
    query = (
        f"{ticker} DCF assumptions WACC terminal growth revenue growth "
        "free cash flow margin tax rate net debt shares outstanding"
    )
    try:
        from web_search import search_exa  # noqa: PLC0415

        raw, _summary = search_exa(
            query, num_results=3, search_type="auto", max_characters=1200,
        )
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DCF web assumptions unavailable ticker=%s error=%s", ticker, exc)
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        title = item.get("title") or "web result"
        url = item.get("url") or ""
        text_parts = []
        for highlight in item.get("highlights") or []:
            text_parts.append(str(highlight))
        if item.get("text"):
            text_parts.append(str(item["text"])[:1200])
        candidates = _extract_candidates_from_text(
            " ".join(text_parts),
            source="web",
            evidence_ref=f"{title} {url}".strip(),
        )
        for field, candidate in candidates.items():
            if field not in merged:
                merged[field] = candidate
    return merged


# ---------------------------------------------------------------------------
# Candidate application and conflict filtering
# ---------------------------------------------------------------------------


def _apply_candidates(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> list[str]:
    """Apply candidate assumptions and return the list of fields actually set.

    Each candidate goes through the per-field range check so an unreliable
    upstream source cannot inject an out-of-bounds number.
    """
    applied: list[str] = []
    for field, candidate in candidates.items():
        if field not in assumptions:
            continue
        try:
            value = float(candidate["value"])
        except (TypeError, ValueError, KeyError):
            continue
        clipped = clip_to_field_range(field, value)
        if clipped is None:
            logger.warning(
                "DCF rejected out-of-range candidate field=%s value=%s source=%s",
                field, value, candidate.get("source"),
            )
            continue
        assumptions[field] = clipped
        meta: dict[str, Any] = {
            "source": candidate.get("source", "unknown"),
            "evidence": candidate.get("evidence", ""),
            "reference": candidate.get("reference", ""),
            "confidence": candidate.get("confidence", 0.5),
        }
        for key in _PROVENANCE_PASSTHROUGH_KEYS:
            if key in candidate:
                meta[key] = candidate[key]
        provenance[field] = meta
        applied.append(field)
    return applied


def _filter_tier_a_conflicts(
    candidates: dict[str, dict[str, Any]],
    canonical_fields: set[str],
    *,
    canonical_provenance: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Strip Tier A entries that conflict with canonical fundamentals.

    External hints can still inform Tier B fields (rates), but level
    variables are locked to canonical or user input.
    """
    filtered: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for field, candidate in candidates.items():
        if field in _TIER_A_FIELDS and field in canonical_fields:
            kept = canonical_provenance.get(field, {})
            conflicts.append({
                "field": field,
                "rejected_value": candidate.get("value"),
                "rejected_source": candidate.get("source"),
                "rejected_reference": candidate.get("reference"),
                "kept_value": kept.get("value") if isinstance(kept, dict) else None,
                "kept_source": kept.get("source") if isinstance(kept, dict) else None,
            })
            continue
        filtered[field] = candidate
    return filtered, conflicts


def _apply_overrides(
    assumptions: dict[str, float],
    provenance: dict[str, dict[str, Any]],
    overrides: dict[str, float],
) -> None:
    """Apply user-provided assumption overrides, with range validation."""
    for key, value in filter_user_assumption_overrides(overrides).items():
        if key not in assumptions:
            continue
        normalized = clip_to_field_range(key, float(value))
        if normalized is None:
            logger.warning(
                "DCF ignored out-of-range override field=%s value=%s",
                key, value,
            )
            continue
        assumptions[key] = normalized
        provenance[key] = {
            "source": "user_override",
            "evidence": "User-provided assumption override.",
            "confidence": 1.0,
        }


# ---------------------------------------------------------------------------
# build_assumptions node (today's version — will evolve to assumption memo)
# ---------------------------------------------------------------------------


def build_assumptions_node(state: dict) -> dict:
    """Merge default → web → doc → canonical → user with Tier A locked.

    Precedence (lowest to highest, last writer wins):
        1. deterministic defaults
        2. web hints (Tier B fields only when canonical exists for Tier A)
        3. document hints (overrides web)
        4. canonical fundamentals (locks Tier A, may also refine Tier B)
        5. explicit user overrides
    """
    from .priors import check_assumption_plausibility  # noqa: PLC0415

    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    emit_step("build_assumptions", "start", parent_step_id)

    assumptions, provenance = _default_assumptions()
    ticker = state["ticker"]
    session_id = state.get("session_id") or ""
    allow_external = bool(state.get("allow_external_assumptions", True))
    fundamentals = state.get("fundamentals") or {}

    canonical_fields = {
        field for field in fundamentals.keys()
        if field in _TIER_A_FIELDS
    }
    canonical_preview: dict[str, dict[str, Any]] = {
        field: {"value": meta.get("value"), "source": meta.get("source")}
        for field, meta in fundamentals.items()
    }

    web_candidates_raw = (
        _infer_assumptions_from_web(ticker) if allow_external else {}
    )
    doc_candidates_raw = _infer_assumptions_from_documents(session_id, ticker)

    web_candidates, web_conflicts = _filter_tier_a_conflicts(
        web_candidates_raw, canonical_fields,
        canonical_provenance=canonical_preview,
    )
    doc_candidates, doc_conflicts = _filter_tier_a_conflicts(
        doc_candidates_raw, canonical_fields,
        canonical_provenance=canonical_preview,
    )

    web_applied = _apply_candidates(assumptions, provenance, web_candidates)
    doc_applied = _apply_candidates(assumptions, provenance, doc_candidates)
    canonical_applied = _apply_candidates(assumptions, provenance, fundamentals)

    overrides = state.get("assumption_overrides") or {}
    _apply_overrides(assumptions, provenance, overrides)

    profile = state.get("profile") or "default"
    features = dict(state.get("features") or {})
    wacc_components = resolve_wacc_from_features(
        assumptions, provenance,
        features=features, profile=profile, overrides=overrides,
    )

    conflicts = web_conflicts + doc_conflicts
    if conflicts:
        for c in conflicts:
            logger.info(
                "DCF assumption conflict field=%s rejected_value=%s rejected_source=%s kept_source=%s",
                c.get("field"), c.get("rejected_value"),
                c.get("rejected_source"), c.get("kept_source"),
            )

    assumption_flags = check_assumption_plausibility(assumptions, profile)
    if assumption_flags:
        for flag in assumption_flags:
            logger.warning(
                "DCF assumption flag severity=%s field=%s value=%s expected=%s",
                flag.get("severity"), flag.get("field"),
                flag.get("value"), flag.get("expected"),
            )

    logger.info(
        "DCF build_assumptions assumptions=%s provenance=%s",
        json.dumps(assumptions, ensure_ascii=False),
        json.dumps(provenance, ensure_ascii=False),
    )
    emit_step(
        "build_assumptions", "complete", parent_step_id,
        {
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "canonical_fields": sorted(canonical_applied),
            "document_fields": sorted(doc_applied),
            "web_fields": sorted(web_applied),
            "override_fields": sorted(overrides.keys()),
            "assumption_conflicts": conflicts,
            "assumption_flags": assumption_flags,
            "profile": profile,
            "wacc_components": wacc_components,
        },
    )
    return {
        "assumptions": assumptions,
        "assumption_provenance": provenance,
        "assumption_conflicts": conflicts,
        "assumption_flags": assumption_flags,
        "wacc_components": wacc_components,
    }

"""Canonical value schemas for KG node types.

The KG stores one logical fact per node, but the ``value`` payload shape varies
by ``node_type`` — some are dicts (document_fact, filing, thesis…), some are
bare numeric scalars (market_metric_fund, run_assumption, run_output). Without a
contract this caused real bugs: ``ingest_fact`` once did ``value.get("as_of")``
on a ``market_metric_fund`` float → ``AttributeError`` that was swallowed and
silently dropped every fundamentals write.

This module is the single source of truth for those shapes. It provides:

* ``SCALAR_NODE_TYPES`` / ``RUN_SCOPED_NODE_TYPES`` — used by the ingest path and
  the audit to reason about shape without ad-hoc ``isinstance`` checks.
* Pydantic models per dict-valued node type (``extra="allow"`` so we never drop
  unknown keys — the KG is additive).
* ``validate_kg_value(node_type, value)`` — non-fatal validation that returns
  ``(value, warnings)``. It NEVER raises and NEVER drops data; it surfaces shape
  problems as warnings the write path can log. The KG stays resilient.

Design note: validation is advisory, not enforcing. We log violations rather
than reject writes — losing a fact is worse than storing a slightly-off one.
The schemas double as living documentation of every node_type's value shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# ── Shape categories ─────────────────────────────────────────────────────────

# Value is a bare numeric scalar (float/int), NOT a dict.
SCALAR_NODE_TYPES: frozenset[str] = frozenset({
    "market_metric_fund",
    "market_metric_price",
    "run_assumption",
    "run_output",
})

# Run-scoped nodes are keyed by run_id — intentionally one per DCF run. The
# cross-source audit excludes these (different runs ≠ contradictions).
RUN_SCOPED_NODE_TYPES: frozenset[str] = frozenset({
    "dcf_run",
    "run_assumption",
    "run_output",
    "run_scenario",
    "scenario_result",
    "valuation_result",
})


def is_scalar_node(node_type: str) -> bool:
    """True when the node's value is a bare numeric scalar, not a dict."""
    return node_type in SCALAR_NODE_TYPES


# ── Dict-valued schemas ──────────────────────────────────────────────────────


class _Base(BaseModel):
    # extra="allow" → unknown keys are preserved (KG is additive; we never drop).
    model_config = ConfigDict(extra="allow")


class DocumentFactValue(_Base):
    """Extracted document fact (document_fact + its semantic subtypes)."""
    value: float | None = None
    text: str = ""
    as_of: str = ""
    period: str = ""
    fact_type: str = "other"
    source_doc_id: str | None = None
    source_filename: str | None = None
    source_page: int | None = None
    confidence: float | None = None


class FilingValue(_Base):
    """Uploaded or SEC-fetched filing (one node per document)."""
    filing_type: str = "filing"
    fiscal_period: str = ""
    as_of: str = ""
    section: str = ""
    filename: str = ""
    text: str = ""
    source_doc_id: str | None = None
    url: str = ""
    chunk_count: int | None = None
    page_count: int | None = None


class NewsItemValue(_Base):
    title: str = ""
    headline: str = ""
    summary: str = ""
    url: str = ""
    sentiment: str = ""
    published_at: str = ""


class DriverValue(_Base):
    direction: str = "neutral"
    conviction: str = "medium"


class DcfRunValue(_Base):
    horizon_years: int = 5
    profile: str | None = None
    confidence_label: str = ""
    model_validity: str = "valid"
    invalidation_reason: str = ""
    result_path: str = ""
    thread_id: str = ""
    run_id: str | None = None


class CompanyAnchorValue(_Base):
    ticker: str = ""


# node_type → validating model. Document-fact subtypes all share the same shape.
_REGISTRY: dict[str, type[_Base]] = {
    "document_fact": DocumentFactValue,
    "key_fact": DocumentFactValue,
    "snippet_fact": DocumentFactValue,
    "guidance": DocumentFactValue,
    "risk_factor": DocumentFactValue,
    "competitive_moat": DocumentFactValue,
    "capital_allocation": DocumentFactValue,
    "filing": FilingValue,
    "news_item": NewsItemValue,
    "driver": DriverValue,
    "dcf_run": DcfRunValue,
    "company": CompanyAnchorValue,
}


def model_for(node_type: str) -> type[_Base] | None:
    """Pydantic value model for a node type, or None if unmodeled."""
    return _REGISTRY.get(node_type)


def validate_kg_value(node_type: str, value: Any) -> tuple[Any, list[str]]:
    """Validate a node value against its schema. Non-fatal.

    Returns ``(value, warnings)``. The value is returned UNCHANGED (we never
    mutate or drop). Warnings describe shape mismatches the caller may log.
    Never raises — KG writes must stay resilient.
    """
    warnings: list[str] = []

    if node_type in SCALAR_NODE_TYPES:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value, warnings
        if isinstance(value, dict) and isinstance(value.get("value"), (int, float)):
            return value, warnings  # tolerate {"value": <num>, ...} wrappers
        warnings.append(
            f"{node_type}: expected numeric scalar, got {type(value).__name__}"
        )
        return value, warnings

    model = _REGISTRY.get(node_type)
    if model is None:
        return value, warnings  # unmodeled type — pass through
    if not isinstance(value, dict):
        warnings.append(
            f"{node_type}: expected dict value, got {type(value).__name__}"
        )
        return value, warnings

    try:
        model(**value)  # validate only; original value is what we persist
    except Exception as exc:  # noqa: BLE001 — advisory, never fatal
        warnings.append(f"{node_type}: value failed schema — {exc}")
    return value, warnings

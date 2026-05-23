"""Natural-language KG query — LLM extracts a structured query, then a
deterministic executor traverses the graph and returns subgraph + answer.

The LLM is NEVER allowed to invent graph results — it only translates
intent to a ``KGQuery`` struct. All facts come from the actual graph.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from . import get_cache
from .cache import KGNode

logger = logging.getLogger(__name__)


_QUERY_LLM = ChatOpenAI(
    model=os.getenv("KG_QUERY_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30,
)


class KGQuery(BaseModel):
    """Structured query extracted from natural language."""
    model_config = ConfigDict(extra="forbid")
    intent: str = Field(
        description=(
            "One of: 'lookup' (single fact), 'compare_runs' (across DCF runs), "
            "'why_assumption' (provenance trace), 'list_drivers', "
            "'recent_changes' (diff)."
        )
    )
    ticker: str = Field(description="Anchor ticker (uppercase). Empty if unknown.")
    node_type: str = Field(
        description=(
            "Anchor node_type. Empty if intent doesn't have a specific anchor. "
            "Examples: 'run_assumption', 'driver', 'user_belief', 'run_output'."
        )
    )
    field: str = Field(description="Specific field name; empty if not applicable.")


async def _nl_to_kg_query(question: str, ticker_hint: str | None) -> KGQuery:
    """LLM translates NL question to structured KGQuery."""
    prompt = (
        "You translate analyst questions into a structured KG query.\n\n"
        f"Question: {question}\n"
        f"Ticker hint: {ticker_hint or '(none)'}\n\n"
        "Output JSON only. If ticker unknown, leave empty.\n"
        "Common node_types: 'run_assumption' (wacc, revenue_growth, fcff_margin), "
        "'run_output' (implied_share_price), 'user_belief', 'driver', 'thesis'.\n"
        "Common intents: 'lookup', 'compare_runs', 'why_assumption', "
        "'list_drivers', 'recent_changes'."
    )
    structured = _QUERY_LLM.with_structured_output(KGQuery)
    return structured.invoke(prompt)  # type: ignore[return-value]


def _execute(query: KGQuery, session_id: str) -> dict[str, Any]:
    """Deterministic executor — walks the cache. Returns subgraph + answer text."""
    cache = get_cache()
    if session_id:
        cache.load_session(session_id)
    if query.ticker:
        cache.load_ticker(query.ticker)

    traversal_path: list[str] = []
    matched: list[KGNode] = []
    answer = ""

    if query.intent == "lookup":
        for n in cache._nodes.values():  # noqa: SLF001 — read-only scan
            if query.ticker and n.get("ticker") != query.ticker:
                continue
            if query.node_type and n.get("node_type") != query.node_type:
                continue
            if query.field and n.get("field") != query.field:
                continue
            matched.append(n)
            traversal_path.append(n["id"])
        if matched:
            latest = max(matched, key=lambda n: n.get("updated_at", 0))
            answer = (
                f"{query.ticker} {query.node_type}::{query.field} = "
                f"{latest.get('value')} (source={latest.get('source')}, "
                f"confidence={latest.get('confidence')})"
            )
        else:
            answer = (
                f"No {query.node_type} found for {query.ticker} "
                f"matching field='{query.field}'."
            )

    elif query.intent == "compare_runs":
        # All run_assumption nodes for ticker, grouped by run_id
        runs: dict[str, dict[str, Any]] = {}
        for n in cache._nodes.values():  # noqa: SLF001
            if n.get("ticker") != query.ticker:
                continue
            if n.get("node_type") != "run_assumption":
                continue
            rid = n.get("run_id") or "unknown"
            field = n.get("field", "?")
            runs.setdefault(rid, {})[field] = n.get("value")
            traversal_path.append(n["id"])
            matched.append(n)
        if runs:
            lines = [f"Run comparison for {query.ticker}:"]
            for rid, fields in runs.items():
                lines.append(f"  {rid}: {fields}")
            answer = "\n".join(lines)
        else:
            answer = f"No DCF runs found for {query.ticker}."

    elif query.intent == "why_assumption":
        # Find the run_assumption, follow edges → sources
        for n in cache._nodes.values():  # noqa: SLF001
            if n.get("ticker") != query.ticker:
                continue
            if n.get("node_type") != "run_assumption":
                continue
            if query.field and n.get("field") != query.field:
                continue
            matched.append(n)
            traversal_path.append(n["id"])
            # Follow edges from this node for provenance
            for edge in cache._edges_by_src.get(n["id"], []):  # noqa: SLF001
                traversal_path.append(edge["tgt_id"])
        if matched:
            sources = [n.get("source") for n in matched]
            answer = (
                f"{query.field} for {query.ticker}: "
                f"sources={sources}. "
                "Trace edges in the panel to see provenance."
            )
        else:
            answer = f"No assumption '{query.field}' found for {query.ticker}."

    elif query.intent == "list_drivers":
        drivers = cache.get_drivers(query.ticker)
        matched = drivers
        traversal_path = [d["id"] for d in drivers]
        if drivers:
            lines = [f"Drivers for {query.ticker}:"]
            for d in drivers:
                v = d.get("value")
                lines.append(f"  {d.get('field')}: {v} (source={d.get('source')})")
            answer = "\n".join(lines)
        else:
            answer = f"No drivers recorded for {query.ticker}."

    elif query.intent == "recent_changes":
        # Top 10 most recently updated nodes for the ticker
        nodes = [
            n for n in cache._nodes.values()  # noqa: SLF001
            if n.get("ticker") == query.ticker
        ]
        nodes.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
        matched = nodes[:10]
        traversal_path = [n["id"] for n in matched]
        lines = [f"Recent changes for {query.ticker}:"]
        for n in matched:
            lines.append(
                f"  {n.get('node_type')}::{n.get('field')} = {n.get('value')} "
                f"(source={n.get('source')})"
            )
        answer = "\n".join(lines) if matched else f"No KG data for {query.ticker}."

    else:
        answer = f"Unsupported intent: {query.intent}"

    return {
        "query": query.model_dump(),
        "answer": answer,
        "matched_nodes": matched,
        "traversal_path": traversal_path,
    }


async def run_nl_query(
    question: str,
    ticker: str | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """End-to-end: NL → structured query → execute → return result."""
    try:
        query = await _nl_to_kg_query(question, ticker)
    except Exception:
        logger.exception("KG NL→query translation failed")
        return {
            "query": None,
            "answer": "Could not parse the question. Try a more specific phrasing.",
            "matched_nodes": [],
            "traversal_path": [],
        }
    # If LLM didn't fill ticker but caller gave one, use caller's
    if not query.ticker and ticker:
        query.ticker = ticker.upper()
    return _execute(query, session_id)

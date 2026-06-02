"""Natural-language KG query — LLM extracts a structured query, then a
deterministic executor traverses the graph and returns subgraph + answer.

The LLM is NEVER allowed to invent graph results — it only translates
intent to a ``KGQuery`` struct. All facts come from the actual graph.

Robustness goals (so the panel is actually usable):
  - ticker normalization: "APPLE" / "Apple Inc" → "AAPL" (alias map + cache-aware)
  - fuzzy field matching: "revenue" matches base_revenue / revenue_growth / …
  - optional node_type: empty node_type scans every type
  - traversal output: returns the ANCESTOR PATH (company → run → node) as ordered
    edges so the UI can draw the route to each answer, not just dots.
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


from functools import lru_cache


@lru_cache(maxsize=1)
def _get_query_llm() -> ChatOpenAI:
    """Lazily build the LLM so importing this module never needs an API key
    (deterministic executor + helpers stay unit-testable offline)."""
    return ChatOpenAI(
        model=os.getenv("KG_QUERY_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=30,
    )


# Common company-name / alias → ticker. Extend freely; cache-aware fallback
# below also resolves anything already present in the loaded graph.
_TICKER_ALIASES: dict[str, str] = {
    "APPLE": "AAPL", "APPLE INC": "AAPL",
    "MICROSOFT": "MSFT",
    "META": "META", "FACEBOOK": "META",
    "NVIDIA": "NVDA",
    "ALPHABET": "GOOGL", "GOOGLE": "GOOGL",
    "AMAZON": "AMZN",
    "TESLA": "TSLA",
    "NETFLIX": "NFLX",
    "FORD": "F", "FORD MOTOR": "F",
    "WALMART": "WMT",
    "COCA-COLA": "KO", "COCA COLA": "KO", "COKE": "KO",
}


# Schema description injected into the LLM prompt so retrieval is structure-aware
# (3-layer KG: durable knowledge / run artifacts / evidence). Keep in sync with
# the node types written by the DCF + deck workflows and the evidence layer.
_KG_SCHEMA = """\
KNOWLEDGE GRAPH SCHEMA (3 layers):

Layer 1 — Durable company knowledge (slow-changing, hash-checked):
  • company            : the ticker anchor.
  • company_synthesis  : LLM synthesis of the business — lifecycle stage, margin
                         trajectory, capital-return policy, growth outlook,
                         growth_drivers. THIS IS THE PRIMARY JUSTIFICATION for
                         the assumptions chosen in a DCF run.
  • thesis             : bull/bear thesis + key_drivers (direction, conviction).
  • driver / risk / theme : individual qualitative factors moving the stock.
  • user_belief        : analyst-stated override / conviction.

Layer 2 — DCF run artifacts (immutable per run, keyed by run_id):
  • dcf_run            : a single valuation run (horizon, timestamp).
  • run_assumption     : ONE assumption used by the run (revenue_growth, wacc,
                         fcff_margin, tax_rate, terminal_growth, base_revenue,
                         net_debt, shares_outstanding, …). A run_assumption's
                         VALUE is a number; its JUSTIFICATION lives in Layer 1
                         (company_synthesis growth_outlook + drivers/thesis).
  • run_output         : a computed result (implied_share_price, equity_value,
                         enterprise_value, terminal_pv, pv_cash_flows).
  • run_scenario       : bull/base/bear scenario output.

Layer 3 — Evidence (provenance for Layer 1):
  • news_item, filing, market_metric_fund, market_metric_price, company_lifecycle.

RELATIONS: company -HAS_RUN-> dcf_run; dcf_run -PRODUCES-> run_output;
dcf_run -LOCKED_ASSUMPTION-> run_assumption; company -HAS_SYNTHESIS-> synthesis;
company -HAS_THESIS-> thesis; company -HAS_DRIVER-> driver; dcf_run -HAS_DECK->
deck_run. NOTE: there is NO direct edge from a run_assumption to the synthesis
or drivers that justify it — you must connect them by shared ticker."""


class KGQuery(BaseModel):
    """Structured query extracted from natural language (DETERMINISTIC FALLBACK).

    Kept for the offline keyword executor + unit tests. The primary path is now
    ``_llm_answer_subgraph`` which reads the whole subgraph instead of guessing a
    rigid node_type filter (which was brittle: "revenue" → wrongly pinned to
    run_output and missed base_revenue / revenue_growth).
    """
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


class KGAnswer(BaseModel):
    """LLM answer grounded in the serialized subgraph.

    The model answers in prose AND returns the exact node IDs it used. We never
    trust the prose for highlighting — only validated ``node_ids`` drive the graph
    route, so a hallucinated ID simply gets dropped.
    """
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(description="Concise analyst answer grounded ONLY in the provided nodes.")
    node_ids: list[str] = Field(
        default_factory=list,
        description="Exact ids of the nodes used to answer. Empty if nothing relevant.",
    )


async def _nl_to_kg_query(question: str, ticker_hint: str | None) -> KGQuery:
    """LLM translates NL question to structured KGQuery."""
    prompt = (
        "You translate analyst questions into a structured KG query.\n\n"
        f"Question: {question}\n"
        f"Ticker hint: {ticker_hint or '(none)'}\n\n"
        "Output JSON only. If ticker unknown, leave empty. Use the ticker symbol "
        "(e.g. AAPL), not the company name.\n"
        "Common node_types: 'run_assumption' (wacc, revenue_growth, fcff_margin), "
        "'run_output' (implied_share_price, enterprise_value, equity_value), "
        "'market_metric_fund' (base_revenue, net_debt, shares_outstanding), "
        "'user_belief', 'driver', 'thesis'. Leave node_type EMPTY if unsure — the "
        "executor will search all types.\n"
        "Common intents: 'lookup', 'compare_runs', 'why_assumption', "
        "'list_drivers', 'recent_changes'."
    )
    structured = _get_query_llm().with_structured_output(KGQuery)
    return structured.invoke(prompt)  # type: ignore[return-value]


# ── Matching helpers ─────────────────────────────────────────────────────────


def _known_tickers(cache) -> set[str]:
    return {str(n.get("ticker")) for n in cache._nodes.values() if n.get("ticker")}  # noqa: SLF001


def _normalize_ticker(raw: str, cache) -> str:
    """Resolve a free-text company reference to a ticker present in the graph.

    Order: alias map → exact ticker in cache → alias substring → ticker prefix.
    Returns the best guess (possibly the cleaned input) so callers can still try.
    """
    if not raw:
        return ""
    t = raw.strip().upper()
    if t in _TICKER_ALIASES:
        return _TICKER_ALIASES[t]
    known = _known_tickers(cache)
    if t in known:
        return t
    for name, tk in _TICKER_ALIASES.items():
        if name in t or t in name:
            return tk
    for tk in known:
        if tk and (t.startswith(tk) or tk.startswith(t)):
            return tk
    return t


def _field_matches(query_field: str, node_field: str | None) -> bool:
    """Fuzzy field match — exact, substring either direction (case-insensitive)."""
    if not query_field:
        return True
    q = query_field.strip().lower()
    n = (node_field or "").strip().lower()
    if not n:
        return False
    return q == n or q in n or n in q


def _field_rank(query_field: str, node_field: str | None) -> int:
    """0 = exact, 1 = substring, 2 = other — for ordering best matches first."""
    q = (query_field or "").strip().lower()
    n = (node_field or "").strip().lower()
    if q == n:
        return 0
    if q and (q in n or n in q):
        return 1
    return 2


def _build_reverse_edges(cache) -> dict[str, list[dict[str, Any]]]:
    """tgt_id → [edges into it]. Cache only indexes by src, so build the inverse."""
    rev: dict[str, list[dict[str, Any]]] = {}
    for bucket in cache._edges_by_src.values():  # noqa: SLF001
        for e in bucket:
            rev.setdefault(e["tgt_id"], []).append(e)
    return rev


def _ancestor_path(
    node_id: str, cache, rev: dict[str, list[dict[str, Any]]], max_hops: int = 6
) -> list[dict[str, Any]]:
    """Walk parent edges upward (toward company/run) → ordered edge list.

    Returns edges from the highest ancestor down to ``node_id`` so the UI can
    animate the route. Stops at a company node or when no parent exists.
    """
    chain: list[dict[str, Any]] = []
    seen = {node_id}
    cur = node_id
    for _ in range(max_hops):
        parents = rev.get(cur) or []
        if not parents:
            break
        # Prefer a company/run parent for a meaningful route.
        def _rank(e: dict[str, Any]) -> int:
            ptype = (cache._nodes.get(e["src_id"]) or {}).get("node_type")  # noqa: SLF001
            return {"company": 0, "dcf_run": 1}.get(ptype, 2)
        parents = sorted(parents, key=_rank)
        edge = parents[0]
        chain.append(edge)
        cur = edge["src_id"]
        if cur in seen:
            break
        seen.add(cur)
        if (cache._nodes.get(cur) or {}).get("node_type") == "company":  # noqa: SLF001
            break
    chain.reverse()  # highest ancestor → target
    return chain


def _execute(query: KGQuery, session_id: str) -> dict[str, Any]:
    """Deterministic executor — walks the cache. Returns subgraph + answer text."""
    cache = get_cache()
    if session_id:
        cache.load_session(session_id)
    # Normalize ticker AFTER session load (so cache-aware resolution can see nodes).
    norm = _normalize_ticker(query.ticker, cache)
    if norm:
        cache.load_ticker(norm)
        query.ticker = norm

    logger.info(
        "KG QUERY intent=%s ticker=%s node_type=%s field=%s session=%s",
        query.intent, query.ticker, query.node_type, query.field, session_id or "-",
    )

    rev = _build_reverse_edges(cache)
    traversal_path: list[str] = []
    traversal_edges: list[dict[str, Any]] = []
    matched: list[KGNode] = []
    answer = ""

    def _add_with_path(node: KGNode) -> None:
        """Record a matched node + its ancestor route (nodes + edges)."""
        if node["id"] not in traversal_path:
            traversal_path.append(node["id"])
        for edge in _ancestor_path(node["id"], cache, rev):
            for nid in (edge["src_id"], edge["tgt_id"]):
                if nid not in traversal_path:
                    traversal_path.append(nid)
            key = (edge["src_id"], edge["tgt_id"], edge.get("relation"))
            if key not in {(e["src_id"], e["tgt_id"], e.get("relation")) for e in traversal_edges}:
                traversal_edges.append({
                    "src_id": edge["src_id"],
                    "tgt_id": edge["tgt_id"],
                    "relation": edge.get("relation"),
                })

    if query.intent == "lookup":
        candidates: list[KGNode] = []
        for n in cache._nodes.values():  # noqa: SLF001
            if query.ticker and n.get("ticker") != query.ticker:
                continue
            if query.node_type and n.get("node_type") != query.node_type:
                continue
            if not _field_matches(query.field, n.get("field")):
                continue
            candidates.append(n)
        # Rank: exact field first, then most recently updated.
        candidates.sort(key=lambda n: (_field_rank(query.field, n.get("field")),
                                       -float(n.get("updated_at", 0) or 0)))
        matched = candidates
        for n in candidates:
            _add_with_path(n)

        if matched:
            latest = matched[0]
            if len(matched) == 1:
                answer = (
                    f"{latest.get('ticker')} {latest.get('node_type')}::{latest.get('field')} = "
                    f"{latest.get('value')} (source={latest.get('source')}, "
                    f"confidence={latest.get('confidence')})"
                )
            else:
                lines = [f"{len(matched)} matches for '{query.field or query.node_type}' "
                         f"on {query.ticker or '(any)'}:"]
                for n in matched[:12]:
                    lines.append(
                        f"  {n.get('node_type')}::{n.get('field')} = {n.get('value')} "
                        f"(source={n.get('source')})"
                    )
                answer = "\n".join(lines)
        else:
            # Helpful miss: list available fields for the ticker.
            available = sorted({
                str(n.get("field")) for n in cache._nodes.values()  # noqa: SLF001
                if (not query.ticker or n.get("ticker") == query.ticker) and n.get("field")
            })
            if available:
                hint = ", ".join(available[:20])
                answer = (
                    f"No match for field='{query.field}' on {query.ticker or '(any)'}. "
                    f"Available fields: {hint}"
                    + ("…" if len(available) > 20 else "")
                )
            else:
                answer = f"No KG data found for {query.ticker or 'that query'}."

    elif query.intent == "compare_runs":
        runs: dict[str, dict[str, Any]] = {}
        for n in cache._nodes.values():  # noqa: SLF001
            if n.get("ticker") != query.ticker:
                continue
            if n.get("node_type") != "run_assumption":
                continue
            rid = n.get("run_id") or "unknown"
            field = n.get("field", "?")
            runs.setdefault(rid, {})[field] = n.get("value")
            _add_with_path(n)
            matched.append(n)
        if runs:
            lines = [f"Run comparison for {query.ticker}:"]
            for rid, fields in runs.items():
                lines.append(f"  {rid}: {fields}")
            answer = "\n".join(lines)
        else:
            answer = f"No DCF runs found for {query.ticker}."

    elif query.intent == "why_assumption":
        for n in cache._nodes.values():  # noqa: SLF001
            if n.get("ticker") != query.ticker:
                continue
            if n.get("node_type") != "run_assumption":
                continue
            if not _field_matches(query.field, n.get("field")):
                continue
            matched.append(n)
            _add_with_path(n)
            # Also follow outgoing edges from this node for provenance.
            for edge in cache._edges_by_src.get(n["id"], []):  # noqa: SLF001
                if edge["tgt_id"] not in traversal_path:
                    traversal_path.append(edge["tgt_id"])
                traversal_edges.append({
                    "src_id": edge["src_id"], "tgt_id": edge["tgt_id"],
                    "relation": edge.get("relation"),
                })
        if matched:
            sources = sorted({str(n.get("source")) for n in matched})
            answer = (
                f"{query.field or 'assumption'} for {query.ticker}: "
                f"sources={sources}. Highlighted edges trace the provenance."
            )
        else:
            answer = f"No assumption '{query.field}' found for {query.ticker}."

    elif query.intent == "list_drivers":
        drivers = cache.get_drivers(query.ticker)
        matched = drivers
        for d in drivers:
            _add_with_path(d)
        if drivers:
            lines = [f"Drivers for {query.ticker}:"]
            for d in drivers:
                lines.append(f"  {d.get('field')}: {d.get('value')} (source={d.get('source')})")
            answer = "\n".join(lines)
        else:
            answer = f"No drivers recorded for {query.ticker}."

    elif query.intent == "recent_changes":
        nodes = [
            n for n in cache._nodes.values()  # noqa: SLF001
            if n.get("ticker") == query.ticker
        ]
        nodes.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
        matched = nodes[:10]
        for n in matched:
            _add_with_path(n)
        lines = [f"Recent changes for {query.ticker}:"]
        for n in matched:
            lines.append(
                f"  {n.get('node_type')}::{n.get('field')} = {n.get('value')} "
                f"(source={n.get('source')})"
            )
        answer = "\n".join(lines) if matched else f"No KG data for {query.ticker}."

    else:
        answer = f"Unsupported intent: {query.intent}"

    logger.info(
        "KG QUERY result intent=%s ticker=%s matched=%d traversal_nodes=%d edges=%d",
        query.intent, query.ticker, len(matched), len(traversal_path), len(traversal_edges),
    )
    return {
        "query": query.model_dump(),
        "answer": answer,
        "matched_nodes": matched,
        "traversal_path": traversal_path,
        "traversal_edges": traversal_edges,
    }


# ── Primary path: LLM reads the subgraph ──────────────────────────────────────


def _fmt_value(value: Any, limit: int = 120) -> str:
    """Compact one-line value for the serialized subgraph."""
    import json

    if isinstance(value, (dict, list)):
        s = json.dumps(value, separators=(",", ":"), default=str)
    else:
        s = str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _subgraph_nodes(cache, ticker: str) -> list[KGNode]:
    """Nodes in scope: the ticker's nodes (or all if no ticker)."""
    out = []
    for n in cache._nodes.values():  # noqa: SLF001
        if ticker and n.get("ticker") != ticker:
            continue
        out.append(n)
    return out


def _serialize_subgraph(nodes: list[KGNode], cache) -> str:
    """Render nodes + their edges as a compact text block for the LLM."""
    lines = ["NODES (id | type | field | value | source conf):"]
    for n in nodes:
        lines.append(
            f"  {n.get('id')} | {n.get('node_type')} | {n.get('field')} | "
            f"{_fmt_value(n.get('value'))} | {n.get('source')} {n.get('confidence')}"
        )
    ids = {n.get("id") for n in nodes}
    edge_lines: list[str] = []
    for src, bucket in cache._edges_by_src.items():  # noqa: SLF001
        if src not in ids:
            continue
        for e in bucket:
            if e["tgt_id"] in ids:
                edge_lines.append(f"  {e['src_id']} -[{e.get('relation')}]-> {e['tgt_id']}")
    if edge_lines:
        lines.append("EDGES (src -[relation]-> tgt):")
        lines.extend(edge_lines)
    return "\n".join(lines)


_WHY_TRIGGERS = ("why", "justif", "reason", "rationale", "because", "based on", "support")


def _augment_with_evidence(
    question: str, matched: list[KGNode], cache
) -> list[KGNode]:
    """For causal questions about an assumption, fold in the company's synthesis
    + drivers as supporting evidence (no direct edge links them in the KG)."""
    q = (question or "").lower()
    if not any(t in q for t in _WHY_TRIGGERS):
        return matched
    tickers = {
        n.get("ticker") for n in matched
        if n.get("node_type") == "run_assumption" and n.get("ticker")
    }
    if not tickers:
        return matched

    seen = {m["id"] for m in matched}
    augmented = list(matched)
    for tk in tickers:
        for n in cache._nodes.values():  # noqa: SLF001
            if n.get("ticker") != tk or n.get("node_type") != "company_synthesis":
                continue
            if n["id"] not in seen:
                seen.add(n["id"]); augmented.append(n)
        for d in cache.get_drivers(str(tk)):
            if d["id"] not in seen:
                seen.add(d["id"]); augmented.append(d)
    return augmented


async def _llm_answer_subgraph(
    question: str, ticker: str, cache, rev: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Serialize the subgraph, let the LLM answer + cite node ids, then build the
    deterministic highlight route from the *validated* ids."""
    nodes = _subgraph_nodes(cache, ticker)
    if not nodes:
        return {
            "query": {"question": question, "ticker": ticker, "mode": "llm_subgraph"},
            "answer": f"No KG data found for {ticker or 'that query'}.",
            "matched_nodes": [], "traversal_path": [], "traversal_edges": [],
        }

    serialized = _serialize_subgraph(nodes, cache)
    prompt = (
        "You answer analyst questions using ONLY the knowledge-graph subgraph below.\n"
        "Never invent facts. If the answer isn't present, say so.\n\n"
        f"{_KG_SCHEMA}\n\n"
        "RETRIEVAL RULES:\n"
        "- Match loosely: 'revenue' may map to base_revenue, revenue_growth, or "
        "revenue_growth_terminal — include ALL relevant nodes.\n"
        "- For CAUSAL questions ('why did we pick X', 'what justifies X', "
        "'rationale for X'): the answer is NOT just the assumption node. Also "
        "cite the supporting context that explains it — company_synthesis, "
        "driver/risk/theme, thesis, and user_belief nodes for the same ticker. "
        "Return ALL of their ids in node_ids.\n"
        "- If the subgraph lacks supporting context (no synthesis/drivers), say "
        "so explicitly rather than implying provenance that isn't recorded.\n"
        "- node_ids must list EVERY node that informs the answer, not only the "
        "single most literal match.\n\n"
        f"Question: {question}\n"
        f"Ticker: {ticker or '(any)'}\n\n"
        f"{serialized}\n\n"
        "Return: a concise answer, plus node_ids = every id you used."
    )
    structured = _get_query_llm().with_structured_output(KGAnswer)
    result: KGAnswer = structured.invoke(prompt)  # type: ignore[assignment]

    by_id = {n.get("id"): n for n in cache._nodes.values()}  # noqa: SLF001
    matched = [by_id[nid] for nid in result.node_ids if nid in by_id]

    # ── Evidence augmentation for "why / justify" questions ───────────────────
    # An assumption is justified by the company's synthesis + drivers, but no
    # direct KG edge connects them. When the question is causal ("why did we
    # pick…", "what justifies…") and an assumption matched, fold in that
    # company's synthesis + drivers so their hubs light up alongside the row.
    matched = _augment_with_evidence(question, matched, cache)

    traversal_path, traversal_edges = _build_traversal(matched, cache, rev)

    logger.info(
        "KG QUERY (llm_subgraph) ticker=%s cited=%d matched=%d edges=%d",
        ticker, len(result.node_ids), len(matched), len(traversal_edges),
    )
    return {
        "query": {"question": question, "ticker": ticker, "mode": "llm_subgraph"},
        "answer": result.answer,
        "matched_nodes": matched,
        "traversal_path": traversal_path,
        "traversal_edges": traversal_edges,
    }


def _build_traversal(
    matched: list[KGNode], cache, rev: dict[str, list[dict[str, Any]]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Shared: from matched nodes, accumulate ancestor route (nodes + edges)."""
    traversal_path: list[str] = []
    seen_edges: set[tuple] = set()
    traversal_edges: list[dict[str, Any]] = []
    for node in matched:
        if node["id"] not in traversal_path:
            traversal_path.append(node["id"])
        for edge in _ancestor_path(node["id"], cache, rev):
            for nid in (edge["src_id"], edge["tgt_id"]):
                if nid not in traversal_path:
                    traversal_path.append(nid)
            key = (edge["src_id"], edge["tgt_id"], edge.get("relation"))
            if key not in seen_edges:
                seen_edges.add(key)
                traversal_edges.append({
                    "src_id": edge["src_id"], "tgt_id": edge["tgt_id"],
                    "relation": edge.get("relation"),
                })
    return traversal_path, traversal_edges


# ── Fallback path: deterministic keyword retrieval (no LLM) ────────────────────

_STOPWORDS = {
    "what", "is", "the", "a", "an", "of", "for", "in", "on", "to", "and", "or",
    "did", "we", "use", "used", "show", "me", "all", "that", "this", "our",
    "why", "how", "are", "was", "were", "about", "since", "last", "run", "with",
    "pick", "picked", "choose", "chose", "value", "values",
}


def _keyword_lookup(
    question: str, ticker: str, cache, rev: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Offline fallback: rank ticker nodes by keyword overlap on field/value/type."""
    terms = [
        w for w in "".join(c.lower() if c.isalnum() else " " for c in question).split()
        if w not in _STOPWORDS and len(w) > 1
    ]
    nodes = _subgraph_nodes(cache, ticker)

    def score(n: KGNode) -> int:
        hay = f"{n.get('node_type')} {n.get('field')} {_fmt_value(n.get('value'))}".lower()
        return sum(1 for t in terms if t in hay)

    ranked = sorted(
        ((score(n), -float(n.get("updated_at", 0) or 0), n) for n in nodes),
        key=lambda x: (-x[0], x[1]),
    )
    matched = [n for s, _, n in ranked if s > 0][:12]
    traversal_path, traversal_edges = _build_traversal(matched, cache, rev)

    if matched:
        lines = [f"{len(matched)} match(es) for {ticker or '(any)'}:"]
        for n in matched:
            lines.append(
                f"  {n.get('node_type')}::{n.get('field')} = {_fmt_value(n.get('value'))} "
                f"(source={n.get('source')})"
            )
        answer = "\n".join(lines)
    else:
        available = sorted({
            str(n.get("field")) for n in nodes if n.get("field")
        })
        answer = (
            f"No match for '{question}' on {ticker or '(any)'}. "
            + (f"Available fields: {', '.join(available[:20])}" if available
               else f"No KG data found for {ticker or 'that query'}.")
        )
    return {
        "query": {"question": question, "ticker": ticker, "mode": "keyword"},
        "answer": answer, "matched_nodes": matched,
        "traversal_path": traversal_path, "traversal_edges": traversal_edges,
    }


async def run_nl_query(
    question: str,
    ticker: str | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """End-to-end: load subgraph → LLM answers over it (fallback: keyword)."""
    cache = get_cache()
    if session_id:
        cache.load_session(session_id)
    norm = _normalize_ticker(ticker or "", cache)
    if norm:
        cache.load_ticker(norm)
    rev = _build_reverse_edges(cache)

    try:
        return await _llm_answer_subgraph(question, norm, cache, rev)
    except Exception:
        logger.exception("KG llm_subgraph failed — falling back to keyword retrieval")
        return _keyword_lookup(question, norm, cache, rev)

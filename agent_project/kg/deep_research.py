"""Multi-hop deep research over the Knowledge Graph.

The single-shot ``run_nl_query`` serializes a ticker's ENTIRE subgraph into one
LLM call. That doesn't scale (a mature ticker has hundreds of nodes) and can't
chain reasoning across runs or tickers ("does the bearish implied price come
from assumptions that conflict with the thesis, and did they drift across
runs?").

This engine reasons iteratively over the graph instead:

    seed (company + 1-hop)
      └─► HOP: serialize visited subgraph → LLM decides:
              sufficient?  → answer + cited node_ids        (stop)
              not enough?  → which relations to expand next  (loop)
      └─► expand frontier along the chosen real edges (bounded)
      └─► SYNTHESIZE final answer + traversal trail

Bounded: max hops, max nodes, per-node neighbor cap. The planner only ever
chooses from relations that actually exist on the current frontier, so it can't
hallucinate a hop. One LLM call per hop (decision = gate + answer + expansion
combined). Cost ≈ hops LLM calls, not per-node.

Returns the same shape as ``run_nl_query`` (answer / matched_nodes /
traversal_path / traversal_edges) plus a ``hops`` log for the UI expander.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .cache import KGNode, get_cache
from .query import (
    _KG_SCHEMA,
    _build_traversal,
    _fmt_temporal,
    _fmt_value,
    _get_query_llm,
    _normalize_ticker,
)

logger = logging.getLogger(__name__)

# ── Bounds ───────────────────────────────────────────────────────────────────
MAX_HOPS = 4
MAX_NODES = 80          # stop expanding once the visited set reaches this
NEIGHBORS_PER_NODE = 24  # cap fan-out per node per hop


class HopDecision(BaseModel):
    """One reason-over-graph step: answer now, or expand along chosen relations."""
    model_config = ConfigDict(extra="forbid")
    sufficient: bool = Field(
        description="True if the current subgraph is enough to answer the question."
    )
    answer: str = Field(
        default="",
        description="The analyst answer when sufficient=True. Empty otherwise.",
    )
    node_ids: list[str] = Field(
        default_factory=list,
        description="Exact ids of nodes used in the answer (when sufficient=True).",
    )
    expand_relations: list[str] = Field(
        default_factory=list,
        description=(
            "When sufficient=False: relations to follow next, chosen ONLY from the "
            "AVAILABLE RELATIONS list provided. Empty if nothing useful to expand."
        ),
    )
    needs_external: bool = Field(
        default=False,
        description=(
            "True when the KG cannot fully answer because its data is STALE or "
            "MISSING for what the question needs — e.g. the question asks for "
            "'latest / current / today' info but the relevant nodes report an old "
            "`as_of` period (or are absent). Set this whenever the freshest "
            "relevant node is too old to answer a current-events question. Still "
            "provide the best answer you can from the (stale) subgraph — the "
            "caller will supplement it with a live web search."
        ),
    )
    external_reason: str = Field(
        default="",
        description=(
            "When needs_external=True: one line naming what is missing and what "
            "the KG has instead, e.g. 'KG has only FY2023 financials; current-year "
            "figures need a web search' or 'no news newer than 40d; today's "
            "headlines need web'."
        ),
    )
    reason: str = Field(default="", description="One short line: why answer / why expand.")


def _adjacency(cache) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Forward (src→edges) and reverse (tgt→edges) adjacency.

    Read from durable storage, not ``cache._edges_by_src`` — the latter is only
    populated by in-session ``add_edge`` writes, so a fresh query process (or
    one that called ``load_ticker``, which doesn't hydrate edges) would see an
    empty graph and never expand.
    """
    import storage  # noqa: PLC0415
    fwd: dict[str, list[dict]] = {}
    rev: dict[str, list[dict]] = {}
    for e in storage.list_kg_edges():
        fwd.setdefault(e["src_id"], []).append(e)
        rev.setdefault(e["tgt_id"], []).append(e)
    return fwd, rev


def _serialize(nodes: list[KGNode], fwd: dict[str, list[dict]]) -> str:
    """Render visited nodes + the edges among them, using storage adjacency."""
    lines = ["NODES (id | type | field | value | recency | source conf):"]
    for n in nodes:
        lines.append(
            f"  {n.get('id')} | {n.get('node_type')} | {n.get('field')} | "
            f"{_fmt_value(n.get('value'))} | {_fmt_temporal(n)} | "
            f"{n.get('source')} {n.get('confidence')}"
        )
    ids = {n.get("id") for n in nodes}
    edge_lines = [
        f"  {e['src_id']} -[{e.get('relation')}]-> {e['tgt_id']}"
        for src in ids
        for e in fwd.get(src, [])
        if e["tgt_id"] in ids
    ]
    if edge_lines:
        lines.append("EDGES (src -[relation]-> tgt):")
        lines.extend(edge_lines)
    return "\n".join(lines)


def _neighbors(node_id: str, fwd, rev) -> list[tuple[str, str]]:
    """(neighbor_id, relation) pairs in both directions for a node."""
    out: list[tuple[str, str]] = []
    for e in fwd.get(node_id, [])[:NEIGHBORS_PER_NODE]:
        out.append((e["tgt_id"], e.get("relation", "")))
    for e in rev.get(node_id, [])[:NEIGHBORS_PER_NODE]:
        out.append((e["src_id"], e.get("relation", "")))
    return out


def _resolve_tickers(question: str, norm: str, known: set[str]) -> list[str]:
    """Tickers in scope: the resolved hint + any known ticker named in the question.

    ``known`` comes from durable storage (not cache hydration) so we can detect
    a second ticker in the question before it's been loaded.
    """
    found: list[str] = []
    if norm:
        found.append(norm)
    upper_q = f" {question.upper()} "
    for tk in known:
        if tk and f" {tk} " in upper_q and tk not in found:
            found.append(tk)
    return found


def _seed(by_id: dict[str, KGNode], tickers: list[str], fwd, rev) -> dict[str, KGNode]:
    """Seed = each ticker's company anchor + its direct (1-hop) neighbors."""
    visited: dict[str, KGNode] = {}
    for tk in tickers:
        anchor_id = f"{tk}::company::anchor"
        anchor = by_id.get(anchor_id)
        if anchor is None:
            # No anchor node — seed with the ticker's most-recent nodes instead.
            tk_nodes = sorted(
                (n for n in by_id.values() if n.get("ticker") == tk),
                key=lambda n: float(n.get("updated_at", 0) or 0), reverse=True,
            )[:12]
            for n in tk_nodes:
                visited[n["id"]] = n
            continue
        visited[anchor_id] = anchor
        for nid, _rel in _neighbors(anchor_id, fwd, rev):
            if nid in by_id and nid not in visited:
                visited[nid] = by_id[nid]
    return visited


def _available_relations(visited: dict[str, KGNode], fwd, rev) -> dict[str, int]:
    """Relations on edges leaving the visited set to UNvisited nodes → counts."""
    rels: dict[str, int] = {}
    for nid in visited:
        for neigh_id, rel in _neighbors(nid, fwd, rev):
            if neigh_id not in visited and rel:
                rels[rel] = rels.get(rel, 0) + 1
    return rels


def _expand(visited: dict[str, KGNode], relations: set[str], fwd, rev,
            by_id: dict[str, KGNode]) -> int:
    """Add unvisited neighbors reached via the chosen relations. Returns count added."""
    added = 0
    for nid in list(visited):
        if len(visited) >= MAX_NODES:
            break
        for neigh_id, rel in _neighbors(nid, fwd, rev):
            if rel in relations and neigh_id not in visited and neigh_id in by_id:
                visited[neigh_id] = by_id[neigh_id]
                added += 1
                if len(visited) >= MAX_NODES:
                    break
    return added


def _decide(question: str, tickers: list[str], visited: dict[str, KGNode],
            available: dict[str, int], fwd, hop: int) -> HopDecision:
    """One LLM call: answer now, or pick relations to expand."""
    serialized = _serialize(list(visited.values()), fwd)
    rel_list = ", ".join(f"{r} ({c})" for r, c in sorted(available.items(), key=lambda x: -x[1]))
    prompt = (
        "The Knowledge Graph is a MEANS to answer the analyst question, not the goal. "
        "Your job is to answer the QUESTION; the subgraph is the evidence you have on "
        "hand. Reason over it, expanding hop-by-hop, but stay honest about whether it "
        "actually holds what the question needs.\n\n"
        f"{_KG_SCHEMA}\n\n"
        f"Question: {question}\n"
        f"Tickers in scope: {', '.join(tickers) or '(any)'}\n"
        f"Hop: {hop + 1}\n\n"
        "CURRENT SUBGRAPH:\n"
        f"{serialized}\n\n"
        f"AVAILABLE RELATIONS to expand next (relation (edge_count)): "
        f"{rel_list or '(none — nothing left to expand)'}\n\n"
        "Decide (read the `recency` column — as_of period + ingest age — before judging):\n"
        "- If the subgraph answers the question with data that is FRESH ENOUGH → "
        "sufficient=true, give the answer, list node_ids = EVERY node you used.\n"
        "- If more KG evidence would help (run_assumptions, drivers, prior runs, "
        "another ticker not yet pulled in) → sufficient=false and set expand_relations "
        "from the AVAILABLE list ONLY.\n"
        "- TEMPORAL HONESTY: if the question asks for 'latest / current / today / "
        "this year' info but the relevant nodes report an OLD `as_of` period (or no "
        "such node exists), the KG is STALE for this question. Set needs_external=true "
        "and external_reason (what's missing + what the KG has instead). Still give the "
        "best answer from the stale nodes, clearly dated (e.g. 'As of FY2023 …') — do "
        "NOT present stale data as current, and do NOT claim 'no recent news' just "
        "because the KG lacks it (that means the KG is stale, not that none exists).\n"
        "- Never invent facts or node ids. Ground every claim in the subgraph."
    )
    structured = _get_query_llm().with_structured_output(HopDecision)
    return structured.invoke(prompt)  # type: ignore[return-value]


def run_deep_research(
    question: str,
    ticker: str | None = None,
    session_id: str = "",
    max_hops: int = MAX_HOPS,
) -> dict[str, Any]:
    """Multi-hop reasoning over the KG. Synchronous (LLM .invoke)."""
    import storage  # noqa: PLC0415
    cache = get_cache()

    # Read the whole graph from durable storage — independent of cache
    # hydration, so multi-ticker detection and cross-ticker edges always work.
    by_id: dict[str, KGNode] = {n["id"]: n for n in storage.list_kg_nodes()}  # type: ignore[misc]
    known = {str(n.get("ticker")) for n in by_id.values() if n.get("ticker")}

    norm = _normalize_ticker(ticker or "", cache)
    if norm and norm not in known:
        norm = ""  # hint resolved to a ticker with no data
    tickers = _resolve_tickers(question, norm, known)

    fwd, rev = _adjacency(cache)
    visited = _seed(by_id, tickers, fwd, rev)

    if not visited:
        return {
            "query": {"question": question, "ticker": norm, "mode": "deep_research"},
            "answer": f"No KG data found for {norm or 'that query'}.",
            "matched_nodes": [], "traversal_path": [], "traversal_edges": [], "hops": [],
            # Empty graph → the KG can't answer anything; caller must go external.
            "needs_external": True,
            "external_reason": f"No KG data for {norm or 'that query'} — use a web search.",
        }

    hops_log: list[dict[str, Any]] = []
    decision: HopDecision | None = None
    for hop in range(max_hops):
        available = _available_relations(visited, fwd, rev)
        decision = _decide(question, tickers, visited, available, fwd, hop)
        hops_log.append({
            "hop": hop + 1,
            "nodes": len(visited),
            "sufficient": decision.sufficient,
            "expanded": list(decision.expand_relations),
            "reason": decision.reason,
        })
        if decision.sufficient or not available:
            break
        chosen = {r for r in decision.expand_relations if r in available}
        if not chosen or len(visited) >= MAX_NODES:
            break
        added = _expand(visited, chosen, fwd, rev, by_id)
        if added == 0:
            break

    # ── Assemble result ──────────────────────────────────────────────────────
    answer = (decision.answer if decision else "").strip()
    cited = [visited[i] for i in (decision.node_ids if decision else []) if i in visited]
    if not answer:
        answer = _synthesize(question, tickers, visited, fwd)
    matched = cited or list(visited.values())[:12]
    traversal_path, traversal_edges = _build_traversal(matched, cache, rev)

    # B2: the planner flags when KG data is too stale/missing to answer alone, so
    # the chat agent knows to supplement with a live web search instead of
    # trusting a stale-but-confident KG answer.
    needs_external = bool(decision.needs_external) if decision else False
    external_reason = (decision.external_reason if decision else "").strip()

    logger.info(
        "KG DEEP RESEARCH tickers=%s hops=%d visited=%d cited=%d needs_external=%s",
        tickers, len(hops_log), len(visited), len(cited), needs_external,
    )
    return {
        "query": {"question": question, "ticker": norm, "mode": "deep_research"},
        "answer": answer,
        "matched_nodes": matched,
        "traversal_path": traversal_path,
        "traversal_edges": traversal_edges,
        "hops": hops_log,
        "needs_external": needs_external,
        "external_reason": external_reason,
    }


def _synthesize(question: str, tickers: list[str], visited: dict[str, KGNode], fwd) -> str:
    """Fallback final answer when the loop ended without a sufficient verdict."""
    serialized = _serialize(list(visited.values()), fwd)
    prompt = (
        "Answer the analyst question using ONLY the knowledge-graph subgraph below. "
        "If the subgraph doesn't fully answer it, say what IS known and what's missing.\n\n"
        f"Question: {question}\n"
        f"Tickers: {', '.join(tickers) or '(any)'}\n\n"
        f"{serialized}\n\nConcise answer:"
    )
    try:
        return _get_query_llm().invoke(prompt).content.strip()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        nodes = list(visited.values())[:10]
        return "Partial KG context (no synthesis):\n" + "\n".join(
            f"  {n.get('node_type')}::{n.get('field')} = {_fmt_value(n.get('value'))}"
            for n in nodes
        )

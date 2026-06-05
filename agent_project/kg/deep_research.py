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
    lines = ["NODES (id | type | field | value | source conf):"]
    for n in nodes:
        lines.append(
            f"  {n.get('id')} | {n.get('node_type')} | {n.get('field')} | "
            f"{_fmt_value(n.get('value'))} | {n.get('source')} {n.get('confidence')}"
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
        "You answer analyst questions by REASONING over a knowledge-graph subgraph, "
        "expanding it hop-by-hop until you have enough to answer.\n\n"
        f"{_KG_SCHEMA}\n\n"
        f"Question: {question}\n"
        f"Tickers in scope: {', '.join(tickers) or '(any)'}\n"
        f"Hop: {hop + 1}\n\n"
        "CURRENT SUBGRAPH:\n"
        f"{serialized}\n\n"
        f"AVAILABLE RELATIONS to expand next (relation (edge_count)): "
        f"{rel_list or '(none — nothing left to expand)'}\n\n"
        "Decide:\n"
        "- If the current subgraph already answers the question → sufficient=true, "
        "give the answer, and list node_ids = EVERY node you used.\n"
        "- If you need more (e.g. the question needs run_assumptions, drivers, prior "
        "runs, or another ticker not yet pulled in) → sufficient=false and set "
        "expand_relations to relations from the AVAILABLE list above that lead toward "
        "the missing evidence. Choose ONLY from that list.\n"
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

    logger.info(
        "KG DEEP RESEARCH tickers=%s hops=%d visited=%d cited=%d",
        tickers, len(hops_log), len(visited), len(cited),
    )
    return {
        "query": {"question": question, "ticker": norm, "mode": "deep_research"},
        "answer": answer,
        "matched_nodes": matched,
        "traversal_path": traversal_path,
        "traversal_edges": traversal_edges,
        "hops": hops_log,
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

"""KG subgraph adapter — pull nodes rooted at an anchor_id from the KG cache.

One KgSubgraphSource → one NormalizedBlock per KG node type found (grouped by
node_type for readability).  Uses ``cache.get_subgraph(ticker)`` which returns
all nodes + edges for a ticker-anchor.

Anchor format: KgSubgraphSource.anchor_id is the ticker string or a
``ticker::node_type`` compound anchor.  We use the first segment as ticker for
the get_subgraph call, then filter by depth (simple BFS over edges).

On KG unavailable (no SQLite file, import error) the adapter returns an empty
list and logs a warning rather than crashing.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from ..state import KgSubgraphSource, NormalizedBlock
from .base import make_block_id

logger = logging.getLogger(__name__)

_MAX_BLOCKS = 10

__all__ = ["KgSubgraphAdapter"]


class KgSubgraphAdapter:
    source_type = "kg_subgraph"

    def normalize(self, source: KgSubgraphSource, *, session_id: str = "") -> list[NormalizedBlock]:  # noqa: C901
        anchor_id = source.anchor_id.strip()
        depth = source.depth

        try:
            from kg.cache import get_cache  # noqa: PLC0415
        except ImportError as exc:
            logger.warning("KG module unavailable (%s) — skipping KgSubgraphSource.", exc)
            return []

        try:
            cache = get_cache()
        except Exception:  # noqa: BLE001
            logger.warning("get_cache() failed — skipping KgSubgraphSource.", exc_info=True)
            return []

        # Ticker = first segment of anchor_id (e.g. "META" from "META::dcf_run::...")
        ticker = anchor_id.split("::")[0].strip() or anchor_id

        try:
            nodes, edges = cache.get_subgraph(ticker)
        except Exception:  # noqa: BLE001
            logger.warning("get_subgraph(%r) failed.", ticker, exc_info=True)
            return []

        if not nodes:
            logger.info("KgSubgraphSource: no nodes found for ticker=%r", ticker)
            return []

        # BFS from anchor node up to `depth` hops.
        node_by_id = {n["id"]: n for n in nodes}
        adj: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            adj[e.get("src_id", "")].append(e.get("tgt_id", ""))
            adj[e.get("tgt_id", "")].append(e.get("src_id", ""))

        # Find anchor node — prefer exact id match, else first node.
        anchor_node = node_by_id.get(anchor_id) or next(iter(node_by_id.values()))
        start_id = anchor_node["id"]

        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_id, 0)])
        reachable: list[dict] = []
        while queue:
            nid, d = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            if nid in node_by_id:
                reachable.append(node_by_id[nid])
            if d < depth:
                for nbr in adj.get(nid, []):
                    if nbr not in visited:
                        queue.append((nbr, d + 1))

        # Group reachable nodes by node_type → one block per type.
        by_type: dict[str, list[dict]] = defaultdict(list)
        for n in reachable:
            by_type[n.get("node_type", "unknown")].append(n)

        blocks: list[NormalizedBlock] = []
        for i, (ntype, group) in enumerate(by_type.items()):
            if i >= _MAX_BLOCKS:
                break
            lines = []
            evidence = []
            for n in group[:12]:
                val = n.get("value")
                if isinstance(val, dict):
                    line = " | ".join(f"{k}: {v}" for k, v in list(val.items())[:6])
                else:
                    line = str(val)[:200] if val is not None else n.get("field", "")
                lines.append(line)
                evidence.append(n["id"])

            body = "\n".join(lines)
            blocks.append(
                NormalizedBlock(
                    block_id=make_block_id(
                        source_type="kg_subgraph",
                        source_ref=anchor_id,
                        idx=i,
                        content_signature=f"{ticker}_{ntype}_{len(group)}",
                    ),
                    kind="narrative",
                    title=f"{ticker} — {ntype.replace('_', ' ').title()} ({len(group)})",
                    content={
                        "ticker": ticker,
                        "node_type": ntype,
                        "node_count": len(group),
                        "body": body,
                        "lines": lines,
                    },
                    source_type="kg_subgraph",
                    source_ref=anchor_id,
                    evidence_refs=evidence[:8],
                    suggested_slide_layouts=["bullets", "narrative"],
                )
            )

        logger.info(
            "KgSubgraphSource: %d block(s) from %d node(s) (ticker=%r, depth=%d)",
            len(blocks), len(reachable), ticker, depth,
        )
        return blocks

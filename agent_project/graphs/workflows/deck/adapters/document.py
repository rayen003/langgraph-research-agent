"""Document adapter — RAG retrieval from ChromaDB via documents.hybrid_search.

One DocumentSource → one NormalizedBlock per retrieved passage (grouped by
source filename for readability).  ``query_hints`` bias retrieval; if empty,
falls back to the deck brief's ``must_cover`` topics (injected at normalize
time via ``source.query_hints``).

Design:
  - Multi-query retrieval: one search per query_hint (deduped by chunk text).
  - Passages grouped → one ``narrative`` block per unique source filename.
  - Max 3 query hints × 8 results each = up to 24 passages before dedup.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..state import DocumentSource, NormalizedBlock
from .base import make_block_id

logger = logging.getLogger(__name__)

_MAX_RESULTS_PER_QUERY = 8
_MAX_BLOCKS = 12  # cap total blocks to avoid outline bloat

__all__ = ["DocumentAdapter"]


class DocumentAdapter:
    source_type = "document"

    def normalize(self, source: DocumentSource, *, session_id: str = "") -> list[NormalizedBlock]:  # noqa: C901
        try:
            from documents import _session_ctx, hybrid_search  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "documents module unavailable — cannot normalize DocumentSource."
            ) from exc

        if not source.doc_ids:
            logger.warning("DocumentSource has no doc_ids — skipping.")
            return []

        # ChromaDB collection is session-scoped: hybrid_search filters by
        # where={"session_id": session_id}.  Caller must pass the active session.
        # Fall back to the ambient session context var if not supplied.
        effective_session = session_id or (_session_ctx.get() if _session_ctx else "") or ""
        if not effective_session:
            logger.warning(
                "DocumentSource: no session_id provided and no ambient session — "
                "retrieval will return empty.",
            )
            return []

        queries = list(source.query_hints) if source.query_hints else ["summary key points"]
        if not queries:
            queries = ["summary key points"]

        # Multi-query retrieval — dedup by text content.
        seen_texts: set[str] = set()
        passages: list[dict] = []
        for q in queries[:5]:  # hard cap on queries
            try:
                results = hybrid_search(q, session_id=effective_session, n_results=_MAX_RESULTS_PER_QUERY)
            except Exception:  # noqa: BLE001
                logger.warning("hybrid_search failed for query=%r", q, exc_info=True)
                continue
            for r in results:
                txt = (r.get("text") or "").strip()
                if not txt or txt in seen_texts:
                    continue
                seen_texts.add(txt)
                # Filter by doc_id if metadata carries it
                meta = r.get("metadata") or {}
                chunk_doc_id = meta.get("doc_id") or meta.get("session_id") or ""
                if source.doc_ids and chunk_doc_id and chunk_doc_id not in source.doc_ids:
                    continue
                passages.append({"text": txt, "metadata": meta})

        if not passages:
            logger.warning("DocumentSource %s: no passages retrieved.", source.doc_ids)
            return []

        # Group by filename → one block per document.
        by_file: dict[str, list[dict]] = defaultdict(list)
        for p in passages:
            fname = p["metadata"].get("filename") or p["metadata"].get("doc_id") or "document"
            by_file[fname].append(p)

        blocks: list[NormalizedBlock] = []
        for i, (fname, chunks) in enumerate(by_file.items()):
            if i >= _MAX_BLOCKS:
                break
            body = "\n\n".join(c["text"] for c in chunks[:8])
            paragraphs = [c["text"] for c in chunks[:8]]
            evidence_ids = [
                c["metadata"].get("chunk_id") or c["metadata"].get("doc_id") or f"{fname}_{j}"
                for j, c in enumerate(chunks[:8])
            ]
            blocks.append(
                NormalizedBlock(
                    block_id=make_block_id(
                        source_type="document",
                        source_ref=fname,
                        idx=i,
                        content_signature=body[:120],
                    ),
                    kind="narrative",
                    title=fname,
                    content={
                        "filename": fname,
                        "body": body,
                        "paragraphs": paragraphs,
                        "chunk_count": len(chunks),
                    },
                    source_type="document",
                    source_ref=fname,
                    evidence_refs=evidence_ids,
                    suggested_slide_layouts=["narrative", "bullets"],
                )
            )

        logger.info("DocumentSource: %d block(s) from %d passage(s)", len(blocks), len(passages))
        return blocks

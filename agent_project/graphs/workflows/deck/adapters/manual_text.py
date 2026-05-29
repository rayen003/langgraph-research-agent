"""Manual text adapter — wraps analyst-provided title+body into a narrative block.

One source → one block.  Body split into paragraphs on double-newline for
downstream outline gen to work with.
"""

from __future__ import annotations

from ..state import ManualTextSource, NormalizedBlock
from .base import make_block_id

__all__ = ["ManualTextAdapter"]


class ManualTextAdapter:
    source_type = "manual_text"

    def normalize(self, source: ManualTextSource, *, session_id: str = "") -> list[NormalizedBlock]:
        body = source.body.strip()
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        bullets = [ln.lstrip("•-*· ").strip() for ln in body.splitlines() if ln.strip()]

        block = NormalizedBlock(
            block_id=make_block_id(
                source_type="manual_text",
                source_ref=source.title,
                idx=0,
                content_signature=body[:120],
            ),
            kind="narrative",
            title=source.title,
            content={
                "title": source.title,
                "body": body,
                "paragraphs": paragraphs,
                "bullets": bullets,
            },
            source_type="manual_text",
            source_ref=source.title,
            evidence_refs=[],
            suggested_slide_layouts=["narrative", "bullets"],
        )
        return [block]

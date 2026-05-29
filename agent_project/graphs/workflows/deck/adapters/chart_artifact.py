"""Chart artifact adapter — pass-through for existing image files.

One ChartArtifactSource → one ``chart`` NormalizedBlock.
Path validation is best-effort: if the file doesn't exist at normalize time
the block is still produced (it may exist by slide-render time), but a warning
is logged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..state import ChartArtifactSource, NormalizedBlock
from .base import make_block_id

logger = logging.getLogger(__name__)

__all__ = ["ChartArtifactAdapter"]


class ChartArtifactAdapter:
    source_type = "chart_artifact"

    def normalize(self, source: ChartArtifactSource, *, session_id: str = "") -> list[NormalizedBlock]:
        path = source.path.strip()
        caption = (source.caption or "").strip()

        if not path:
            logger.warning("ChartArtifactSource has empty path — skipping.")
            return []

        if not Path(path).exists():
            logger.warning("ChartArtifactSource path not found: %s (continuing)", path)

        title = caption or Path(path).stem.replace("_", " ").title()

        block = NormalizedBlock(
            block_id=make_block_id(
                source_type="chart_artifact",
                source_ref=path,
                idx=0,
                content_signature=(caption or path)[:120],
            ),
            kind="chart",
            title=title,
            content={
                "path": path,
                "caption": caption,
            },
            source_type="chart_artifact",
            source_ref=path,
            evidence_refs=[],
            suggested_slide_layouts=["chart_caption"],
        )
        return [block]

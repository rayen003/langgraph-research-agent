"""Web adapter — fetch + summarize URLs via Exa search.

One WebSource → one NormalizedBlock per URL (up to 8).
If ``urls`` is empty, no blocks are produced.

Each URL is fetched via Exa highlight extraction.  On Exa failure (no API key,
network error) the adapter falls back to a stub block so the deck can still
proceed with a "(web content unavailable)" note rather than crashing.
"""

from __future__ import annotations

import json
import logging

from ..state import NormalizedBlock, WebSource
from .base import make_block_id

logger = logging.getLogger(__name__)

_MAX_URLS = 8
_MAX_CHARS_PER_URL = 2000

__all__ = ["WebAdapter"]


class WebAdapter:
    source_type = "web"

    def normalize(self, source: WebSource, *, session_id: str = "") -> list[NormalizedBlock]:
        urls = list(source.urls[:_MAX_URLS])
        if not urls:
            logger.warning("WebSource has no URLs — skipping.")
            return []

        try:
            from web_search import search_exa  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("web_search module unavailable.") from exc

        blocks: list[NormalizedBlock] = []
        for i, url in enumerate(urls):
            # Use Exa URL-targeted fetch (keyword search scoped to the URL).
            raw_json, _summary = search_exa(
                query=url,
                num_results=1,
                search_type="keyword",
                max_characters=_MAX_CHARS_PER_URL,
            )
            try:
                data = json.loads(raw_json)
            except Exception:  # noqa: BLE001
                data = {}

            results = data.get("results") or []
            if results:
                item = results[0]
                title = item.get("title") or url
                highlights = item.get("highlights") or []
                body_text = item.get("text") or ""
                paragraphs = highlights if highlights else ([body_text] if body_text else ["(no content retrieved)"])
                body = "\n\n".join(paragraphs)
            else:
                # Exa unavailable or no result — stub block so deck continues.
                err = data.get("error") or "no results"
                title = url
                body = f"(Web content unavailable: {err})"
                paragraphs = [body]

            blocks.append(
                NormalizedBlock(
                    block_id=make_block_id(
                        source_type="web",
                        source_ref=url,
                        idx=i,
                        content_signature=body[:120],
                    ),
                    kind="narrative",
                    title=title,
                    content={
                        "url": url,
                        "title": title,
                        "body": body,
                        "paragraphs": paragraphs,
                    },
                    source_type="web",
                    source_ref=url,
                    evidence_refs=[url],
                    suggested_slide_layouts=["narrative", "bullets"],
                )
            )

        logger.info("WebSource: %d block(s) from %d URL(s)", len(blocks), len(urls))
        return blocks

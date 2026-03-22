"""Fetch and extract main text content from a URL."""

import trafilatura
from langchain_core.tools import tool

from utils.persistence import persist_tool_result


@tool
def fetch_url(url: str) -> str:
    """Fetch a webpage and extract its main text content.

    Returns a tool_result_id pointer — call retrieve_tool_result(tool_result_id)
    to read the full extracted text. Use this when you need the full content of
    an article, report, SEC filing, or any page — not just a search snippet.
    """
    try:
        html = trafilatura.fetch_url(url)
        if not html:
            summary = f"fetch_url failed: no content returned for {url}"
            return persist_tool_result("fetch_url", {"url": url}, "", summary)

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if not text:
            summary = f"fetch_url fetched {url} but could not extract main content (JS-rendered or paywalled)"
            return persist_tool_result("fetch_url", {"url": url}, "", summary)

        char_count = len(text)
        summary = f"Fetched {url} — extracted {char_count:,} chars of main content."
        return persist_tool_result("fetch_url", {"url": url}, text, summary)

    except Exception as exc:  # noqa: BLE001
        summary = f"fetch_url error for {url}: {exc}"
        return persist_tool_result("fetch_url", {"url": url}, "", summary)

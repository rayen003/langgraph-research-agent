"""Web search tool backed by Tavily."""

import json
import os

from langchain_core.tools import tool
from langchain_tavily import TavilySearch

from utils.persistence import persist_tool_result


@tool
def search_web(query: str) -> str:
    """Search the web with Tavily."""
    tavily = TavilySearch(api_key=os.getenv("TAVILY_API_KEY"), max_results=5, topic="general")
    raw = tavily.run(query)
    summary = f"Web search completed for '{query}'."
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            summary = f"Web search for '{query}' returned {len(parsed.get('results', []))} result(s)."
    except (json.JSONDecodeError, TypeError):
        pass
    return persist_tool_result("search_web", {"query": query}, raw, summary)

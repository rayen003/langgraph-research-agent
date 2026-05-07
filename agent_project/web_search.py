"""Web search helpers."""

from __future__ import annotations

import json
import os
from typing import Any

import requests


EXA_SEARCH_URL = "https://api.exa.ai/search"


def search_exa(
    query: str,
    *,
    num_results: int,
    search_type: str,
    max_characters: int,
) -> tuple[str, str]:
    """Search Exa and return a normalized JSON payload plus UI summary."""
    api_key = os.getenv("EXA_API_KEY")
    if not api_key or api_key == "your_exa_api_key_here":
        payload = {
            "provider": "exa",
            "query": query,
            "error": "EXA_API_KEY is not set",
            "results": [],
        }
        return json.dumps(payload), "Exa search failed: EXA_API_KEY is not set."

    body: dict[str, Any] = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "contents": {
            "highlights": {
                "maxCharacters": max_characters,
            }
        },
    }

    try:
        response = requests.post(
            EXA_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json=body,
            timeout=20,
        )
        response.raise_for_status()
        raw = response.json()
    except requests.RequestException as exc:
        payload = {
            "provider": "exa",
            "query": query,
            "error": str(exc),
            "results": [],
        }
        return json.dumps(payload), f"Exa search failed for '{query}'."

    results = []
    for item in raw.get("results", []) if isinstance(raw, dict) else []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "published_date": item.get("publishedDate"),
                "author": item.get("author"),
                "highlights": item.get("highlights") or [],
                "text": item.get("text"),
            }
        )

    payload = {
        "provider": "exa",
        "query": query,
        "request_id": raw.get("requestId") if isinstance(raw, dict) else None,
        "results": results,
    }
    summary = f"Exa search for '{query}' returned {len(results)} result(s)."
    return json.dumps(payload), summary

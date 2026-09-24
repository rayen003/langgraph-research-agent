"""Web search helpers."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import requests


EXA_SEARCH_URL = "https://api.exa.ai/search"


def _missing_api_key_result(query: str) -> tuple[str, str]:
    payload = {
        "provider": "exa",
        "query": query,
        "error": "EXA_API_KEY is not set",
        "results": [],
    }
    return json.dumps(payload), "Exa search failed: EXA_API_KEY is not set."


def _request_body(
    query: str,
    *,
    num_results: int,
    search_type: str,
    max_characters: int,
) -> dict[str, Any]:
    return {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "contents": {"highlights": {"maxCharacters": max_characters}},
    }


def _normalize_response(query: str, raw: Any) -> tuple[str, str]:
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


def _error_result(query: str, exc: Exception) -> tuple[str, str]:
    payload = {
        "provider": "exa",
        "query": query,
        "error": str(exc),
        "results": [],
    }
    return json.dumps(payload), f"Exa search failed for '{query}'."


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
        return _missing_api_key_result(query)

    body = _request_body(
        query,
        num_results=num_results,
        search_type=search_type,
        max_characters=max_characters,
    )

    try:
        response = requests.post(
            EXA_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json=body,
            # Separate connect/read limits prevent a slow proxy or upstream
            # from consuming roughly both sides of the old combined timeout.
            timeout=(5, 12),
        )
        response.raise_for_status()
        raw = response.json()
    except requests.RequestException as exc:
        return _error_result(query, exc)

    return _normalize_response(query, raw)


async def search_exa_async(
    query: str,
    *,
    num_results: int,
    search_type: str,
    max_characters: int,
) -> tuple[str, str]:
    """Nonblocking Exa search for LangGraph async tool execution."""
    api_key = os.getenv("EXA_API_KEY")
    if not api_key or api_key == "your_exa_api_key_here":
        return _missing_api_key_result(query)

    body = _request_body(
        query,
        num_results=num_results,
        search_type=search_type,
        max_characters=max_characters,
    )
    timeout = httpx.Timeout(connect=5, read=12, write=5, pool=5)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                EXA_SEARCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                },
                json=body,
            )
            response.raise_for_status()
            raw = response.json()
    except httpx.HTTPError as exc:
        return _error_result(query, exc)

    return _normalize_response(query, raw)

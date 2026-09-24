"""Async network boundary tests for tools exposed through ToolNode."""

from __future__ import annotations

import asyncio
import json

import documents
import tools
import web_search


def test_network_backed_tools_expose_async_coroutines() -> None:
    assert tools.search_web.coroutine is not None
    assert tools.fetch_sec_filing.coroutine is not None
    assert tools.query_knowledge_graph.coroutine is not None
    assert documents.search_documents.coroutine is not None


def test_search_web_tool_ainvoke_uses_async_provider(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_search(query: str, **_kwargs) -> tuple[str, str]:
        calls.append(query)
        await asyncio.sleep(0)
        return json.dumps({"provider": "exa", "results": []}), "async result"

    monkeypatch.setattr(tools, "search_exa_async", fake_search)
    monkeypatch.setattr(
        tools,
        "persist_tool_result",
        lambda name, args, raw, summary: json.dumps({
            "tool_name": name,
            "query": args["query"],
            "summary": summary,
        }),
    )

    raw = asyncio.run(tools.search_web.ainvoke({"query": "latest Apple news"}))

    assert calls == ["latest Apple news"]
    assert json.loads(raw)["summary"] == "async result"


def test_search_exa_async_uses_nonblocking_http_client(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "requestId": "req-1",
                "results": [{
                    "title": "Apple update",
                    "url": "https://example.com/apple",
                    "publishedDate": "2026-09-04",
                    "highlights": ["Update"],
                }],
            }

    class AsyncClient:
        def __init__(self, *, timeout) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            await asyncio.sleep(0)
            captured.update(url=url, headers=headers, body=json)
            return Response()

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(web_search.httpx, "AsyncClient", AsyncClient)

    raw, summary = asyncio.run(web_search.search_exa_async(
        "latest Apple news",
        num_results=6,
        search_type="auto",
        max_characters=4_000,
    ))

    payload = json.loads(raw)
    assert captured["url"] == web_search.EXA_SEARCH_URL
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["body"]["numResults"] == 6
    assert payload["request_id"] == "req-1"
    assert payload["results"][0]["title"] == "Apple update"
    assert summary.endswith("returned 1 result(s).")


def test_search_exa_async_returns_normalized_transport_error(monkeypatch) -> None:
    class AsyncClient:
        def __init__(self, *, timeout) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            raise web_search.httpx.ConnectError("provider unavailable")

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(web_search.httpx, "AsyncClient", AsyncClient)

    raw, summary = asyncio.run(web_search.search_exa_async(
        "latest Apple news",
        num_results=2,
        search_type="auto",
        max_characters=500,
    ))

    payload = json.loads(raw)
    assert payload["results"] == []
    assert "provider unavailable" in payload["error"]
    assert summary == "Exa search failed for 'latest Apple news'."

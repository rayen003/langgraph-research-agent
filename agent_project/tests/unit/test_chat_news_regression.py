"""Regression contracts for fast current-news chat turns.

These tests use the real persisted tool-result envelope. Earlier tests used
small fake dictionaries, which missed failures in pointer dereferencing and
post-tool model loops.
"""

import json
from langchain_core.messages import AIMessage, HumanMessage

import graphs.conversational as conversational
from utils import set_thread_id


class _NewsSynthesisLLM:
    def __init__(self):
        self.calls = 0
        self.messages = None

    def stream(self, messages):
        self.calls += 1
        self.messages = messages
        yield AIMessage(content="Apple scheduled a product event, creating a near-term catalyst. [S1]\n\n")
        yield AIMessage(content=(
            "- **Product event:** Apple announced a September 9 event. "
            "**Why it matters:** New-device details can affect demand expectations. [S1]"
        ))


class _BrokenNewsSynthesisLLM:
    def stream(self, _messages):
        raise TimeoutError("synthetic synthesis timeout")
        yield  # pragma: no cover


class _SearchTool:
    name = "search_web"

    def __init__(self, pointer):
        self.pointer = pointer
        self.calls = []

    def invoke(self, args):
        self.calls.append(args)
        return json.dumps(self.pointer)


def _news_state():
    return {
        "messages": [HumanMessage(content="what are the latest Apple news")],
        "session_id": "news-regression",
        "selected_playbook": {"id": "latest_company_news"},
        "tool_policy": {
            "allowed_tools": ["search_web", "query_knowledge_graph", "retrieve_tool_result"],
            "max_tool_calls": 1,
        },
    }


def test_news_pointer_fallback_reads_stringified_result(monkeypatch, tmp_path):
    """A successful search pointer must produce answer content without another LLM."""
    thread_id = "news-pointer-regression"
    result_id = "search_web_result"
    tool_dir = tmp_path / "tool_results"
    tool_dir.mkdir()
    (tool_dir / f"{result_id}.json").write_text(json.dumps({
        "tool_result_id": result_id,
        "tool_name": "search_web",
        "summary": "one Apple result",
        "result": json.dumps({
            "provider": "exa",
            "results": [{
                "title": "Apple event set for September 9",
                "url": "https://example.test/apple",
                "published_date": "2026-09-03T12:00:00Z",
                "highlights": ["Apple announced its next event."],
            }],
        }),
    }), encoding="utf-8")
    monkeypatch.setattr(conversational, "get_run_dir", lambda: tmp_path)
    set_thread_id(thread_id)

    answer = conversational._fallback_answer_from_tool_results([
        conversational.ToolMessage(
            content=json.dumps({"tool_result_id": result_id}),
            tool_call_id="search-1",
        ),
    ])

    assert "Apple event set" in answer
    assert "Apple announced" in answer
    assert answer.startswith("Latest coverage contains")
    assert "I found these relevant sources" not in answer


def test_latest_news_playbook_executes_one_search_and_one_grounded_synthesis(monkeypatch, tmp_path):
    """Current-news micro playbook must answer, not expose raw search results."""
    events = []
    result_id = "search_web_result"
    tool_dir = tmp_path / "tool_results"
    tool_dir.mkdir()
    (tool_dir / f"{result_id}.json").write_text(json.dumps({
        "tool_result_id": result_id,
        "tool_name": "search_web",
        "summary": "one Apple result",
        "result": json.dumps({
            "provider": "exa",
            "results": [{
                "title": "Apple event set for September 9",
                "url": "https://example.test/apple",
                "published_date": "2026-09-03T12:00:00Z",
                "highlights": ["Apple announced its next event."],
            }],
        }),
    }), encoding="utf-8")
    search = _SearchTool({
        "tool_result_id": result_id,
        "tool_name": "search_web",
        "summary": "one Apple result",
    })
    model = _NewsSynthesisLLM()
    monkeypatch.setattr(conversational, "get_run_dir", lambda: tmp_path)
    monkeypatch.setattr(conversational, "news_synthesis_llm", model)
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", {"search_web": search})
    monkeypatch.setattr(conversational, "emit_ui_event", events.append)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)
    set_thread_id("news-loop-regression")

    result = conversational._chat_node_inner(_news_state())

    assert model.calls == 1
    assert search.calls == [{"query": "latest company news for AAPL stock"}]
    answer = result["messages"][-1].content
    assert answer.startswith("Apple scheduled")
    assert "Why it matters" in answer
    assert "[example.test, Sep 3, 2026](https://example.test/apple)" in answer
    assert "I found these relevant sources" not in answer
    assert "https://example.test/apple" not in model.messages[-1].content
    assert "".join(
        event.get("token", "") for event in events if event.get("type") == "chat_token"
    ).startswith("Apple scheduled")


def test_news_ranking_removes_generic_pages_and_duplicate_stories() -> None:
    payload = {
        "provider": "exa",
        "results": [
            {
                "title": "Newsroom - Apple",
                "url": "https://www.apple.com/newsroom/",
                "published_date": None,
                "highlights": ["Stay up to date with Apple Newsroom."],
            },
            {
                "title": "Apple faces major UK application tracking lawsuit",
                "url": "https://www.reuters.com/technology/apple-uk-lawsuit-1",
                "published_date": "2026-09-04T06:00:00Z",
                "highlights": ["Apple faces a UK application tracking lawsuit."],
            },
            {
                "title": "Apple faces UK application tracking lawsuit",
                "url": "https://example.test/apple-uk-lawsuit-copy",
                "published_date": "2026-09-04T05:00:00Z",
                "highlights": ["Syndicated coverage of the same lawsuit."],
            },
            {
                "title": "Apple supplier raises component price guidance",
                "url": "https://www.cnbc.com/apple-supplier-prices",
                "published_date": "2026-09-04T04:00:00Z",
                "highlights": ["Supplier guidance points to higher component costs."],
            },
        ],
    }

    ranked = conversational._rank_news_results(payload)

    assert [item["source"] for item in ranked] == ["Reuters", "CNBC"]
    assert all("Newsroom - Apple" not in item["title"] for item in ranked)
    assert [item["source_id"] for item in ranked] == ["S1", "S2"]


def test_latest_news_query_resolves_company_alias_before_search() -> None:
    query = conversational._latest_news_query(
        "Whar are apples latest news?",
        {"turn_context": {"active_entities": []}, "memory_context": {}},
    )

    assert "AAPL" in query
    assert "company" in query.lower()
    assert query != "Whar are apples latest news?"


def test_news_fast_path_rejects_non_answer_deliverables() -> None:
    assert conversational._can_use_latest_news_fast_path(
        {"id": "latest_company_news"},
        ["answer"],
    )
    assert not conversational._can_use_latest_news_fast_path(
        {"id": "latest_company_news"},
        ["answer", "chart"],
    )


def test_chat_does_not_attach_stale_deck_from_run_directory(monkeypatch) -> None:
    monkeypatch.setattr(
        conversational,
        "list_deck_artifact_paths",
        lambda: ["decks/stale-prior-turn.pptx"],
    )

    assert conversational._extract_deck_artifact_paths([]) == []


def test_news_synthesis_timeout_uses_dated_grounded_fallback(monkeypatch, tmp_path):
    events = []
    result_id = "search_web_timeout_fallback"
    tool_dir = tmp_path / "tool_results"
    tool_dir.mkdir()
    (tool_dir / f"{result_id}.json").write_text(json.dumps({
        "tool_result_id": result_id,
        "tool_name": "search_web",
        "result": json.dumps({
            "provider": "exa",
            "query": "latest Apple news",
            "results": [{
                "title": "Apple event set for September 9",
                "url": "https://www.reuters.com/technology/apple-event",
                "published_date": "2026-09-03T12:00:00Z",
                "highlights": ["Apple announced its next product event for September 9."],
            }],
        }),
    }), encoding="utf-8")
    search = _SearchTool({"tool_result_id": result_id, "tool_name": "search_web"})
    monkeypatch.setattr(conversational, "get_run_dir", lambda: tmp_path)
    monkeypatch.setattr(conversational, "news_synthesis_llm", _BrokenNewsSynthesisLLM())
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", {"search_web": search})
    monkeypatch.setattr(conversational, "emit_ui_event", events.append)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    result = conversational._chat_node_inner(_news_state())
    answer = result["messages"][-1].content

    assert answer.startswith("Latest coverage contains")
    assert "Apple event set for September 9" in answer
    assert "Reuters, Sep 3, 2026" in answer
    assert any(event.get("type") == "chat_complete" for event in events)


def test_provider_clients_disable_hidden_retries():
    """A transient provider failure must not multiply latency in short turns."""
    import file as main_graph

    assert conversational.llm.max_retries == 0
    assert main_graph.intent_llm.max_retries == 0


def test_exa_search_uses_separate_bounded_timeouts(monkeypatch):
    import web_search

    calls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def fake_post(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return _Response()

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(web_search.requests, "post", fake_post)
    web_search.search_exa("latest Apple news", num_results=1, search_type="auto", max_characters=100)

    assert calls == [(5, 12)]

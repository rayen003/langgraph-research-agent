from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@pytest.fixture()
def isolated_runtime(monkeypatch: pytest.MonkeyPatch):
    import storage
    import utils

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setattr(storage, "DB_PATH", root / "agent.db")
        monkeypatch.setattr(storage, "RUNS_DIR", root / "runs")
        monkeypatch.setattr(utils, "RUNS_DIR", root / "runs")
        storage.init_db()
        utils.set_thread_id("reference-harness")
        yield root, storage


def test_reference_broker_resolves_tool_result_and_object_version(isolated_runtime) -> None:
    root, storage = isolated_runtime
    from reference_broker import resolve_ref
    from utils import persist_tool_result

    pointer = json.loads(persist_tool_result(
        "lookup",
        {"ticker": "AAPL"},
        json.dumps({"value": 42}),
        "Found AAPL value",
    ))
    stored = storage.upsert_workspace_object({
        "object_id": "chart:aapl",
        "object_type": "chart",
        "title": "Apple revenue chart",
        "status": "complete",
        "session_id": "session-1",
        "artifact_paths": [str(root / "aapl.png")],
        "payload": {"source_result_ids": [pointer["tool_result_id"]]},
    })

    tool_ref = resolve_ref(pointer["tool_result_id"])
    object_ref = resolve_ref(stored["version_id"])

    assert tool_ref["kind"] == "tool_result"
    assert json.loads(tool_ref["payload"]["result"])["value"] == 42
    assert object_ref["kind"] == "object_version"
    assert object_ref["payload"]["object_id"] == "chart:aapl"


def test_collect_refs_uses_existing_normalized_tool_result_envelope() -> None:
    from domain.tool_execution import ToolResult
    from reference_broker import collect_refs

    normalized = ToolResult(
        invocation_id="inv-1",
        tool_id="render_financial_chart",
        status="success",
        summary="Rendered chart",
        output={"tool_result_id": "render_financial_chart_123"},
        payload_ref="/tmp/render_financial_chart_123.json",
        artifact_paths=["/tmp/apple-financials.png"],
        object_version_ids=["chart:aapl:v000001"],
    )
    message = ToolMessage(
        content=json.dumps(normalized.output),
        tool_call_id="call-1",
        name="render_financial_chart",
        artifact={"tool_result": normalized.model_dump(mode="json")},
    )

    refs = collect_refs([message])

    assert refs["result_refs"] == ["render_financial_chart_123"]
    assert refs["artifact_refs"] == ["chart:aapl:v000001"]
    assert refs["artifact_paths"] == ["/tmp/apple-financials.png"]


def test_generated_artifact_markdown_is_removed_before_chat_delivery() -> None:
    from graphs.conversational import _strip_generated_artifact_markdown

    artifact = "/tmp/runs/chat-1/artifacts/aapl-stock-price.png"
    result = _strip_generated_artifact_markdown(
        f"Chart ready.\n\n![AAPL Stock Price Chart](sandbox:{artifact})",
        [artifact],
    )

    assert result == "Chart ready."


def test_dcf_financial_client_exposes_multi_period_statement_history(monkeypatch) -> None:
    from graphs.workflows.dcf import fundamentals

    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def fake_get(path: str, _api_key: str):
        if path.startswith("income-statement"):
            return [
                {"date": "2025-09-27", "calendarYear": "2025", "revenue": 416_161, "netIncome": 112_010, "operatingIncome": 133_050, "epsDiluted": 7.46},
                {"date": "2024-09-28", "calendarYear": "2024", "revenue": 391_035, "netIncome": 93_736, "operatingIncome": 123_216, "epsDiluted": 6.08},
            ]
        if path.startswith("cash-flow-statement"):
            return [
                {"date": "2025-09-27", "freeCashFlow": 98_000, "capitalExpenditure": -12_000},
                {"date": "2024-09-28", "freeCashFlow": 90_000, "capitalExpenditure": -10_000},
            ]
        return []

    monkeypatch.setattr(fundamentals, "_fmp_get_json", fake_get)

    result = fundamentals.fetch_company_financial_history("AAPL", period="annual", limit=2)

    assert result["provider"] == "fmp"
    assert result["ticker"] == "AAPL"
    assert [row["fiscal_year"] for row in result["series"]] == ["2024", "2025"]
    assert result["series"][-1]["revenue"] == 416_161
    assert result["series"][-1]["free_cash_flow"] == 98_000
    assert result["coverage"] >= {"revenue", "net_income", "free_cash_flow"}


def test_financial_client_accepts_natural_quarterly_period(monkeypatch) -> None:
    from graphs.workflows.dcf import fundamentals

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    paths: list[str] = []

    def fake_get(path: str, _api_key: str):
        paths.append(path)
        return [{"date": "2026-06-27", "calendarYear": "2026", "period": "Q3", "revenue": 109_400}]

    monkeypatch.setattr(fundamentals, "_fmp_get_json", fake_get)

    result = fundamentals.fetch_company_financial_history("AAPL", period="quarterly", limit=4)

    assert result["period"] == "quarter"
    assert all("period=quarter" in path for path in paths)


def test_financial_tool_then_chart_consumes_result_id_and_persists_object(
    monkeypatch,
    isolated_runtime,
) -> None:
    _root, storage = isolated_runtime
    import tools

    monkeypatch.setattr(tools, "fetch_company_financial_history", lambda *_args, **_kwargs: {
        "provider": "fmp",
        "ticker": "AAPL",
        "period": "annual",
        "currency": "USD",
        "coverage": ["revenue", "net_income"],
        "series": [
            {"fiscal_year": "2024", "date": "2024-09-28", "revenue": 391_035, "net_income": 93_736},
            {"fiscal_year": "2025", "date": "2025-09-27", "revenue": 416_161, "net_income": 112_010},
        ],
        "source_refs": [{"source_id": "fmp:AAPL:income:annual", "source_type": "api", "title": "FMP income statement", "provider": "FMP", "period": "FY2025"}],
    })

    financial_pointer = json.loads(tools.get_company_financials.invoke({
        "ticker": "AAPL",
        "period": "annual",
        "limit": 5,
    }))
    assert financial_pointer["next_actions"] == [
        "Pass tool_result_id directly to render_financial_chart when a chart is requested."
    ]
    assert "Do not retrieve" in financial_pointer["hint"]
    chart_pointer = json.loads(tools.render_financial_chart.invoke({
        "input_result_id": financial_pointer["tool_result_id"],
        "metrics": ["revenue", "net_income"],
        "chart_type": "bar",
        "title": "Apple financial performance",
    }))

    assert chart_pointer["artifact_paths"]
    assert Path(chart_pointer["artifact_paths"][0]).exists()
    assert chart_pointer["object_version_ids"]
    chart_object = storage.get_workspace_object(chart_pointer["object_id"])
    assert chart_object["object_type"] == "chart"
    assert chart_object["payload"]["source_result_ids"] == [financial_pointer["tool_result_id"]]


def test_financial_chart_uses_readable_quarter_labels_and_separate_eps_axis(
    monkeypatch,
    isolated_runtime,
) -> None:
    _root, storage = isolated_runtime
    import tools

    monkeypatch.setattr(tools, "fetch_company_financial_history", lambda *_args, **_kwargs: {
        "provider": "fmp",
        "ticker": "AAPL",
        "period": "quarter",
        "currency": "USD",
        "coverage": ["revenue", "net_income", "diluted_eps"],
        "series": [
            {"date": "2025-09-27", "period": "Q4", "revenue": 102_466_000_000, "net_income": 27_466_000_000, "diluted_eps": 1.85},
            {"date": "2025-12-27", "period": "Q1", "revenue": 143_756_000_000, "net_income": 42_097_000_000, "diluted_eps": 2.84},
        ],
        "source_refs": [],
    })
    financial_pointer = json.loads(tools.get_company_financials.invoke({
        "ticker": "AAPL", "period": "quarter", "limit": 2,
    }))
    chart_pointer = json.loads(tools.render_financial_chart.invoke({
        "input_result_id": financial_pointer["tool_result_id"],
        "metrics": ["revenue", "net_income", "diluted_eps"],
        "chart_type": "bar",
    }))

    chart_object = storage.get_workspace_object(chart_pointer["object_id"])
    assert chart_object["payload"]["periods"] == ["Sep 2025", "Dec 2025"]
    assert chart_object["payload"]["metric_axes"] == {
        "revenue": "currency_billions",
        "net_income": "currency_billions",
        "diluted_eps": "per_share",
    }


def test_stock_price_tool_then_chart_uses_actual_history_result_id(
    monkeypatch,
    isolated_runtime,
) -> None:
    _root, storage = isolated_runtime
    import pandas as pd
    import tools
    import yfinance

    frame = pd.DataFrame(
        {"Close": [150.0, 165.0, 180.0]},
        index=pd.to_datetime(["2021-09-01", "2023-09-01", "2026-09-01"]),
    )
    calls: list[dict] = []

    def fake_download(ticker: str, **kwargs):
        calls.append({"ticker": ticker, **kwargs})
        return frame

    monkeypatch.setattr(yfinance, "download", fake_download)
    price_pointer = json.loads(tools.get_stock_price_history.invoke({
        "ticker": "aapl",
        "period": "5y",
    }))

    assert calls == [{
        "ticker": "AAPL",
        "period": "5y",
        "auto_adjust": True,
        "multi_level_index": False,
        "progress": False,
    }]
    assert price_pointer["ticker"] == "AAPL"
    assert price_pointer["result_schema"] == "market_price_history.v1"
    assert "render_stock_price_chart" in price_pointer["next_actions"][0]

    chart_pointer = json.loads(tools.render_stock_price_chart.invoke({
        "input_result_id": price_pointer["tool_result_id"],
        "title": "Apple five-year stock price",
    }))

    assert Path(chart_pointer["artifact_paths"][0]).exists()
    chart_object = storage.get_workspace_object(chart_pointer["object_id"])
    assert chart_object["payload"]["source_result_ids"] == [price_pointer["tool_result_id"]]
    assert chart_object["payload"]["total_return_pct"] == pytest.approx(20.0)


def test_normalized_stock_comparison_uses_two_result_ids(monkeypatch, isolated_runtime) -> None:
    _root, storage = isolated_runtime
    import pandas as pd
    import tools
    import yfinance

    frames = {
        "AAPL": pd.DataFrame(
            {"Close": [100.0, 130.0, 150.0]},
            index=pd.to_datetime(["2021-09-01", "2023-09-01", "2026-09-01"]),
        ),
        "NVDA": pd.DataFrame(
            {"Close": [50.0, 100.0, 175.0]},
            index=pd.to_datetime(["2021-09-01", "2023-09-01", "2026-09-01"]),
        ),
    }
    monkeypatch.setattr(yfinance, "download", lambda ticker, **_kwargs: frames[ticker].copy())

    apple = json.loads(tools.get_stock_price_history.invoke({"ticker": "AAPL", "period": "5y"}))
    nvidia = json.loads(tools.get_stock_price_history.invoke({"ticker": "NVDA", "period": "5y"}))
    comparison = json.loads(tools.render_normalized_stock_comparison.invoke({
        "input_result_ids": [apple["tool_result_id"], nvidia["tool_result_id"]],
    }))

    assert Path(comparison["artifact_paths"][0]).exists()
    assert comparison["source_result_ids"] == [apple["tool_result_id"], nvidia["tool_result_id"]]
    chart_object = storage.get_workspace_object(comparison["object_id"])
    assert chart_object["payload"]["base_index"] == 100
    assert chart_object["payload"]["total_return_pct"] == {"AAPL": 50.0, "NVDA": 250.0}


class _HarnessRouter:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.calls += 1
        self.prompts.append(str(messages[-1].content))
        reuse_turn = self.calls > 1
        return AIMessage(content=json.dumps({
            "route_level": "direct" if reuse_turn else "small_task",
            "playbook_id": None,
            "selected_workflow": None,
            "selected_case_type": None,
            "confidence": 0.98,
            "latency_class": "instant" if reuse_turn else "medium",
            "max_tool_calls": 0 if reuse_turn else 4,
            "creates_objects": not reuse_turn,
            "needs_confirmation": False,
            "object_policy": "none" if reuse_turn else "artifact",
            "speech_act": "context_recall" if reuse_turn else "fresh_lookup",
            "context_reference": "workspace" if reuse_turn else "recent_thread",
            "data_requirement": "workspace" if reuse_turn else "fresh_external",
            "requested_outputs": ["answer"] if reuse_turn else ["answer", "chart"],
            "reason": "reuse existing chart" if reuse_turn else "financial analysis plus chart",
        }))


class _HarnessModel:
    def __init__(self) -> None:
        self.calls = 0
        self.histories: list[list] = []

    def invoke(self, history):
        self.calls += 1
        self.histories.append(list(history))
        if self.calls == 1:
            return AIMessage(content="Apple delivered stronger revenue and net income.")
        if self.calls == 2:
            return AIMessage(content="", tool_calls=[
                {"id": "financials", "name": "get_company_financials", "args": {"ticker": "AAPL", "period": "annual", "limit": 5}},
                {"id": "news", "name": "search_web", "args": {"query": "Apple current-year financial performance"}},
            ])
        if self.calls == 3:
            return AIMessage(content="", tool_calls=[
                {"id": "chart", "name": "render_financial_chart", "args": {"input_result_id": "financials_123", "metrics": ["revenue", "net_income"], "chart_type": "bar"}},
            ])
        if self.calls == 4:
            return AIMessage(content="Apple revenue and net income increased. Chart attached.")
        return AIMessage(content="Yes. I will reuse the existing Apple financial chart.")


class _PointerTool:
    def __init__(self, name: str, payload: dict):
        self.name = name
        self.payload = payload
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(args)
        return json.dumps(self.payload)


def test_chart_completion_recovery_prefers_multi_result_renderer_outside_react_budget(
    monkeypatch,
    isolated_runtime,
) -> None:
    import graphs.conversational as conversational
    from reference_broker import collect_refs

    comparison = _PointerTool("render_normalized_stock_comparison", {
        "tool_result_id": "comparison_123",
        "tool_name": "render_normalized_stock_comparison",
        "summary": "Rendered normalized AAPL and NVDA comparison",
        "artifact_paths": ["/tmp/aapl-nvda-normalized.png"],
        "object_version_ids": ["chart:aapl-nvda:v000001"],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map[comparison.name] = comparison
    schemas = {
        "financials_123": "company_financial_history.v1",
        "prices_aapl_123": "market_price_history.v1",
        "prices_nvda_123": "market_price_history.v1",
    }

    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(
        conversational,
        "resolve_ref",
        lambda result_id: {"payload": {"result_schema": schemas[result_id]}},
    )

    recovery = conversational._recover_missing_artifact_tools(
        required_outputs=["answer", "chart"],
        current_turn={"result_refs": list(schemas)},
        allowed_tool_names=set(tool_map),
        remaining_tool_calls=0,
        execution_context={"thread_id": "chart-recovery", "session_id": "session-1"},
    )

    assert recovery is not None
    recovery_call, messages = recovery
    assert recovery_call.tool_calls[0]["name"] == "render_normalized_stock_comparison"
    assert comparison.calls[0]["input_result_ids"] == [
        "prices_aapl_123",
        "prices_nvda_123",
    ]
    assert collect_refs(messages)["artifact_paths"] == ["/tmp/aapl-nvda-normalized.png"]


def test_graph_materializes_chart_after_source_calls_exhaust_react_budget(
    monkeypatch,
    isolated_runtime,
) -> None:
    import file as main_graph
    import graphs.conversational as conversational

    class Router:
        def invoke(self, _messages):
            return AIMessage(content=json.dumps({
                "route_level": "small_task",
                "playbook_id": None,
                "selected_workflow": None,
                "selected_case_type": None,
                "confidence": 0.98,
                "latency_class": "medium",
                "max_tool_calls": 3,
                "creates_objects": True,
                "needs_confirmation": False,
                "object_policy": "artifact",
                "speech_act": "create_artifact",
                "context_reference": "none",
                "data_requirement": "fresh_external",
                "requested_outputs": ["answer", "chart"],
                "reason": "financial context plus normalized market-price chart",
            }))

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _history):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {"id": "financials", "name": "get_company_financials", "args": {"ticker": "AAPL"}},
                    {"id": "aapl_prices", "name": "get_stock_price_history", "args": {"ticker": "AAPL", "period": "5y"}},
                    {"id": "nvda_prices", "name": "get_stock_price_history", "args": {"ticker": "NVDA", "period": "5y"}},
                ])
            for index, message in enumerate(_history):
                if not isinstance(message, ToolMessage):
                    continue
                preceding = next(
                    candidate
                    for candidate in reversed(_history[:index])
                    if isinstance(candidate, AIMessage)
                )
                assert isinstance(preceding, AIMessage)
                assert any(call["id"] == message.tool_call_id for call in preceding.tool_calls)
            return AIMessage(content="Apple financials loaded. Normalized Apple and Nvidia chart attached.")

    class PriceTool:
        name = "get_stock_price_history"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke(self, args):
            self.calls.append(args)
            ticker = str(args["ticker"]).upper()
            return json.dumps({
                "tool_result_id": f"prices_{ticker.lower()}_123",
                "summary": f"{ticker} price history",
            })

    model = Model()
    financials = _PointerTool("get_company_financials", {
        "tool_result_id": "financials_123", "summary": "Apple financial history",
    })
    prices = PriceTool()
    comparison = _PointerTool("render_normalized_stock_comparison", {
        "tool_result_id": "comparison_123",
        "summary": "Normalized stock comparison",
        "artifact_paths": ["/tmp/aapl-nvda-normalized.png"],
        "object_version_ids": ["chart:aapl-nvda:v000001"],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map.update({tool.name: tool for tool in (financials, prices, comparison)})
    schemas = {
        "financials_123": "company_financial_history.v1",
        "prices_aapl_123": "market_price_history.v1",
        "prices_nvda_123": "market_price_history.v1",
    }
    events: list[dict] = []

    monkeypatch.setattr(main_graph, "intent_llm", Router())
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: model)
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "resolve_ref", lambda result_id: {"payload": {"result_schema": schemas[result_id]}})
    monkeypatch.setattr(conversational, "emit_ui_event", events.append)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    result = main_graph.app.invoke({
        "messages": [HumanMessage(content="Get Apple's financials and plot its stock price against Nvidia")],
        "mode": "auto",
        "session_id": "session-chart-recovery",
    }, config={"configurable": {"thread_id": "thread-chart-recovery"}})

    assert model.calls == 3
    assert len(comparison.calls) == 1
    assert comparison.calls[0]["input_result_ids"] == ["prices_aapl_123", "prices_nvda_123"]
    assert result["current_turn"]["artifact_paths"] == ["/tmp/aapl-nvda-normalized.png"]
    complete = next(event for event in events if event["type"] == "chat_complete")
    assert complete["artifact_paths"] == ["/tmp/aapl-nvda-normalized.png"]


def test_multiturn_graph_enforces_chart_completion_and_registers_refs(
    monkeypatch,
    isolated_runtime,
) -> None:
    import file as main_graph
    import graphs.conversational as conversational

    model = _HarnessModel()
    router = _HarnessRouter()
    financials = _PointerTool("get_company_financials", {
        "tool_result_id": "financials_123",
        "tool_name": "get_company_financials",
        "summary": "Loaded annual Apple financials",
        "stored_at": "/tmp/financials_123.json",
    })
    search = _PointerTool("search_web", {
        "tool_result_id": "search_123",
        "tool_name": "search_web",
        "summary": "Found current Apple performance context",
        "stored_at": "/tmp/search_123.json",
    })
    chart = _PointerTool("render_financial_chart", {
        "tool_result_id": "chart_result_123",
        "tool_name": "render_financial_chart",
        "summary": "Rendered Apple financial chart",
        "stored_at": "/tmp/chart_result_123.json",
        "artifact_paths": ["/tmp/apple-financials.png"],
        "object_version_ids": ["chart:aapl:v000001"],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map.update({tool.name: tool for tool in (financials, search, chart)})

    monkeypatch.setattr(main_graph, "intent_llm", router)
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: model)
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    result = main_graph.app.invoke({
        "messages": [HumanMessage(content="Find Apple's current-year financial performance and generate a graph")],
        "mode": "auto",
        "session_id": "session-reference-harness",
    }, config={"configurable": {"thread_id": "thread-reference-harness"}})

    assert model.calls == 4
    assert len(financials.calls) == len(search.calls) == len(chart.calls) == 1
    assert chart.calls[0]["input_result_id"] == "financials_123"
    assert result["current_turn"]["result_refs"] == ["financials_123", "search_123", "chart_result_123"]
    assert result["current_turn"]["artifact_refs"] == ["chart:aapl:v000001"]
    assert result["current_turn"]["artifact_paths"] == ["/tmp/apple-financials.png"]
    assert result["messages"][-1].content == "Apple revenue and net income increased. Chart attached."
    missing_feedback = [
        message.content
        for message in model.histories[1]
        if isinstance(message, HumanMessage) and "missing requested outputs" in str(message.content).lower()
    ]
    assert missing_feedback

    storage = isolated_runtime[1]
    storage.upsert_workspace_object({
        "object_id": "chart:aapl",
        "object_type": "chart",
        "schema_ref": "chart_artifact.v1",
        "title": "Apple financial performance",
        "summary": "Revenue and net income across current fiscal periods.",
        "status": "complete",
        "session_id": "session-reference-harness",
        "thread_id": "thread-reference-harness",
        "entity_refs": [{"kind": "company", "name": "Apple", "ticker": "AAPL"}],
        "artifact_paths": ["/tmp/apple-financials.png"],
        "payload": {"source_result_ids": ["financials_123"]},
    })

    second = main_graph.app.invoke({
        "messages": [HumanMessage(content="Can we reuse that chart in the next analysis?")],
        "mode": "auto",
        "session_id": "session-reference-harness",
    }, config={"configurable": {"thread_id": "thread-reference-harness"}})

    assert router.calls == 2
    assert '"object_id": "chart:aapl"' in router.prompts[1]
    assert model.calls == 5
    assert len(financials.calls) == len(search.calls) == len(chart.calls) == 1
    assert second["current_turn"]["route"]["route_level"] == "direct"
    assert second["current_turn"]["result_refs"] == []
    assert second["messages"][-1].content == "Yes. I will reuse the existing Apple financial chart."

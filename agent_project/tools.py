"""Canonical tool definitions shared by all subgraphs.

Every tool returns a pointer via persist_tool_result — the caller sees a
{``tool_result_id``, ``summary``} envelope in the ToolMessage.  Full
payloads live on disk and are retrieved on demand via retrieve_tool_result.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from langchain_core.tools import tool
from simpleeval import simple_eval

from documents import search_documents, _session_ctx
from graphs.workflows.dcf import dcf_workflow_app, run_dcf_workflow_sync, summarize_dcf_payload
from graphs.workflows.dcf.fundamentals import fetch_company_financial_history
from graphs.workflows.dcf.hitl_snapshot import build_hitl_snapshot
from lg_compat import Command
from utils import (
    get_artifacts_dir,
    get_run_dir,
    persist_tool_result,
    emit_ui_event,
    set_dcf_hitl_payload,
    set_deck_hitl_payload,
    set_thread_id,
)
from tool_runtime import write_tool_progress
from web_search import search_exa, search_exa_async

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYTHON_EXEC_TIMEOUT = 60
_YFINANCE_DOWNLOAD_LOCK = threading.Lock()

_PYTHON_PRELUDE = '''
import os, warnings
warnings.filterwarnings("ignore")
artifacts_dir = os.environ.get("ARTIFACTS_DIR", ".")

import matplotlib
matplotlib.use("Agg")

def get_stock_data(ticker: str, period: str = "5y"):
    """Return a clean DataFrame with columns Date, Open, High, Low, Close, Volume."""
    import yfinance as yf
    import pandas as pd
    df = yf.download(ticker, period=period, auto_adjust=True,
                     multi_level_index=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    if "Price" in df.columns and "Close" not in df.columns:
        df = df.rename(columns={"Price": "Close"})
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = df[col].squeeze()
    return df
'''


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _persist_dcf_payload(payload: dict, args: dict[str, Any]) -> str:
    """Persist a completed DCF payload using the canonical tool pointer shape."""
    summary = summarize_dcf_payload(payload)
    pointer = json.loads(
        persist_tool_result(
            "run_dcf_workflow",
            args,
            json.dumps(payload, ensure_ascii=False),
            summary,
        )
    )
    pointer["dcf_report_verbatim"] = True
    return json.dumps(pointer, ensure_ascii=False)


def resume_dcf_workflow_after_hitl(
    *,
    resume_payload: dict[str, Any],
    thread_id: str | None = None,
    args: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Resume the interrupted DCF graph with LangGraph Command(resume=...).

    This is the native post-HITL path. It preserves the interrupted graph state
    instead of starting a second compiled graph and trying to reconstruct state
    from frontend overrides.
    """
    if thread_id:
        set_thread_id(thread_id)
    config = {
        "configurable": {"thread_id": thread_id or get_run_dir().name},
        "recursion_limit": 50,
    }
    result = dcf_workflow_app.invoke(Command(resume=resume_payload), config=config)
    result_path = result.get("result_path") if isinstance(result, dict) else None
    if not result_path:
        raise RuntimeError("DCF resume finished without a result path.")

    out_path = Path(str(result_path))
    if not out_path.exists():
        raise FileNotFoundError(f"DCF workflow result not found: {result_path}")

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["result_path"] = str(out_path)
    pointer = _persist_dcf_payload(payload, args or {"resume_payload": resume_payload})
    report = summarize_dcf_payload(payload)
    return payload, pointer, report


@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression such as '2 + 3 * 4'."""
    try:
        value = str(simple_eval(expression))
        return persist_tool_result(
            "calculator", {"expression": expression},
            value, f"Calculated '{expression}' = {value}",
        )
    except Exception as exc:  # noqa: BLE001
        err = f"Error: {exc}"
        return persist_tool_result(
            "calculator", {"expression": expression},
            err, f"Calculator failed for '{expression}'",
        )


@tool
def search_web(query: str) -> str:
    """Search the web with Exa.

    Returns a tool_result_id pointer + one-line summary.  You MUST call
    retrieve_tool_result(tool_result_id) to read the full content.
    """
    raw, summary = search_exa(
        query,
        num_results=6,
        search_type="auto",
        max_characters=4_000,
    )
    return persist_tool_result("search_web", {"query": query}, raw, summary)


async def _search_web_async(query: str) -> str:
    write_tool_progress(
        "Sending Exa search request",
        partial={"provider": "exa", "query": query},
    )
    raw, summary = await search_exa_async(
        query,
        num_results=6,
        search_type="auto",
        max_characters=4_000,
    )
    try:
        payload = json.loads(raw)
        result_count = len(payload.get("results") or []) if isinstance(payload, dict) else 0
    except json.JSONDecodeError:
        result_count = 0
    write_tool_progress(
        summary,
        partial={"provider": "exa", "query": query, "result_count": result_count},
    )
    return await asyncio.to_thread(
        persist_tool_result,
        "search_web",
        {"query": query},
        raw,
        summary,
    )


search_web.coroutine = _search_web_async


@tool
def retrieve_tool_result(tool_result_id: str) -> str:
    """Read the full content of a previously stored tool result by its ID."""
    tool_dir = get_run_dir() / "tool_results"
    file_path = tool_dir / f"{tool_result_id}.json"
    if not file_path.exists():
        return json.dumps({
            "error": f"No result found for id '{tool_result_id}'",
            "tool_result_id": tool_result_id,
        })
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.dumps({
            "error": "Corrupt file",
            "tool_result_id": tool_result_id,
        })
    return json.dumps(payload, ensure_ascii=False)


@tool
def retrieve_context(step_id: str) -> str:
    """Retrieve a prior research step summary and its result references."""
    plans_dir = get_run_dir() / "plans"
    if not plans_dir.exists():
        return json.dumps({"step_id": step_id, "matches": []})
    plan_files = sorted(plans_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for plan_file in plan_files:
        try:
            payload = json.loads(plan_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for step in payload.get("steps", []):
            if step.get("id") == step_id:
                return json.dumps({
                    "step_id": step_id,
                    "matches": [{
                        "step_id": step.get("id"),
                        "description": step.get("description"),
                        "status": step.get("status"),
                        "result": step.get("result"),
                        "tool_result_ids": step.get("tool_result_ids", []),
                    }],
                }, ensure_ascii=False)
    return json.dumps({"step_id": step_id, "matches": []}, ensure_ascii=False)


@tool
def get_company_financials(
    ticker: str,
    period: Literal["annual", "quarter", "quarterly"] = "annual",
    limit: int = 5,
) -> str:
    """Fetch reusable multi-period company financial statements.

    Returns compact manifest plus tool_result_id. Full statement history stays
    in result store and can be consumed directly by downstream tools.
    """
    payload = fetch_company_financial_history(ticker, period=period, limit=limit)
    coverage = sorted(str(field) for field in (payload.get("coverage") or []))
    payload = {**payload, "coverage": coverage}
    periods = [
        str(row.get("date") or row.get("fiscal_year") or "")
        for row in payload.get("series") or []
        if isinstance(row, dict)
    ]
    return persist_tool_result(
        "get_company_financials",
        {"ticker": ticker.upper(), "period": period, "limit": limit},
        json.dumps(payload, ensure_ascii=False),
        f"Loaded {len(payload.get('series') or [])} {period} financial periods for {ticker.upper()}",
        manifest={
            "result_schema": "company_financial_history.v1",
            "ticker": ticker.upper(),
            "period": period,
            "coverage": coverage,
            "periods": periods,
            "citation_refs": payload.get("source_refs") or [],
            "next_actions": [
                "Pass tool_result_id directly to render_financial_chart when a chart is requested."
            ],
            "hint": (
                "Pass this tool_result_id directly to downstream tools. "
                "Do not retrieve full payload before render_financial_chart."
            ),
        },
    )


async def _get_company_financials_async(
    ticker: str,
    period: Literal["annual", "quarter", "quarterly"] = "annual",
    limit: int = 5,
) -> str:
    write_tool_progress(
        "Fetching structured financial statements",
        partial={"ticker": ticker.upper(), "period": period},
    )
    result = await asyncio.to_thread(
        get_company_financials.func,
        ticker,
        period,
        limit,
    )
    write_tool_progress(
        "Financial statements ready",
        partial={"ticker": ticker.upper(), "period": period},
    )
    return result


get_company_financials.coroutine = _get_company_financials_async


@tool
def render_financial_chart(
    input_result_id: str,
    metrics: list[str] | None = None,
    chart_type: str = "bar",
    title: str | None = None,
) -> str:
    """Render chart from stored financial result without loading raw data into model context."""
    from reference_broker import resolve_ref  # noqa: PLC0415
    from storage import upsert_workspace_object  # noqa: PLC0415

    if chart_type not in {"bar", "line"}:
        raise ValueError("chart_type must be 'bar' or 'line'")
    resolved = resolve_ref(input_result_id)
    if resolved.get("kind") != "tool_result":
        raise ValueError("input_result_id must reference a stored tool result")
    stored = resolved["payload"]
    raw_payload = stored.get("result") if isinstance(stored, dict) else None
    if isinstance(raw_payload, str):
        try:
            financials = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("financial result payload is not valid JSON") from exc
    elif isinstance(raw_payload, dict):
        financials = raw_payload
    else:
        raise ValueError("financial result payload is missing")
    series = [row for row in (financials.get("series") or []) if isinstance(row, dict)]
    if not series:
        raise ValueError("financial result contains no series")

    requested = [str(metric) for metric in (metrics or ["revenue", "net_income"])]
    available = [
        metric
        for metric in requested
        if any(isinstance(row.get(metric), (int, float)) for row in series)
    ]
    if not available:
        raise ValueError(f"none of requested metrics are available: {requested}")

    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    def period_label(row: dict, index: int) -> str:
        date = str(row.get("date") or "")
        if financials.get("period") == "quarter" and date:
            try:
                return datetime.strptime(date, "%Y-%m-%d").strftime("%b %Y")
            except ValueError:
                pass
        return str(row.get("fiscal_year") or date or index + 1)

    labels = [period_label(row, index) for index, row in enumerate(series)]
    ticker = str(financials.get("ticker") or "Company").upper()
    figure, axis = plt.subplots(figsize=(9, 5.2))
    colors = ["#2f80ed", "#16a085", "#f2994a", "#8e5bd9"]
    positions = np.arange(len(labels))
    secondary_metrics = [
        metric for metric in available
        if any(token in metric for token in ("eps", "per_share", "margin", "rate", "yield"))
    ]
    primary_metrics = [metric for metric in available if metric not in secondary_metrics]
    metric_axes = {
        metric: "per_share" if "eps" in metric or "per_share" in metric else "ratio"
        for metric in secondary_metrics
    }
    metric_axes.update({metric: "currency_billions" for metric in primary_metrics})
    if chart_type == "line":
        for index, metric in enumerate(primary_metrics):
            values = [float(row.get(metric) or 0.0) / 1_000_000_000 for row in series]
            axis.plot(positions, values, marker="o", linewidth=2.2, label=metric.replace("_", " ").title(), color=colors[index % len(colors)])
    else:
        width = 0.72 / max(len(primary_metrics), 1)
        for index, metric in enumerate(primary_metrics):
            values = [float(row.get(metric) or 0.0) / 1_000_000_000 for row in series]
            offset = (index - (len(primary_metrics) - 1) / 2) * width
            axis.bar(positions + offset, values, width=width, label=metric.replace("_", " ").title(), color=colors[index % len(colors)])
    secondary_axis = axis.twinx() if secondary_metrics and primary_metrics else axis
    for index, metric in enumerate(secondary_metrics, start=len(primary_metrics)):
        values = [float(row.get(metric) or 0.0) for row in series]
        secondary_axis.plot(
            positions,
            values,
            marker="o",
            linewidth=2.2,
            linestyle="--",
            label=metric.replace("_", " ").title(),
            color=colors[index % len(colors)],
        )
    axis.set_xticks(positions, labels)
    axis.set_title(title or f"{ticker} financial performance")
    if primary_metrics:
        axis.set_ylabel(f"{str(financials.get('currency') or 'USD')} billions")
    if secondary_metrics and secondary_axis is not axis:
        secondary_axis.set_ylabel("Per-share / ratio")
    axis.grid(axis="y", alpha=0.2)
    handles, legend_labels = axis.get_legend_handles_labels()
    if secondary_axis is not axis:
        secondary_handles, secondary_labels = secondary_axis.get_legend_handles_labels()
        handles.extend(secondary_handles)
        legend_labels.extend(secondary_labels)
    axis.legend(handles, legend_labels, frameon=False)
    figure.tight_layout()

    artifacts_dir = get_artifacts_dir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{ticker.lower()}-financial-performance-{uuid4().hex[:8]}.png"
    artifact_path = artifacts_dir / filename
    figure.savefig(artifact_path, dpi=160, bbox_inches="tight")
    plt.close(figure)

    thread_id = get_run_dir().name
    object_id = f"chart:{thread_id}:{uuid4().hex[:12]}"
    source_refs = financials.get("source_refs") or []
    chart_object = upsert_workspace_object({
        "object_id": object_id,
        "object_type": "chart",
        "schema_ref": "chart_artifact.v1",
        "schema_version": "1.0",
        "title": title or f"{ticker} financial performance",
        "status": "complete",
        "session_id": _session_ctx.get() or None,
        "thread_id": thread_id,
        "created_by": "tool:render_financial_chart",
        "updated_by": "tool:render_financial_chart",
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
        "source_refs": source_refs,
        "artifact_paths": [str(artifact_path)],
        "summary": f"{chart_type.title()} chart of {', '.join(available)} across {len(series)} periods.",
        "search_text": f"{ticker} financial chart {' '.join(available)} {' '.join(labels)}",
        "tags": ["chart", "financials", ticker.lower()],
        "payload": {
            "source_result_ids": [input_result_id],
            "metrics": available,
            "chart_type": chart_type,
            "periods": labels,
            "metric_axes": metric_axes,
        },
    })
    result_payload = {
        "object_id": object_id,
        "object_version_id": chart_object.get("version_id"),
        "artifact_path": str(artifact_path),
        "source_result_ids": [input_result_id],
        "metrics": available,
    }
    return persist_tool_result(
        "render_financial_chart",
        {
            "input_result_id": input_result_id,
            "metrics": available,
            "chart_type": chart_type,
            "title": title,
        },
        json.dumps(result_payload, ensure_ascii=False),
        f"Rendered {ticker} financial chart with {len(available)} metric(s)",
        manifest={
            "artifact_paths": [str(artifact_path)],
            "object_version_ids": [str(chart_object.get("version_id"))],
            "object_id": object_id,
            "source_result_ids": [input_result_id],
            "citation_refs": source_refs,
        },
    )


@tool
def get_stock_price_history(
    ticker: str,
    period: Literal["1y", "2y", "5y", "10y", "max"] = "5y",
) -> str:
    """Fetch actual adjusted daily stock prices for a listed ticker.

    Returns a compact result reference. For a price chart, pass its
    tool_result_id directly to render_stock_price_chart. Never fabricate,
    simulate, or infer market prices in Python.
    """
    import yfinance as yf  # noqa: PLC0415

    normalized_ticker = ticker.upper().strip()
    if not normalized_ticker or not normalized_ticker.replace(".", "").replace("-", "").isalnum():
        raise ValueError("ticker must be a market symbol such as AAPL")
    # yfinance uses shared module-level request/cache state. Concurrent
    # downloads can return another ticker's frame, so provider calls serialize.
    with _YFINANCE_DOWNLOAD_LOCK:
        frame = yf.download(
            normalized_ticker,
            period=period,
            auto_adjust=True,
            multi_level_index=False,
            progress=False,
        )
    if frame is None or frame.empty or "Close" not in frame.columns:
        raise ValueError(f"No market-price history returned for {normalized_ticker}")

    series = []
    for index, row in frame.iterrows():
        try:
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0:
            continue
        date = index.strftime("%Y-%m-%d") if hasattr(index, "strftime") else str(index)[:10]
        series.append({"date": date, "close": round(close, 6)})
    if not series:
        raise ValueError(f"No valid adjusted close prices returned for {normalized_ticker}")

    payload = {
        "schema_version": "market_price_history.v1",
        "ticker": normalized_ticker,
        "period": period,
        "price_basis": "adjusted_close",
        "currency": "USD",
        "source": "Yahoo Finance via yfinance",
        "series": series,
    }
    return persist_tool_result(
        "get_stock_price_history",
        {"ticker": normalized_ticker, "period": period},
        json.dumps(payload, ensure_ascii=False),
        f"Loaded {len(series)} adjusted daily prices for {normalized_ticker} from {series[0]['date']} to {series[-1]['date']}",
        manifest={
            "result_schema": "market_price_history.v1",
            "ticker": normalized_ticker,
            "period": period,
            "source": payload["source"],
            "start_date": series[0]["date"],
            "end_date": series[-1]["date"],
            "next_actions": [
                "Pass one tool_result_id to render_stock_price_chart for a single-series chart.",
                "Pass two or more same-period tool_result_ids to render_normalized_stock_comparison for a normalized comparison.",
            ],
            "hint": "Pass tool_result_id directly to a stock chart renderer. Do not retrieve full payload or synthesize price history.",
        },
    )


async def _get_stock_price_history_async(
    ticker: str,
    period: Literal["1y", "2y", "5y", "10y", "max"] = "5y",
) -> str:
    write_tool_progress(
        "Fetching market-price history",
        partial={"ticker": ticker.upper(), "period": period},
    )
    result = await asyncio.to_thread(get_stock_price_history.func, ticker, period)
    write_tool_progress(
        "Market-price history ready",
        partial={"ticker": ticker.upper(), "period": period},
    )
    return result


get_stock_price_history.coroutine = _get_stock_price_history_async


@tool
def render_stock_price_chart(input_result_id: str, title: str | None = None) -> str:
    """Render a versioned stock-price chart from get_stock_price_history result ID."""
    from datetime import datetime  # noqa: PLC0415
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from reference_broker import resolve_ref  # noqa: PLC0415
    from storage import upsert_workspace_object  # noqa: PLC0415

    resolved = resolve_ref(input_result_id)
    if resolved.get("kind") != "tool_result":
        raise ValueError("input_result_id must reference a stored tool result")
    stored = resolved.get("payload") if isinstance(resolved, dict) else None
    raw_payload = stored.get("result") if isinstance(stored, dict) else None
    try:
        price_history = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except json.JSONDecodeError as exc:
        raise ValueError("market-price result payload is not valid JSON") from exc
    if not isinstance(price_history, dict) or price_history.get("schema_version") != "market_price_history.v1":
        raise ValueError("input_result_id is not a market_price_history result")

    rows = [row for row in (price_history.get("series") or []) if isinstance(row, dict)]
    if len(rows) < 2:
        raise ValueError("market-price history needs at least two observations")
    dates = [str(row.get("date") or "") for row in rows]
    closes = [float(row["close"]) for row in rows]
    parsed_dates = [datetime.strptime(date, "%Y-%m-%d") for date in dates]
    ticker = str(price_history.get("ticker") or "Company").upper()

    figure, axis = plt.subplots(figsize=(9, 5.2))
    axis.plot(parsed_dates, closes, color="#2f80ed", linewidth=2.1, label="Adjusted close")
    axis.fill_between(parsed_dates, closes, min(closes), color="#2f80ed", alpha=0.08)
    axis.set_title(title or f"{ticker} stock price ({price_history.get('period', 'history')})")
    axis.set_ylabel("USD per share")
    axis.grid(axis="y", alpha=0.2)
    axis.xaxis.set_major_locator(mdates.YearLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axis.legend(frameon=False)
    figure.tight_layout()

    artifacts_dir = get_artifacts_dir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / f"{ticker.lower()}-stock-price-{uuid4().hex[:8]}.png"
    figure.savefig(artifact_path, dpi=160, bbox_inches="tight")
    plt.close(figure)

    total_return = ((closes[-1] / closes[0]) - 1) * 100
    thread_id = get_run_dir().name
    object_id = f"chart:{thread_id}:{uuid4().hex[:12]}"
    chart_object = upsert_workspace_object({
        "object_id": object_id,
        "object_type": "chart",
        "schema_ref": "chart_artifact.v1",
        "schema_version": "1.0",
        "title": title or f"{ticker} stock price ({price_history.get('period', 'history')})",
        "status": "complete",
        "session_id": _session_ctx.get() or None,
        "thread_id": thread_id,
        "created_by": "tool:render_stock_price_chart",
        "updated_by": "tool:render_stock_price_chart",
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
        "source_refs": [{"source": price_history.get("source"), "start_date": dates[0], "end_date": dates[-1]}],
        "artifact_paths": [str(artifact_path)],
        "summary": f"Adjusted close from {dates[0]} to {dates[-1]}; total return {total_return:.1f}%.",
        "search_text": f"{ticker} stock price adjusted close {price_history.get('period')} {dates[0]} {dates[-1]}",
        "tags": ["chart", "market-price", ticker.lower()],
        "payload": {
            "source_result_ids": [input_result_id],
            "metric": "adjusted_close",
            "period": price_history.get("period"),
            "start_date": dates[0],
            "end_date": dates[-1],
            "total_return_pct": round(total_return, 4),
        },
    })
    result_payload = {
        "object_id": object_id,
        "object_version_id": chart_object.get("version_id"),
        "artifact_path": str(artifact_path),
        "source_result_ids": [input_result_id],
        "total_return_pct": round(total_return, 4),
    }
    return persist_tool_result(
        "render_stock_price_chart",
        {"input_result_id": input_result_id, "title": title},
        json.dumps(result_payload, ensure_ascii=False),
        f"Rendered {ticker} stock-price chart; adjusted-close return {total_return:.1f}%",
        manifest={
            "artifact_paths": [str(artifact_path)],
            "object_version_ids": [str(chart_object.get("version_id"))],
            "object_id": object_id,
            "source_result_ids": [input_result_id],
        },
    )


def _load_market_price_history(input_result_id: str) -> dict[str, Any]:
    """Load validated stored market-price result without exposing full series to model."""
    from reference_broker import resolve_ref  # noqa: PLC0415

    resolved = resolve_ref(input_result_id)
    if resolved.get("kind") != "tool_result":
        raise ValueError("input_result_id must reference a stored tool result")
    stored = resolved.get("payload") if isinstance(resolved, dict) else None
    raw_payload = stored.get("result") if isinstance(stored, dict) else None
    try:
        price_history = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except json.JSONDecodeError as exc:
        raise ValueError("market-price result payload is not valid JSON") from exc
    if not isinstance(price_history, dict) or price_history.get("schema_version") != "market_price_history.v1":
        raise ValueError("input_result_id is not a market_price_history result")
    return price_history


@tool
def render_normalized_stock_comparison(
    input_result_ids: list[str],
    title: str | None = None,
) -> str:
    """Render normalized price comparison from two or more stored price-history result IDs.

    Fetch each ticker with get_stock_price_history first. Pass result IDs directly.
    Each series starts at 100 on first common trading date. Never use execute_python
    or retrieve_tool_result for this workflow.
    """
    from datetime import datetime  # noqa: PLC0415
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from storage import upsert_workspace_object  # noqa: PLC0415

    unique_ids = list(dict.fromkeys(str(value).strip() for value in input_result_ids if str(value).strip()))
    if len(unique_ids) < 2:
        raise ValueError("normalized comparison needs at least two price-history result IDs")

    histories = [_load_market_price_history(result_id) for result_id in unique_ids]
    tickers = [str(history.get("ticker") or "Unknown").upper() for history in histories]
    if len(set(tickers)) != len(tickers):
        raise ValueError("normalized comparison needs distinct tickers")

    by_date = [
        {
            str(row.get("date")): float(row["close"])
            for row in (history.get("series") or [])
            if isinstance(row, dict) and row.get("date") and float(row.get("close") or 0) > 0
        }
        for history in histories
    ]
    common_dates = sorted(set.intersection(*(set(series) for series in by_date)))
    if len(common_dates) < 2:
        raise ValueError("price histories have insufficient overlapping trading dates")

    parsed_dates = [datetime.strptime(date, "%Y-%m-%d") for date in common_dates]
    normalized_series: dict[str, list[float]] = {}
    total_returns: dict[str, float] = {}
    for ticker, series in zip(tickers, by_date, strict=True):
        closes = [series[date] for date in common_dates]
        baseline = closes[0]
        normalized_series[ticker] = [round((close / baseline) * 100, 4) for close in closes]
        total_returns[ticker] = round(((closes[-1] / baseline) - 1) * 100, 4)

    figure, axis = plt.subplots(figsize=(10, 5.6))
    colors = ["#2f80ed", "#00a78e", "#805ad5", "#e76f51", "#d69e2e"]
    for index, ticker in enumerate(tickers):
        axis.plot(parsed_dates, normalized_series[ticker], linewidth=2.1, label=ticker, color=colors[index % len(colors)])
    axis.axhline(100, color="#6b7280", linewidth=0.9, linestyle="--", alpha=0.65)
    axis.set_title(title or f"Normalized stock-price performance: {' vs '.join(tickers)}")
    axis.set_ylabel("Indexed adjusted close (start = 100)")
    axis.grid(axis="y", alpha=0.2)
    axis.xaxis.set_major_locator(mdates.YearLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axis.legend(frameon=False, ncols=min(len(tickers), 4))
    figure.tight_layout()

    artifact_path = get_artifacts_dir() / f"{'-'.join(ticker.lower() for ticker in tickers)}-normalized-performance-{uuid4().hex[:8]}.png"
    figure.savefig(artifact_path, dpi=160, bbox_inches="tight")
    plt.close(figure)

    thread_id = get_run_dir().name
    object_id = f"chart:{thread_id}:{uuid4().hex[:12]}"
    chart_object = upsert_workspace_object({
        "object_id": object_id,
        "object_type": "chart",
        "schema_ref": "chart_artifact.v1",
        "schema_version": "1.0",
        "title": title or f"Normalized stock-price performance: {' vs '.join(tickers)}",
        "status": "complete",
        "session_id": _session_ctx.get() or None,
        "thread_id": thread_id,
        "created_by": "tool:render_normalized_stock_comparison",
        "updated_by": "tool:render_normalized_stock_comparison",
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker} for ticker in tickers],
        "source_refs": [{"source": history.get("source"), "ticker": ticker} for ticker, history in zip(tickers, histories, strict=True)],
        "artifact_paths": [str(artifact_path)],
        "summary": f"Normalized adjusted-close performance from {common_dates[0]} to {common_dates[-1]}.",
        "search_text": f"{' '.join(tickers)} normalized stock-price performance adjusted close",
        "tags": ["chart", "market-price", "normalized", *(ticker.lower() for ticker in tickers)],
        "payload": {
            "source_result_ids": unique_ids,
            "metric": "adjusted_close_normalized",
            "base_index": 100,
            "start_date": common_dates[0],
            "end_date": common_dates[-1],
            "total_return_pct": total_returns,
        },
    })
    result_payload = {
        "object_id": object_id,
        "object_version_id": chart_object.get("version_id"),
        "artifact_path": str(artifact_path),
        "source_result_ids": unique_ids,
        "base_index": 100,
        "total_return_pct": total_returns,
    }
    return persist_tool_result(
        "render_normalized_stock_comparison",
        {"input_result_ids": unique_ids, "title": title},
        json.dumps(result_payload, ensure_ascii=False),
        f"Rendered normalized {', '.join(tickers)} stock comparison from {common_dates[0]} to {common_dates[-1]}",
        manifest={
            "artifact_paths": [str(artifact_path)],
            "object_version_ids": [str(chart_object.get("version_id"))],
            "object_id": object_id,
            "source_result_ids": unique_ids,
        },
    )


@tool
def execute_python(code: str, output_paths: list[str] | None = None) -> str:
    """Run Python code locally for computation, data fetching, and matplotlib visualizations.

    The code runs with the current Python interpreter.  The artifacts directory
    is available as the ARTIFACTS_DIR environment variable — save output files
    there so they are automatically picked up.

    A helper is pre-imported: get_stock_data(ticker, period='5y') returns a
    clean DataFrame with columns [Date, Open, High, Low, Close, Volume].
    Use it for all stock price fetching.

    Include paths (relative to ARTIFACTS_DIR) you saved in output_paths to
    confirm them.
    """
    output_paths = output_paths or []
    artifacts_dir = get_artifacts_dir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    script_path: str | None = None
    stdout = stderr = ""
    exit_code = -1
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(_PYTHON_PRELUDE + "\n" + code)
            script_path = f.name

        mpl_cache = artifacts_dir / ".mplcache"
        mpl_cache.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "ARTIFACTS_DIR": str(artifacts_dir),
            "MPLCONFIGDIR": str(mpl_cache),
        }
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True,
            timeout=PYTHON_EXEC_TIMEOUT, env=env,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        stderr = f"Execution timed out after {PYTHON_EXEC_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001
        stderr = str(exc)
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:  # noqa: BLE001
                pass

    confirmed_artifacts: list[dict] = []
    for path_str in output_paths:
        p = Path(path_str)
        if not p.is_absolute():
            p = artifacts_dir / p.name
        confirmed_artifacts.append({"path": str(p), "exists": p.exists()})

    result_payload = {
        "exit_code": exit_code,
        "stdout": stdout[:4000],
        "stderr": stderr[:2000] if stderr else "",
        "local_artifacts_dir": str(artifacts_dir),
        "confirmed_artifacts": confirmed_artifacts,
    }
    ok = exit_code == 0
    if stderr and not ok:
        summary = (
            f"Python execution failed (exit {exit_code}). "
            f"stderr: {stderr[:300]}"
        )
    else:
        summary = (
            f"Python execution {'succeeded' if ok else 'finished with warnings'} "
            f"(exit {exit_code}). stdout: {stdout[:300]}"
        )

    return persist_tool_result(
        "execute_python",
        {"code": code, "output_paths": output_paths},
        json.dumps(result_payload, ensure_ascii=False),
        summary,
    )


@tool
def run_dcf_workflow(
    ticker: str,
    horizon_years: int = 5,
    assumption_review_mode: bool = True,
    allow_external_assumptions: bool = True,
    assumption_overrides: dict[str, float] | None = None,
    parent_step_id: str = "workflow_dcf",
    parent_run_id: str | None = None,
    workflow_context: dict[str, Any] | None = None,
) -> str:
    """Run a deterministic DCF valuation workflow for a ticker.

    Default (assumption_review_mode=True): gathers evidence, proposes
    assumptions, returns them for review.  After user responds, call again
    with assumption_overrides and assumption_review_mode=False to complete.

    ``parent_run_id`` links this run to a source run (clone/rerun) so the KG
    records lineage and the run accumulates instead of overwriting.

    The tool result includes a detailed report with assumption provenance,
    WACC decomposition, confidence label, and quality flags.
    """
    context_assumptions = {}
    if isinstance(workflow_context, dict) and isinstance(workflow_context.get("user_assumptions"), dict):
        context_assumptions = workflow_context["user_assumptions"]
    merged_overrides = {**context_assumptions, **(assumption_overrides or {})}

    payload = run_dcf_workflow_sync(
        ticker=ticker,
        horizon_years=horizon_years,
        assumption_review_mode=assumption_review_mode,
        allow_external_assumptions=allow_external_assumptions,
        assumption_overrides=merged_overrides or None,
        parent_step_id=parent_step_id,
        session_id=_session_ctx.get(),
        parent_run_id=parent_run_id,
        run_trigger="rerun" if parent_run_id else "initial",
        workflow_context=workflow_context or {},
    )

    if payload.get("__dcf_hitl__"):
        snapshot = build_hitl_snapshot(payload)
        memo_proposals = payload.get("memo_proposals", {})

        # Emit DCF review event for frontend (works for both chat and research modes)
        set_dcf_hitl_payload(snapshot)
        emit_ui_event({
            "type": "dcf_assumptions_review",
            **snapshot,
            "memo_proposals": memo_proposals,
        })

        ticker_label = str(payload.get("ticker") or "?")
        horizon_label = int(payload.get("horizon_years") or 5)
        return json.dumps({
            "__dcf_hitl__": True,
            "workflow": "dcf",
            "type": "dcf_assumptions_review",
            "status": "waiting",
            "ticker": ticker_label,
            "horizon_years": horizon_label,
            "summary": f"Assumptions ready for review · {ticker_label} · {horizon_label}yr",
            "next_actions": ["Await user approval, edits, or rejection."],
        })

    return _persist_dcf_payload(
        payload,
        {
            "ticker": ticker,
            "horizon_years": horizon_years,
            "allow_external_assumptions": allow_external_assumptions,
            "assumption_overrides": merged_overrides or {},
            "workflow_context": workflow_context or {},
        },
    )


@tool
def run_deck_workflow(
    brief: dict | str | Any,
    sources: Any = None,
    session_id: str = "",
) -> str:
    """Generate a slide deck (PPTX) from typed input sources.

    **Prefer passing only ``brief``** after a completed DCF in this thread —
    ``sources`` are auto-loaded from ``dcf_output.json`` on disk (plus sensitivity
    chart when present).  Do NOT pass placeholder strings or partial DCF summaries
    in ``sources``.

    ``brief`` — deck intent (required):
      - ``title`` (required): deck title used for filename
      - ``audience``: ``board`` | ``ic`` | ``internal`` | ``client`` | ``generic``
      - ``hitl_mode``: ``disabled`` | ``partial`` (default) | ``full``
      - ``slide_count_target``: optional int 1–40
      - ``must_cover``: optional list of topic strings

    ``sources`` — optional list of typed source objects (usually omit after DCF):
      - ``{"type": "dcf_output", "payload_path": "..."}`` — completed DCF run
      - ``{"type": "manual_text", "title": "...", "body": "..."}`` — analyst notes
      - ``{"type": "document", "doc_ids": [...], "query_hints": [...]}`` — uploaded docs
      - ``{"type": "chart_artifact", "path": "...", "caption": "..."}`` — chart image

    When ``hitl_mode`` is ``"partial"`` (default), pauses after outline generation for
    sidebar review. The server resumes slide generation after the user approves — do
    NOT call this tool a second time for approval.
    """
    from graphs.workflows.deck.inputs import resolve_deck_workflow_inputs  # noqa: PLC0415
    from graphs.workflows.deck import run_deck_workflow_sync  # noqa: PLC0415

    try:
        sources, brief = resolve_deck_workflow_inputs(sources, brief)
    except ValueError as exc:
        return json.dumps({"error": str(exc), "tool_name": "run_deck_workflow"})

    effective_session = session_id or _session_ctx.get() or ""
    result = run_deck_workflow_sync(
        sources=sources,
        brief=brief,
        session_id=effective_session,
        parent_step_id="workflow_deck",
    )

    if result.get("__deck_hitl__"):
        outline = result.get("outline", {})
        slides = outline.get("slides", [])
        blocks_preview = result.get("blocks_preview", [])
        hitl_mode = result.get("hitl_mode")

        # Persist for cross-thread retrieval (mirrors set_dcf_hitl_payload pattern).
        review_snapshot = {
            "workflow": "deck",
            "hitl_mode": hitl_mode,
            "deck_title": brief.get("title"),
            "outline": outline,
            "blocks_preview": blocks_preview,
            "slide_count": len(slides),
            "deck_context_version_id": result.get("deck_context_version_id"),
        }
        set_deck_hitl_payload(review_snapshot)

        # Frontend event so the execution sidebar can render the outline review UI.
        emit_ui_event({
            "type": "deck_outline_review",
            **review_snapshot,
        })

        return json.dumps({
            "__deck_hitl__": True,
            "workflow": "deck",
            "type": "deck_outline_review",
            "status": "waiting",
            "deck_title": brief.get("title"),
            "slide_count": len(slides),
            "summary": f"Deck outline ready for review · {len(slides)} slides",
            "next_actions": ["Await user approval, edits, or rejection."],
        })

    if result.get("__deck_rejected__"):
        feedback = result.get("feedback") or "(no feedback provided)"
        return (
            "Deck outline was rejected. No slides were generated.\n\n"
            f"User feedback: {feedback}\n\n"
            "Acknowledge the rejection to the user and ask whether they want to "
            "revise the brief or sources and try again."
        )

    pointer = json.loads(
        persist_tool_result(
            "run_deck_workflow",
            {"sources_count": len(sources), "brief_title": brief.get("title")},
            json.dumps(result, ensure_ascii=False),
            f"Deck '{brief.get('title', '?')}': {len(result.get('slides', []))} slides → {result.get('pptx_path', 'n/a')}",
        )
    )
    return json.dumps(pointer, ensure_ascii=False)


@tool
def run_memo_workflow(
    brief: dict | str,
    sources: list[dict[str, Any]] | None = None,
    workflow_context: dict[str, Any] | None = None,
    session_id: str = "",
) -> str:
    """Generate durable investment memo from selected workspace objects/documents.

    ``brief`` defines title, company, audience, required sections, focus areas,
    and ``hitl_mode`` (``review`` or ``disabled``). ``sources`` accepts typed
    workspace_object, document, or manual_text records. Approved main-graph
    workflow context can supply selected sources automatically.
    """
    from graphs.workflows.memo import resolve_memo_inputs, run_memo_workflow_sync  # noqa: PLC0415

    try:
        resolved_sources, resolved_brief = resolve_memo_inputs(
            sources, brief, workflow_context=workflow_context,
        )
        result = run_memo_workflow_sync(
            sources=resolved_sources,
            brief=resolved_brief,
            session_id=session_id or _session_ctx.get() or "",
        )
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc), "tool_name": "run_memo_workflow"})

    if result.get("__memo_hitl__"):
        review_payload = {
            **result,
            "status": "waiting",
            "summary": "Memo draft ready for review",
            "next_actions": ["approve", "edit", "reject"],
        }
        emit_ui_event({"type": "memo_draft_review", **review_payload})
        return json.dumps(review_payload, ensure_ascii=False)
    if result.get("__memo_rejected__"):
        return json.dumps({**result, "summary": "Memo draft rejected"}, ensure_ascii=False)
    return persist_tool_result(
        "run_memo_workflow",
        {"brief": resolved_brief, "source_count": len(resolved_sources)},
        json.dumps(result, ensure_ascii=False),
        f"Memo '{result.get('title', '?')}' completed",
    )


@tool
def fetch_sec_filing(ticker: str, filing_type: str = "10-K") -> str:
    """Fetch recent SEC EDGAR filings (10-K or 10-Q) for a company.

    Returns extracted text from Risk Factors, MD&A, Business overview, and
    quantitative disclosures sections.  Use for any question about a company's
    financials, risks, business model, or regulatory disclosures.
    Prefer this over search_web for fundamental company research.
    """
    from graphs.workflows.dcf.sec_filings import fetch_sec_filings as _fetch  # noqa: PLC0415

    items = _fetch(ticker.upper().strip(), max_filings=2)
    if not items:
        no_result = {"ticker": ticker, "error": f"No SEC filings found for {ticker}"}
        return persist_tool_result(
            "fetch_sec_filing", {"ticker": ticker, "filing_type": filing_type},
            json.dumps(no_result), f"No SEC filings found for {ticker}",
        )
    # Limit per-section text to keep context manageable
    sections = []
    for item in items[:10]:
        meta = item.get("metadata", {})
        sections.append({
            "filing_type": meta.get("filing_type", "?"),
            "section": meta.get("section", "?"),
            "as_of": item.get("as_of", "?"),
            "text": (item.get("text") or "")[:2000],
        })
    filing_types = list({s["filing_type"] for s in sections})
    summary = (
        f"SEC filings for {ticker}: {len(sections)} section(s) "
        f"from {filing_types}"
    )
    return persist_tool_result(
        "fetch_sec_filing", {"ticker": ticker, "filing_type": filing_type},
        json.dumps({"ticker": ticker, "sections": sections}, ensure_ascii=False),
        summary,
    )


async def _fetch_sec_filing_async(ticker: str, filing_type: str = "10-K") -> str:
    write_tool_progress(
        "Fetching SEC filing index and sections",
        partial={"ticker": ticker.upper().strip(), "filing_type": filing_type},
    )
    result = await asyncio.to_thread(fetch_sec_filing.func, ticker, filing_type)
    try:
        pointer = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        pointer = {}
    write_tool_progress(
        str(pointer.get("summary") or f"SEC retrieval finished for {ticker}"),
        partial={
            "ticker": ticker.upper().strip(),
            "filing_type": filing_type,
            "tool_result_id": pointer.get("tool_result_id"),
        },
    )
    return result


fetch_sec_filing.coroutine = _fetch_sec_filing_async


@tool
def query_knowledge_graph(question: str, ticker: str = "") -> str:
    """Query the agent's OWN Knowledge Graph with multi-hop reasoning.

    The KG is your structured memory — it holds every prior DCF run (assumptions,
    outputs, scenarios), investment theses, company synthesis, drivers,
    fundamentals (revenue/margins/wacc), SEC filings, and uploaded-document
    facts. Consult this FIRST for anything about a ticker you've already
    analyzed: it's cheaper, faster, and more grounded than a web search.

    Use it for chained/analytical questions — "which assumptions drove AAPL's
    implied price and do they match the thesis?", "compare tax rate across runs",
    "how did META's growth assumption change?". The engine walks the graph
    hop-by-hop (run → assumptions → thesis → drivers) and synthesizes an answer.

    Args:
        question: The analyst question, natural language.
        ticker: Optional ticker hint (e.g. "AAPL"). Leave empty to infer from
            the question.

    Returns a JSON string with the synthesized ``answer`` plus a
    ``tool_result_id`` pointing at the full traversal trail (hops + nodes +
    edges) for inspection. The answer is usable directly; you do not need to
    retrieve the trail unless you want to cite specific node ids.
    """
    from kg.deep_research import run_deep_research  # noqa: PLC0415

    try:
        result = run_deep_research(question, ticker=ticker or None)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc), "tool_name": "query_knowledge_graph"})

    answer = result.get("answer", "")
    matched = result.get("matched_nodes", []) or []
    trail = {
        "question": question,
        "ticker": ticker,
        "hops": result.get("hops", []),
        "traversal_path": result.get("traversal_path", []),
        "traversal_edges": result.get("traversal_edges", []),
        "nodes": [
            {
                "id": n.get("id"), "ticker": n.get("ticker"),
                "node_type": n.get("node_type"), "field": n.get("field"),
                "value": n.get("value"), "source": n.get("source"),
            }
            for n in matched[:40]
        ],
    }

    # Surface the traversal to the UI as an on-demand expander in the chat
    # activity stream (collapsed by default; the analyst can open the trail).
    emit_ui_event({
        "type": "kg_traversal",
        "question": question,
        "ticker": ticker,
        "hops": result.get("hops", []),
        "node_count": len(result.get("traversal_path", [])),
        "edge_count": len(result.get("traversal_edges", [])),
        "traversal_path": result.get("traversal_path", []),
        "traversal_edges": result.get("traversal_edges", []),
        "nodes": trail["nodes"],
    })

    pointer_json = persist_tool_result(
        "query_knowledge_graph",
        {"question": question, "ticker": ticker},
        json.dumps(trail, ensure_ascii=False),
        summary=answer[:200],
    )
    pid = json.loads(pointer_json).get("tool_result_id")
    # B2: pass the planner's staleness verdict up to the chat agent. When
    # needs_external is True the KG answer is the best available from cached
    # (possibly stale) data — the agent MUST supplement with search_web rather
    # than presenting it as current. external_reason names the gap.
    payload = {
        "answer": answer,
        "tool_result_id": pid,
        "hops": len(result.get("hops", [])),
        "nodes_traversed": len(result.get("traversal_path", [])),
        "needs_external": bool(result.get("needs_external", False)),
    }
    external_reason = str(result.get("external_reason", "") or "").strip()
    if external_reason:
        payload["external_reason"] = external_reason
    return json.dumps(payload, ensure_ascii=False)


async def _query_knowledge_graph_async(question: str, ticker: str = "") -> str:
    write_tool_progress(
        "Traversing knowledge graph",
        partial={"question": question, "ticker": ticker},
    )
    result = await asyncio.to_thread(query_knowledge_graph.func, question, ticker)
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    write_tool_progress(
        "Knowledge graph traversal completed",
        partial={
            "hops": payload.get("hops"),
            "nodes_traversed": payload.get("nodes_traversed"),
            "needs_external": payload.get("needs_external"),
        },
    )
    return result


query_knowledge_graph.coroutine = _query_knowledge_graph_async


# ---------------------------------------------------------------------------
# Tool collections
# ---------------------------------------------------------------------------

# Full implementation set. Surface exposure comes from capability registry.
ALL_TOOLS = [
    calculator,
    search_web,
    retrieve_tool_result,
    get_company_financials,
    render_financial_chart,
    get_stock_price_history,
    render_stock_price_chart,
    render_normalized_stock_comparison,
    execute_python,
    run_dcf_workflow,
    run_deck_workflow,
    run_memo_workflow,
    search_documents,
    fetch_sec_filing,
    query_knowledge_graph,
    retrieve_context,
]

# Chat subset — same tools minus research-only ones.  The chat subgraph
# wraps run_dcf_workflow with UI-side-effect helpers, so it builds its
# own list from the canonical definitions.
CHAT_CANONICAL = [
    calculator,
    search_web,
    retrieve_tool_result,
    get_company_financials,
    render_financial_chart,
    get_stock_price_history,
    render_stock_price_chart,
    render_normalized_stock_comparison,
    execute_python,
    run_dcf_workflow,
    run_deck_workflow,
    run_memo_workflow,
    search_documents,
    fetch_sec_filing,
    query_knowledge_graph,
]

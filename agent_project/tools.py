"""Canonical tool definitions shared by all subgraphs.

Every tool returns a pointer via persist_tool_result — the caller sees a
{``tool_result_id``, ``summary``} envelope in the ToolMessage.  Full
payloads live on disk and are retrieved on demand via retrieve_tool_result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool
from simpleeval import simple_eval

from documents import search_documents, _session_ctx
from graphs.workflows.dcf import run_dcf_workflow_sync, summarize_dcf_payload
from utils import get_artifacts_dir, get_run_dir, persist_tool_result, emit_ui_event, set_dcf_hitl_payload
from web_search import search_exa

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYTHON_EXEC_TIMEOUT = 60

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
) -> str:
    """Run a deterministic DCF valuation workflow for a ticker.

    Default (assumption_review_mode=True): gathers evidence, proposes
    assumptions, returns them for review.  After user responds, call again
    with assumption_overrides and assumption_review_mode=False to complete.

    The tool result includes a detailed report with assumption provenance,
    WACC decomposition, confidence label, and quality flags.
    """
    payload = run_dcf_workflow_sync(
        ticker=ticker,
        horizon_years=horizon_years,
        assumption_review_mode=assumption_review_mode,
        allow_external_assumptions=allow_external_assumptions,
        assumption_overrides=assumption_overrides,
        parent_step_id=parent_step_id,
        session_id=_session_ctx.get(),
    )

    if payload.get("__dcf_hitl__"):
        assumptions = payload.get("assumptions", {})
        provenance = payload.get("assumption_provenance", {})
        memo_proposals = payload.get("memo_proposals", {})
        evidence_items = payload.get("evidence_items", [])

        # Emit DCF review event for frontend (works for both chat and research modes)
        set_dcf_hitl_payload({
            "ticker": payload.get("ticker", "?"),
            "horizon_years": payload.get("horizon_years", 5),
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "memo_proposals": memo_proposals,
            "evidence_items": evidence_items,
        })
        emit_ui_event({
            "type": "dcf_assumptions_review",
            "ticker": payload.get("ticker", "?"),
            "horizon_years": payload.get("horizon_years", 5),
            "assumptions": assumptions,
            "assumption_provenance": provenance,
            "memo_proposals": memo_proposals,
            "evidence_items": evidence_items,
        })

        lines = [
            "⛔ STOP — DO NOT CALL MORE TOOLS. Present these assumptions for review.",
            "",
            f"## DCF Assumptions for {payload.get('ticker', '?')} ({payload.get('horizon_years', 5)}yr)",
            "",
            "| Field | Value | Source | Confidence |",
            "|-------|-------|--------|------------|",
        ]
        for field in [
            "revenue_growth", "fcff_margin", "terminal_growth", "tax_rate", "wacc",
        ]:
            val = assumptions.get(field)
            if val is None:
                continue
            prov = provenance.get(field, {})
            source = prov.get("source", "?")
            conf = prov.get("confidence", 0.5)
            lines.append(f"| {field} | {val:.2%} | {source} | {conf:.0%} |")
        lines.append("")
        lines.append("Ask the user to approve, edit values, or reject.")
        lines.append(
            "After they respond, call again with assumption_overrides "
            "and assumption_review_mode=False."
        )
        return "\n".join(lines)

    summary = summarize_dcf_payload(payload)
    return persist_tool_result(
        "run_dcf_workflow",
        {
            "ticker": ticker,
            "horizon_years": horizon_years,
            "allow_external_assumptions": allow_external_assumptions,
            "assumption_overrides": assumption_overrides or {},
        },
        json.dumps(payload, ensure_ascii=False),
        summary,
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


# ---------------------------------------------------------------------------
# Tool collections
# ---------------------------------------------------------------------------

# Full set (used by the research subgraph — includes retrieve_context which
# is research-only and defined in graphs/research.py).
ALL_TOOLS = [
    calculator,
    search_web,
    retrieve_tool_result,
    execute_python,
    run_dcf_workflow,
    search_documents,
    fetch_sec_filing,
]

# Chat subset — same tools minus research-only ones.  The chat subgraph
# wraps run_dcf_workflow with UI-side-effect helpers, so it builds its
# own list from the canonical definitions.
CHAT_CANONICAL = [
    calculator,
    search_web,
    retrieve_tool_result,
    execute_python,
    run_dcf_workflow,
    search_documents,
    fetch_sec_filing,
]

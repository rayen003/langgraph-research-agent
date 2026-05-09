"""Conversational subgraph — ReAct agent with tool access, no HITL."""

import json
import os

import dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from simpleeval import simple_eval

from documents import search_documents, _session_ctx
from graphs.workflows.dcf import run_dcf_workflow_sync, summarize_dcf_payload
from utils import console, emit_ui_event, get_run_dir, persist_tool_result, track_tool
from web_search import search_exa

dotenv.load_dotenv()

# Use the same model as research; chat is lighter (fewer rounds) so cost stays low
llm = ChatOpenAI(model="gpt-5-nano", api_key=os.getenv("OPENAI_API_KEY"), timeout=60)

MAX_CHAT_ROUNDS = 4

# ---------------------------------------------------------------------------
# Tools available in chat (no plan-step retrieval tools — those are research-only)
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
        return persist_tool_result(
            "calculator", {"expression": expression},
            f"Error: {exc}", f"Calculator failed for '{expression}'",
        )


@tool
def search_web(query: str) -> str:
    """Search the web with Exa for current information, news, prices, or factual queries."""
    raw, summary = search_exa(
        query,
        num_results=6,
        search_type="auto",
        max_characters=4_000,
    )
    return json.dumps(
        {
            "tool_name": "search_web",
            "summary": summary,
            "usage_hint": (
                "Synthesize an answer from these excerpts. Do not list sources as a directory. "
                "Explain what happened, why it matters, and cite source names inline."
            ),
            "result": json.loads(raw),
        },
        ensure_ascii=False,
    )


@tool
def retrieve_tool_result(tool_result_id: str) -> str:
    """Read the full content of a previously stored tool result by its tool_result_id."""
    tool_dir = get_run_dir() / "tool_results"
    file_path = tool_dir / f"{tool_result_id}.json"
    if not file_path.exists():
        return json.dumps({"error": f"No result found for id '{tool_result_id}'", "tool_result_id": tool_result_id})
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.dumps({"error": "Corrupt file", "tool_result_id": tool_result_id})
    return json.dumps(payload, ensure_ascii=False)


@tool
def execute_python(code: str) -> str:
    """Run Python for calculations, data fetching, or quick analysis.

    get_stock_data(ticker, period='1y') is pre-imported and returns a clean
    DataFrame [Date, Open, High, Low, Close, Volume].
    ARTIFACTS_DIR env var is set — save any plots there.
    """
    import subprocess, sys, tempfile, pathlib as _pl

    from utils import get_artifacts_dir as _gad

    _PRELUDE = '''
import os, warnings
warnings.filterwarnings("ignore")
artifacts_dir = os.environ.get("ARTIFACTS_DIR", ".")
import matplotlib; matplotlib.use("Agg")

def get_stock_data(ticker, period="1y"):
    import yfinance as yf, pandas as pd
    df = yf.download(ticker, period=period, auto_adjust=True,
                     multi_level_index=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    for col in ["Open","High","Low","Close","Volume"]:
        if col in df.columns: df[col] = df[col].squeeze()
    return df
'''
    artifacts_dir = _gad()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    script = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(_PRELUDE + "\n" + code)
            script = f.name
        env = {**os.environ, "ARTIFACTS_DIR": str(artifacts_dir)}
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=30, env=env,
        )
        stdout, stderr, code_val = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, code_val = "", "Timed out after 30s", -1
    except Exception as e:
        stdout, stderr, code_val = "", str(e), -1
    finally:
        if script:
            try: os.unlink(script)
            except Exception: pass

    payload = {"exit_code": code_val, "stdout": stdout[:3000], "stderr": stderr[:1000]}
    ok = code_val == 0
    summary = f"Python {'succeeded' if ok else 'failed'} (exit {code_val}). stdout: {stdout[:200]}"
    return persist_tool_result("execute_python", {"code": code}, json.dumps(payload), summary)


@tool
def run_dcf_workflow(
    ticker: str,
    horizon_years: int = 5,
    assumption_review_mode: bool = True,
    allow_external_assumptions: bool = True,
    assumption_overrides: dict[str, float] | None = None,
) -> str:
    """Run a deterministic DCF valuation workflow for a ticker.

    Default (assumption_review_mode=True): gathers evidence, proposes
    assumptions, returns them for your review. Present to user. After
    user approves/edits, call again with their edits as assumption_overrides
    AND assumption_review_mode=False to complete the valuation.
    """
    from utils import set_dcf_hitl_payload  # noqa: PLC0415

    payload = run_dcf_workflow_sync(
        ticker=ticker,
        horizon_years=horizon_years,
        assumption_review_mode=assumption_review_mode,
        allow_external_assumptions=allow_external_assumptions,
        assumption_overrides=assumption_overrides,
        parent_step_id="chat",
        session_id=_session_ctx.get(),
    )

    if payload.get("__dcf_hitl__"):
        # Store HITL payload for _run_agent_task to detect and handle
        set_dcf_hitl_payload({
            "ticker": payload.get("ticker", "?"),
            "horizon_years": payload.get("horizon_years", 5),
            "assumptions": payload.get("assumptions", {}),
            "assumption_provenance": payload.get("assumption_provenance", {}),
            "memo_proposals": payload.get("memo_proposals", {}),
            "evidence_items": payload.get("evidence_items", []),
        })
        emit_ui_event({
            "type": "dcf_assumptions_review",
            "ticker": payload.get("ticker", "?"),
            "horizon_years": payload.get("horizon_years", 5),
            "assumptions": payload.get("assumptions", {}),
            "assumption_provenance": payload.get("assumption_provenance", {}),
            "memo_proposals": payload.get("memo_proposals", {}),
            "evidence_items": payload.get("evidence_items", []),
        })
        assumptions = payload.get("assumptions", {})
        provenance = payload.get("assumption_provenance", {})
        lines = [
            "⛔ STOP — DO NOT CALL MORE TOOLS. Present these assumptions for review.",
            "",
            f"## DCF Assumptions for {payload.get('ticker', '?')} ({payload.get('horizon_years', 5)}yr)",
            "",
            "| Field | Value | Source | Confidence |",
            "|-------|-------|--------|------------|",
        ]
        for field in ["revenue_growth", "fcff_margin", "terminal_growth", "tax_rate", "wacc"]:
            val = assumptions.get(field)
            if val is None:
                continue
            prov = provenance.get(field, {})
            source = prov.get("source", "?")
            conf = prov.get("confidence", 0.5)
            lines.append(f"| {field} | {val:.2%} | {source} | {conf:.0%} |")
        lines.append("")
        lines.append("Ask the user to approve, edit values, or reject.")
        lines.append("After they respond, call again with assumption_overrides and assumption_review_mode=False.")
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
    quantitative disclosures sections. Use for any question about a company's
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


CHAT_TOOLS = [calculator, search_web, execute_python, search_documents, fetch_sec_filing, retrieve_tool_result, run_dcf_workflow]
CHAT_TOOLS_BY_NAME = {t.name: t for t in CHAT_TOOLS}
chat_agent_llm = llm.bind_tools(CHAT_TOOLS)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_CHAT_SYSTEM = (
    "You are a knowledgeable financial research assistant with tool access.\n\n"
    "## Tools available\n"
    "- search_documents: search the user's uploaded files (PDFs, spreadsheets, reports). "
    "ALWAYS call this BEFORE search_web for any factual query — documents already uploaded may contain exactly what you need. "
    "Only fall back to search_web if search_documents returns no relevant results.\n"
    "- fetch_sec_filing: fetch 10-K/10-Q filings from SEC EDGAR. Use for company risks, MD&A, or business overview — prefer over search_web for company fundamentals.\n"
    "- search_web: look up current news, prices, filings, or factual information NOT found in uploaded documents\n"
    "- retrieve_tool_result: read stored execute_python payloads when a tool returns a tool_result_id\n"
    "- calculator: evaluate mathematical expressions\n"
    "- execute_python: run code for data analysis, computations, or quick charts\n\n"
    "- run_dcf_workflow: deterministic DCF valuation for explicit intrinsic-value requests. "
    "**Always call with assumption_review_mode=True first.** "
    "This presents an interactive assumption review card to the user before computing valuation. "
    "After the user reviews and approves (or edits) the assumptions, call again with assumption_review_mode=False "
    "and any assumption_overrides the user specified. "
    "The result includes full assumption provenance, WACC decomposition, confidence label, and quality flags. "
    "When confidence is medium or low, mention flagged assumptions and any implied-vs-spot gap.\n\n"
    "## Behaviour\n"
    "- Use tools when the question requires current data or computation — don't guess.\n"
    "- For pure conceptual questions (e.g. 'what is DCF?'), answer directly without tools.\n"
    "- For DCF/valuation requests: call run_dcf_workflow with assumption_review_mode=True first. "
    "Wait for user to review the assumptions card. Then call again with assumption_review_mode=False "
    "and assumption_overrides from user edits. Do NOT search_web for beta, shares outstanding, "
    "WACC, or other DCF inputs — the workflow handles all of that.\n"
    "- When user message starts with [DCF_APPROVED], parse the JSON after the colon. "
    "Immediately call run_dcf_workflow with: ticker, horizon_years from the JSON, "
    "assumption_review_mode=False, and assumption_overrides set to the 'all_assumptions' dict from the JSON "
    "(pass ALL fields — this enables the fast valuation path that skips re-running evidence collection). "
    "Do NOT output any text before calling the tool. Do NOT ask for confirmation. Do NOT modify the assumptions.\n"
    "- Keep answers focused and well-structured. Use markdown when helpful.\n"
    "- For news/current-events questions, produce an analyst brief: one-sentence bottom line, then 3-5 bullets covering what happened, why it matters, dates/numbers, and source names.\n"
    "- Do NOT answer by listing links or saying 'here are sources'. Links are citations, not the answer.\n"
    "- Cite sources inline with names like (Meta investor relations, Apr. 29, 2026) or (AP, Apr. 29, 2026). Do not paste raw URLs unless asked.\n"
    "- Do not offer follow-up questions or say 'let me know if you need more'.\n"
    "- Do not end with optional next steps or 'If you want'.\n"
    "- Do not say you cannot access real-time data — you can, via search_web.\n"
    "- After search_web returns results, answer from those results. Do not repeat similar web searches unless the first result set is clearly irrelevant.\n"
    "- If you reference prior conversation context, be explicit about what you're building on."
)

# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def _normalize_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    if isinstance(args.get("parameters"), dict) and len(args) == 1:
        return args["parameters"]
    return args


def _fallback_answer_from_tool_results(history: list) -> str:
    """Create a minimal answer if model exhausts tool rounds without final text."""
    for message in reversed(history):
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(str(message.content))
        except (json.JSONDecodeError, TypeError):
            continue

        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or result.get("provider") != "exa":
            continue
        if result.get("error"):
            return f"Web search failed: {result['error']}"

        rows = []
        for item in result.get("results", [])[:4]:
            title = item.get("title") or "Untitled source"
            url = item.get("url") or ""
            highlights = item.get("highlights") or []
            snippet = highlights[0] if highlights else item.get("text") or ""
            snippet = " ".join(str(snippet).split())
            if len(snippet) > 500:
                snippet = snippet[:500].rstrip() + "..."
            rows.append(f"- **{title}**\n  {snippet}\n  {url}".strip())

        if rows:
            return "I found these relevant sources:\n\n" + "\n\n".join(rows)

    return ""


def chat_node(state: dict) -> dict:
    """ReAct loop: reason → optional tool calls → final answer."""
    messages = state.get("messages", [])
    session_memory = state.get("session_memory") or ""
    _session_ctx.set(state.get("session_id") or "")

    system_content = _CHAT_SYSTEM
    if session_memory:
        system_content += f"\n\n## Prior research in this session\n{session_memory}"

    history = [SystemMessage(content=system_content)] + messages[-20:]

    console.print("[bold cyan]💬 Chat (ReAct)...[/bold cyan]")
    emit_ui_event({"type": "chat_start"})
    used_tools = False

    # ── ReAct loop ────────────────────────────────────────────────────────────
    for round_idx in range(MAX_CHAT_ROUNDS):
        response = chat_agent_llm.invoke(history)
        history.append(response)

        if not response.tool_calls:
            # Final answer reached
            break

        # Process tool calls
        for tc in response.tool_calls:
            used_tools = True
            tool_fn = CHAT_TOOLS_BY_NAME.get(tc["name"])
            args = _normalize_args(tc.get("args", {}))

            args_preview = json.dumps(args, ensure_ascii=False)[:120]
            # Run the tool inside a track_tool span — the only event emitter
            # for chat-mode tool telemetry now that the legacy
            # `tool_call_start/end/error` events have been removed. The span
            # is allowed to swallow exceptions because original behaviour
            # was to fold tool errors into the result payload.
            result: str
            try:
                with track_tool(
                    name=tc["name"],
                    scope="chat",
                    step_id="chat",
                    args_preview=args_preview,
                ) as span:
                    if not tool_fn:
                        raise RuntimeError(f"unknown tool: {tc['name']}")
                    result = tool_fn.invoke(args)
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict):
                            span["summary"] = parsed.get("summary", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception as e:
                result = json.dumps({"error": str(e)})

            history.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

            # If the tool returned a HITL result (DCF assumptions for review),
            # break the ReAct loop immediately — the LLM must present to user.
            if "⛔ STOP" in str(result) or "DCF Assumptions for" in str(result):
                break
        else:
            continue
        break  # outer break — exit the for-loop over tool calls, then exit the round loop

    # ── Emit final response ───────────────────────────────────────────────────
    last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
    final_text = (last_ai.content if last_ai and isinstance(last_ai.content, str) else "") or ""

    # Detect DCF HITL card — skip verbose synthesis, use a single focused line.
    _hitl_ticker = None
    for _m in reversed(history):
        if isinstance(_m, ToolMessage):
            content = str(_m.content)
            if "DCF Assumptions for" in content or "⛔ STOP" in content:
                import re as _re
                _match = _re.search(r"DCF Assumptions for (\w+)", content)
                _hitl_ticker = _match.group(1) if _match else "?"
                break

    if _hitl_ticker:
        # Do NOT emit chat_complete here — bridge already set status=awaiting_assumptions
        # from the dcf_assumptions_review event, and we need the SSE stream to stay open
        # so the user's /dcf-decision response can stream valuation events back.
        return {"messages": [AIMessage(content="DCF assumptions ready for review.")]}
    elif used_tools:
        history.append(HumanMessage(content=(
            "Now write the final answer from the tool results above. "
            "Do not call more tools. Do not list raw sources or links. "
            "Synthesize what happened, why it matters, and cite source names inline."
        )))
        response = llm.invoke(history)
        final_text = response.content if isinstance(response.content, str) else final_text

    if not final_text.strip():
        history.append(HumanMessage(content="Use the available tool results above to produce the final answer now. Do not call more tools."))
        response = llm.invoke(history)
        final_text = response.content if isinstance(response.content, str) else ""
    if not final_text.strip():
        final_text = _fallback_answer_from_tool_results(history)
    if not final_text.strip():
        final_text = "I could not generate a final answer from the tool results. Check the backend log at agent_project/runs/server.log."

    console.print(f"[dim]Chat: {final_text[:80]}...[/dim]")
    emit_ui_event({"type": "chat_complete", "content": final_text})

    return {"messages": [AIMessage(content=final_text)]}

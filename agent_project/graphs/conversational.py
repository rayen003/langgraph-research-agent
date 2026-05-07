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
    assumption_review_mode: bool = False,
    allow_external_assumptions: bool = True,
    assumption_overrides: dict[str, float] | None = None,
) -> str:
    """Run a deterministic DCF valuation workflow for a ticker."""
    payload = run_dcf_workflow_sync(
        ticker=ticker,
        horizon_years=horizon_years,
        assumption_review_mode=assumption_review_mode,
        allow_external_assumptions=allow_external_assumptions,
        assumption_overrides=assumption_overrides,
        parent_step_id="chat",
        session_id=_session_ctx.get(),
    )
    summary = summarize_dcf_payload(payload)
    return persist_tool_result(
        "run_dcf_workflow",
        {
            "ticker": ticker,
            "horizon_years": horizon_years,
            "assumption_review_mode": assumption_review_mode,
            "allow_external_assumptions": allow_external_assumptions,
            "assumption_overrides": assumption_overrides or {},
        },
        json.dumps(payload, ensure_ascii=False),
        summary,
    )


CHAT_TOOLS = [calculator, search_web, retrieve_tool_result, execute_python, run_dcf_workflow, search_documents]
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
    "- search_web: look up current news, prices, filings, or factual information NOT found in uploaded documents\n"
    "- retrieve_tool_result: read stored execute_python payloads when a tool returns a tool_result_id\n"
    "- calculator: evaluate mathematical expressions\n"
    "- execute_python: run code for data analysis, computations, or quick charts\n\n"
    "- run_dcf_workflow: deterministic DCF valuation with sensitivity table for explicit intrinsic-value requests; "
    "it can use uploaded documents and capped web search for assumptions. The tool result includes a "
    "confidence label and quality flags — when confidence is medium or low, you must explicitly mention the "
    "flagged assumptions and any implied-vs-spot price gap rather than presenting the result as a clean answer.\n\n"
    "## Behaviour\n"
    "- Use tools when the question requires current data or computation — don't guess.\n"
    "- For pure conceptual questions (e.g. 'what is DCF?'), answer directly without tools.\n"
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

    # ── Emit final response ───────────────────────────────────────────────────
    last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
    final_text = (last_ai.content if last_ai and isinstance(last_ai.content, str) else "") or ""

    if used_tools:
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

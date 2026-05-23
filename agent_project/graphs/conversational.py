"""Conversational subgraph — ReAct agent with tool access, no HITL."""

import json
import logging
import os

import dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from documents import search_documents, _session_ctx
from tools import (
    calculator,
    execute_python,
    fetch_sec_filing,
    retrieve_tool_result,
    run_dcf_workflow,
    search_web,
)
import agent_log
from utils import console, emit_ui_event, get_run_dir, list_artifact_paths, set_dcf_hitl_payload, track_tool

logger = logging.getLogger(__name__)

dotenv.load_dotenv()

llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY"), timeout=60)

MAX_CHAT_ROUNDS = 4

# ---------------------------------------------------------------------------
# Tools (canonical definitions in tools.py)
# ---------------------------------------------------------------------------

CHAT_TOOLS = [
    calculator,
    search_web,
    execute_python,
    search_documents,
    fetch_sec_filing,
    retrieve_tool_result,
    run_dcf_workflow,
]
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
    "- search_web: look up current news, prices, filings, or factual information NOT found in uploaded documents. "
    "Returns a tool_result_id pointer + one-line summary — you MUST call retrieve_tool_result to read the full content.\n"
    "- retrieve_tool_result: read the full content of any tool result by its tool_result_id (search_web, execute_python, etc.)\n"
    "- calculator: evaluate mathematical expressions\n"
    "- execute_python: run code for data analysis, computations, or quick charts\n\n"
    "- run_dcf_workflow: deterministic DCF valuation for explicit intrinsic-value requests. "
    "**Always call with assumption_review_mode=True first.** "
    "This presents an interactive assumption review card to the user before computing valuation. "
    "After the user reviews and approves (or edits) the assumptions, call again with assumption_review_mode=False "
    "and any assumption_overrides the user specified. "
    "The tool returns a full markdown report — present it **verbatim** to the user (do NOT rewrite as a summary). "
    "Use only [n] citations from the report's ## References section; never cite 'tool results'.\n\n"
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


def _restore_hitl_from_approval(messages: list) -> None:
    """Restore DCF HITL snapshot when user approves assumptions via [DCF_APPROVED]."""
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        content = str(message.content or "")
        if not content.startswith("[DCF_APPROVED]:"):
            break
        try:
            payload = json.loads(content.split(":", 1)[1])
        except (json.JSONDecodeError, IndexError, TypeError):
            break
        snapshot = payload.get("hitl_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("assumptions"):
            set_dcf_hitl_payload(snapshot)
        break


def _extract_dcf_report(history: list) -> str | None:
    """If the last completed run_dcf_workflow produced a report, return it verbatim."""
    from graphs.workflows.dcf.payload import extract_dcf_report_from_tool_pointer  # noqa: PLC0415

    for message in reversed(history):
        if isinstance(message, ToolMessage):
            report = extract_dcf_report_from_tool_pointer(str(message.content))
            if report:
                return report
    return None


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

        if not isinstance(payload, dict):
            continue

        # Handle pointer format: read full result from disk
        if payload.get("tool_result_id"):
            tool_dir = get_run_dir() / "tool_results"
            file_path = tool_dir / f"{payload['tool_result_id']}.json"
            if file_path.exists():
                try:
                    payload = json.loads(file_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
            else:
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


def _is_quota_error(exc: Exception) -> bool:
    """Detect rate-limit / quota exhaustion via error message."""
    msg = str(exc).lower()
    return "insufficient_quota" in msg or "rate limit" in msg or "429" in msg or "quota" in msg


_QUOTA_FALLBACK_MSG = (
    "⚠️ API quota exhausted (HTTP 429 insufficient_quota). "
    "The DCF workflow completed any deterministic steps and persisted what it could, "
    "but I can't synthesize a final answer until the API key has credits again. "
    "Refill your DeepSeek credits at https://platform.deepseek.com or set a different DEEPSEEK_API_KEY."
)


def chat_node(state: dict) -> dict:
    """ReAct loop: reason → optional tool calls → final answer.

    Wrapped end-to-end in a quota guard so OpenAI 429 / insufficient_quota
    failures degrade to a user-visible message instead of crashing the graph.
    Deterministic work already done (DCF outputs, KG writes) is preserved.
    """
    try:
        return _chat_node_inner(state)
    except Exception as exc:  # noqa: BLE001
        if _is_quota_error(exc):
            logger.warning("chat_node: OpenAI quota exhausted, emitting fallback message")
            emit_ui_event({"type": "chat_complete", "content": _QUOTA_FALLBACK_MSG})
            return {"messages": [AIMessage(content=_QUOTA_FALLBACK_MSG)]}
        raise


def _chat_node_inner(state: dict) -> dict:
    messages = state.get("messages", [])
    session_memory = state.get("session_memory") or ""
    _session_ctx.set(state.get("session_id") or "")
    _restore_hitl_from_approval(messages)

    system_content = _CHAT_SYSTEM
    if session_memory:
        system_content += f"\n\n## Prior research in this session\n{session_memory}"

    history = [SystemMessage(content=system_content)] + messages[-20:]

    _chat_t = agent_log.chat_start()
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
        agent_log.chat_hitl(_hitl_ticker)
        return {"messages": [AIMessage(content="DCF assumptions ready for review.")]}
    dcf_report = _extract_dcf_report(history)
    if dcf_report:
        final_text = dcf_report
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

    agent_log.chat_done(final_text, _chat_t)
    complete_event: dict = {"type": "chat_complete", "content": final_text}
    if dcf_report or final_text.startswith("# DCF Valuation:"):
        artifact_paths = list_artifact_paths()
        if artifact_paths:
            complete_event["artifact_paths"] = artifact_paths
    emit_ui_event(complete_event)

    return {"messages": [AIMessage(content=final_text)]}

"""Conversational subgraph — ReAct agent with tool access, no HITL."""

import json
import logging
import os
import re
import time
from pathlib import Path

import dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode

from documents import search_documents, _session_ctx
from tools import (
    calculator,
    execute_python,
    fetch_sec_filing,
    query_knowledge_graph,
    retrieve_tool_result,
    run_dcf_workflow,
    run_deck_workflow,
    search_web,
)
import agent_log
from graphs.workflows.dcf.state import filter_user_assumption_overrides
from utils import console, emit_ui_event, get_run_dir, list_artifact_paths, list_deck_artifact_paths, relative_run_path, set_dcf_hitl_payload, track_tool

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
    query_knowledge_graph,
    retrieve_tool_result,
    run_dcf_workflow,
    run_deck_workflow,
]
CHAT_TOOLS_BY_NAME = {t.name: t for t in CHAT_TOOLS}
# ToolNode for native parallel execution — replaces the manual for-loop
chat_tool_node = ToolNode(CHAT_TOOLS)
chat_agent_llm = llm.bind_tools(CHAT_TOOLS)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_CHAT_SYSTEM = (
    "You are a knowledgeable financial research assistant with tool access.\n\n"
    "## Tools available\n"
    "- search_documents returns a relevance verdict with the relevant passages INLINE: "
    "{status: relevant|partial|mismatch|none, covered: [...], missing: [...], chunks: [{text, source, page, ticker}, ...]}. "
    "ALWAYS call this BEFORE search_web for any factual query. The `chunks` array already holds the full passage text — "
    "answer directly from it; there is NO separate fetch step. "
    "Pass a CONTENT query (key topics/metrics like 'revenue growth margins guidance'), "
    "NOT a meta word like 'analysis' or 'summary' — vague queries retrieve poorly.\n"
    "* status='relevant': all needed content is in `chunks` → answer from them.\n"
    "* status='partial': `chunks` cover some of what's asked, missing topics listed → answer from chunks + search_web for missing.\n"
    "* status='mismatch': docs are about DIFFERENT entities than what the user asked → "
    "TELL the user: 'The uploaded document appears to be about [company from docs], but you asked about [user's topic]. Which should I analyze?' "
    "Do NOT silently fall back to search_web on mismatch. Ask the user.\n"
    "* status='gate_skipped': passed skip_gate=True — `chunks` array has full text + metadata, evaluate relevance yourself.\n"
    "* status='none': no docs or no matches → proceed with search_web.\n"
    "Pass skip_gate=True when you already know the docs from prior turns — saves ~1-2s latency.\n"
    "- query_knowledge_graph: your OWN memory — the Knowledge Graph of everything you've already analyzed (prior DCF runs + assumptions + outputs, investment theses, company synthesis, drivers, fundamentals like revenue/margins/wacc, SEC filings, uploaded-doc facts, and saved news). "
    "It is a fast CACHE, a MEANS to an answer — not the answer itself, and never the fallback-of-last-resort for current events. "
    "For company questions it's worth a quick check: if it returns data that is RECENT ENOUGH for the question (judge from the as_of period + age it reports), answer from it (zero latency, zero cost). "
    "The tool returns **`needs_external: true`** plus an `external_reason` when its data is too STALE or MISSING to answer alone — the `answer` it gives in that case is the best from cached (possibly stale) data. "
    "When you see `needs_external: true`, you MUST call search_web to supplement, and you MUST present cached figures with their period (e.g. 'As of FY2023 …'), never as current. "
    "Hard rule: for any 'latest / current / today / this year' question, if the KG news is >24h old, the financials are not current-year, or `needs_external` is set → you MUST search_web. "
    "Never answer 'no recent news' from the KG alone — an empty/stale KG means the KG is stale, NOT that no news exists.\n"
    "- fetch_sec_filing: fetch 10-K/10-Q filings from SEC EDGAR. Use for company risks, MD&A, or business overview — prefer over search_web for company fundamentals.\n"
    "- search_web: look up current news, prices, or information that the KG/uploaded docs don't hold FRESH. "
    "Reach for it whenever the question is time-sensitive ('latest/current/today/this year') and the KG lacks current-enough data, "
    "or for any company you've never analyzed — don't force a stale KG answer when a web search is what the question needs. "
    "Returns a tool_result_id pointer + one-line summary — you MUST call retrieve_tool_result to read the full content.\n"
    "- retrieve_tool_result: read the full content of any tool result by its tool_result_id (search_web, execute_python, etc.)\n"
    "- calculator: evaluate mathematical expressions\n"
    "- execute_python: run code for data analysis, computations, or quick charts\n\n"
    "- run_dcf_workflow: deterministic DCF valuation for explicit intrinsic-value requests. "
    "Use the current User validation settings to decide assumption_review_mode. "
    "When assumption_review_mode=True, this presents an interactive assumption review card to the user before computing valuation. "
    "After the user reviews and approves (or edits) the assumptions, call again with assumption_review_mode=False "
    "and any assumption_overrides the user specified. "
    "The tool returns a full markdown report — present it **verbatim** to the user (do NOT rewrite as a summary). "
    "Use only [n] citations from the report's ## References section; never cite 'tool results'.\n"
    "- run_deck_workflow: generate a real PowerPoint deck (PPTX). "
    "After a completed DCF, call with **only** ``brief`` (title, audience, must_cover) — "
    "sources are auto-loaded from dcf_output.json; do NOT pass payload_inline or placeholder strings. "
    "Never invent slide outlines in chat when this tool is available. "
    "Set ``hitl_mode`` from the current User validation settings.\n\n"
    "## Behaviour\n"
    "- **This is chat mode** — handle most queries here. Research mode is reserved for deep multi-step research only.\n"
    "- Use tools when the question requires current data or computation — don't guess.\n"
    "- For pure conceptual questions (e.g. 'what is DCF?'), answer directly without tools.\n"
    "- For DCF/valuation requests: call run_dcf_workflow with assumption_review_mode from User validation settings. "
    "If assumption_review_mode=True, wait for user to review the assumptions card. Then call again with assumption_review_mode=False "
    "and assumption_overrides from user edits. Do NOT search_web for beta, shares outstanding, "
    "WACC, or other DCF inputs — the workflow handles all of that.\n"
    "- When user message starts with [DCF_APPROVED], parse the JSON after the colon. "
    "Immediately call run_dcf_workflow with: ticker, horizon_years from the JSON, "
    "assumption_review_mode=False, and assumption_overrides set only to editable model assumptions from the JSON "
    "(do not pass base_revenue, shares_outstanding, or net_debt; those are canonical facts). "
    "Do NOT output any text before calling the tool. Do NOT ask for confirmation.\n"
    "- For deck/presentation requests (slides, PPTX, pitch deck, IC deck, 'build a deck from this DCF'): "
    "**always call run_deck_workflow** — never write a fake slide-by-slide outline in chat. "
    "If a completed DCF exists in this thread, pass it as a `dcf_output` source (see tool doc). "
    "If no structured sources exist yet, run DCF first or ask which materials to include.\n"
    "- When user message starts with [DECK_COMPLETE], parse the JSON after the colon. "
    "Tell the user the deck is ready with slide count and deck title. "
    "Do NOT include filesystem paths or markdown download links — the UI renders "
    "Preview and Download controls automatically.\n"
    "- Keep answers focused and well-structured. Use markdown when helpful.\n"
    "- For news/current-events questions, produce an analyst brief: one-sentence bottom line, then 3-5 bullets covering what happened, why it matters, dates/numbers, and source names.\n"
    "- Do NOT answer by listing links or saying 'here are sources'. Links are citations, not the answer.\n"
    "- Cite sources inline with names like (Meta investor relations, Apr. 29, 2026) or (AP, Apr. 29, 2026). Do not paste raw URLs unless asked.\n"
    "- Do not offer follow-up questions or say 'let me know if you need more'.\n"
    "- Do not end with optional next steps or 'If you want'.\n"
    "- Do not say you cannot access real-time data — you can, via search_web.\n"
     "- **Never call the same tool more than once for the same ticker in a single turn** — a single fetch_sec_filing/search_web call returns all available data. Duplicate calls waste latency with zero new information.\n"
    "- **For financial metrics when the KG is empty**: use search_web with queries like 'AMZN revenue net income FY2025 earnings' rather than fetch_sec_filing. SEC filings return raw legal text that's hard to parse into numbers; web search returns articles with pre-extracted metrics. Only use fetch_sec_filing for risks, MD&A narrative, or business overview.\n"
    "- **Tool batching**: When you need multiple independent sources (e.g., KG + web, or multiple search_web calls for different topics), call them all in a SINGLE turn. "
    "Do NOT call one tool, wait for the result, then call another — that wastes 5-10s per turn. "
    "Independent tools that don't depend on each other's output should always be batched.\n"
    "- After search_web returns results, answer from those results. Do not repeat similar web searches unless the first result set is clearly irrelevant.\n"
    "- If you reference prior conversation context, be explicit about what you're building on."
)

_DECK_REQUEST_RE = re.compile(
    r"\b("
    r"deck|slides?|presentation|powerpoint|pptx|pitch deck|"
    r"ic deck|slide deck|build a deck|make a deck|turn.*into.*deck"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Deck routing helpers
# ---------------------------------------------------------------------------


def _user_wants_deck(messages: list) -> bool:
    """True when the latest user message requests a slide deck."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return bool(_DECK_REQUEST_RE.search(str(message.content or "")))
    return False


def _extract_dcf_payload_from_history(history: list) -> dict | None:
    """Return the most recent completed DCF JSON payload from chat history or disk."""
    from graphs.workflows.dcf.payload import (  # noqa: PLC0415
        extract_dcf_payload_from_tool_pointer,
        _load_persisted_dcf_payload,
    )

    for message in reversed(history):
        if isinstance(message, ToolMessage):
            payload = extract_dcf_payload_from_tool_pointer(str(message.content))
            if payload:
                return payload
    return _load_persisted_dcf_payload()


def _deck_hitl_mode_from_settings(user_settings: dict | None) -> str:
    validation = user_settings.get("validation") if isinstance(user_settings, dict) else {}
    if not isinstance(validation, dict):
        validation = {}
    require_hitl = bool(validation.get("requireHitl", True))
    deck_hitl = str(validation.get("deckHitlMode") or "partial").lower()
    if not require_hitl:
        deck_hitl = "disabled"
    if deck_hitl not in {"disabled", "partial", "full"}:
        deck_hitl = "partial"
    return deck_hitl


def _build_deck_workflow_nudge(history: list, user_settings: dict | None = None) -> str | None:
    """Inject exact run_deck_workflow args when user wants a deck and DCF exists."""
    if not _user_wants_deck(history):
        return None

    payload = _extract_dcf_payload_from_history(history)
    if not payload:
        return (
            "\n\n## Deck build request\n"
            "The user wants a slide deck. Call `run_deck_workflow` with appropriate "
            "`sources` and `brief`. If no completed DCF or uploaded documents exist in "
            "this thread, run DCF first or ask which sources to use. "
            "Do NOT invent slide outlines in chat."
        )

    ticker = str(payload.get("ticker") or "?").upper()
    brief = {
        "title": f"{ticker} — DCF Investment Case",
        "audience": "ic",
        "hitl_mode": _deck_hitl_mode_from_settings(user_settings),
        "slide_count_target": 10,
        "must_cover": [
            "executive summary",
            "thesis",
            "scenarios",
            "assumptions",
            "valuation",
            "sensitivity",
            "risks",
        ],
    }
    return (
        "\n\n## Deck build request — call run_deck_workflow now\n"
        "A completed DCF exists in this thread. Call `run_deck_workflow` with ONLY:\n"
        f"brief={json.dumps(brief, ensure_ascii=False)}\n"
        "Do NOT pass `sources` (auto-loaded from dcf_output.json). "
        "Do NOT pass payload_inline or placeholder strings. "
        "Do NOT write slide outlines in chat."
    )

# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def _extract_deck_artifact_paths(history: list) -> list[str]:
    """Return run-relative deck PPTX path(s) from tool results or disk."""
    for message in reversed(history):
        if not isinstance(message, ToolMessage):
            continue
        content = str(message.content)
        try:
            pointer = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(pointer, dict) or pointer.get("tool_name") != "run_deck_workflow":
            continue
        tool_result_id = pointer.get("tool_result_id")
        if tool_result_id:
            file_path = get_run_dir() / "tool_results" / f"{tool_result_id}.json"
            if file_path.exists():
                try:
                    stored = json.loads(file_path.read_text(encoding="utf-8"))
                    payload = json.loads(stored.get("result") or "{}")
                    rel = relative_run_path(payload.get("pptx_path"))
                    if rel:
                        return [rel]
                except (json.JSONDecodeError, OSError, TypeError):
                    pass
    return list_deck_artifact_paths()


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


def _extract_dcf_source_metadata(history: list) -> dict | None:
    """If the last completed DCF run has citation metadata, return it."""
    from graphs.workflows.dcf.payload import extract_dcf_source_metadata_from_tool_pointer  # noqa: PLC0415

    for message in reversed(history):
        if isinstance(message, ToolMessage):
            metadata = extract_dcf_source_metadata_from_tool_pointer(str(message.content))
            if metadata:
                return metadata
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


def _is_timeout_error(exc: Exception) -> bool:
    """Detect provider/network timeouts without importing provider-specific classes."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in msg or "timed out" in msg or "readtimeout" in msg


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


def _direct_dcf_approval(messages: list) -> dict | None:
    """Deterministically run a [DCF_APPROVED] valuation, bypassing the ReAct loop.

    The chat LLM was unreliable on this path — it would re-call run_dcf_workflow
    with assumption_review_mode=True (re-triggering the HITL "confirm assumptions"
    card) and fire several redundant/erroring calls, and the final report often
    never rendered. For an *approved* run the action is fully determined: run the
    valuation once with the supplied overrides and present the report verbatim.

    Returns the node result dict, or None when the latest human turn is not a
    DCF approval (so the normal ReAct loop runs instead).
    """
    last_human = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
    )
    if last_human is None:
        return None
    content = str(last_human.content or "")
    if not content.startswith("[DCF_APPROVED]:"):
        return None
    try:
        payload = json.loads(content.split(":", 1)[1])
    except (json.JSONDecodeError, IndexError, TypeError):
        return None

    ticker = str(payload.get("ticker") or "").upper()
    horizon_years = int(payload.get("horizon_years") or 5)
    overrides = (
        payload.get("all_assumptions")
        or payload.get("assumption_overrides")
        or {}
    )
    if not ticker or not isinstance(overrides, dict) or not overrides:
        return None

    filtered_overrides = filter_user_assumption_overrides(overrides)
    if not filtered_overrides:
        return None

    args = {
        "ticker": ticker,
        "horizon_years": horizon_years,
        "assumption_review_mode": False,
        "assumption_overrides": filtered_overrides,
    }
    # Lineage: when the approval came from rerunning an existing KG run, link
    # the new run to its parent so the KG records the derivation chain.
    parent_run_id = payload.get("parent_run_id")
    if parent_run_id:
        args["parent_run_id"] = parent_run_id

    chat_t = agent_log.chat_start()
    emit_ui_event({"type": "chat_start"})

    tool_fn = CHAT_TOOLS_BY_NAME.get("run_dcf_workflow")
    history: list = []
    try:
        with track_tool(
            name="run_dcf_workflow",
            scope="chat",
            step_id="chat",
            args_preview=json.dumps(args, ensure_ascii=False)[:120],
        ) as span:
            if not tool_fn:
                raise RuntimeError("run_dcf_workflow tool not registered")
            result = tool_fn.invoke(args)
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    span["summary"] = parsed.get("summary", "")
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as exc:  # noqa: BLE001
        logger.error("Direct DCF approval run failed: %s", exc, exc_info=True)
        msg = f"DCF rerun failed: {exc}"
        emit_ui_event({"type": "chat_complete", "content": msg})
        return {"messages": [AIMessage(content=msg)]}

    history.append(ToolMessage(content=str(result), tool_call_id="dcf_approved_direct"))

    final_text = _extract_dcf_report(history) or ""
    if not final_text.strip():
        final_text = "DCF rerun completed but produced no report."

    agent_log.chat_done(final_text, chat_t)
    complete_event: dict = {"type": "chat_complete", "content": final_text}
    artifact_paths = list_artifact_paths()
    if artifact_paths:
        complete_event["artifact_paths"] = artifact_paths
    source_metadata = _extract_dcf_source_metadata(history)
    if source_metadata:
        complete_event.update(source_metadata)
    emit_ui_event(complete_event)

    return {"messages": [AIMessage(content=final_text)]}


def _build_doc_inventory(session_id: str) -> str:
    """List uploaded documents (+ extracted entities) for the system prompt.

    Without this the agent is blind to uploads — it asks the user to "please
    upload" even when a doc is already indexed. Surfacing the inventory makes
    the agent call search_documents instead of stalling.
    """
    if not session_id:
        return ""
    try:
        from documents import list_docs  # noqa: PLC0415

        docs = list_docs(session_id)
    except Exception:  # noqa: BLE001
        return ""

    ready = [d for d in docs if d.get("status") == "ready"]
    pending = [d for d in docs if d.get("status") == "processing"]
    if not ready and not pending:
        return ""

    lines = ["\n\n## Uploaded documents (this session)"]
    for d in ready:
        ent = []
        if d.get("company"):
            ent.append(str(d["company"]))
        if d.get("ticker"):
            ent.append(str(d["ticker"]))
        if d.get("doc_type"):
            ent.append(str(d["doc_type"]).replace("_", " "))
        if d.get("fiscal_period"):
            ent.append(str(d["fiscal_period"]))
        meta = f" — {', '.join(ent)}" if ent else ""
        lines.append(f"- {d.get('filename', 'document')}{meta} [ready]")
    for d in pending:
        lines.append(f"- {d.get('filename', 'document')} [still indexing]")
    lines.append(
        "These are already indexed. When the user asks about their content, "
        "call search_documents — do NOT ask the user to upload again."
    )
    return "\n".join(lines)


def _build_user_settings_prompt(user_settings: dict) -> str:
    validation = user_settings.get("validation") if isinstance(user_settings, dict) else {}
    if not isinstance(validation, dict):
        validation = {}
    require_hitl = bool(validation.get("requireHitl", True))
    dcf_hitl = bool(validation.get("dcfHitl", True)) and require_hitl
    deck_hitl = _deck_hitl_mode_from_settings(user_settings)
    return (
        "\n\n## User validation settings\n"
        f"- DCF: call run_dcf_workflow with assumption_review_mode={str(dcf_hitl)}.\n"
        f"- Decks: call run_deck_workflow with brief.hitl_mode='{deck_hitl}'.\n"
        "- These settings override generic workflow defaults.\n"
    )


def _build_kg_state_injection(query: str) -> str:
    """Build a compact KG state summary for tickers mentioned in the query.

    Injects into the first user message so the LLM knows on turn 1 what data
    the KG already has — no tool calls wasted on discovery.
    """
    try:
        import storage  # noqa: PLC0415
        from collections import Counter

        # Get all known tickers from KG
        all_nodes = storage.list_kg_nodes()
        known_tickers: set[str] = set()
        for n in all_nodes:
            t = (n.get("ticker") or "").upper().strip()
            if t:
                known_tickers.add(t)
        if not known_tickers:
            return ""

        # Find which known tickers appear as whole words in the query
        query_upper = query.upper()
        mentioned: set[str] = set()
        for t in known_tickers:
            # Match as whole word (preceded/followed by non-alpha or boundary)
            import re
            if re.search(rf'\b{re.escape(t)}\b', query_upper):
                mentioned.add(t)

        if not mentioned:
            return ""

        # Build summary per ticker
        lines: list[str] = []
        now = __import__("time").time()
        for ticker in sorted(mentioned):
            nodes = [n for n in all_nodes if (n.get("ticker") or "").upper() == ticker]
            type_counts: Counter[str] = Counter()
            latest_ts: dict[str, float] = {}
            for n in nodes:
                nt = n.get("node_type", "?")
                type_counts[nt] += 1
                ts = n.get("updated_at") or n.get("created_at") or 0
                if nt not in latest_ts or float(ts) > latest_ts[nt]:
                    latest_ts[nt] = float(ts)

            parts: list[str] = [ticker]

            # News recency — flag staleness so the agent doesn't read "we have
            # news" as "we have the answer". News older than 24h cannot satisfy
            # a "latest/current" question on its own.
            news_count = type_counts.get("news_item", 0)
            if news_count > 0:
                latest_news = latest_ts.get("news_item", 0)
                age_h = (now - latest_news) / 3600 if latest_news else 999
                age_str = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h / 24:.0f}d ago"
                flag = " ⚠ stale for current-events" if age_h > 24 else ""
                parts.append(f"{news_count} news, latest {age_str}{flag}")

            # Filings
            filing_count = type_counts.get("filing", 0)
            if filing_count > 0:
                parts.append(f"{filing_count} filings")

            # Financial metrics (structured_fundamental or financials_hub). Always
            # flag the period — a cached FY2023 figure must not be presented as
            # current-year without verification.
            fin_count = type_counts.get("structured_fundamental", 0)
            hub = [n for n in nodes if n.get("node_type") == "financials_hub"]
            if hub:
                hub_val = hub[0].get("val") if isinstance(hub[0], dict) else None
                as_of = (hub_val or {}).get("as_of", "") if isinstance(hub_val, dict) else ""
                parts.append(
                    f"financials as_of {as_of} ⚠ verify if current-year needed"
                    if as_of else "financials cached ⚠ verify period"
                )
            elif fin_count > 0:
                parts.append(f"{fin_count} financial metrics ⚠ verify period")
            else:
                parts.append("no financials cached")

            # Prior DCF runs
            dcf_runs = type_counts.get("dcf_run", 0)
            if dcf_runs > 0:
                parts.append(f"{dcf_runs} prior DCF runs")

            lines.append(" · ".join(parts))

        if not lines:
            return ""

        return (
            "\n## Background — cached KG data (a HINT, not the answer)\n"
            "You MAY already hold the data points below. They are a fast cache, not "
            "ground truth: check the freshness flags before trusting them, and "
            "web-search to fill anything stale (⚠) or missing. Do NOT answer a "
            "current-events question from a ⚠ item alone.\n"
            + "\n".join(lines) + "\n"
        )
    except Exception:
        return ""  # Never let KG pre-fetch crash the chat


def _build_today_anchor() -> str:
    """Anchor the agent in real time.

    Without a current-date anchor the model cannot resolve "this year" / "the
    year" / "current", so prompt rules about "current-year financials" are
    meaningless and it falls back to whatever (stale) year the KG cache names —
    e.g. answering "financials for the year" with FY2023 in 2026.
    """
    import datetime  # noqa: PLC0415

    today = datetime.date.today()
    y = today.year
    return (
        "\n\n## Today\n"
        f"Today's date is {today:%Y-%m-%d}. The current calendar year is {y}.\n"
        f"- 'this year' / 'the year' / 'current' / 'latest' refer to {y}.\n"
        f"- The most recent COMPLETED and reported fiscal year is normally FY{y - 1}. "
        f"When a question asks for financials 'for the year' without naming one, use "
        f"FY{y - 1} (latest reported annual), or {y} year-to-date quarterly if asked.\n"
        f"- NEVER answer with a fiscal year more than ~1 year stale (e.g. FY{y - 3}) "
        f"just because the KG cached it — that is STALE. Resolve the year from today's "
        f"date and web-search the current figures.\n"
    )


def _stream_final_answer(history: list) -> str:
    """Stream the final synthesis token-by-token via ``chat_token`` and return
    the full text.

    The chat answer is otherwise generated with ``llm.invoke`` and dumped whole
    via ``chat_complete`` after ~10s — all dead air. Streaming here makes the
    answer appear live. ThinkingDots persist until the first token because
    ``chat_start`` fired earlier and NO ``chat_token`` is emitted during the
    tool-routing rounds (only here, at genuine answer generation).

    Falls back to a single non-streaming ``invoke`` if streaming raises before
    any token was emitted, so a transient stream error never drops the answer.
    """
    parts: list[str] = []
    try:
        for chunk in llm.stream(history):
            text = chunk.content
            if isinstance(text, str) and text:
                parts.append(text)
                emit_ui_event({"type": "chat_token", "token": text})
    except Exception:  # noqa: BLE001
        if parts:
            # Already streamed some tokens — keep them; the loop will reconcile
            # the full text via chat_complete.
            return "".join(parts)
        resp = llm.invoke(history)
        full = resp.content if isinstance(resp.content, str) else ""
        if full:
            emit_ui_event({"type": "chat_token", "token": full})
        return full
    return "".join(parts)


def _chat_node_inner(state: dict) -> dict:
    messages = state.get("messages", [])
    session_memory = state.get("session_memory") or ""
    _session_ctx.set(state.get("session_id") or "")
    _restore_hitl_from_approval(messages)

    # Approved DCF runs are deterministic — run them directly instead of letting
    # the ReAct loop improvise (it would re-trigger the assumption-review HITL
    # and fire redundant workflow calls). Returns None for non-approval turns.
    direct = _direct_dcf_approval(messages)
    if direct is not None:
        return direct

    system_content = _CHAT_SYSTEM
    system_content += _build_today_anchor()
    system_content += _build_user_settings_prompt(state.get("user_settings") or {})
    doc_inventory = _build_doc_inventory(state.get("session_id") or "")
    if doc_inventory:
        system_content += doc_inventory
    if session_memory:
        system_content += f"\n\n## Prior research in this session\n{session_memory}"
    deck_nudge = _build_deck_workflow_nudge(messages, state.get("user_settings") or {})
    if deck_nudge:
        system_content += deck_nudge

    # ── KG state injection: tell the LLM what data it already has ────────
    # Injected into the first user message (not the system prompt) to preserve
    # KV-cache stability for the system prefix across different queries.
    messages_list = list(messages)
    if messages_list:
        last_user_msg = next(
            (m for m in reversed(messages_list) if isinstance(m, HumanMessage)), None
        )
        if last_user_msg is not None and isinstance(last_user_msg.content, str):
            kg_state = _build_kg_state_injection(last_user_msg.content)
            if kg_state:
                last_user_msg.content = kg_state + "\n" + last_user_msg.content

    history = [SystemMessage(content=system_content)] + messages_list[-20:]

    _chat_t = agent_log.chat_start()
    emit_ui_event({"type": "chat_start"})
    used_tools = False

    # ── ReAct loop ────────────────────────────────────────────────────────────
    for round_idx in range(MAX_CHAT_ROUNDS):
        try:
            response = chat_agent_llm.invoke(history)
        except Exception as exc:  # noqa: BLE001
            # If a DCF tool call already completed, never let a post-tool
            # synthesis timeout break the UI. The report is already in the
            # tool result and can be emitted verbatim below.
            if _is_timeout_error(exc) and _extract_dcf_report(history):
                logger.warning(
                    "chat_node: post-DCF synthesis timed out; emitting report fallback",
                    exc_info=True,
                )
                break
            raise
        history.append(response)

        if not response.tool_calls:
            # Final answer reached
            break

        # Process tool calls via ToolNode (native parallel execution)
        if response.tool_calls:
            used_tools = True
            # Pre-process: resolve deck workflow inputs inline so ToolNode
            # can execute them alongside other tools in parallel
            for tc in response.tool_calls:
                if tc["name"] == "run_deck_workflow":
                    from graphs.workflows.deck.inputs import resolve_deck_workflow_inputs  # noqa: PLC0415
                    dcf_payload = _extract_dcf_payload_from_history(history)
                    args = _normalize_args(tc.get("args", {}))
                    try:
                        resolved_sources, resolved_brief = resolve_deck_workflow_inputs(
                            args.get("sources"),
                            args.get("brief"),
                            dcf_payload=dcf_payload,
                        )
                        tc["args"] = {**args, "sources": resolved_sources, "brief": resolved_brief}
                    except ValueError:
                        tc["args"] = args  # let the tool fail with a clear error

            # Execute all tool calls via ToolNode (parallel), with activity
            # telemetry matching the old per-tool track_tool pattern.
            tool_spans = []
            for tc in response.tool_calls:
                args_preview = json.dumps(_normalize_args(tc.get("args", {})), ensure_ascii=False)[:120]
                span_ctx = track_tool(
                    name=tc["name"], scope="chat", step_id="chat",
                    args_preview=args_preview,
                )
                span = span_ctx.__enter__()
                tool_spans.append((tc, span, span_ctx))
            try:
                tool_result = chat_tool_node.invoke({"messages": [response]})
                tool_messages = tool_result.get("messages", [])
                history.extend(tool_messages)
                # Populate span summaries from tool results
                for tc, span, _span_ctx in tool_spans:
                    for tm in tool_messages:
                        if isinstance(tm, ToolMessage) and tm.tool_call_id == tc.get("id"):
                            try:
                                parsed = json.loads(str(tm.content))
                                if isinstance(parsed, dict):
                                    span["summary"] = parsed.get("summary", "")
                            except (json.JSONDecodeError, TypeError):
                                pass
                            break
            except Exception:
                raise
            finally:
                for _tc, _span, span_ctx in reversed(tool_spans):
                    span_ctx.__exit__(None, None, None)

            # Post-process: check for DCF reports or HITL
            hitl_found = False
            for tm in tool_messages:
                if not isinstance(tm, ToolMessage):
                    continue
                result_str = str(tm.content)
                if "Draft Deck Outline" in result_str:
                    hitl_found = True
                    break
                if "DCF Assumptions for" in result_str or (
                    "⛔ STOP" in result_str and "assumption" in result_str.lower()
                ):
                    hitl_found = True
                    break
            if hitl_found or _extract_dcf_report(history):
                break  # HITL or DCF report found → exit the round loop

    # ── Emit final response ───────────────────────────────────────────────────
    last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
    final_text = (last_ai.content if last_ai and isinstance(last_ai.content, str) else "") or ""

    # Detect DCF HITL card — skip verbose synthesis, use a single focused line.
    _hitl_ticker = None
    for _m in reversed(history):
        if isinstance(_m, ToolMessage):
            content = str(_m.content)
            if "Draft Deck Outline" in content:
                break
            if "DCF Assumptions for" in content or (
                "⛔ STOP" in content and "assumption" in content.lower()
            ):
                import re as _re
                _match = _re.search(r"DCF Assumptions for (\w+)", content)
                _hitl_ticker = _match.group(1) if _match else "?"
                break

    _deck_outline_hitl = False
    _deck_outline_text = ""
    for _m in reversed(history):
        if isinstance(_m, ToolMessage):
            content = str(_m.content)
            if "Draft Deck Outline" in content:
                _deck_outline_hitl = True
                _deck_outline_text = content
                break

    if _deck_outline_hitl:
        # Server keeps SSE open for deck outline review (deck_hitl_payload).
        preview = _deck_outline_text.split("## Draft Deck Outline", 1)
        body = "## Draft Deck Outline" + preview[1] if len(preview) > 1 else _deck_outline_text
        return {"messages": [AIMessage(content=body.strip())]}

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
        streamed = _stream_final_answer(history)
        final_text = streamed if streamed.strip() else final_text

    if not final_text.strip():
        history.append(HumanMessage(content="Use the available tool results above to produce the final answer now. Do not call more tools."))
        streamed = _stream_final_answer(history)
        final_text = streamed if isinstance(streamed, str) else ""
    if not final_text.strip():
        final_text = _fallback_answer_from_tool_results(history)
    if not final_text.strip():
        final_text = "I could not generate a final answer from the tool results. Check the backend log at agent_project/runs/server.log."

    agent_log.chat_done(final_text, _chat_t)
    complete_event: dict = {"type": "chat_complete", "content": final_text}
    deck_artifacts = _extract_deck_artifact_paths(history)
    if deck_artifacts:
        complete_event["artifact_paths"] = deck_artifacts
    if dcf_report or final_text.startswith("# DCF Valuation:"):
        artifact_paths = list_artifact_paths()
        if artifact_paths:
            complete_event["artifact_paths"] = artifact_paths
        source_metadata = _extract_dcf_source_metadata(history)
        if source_metadata:
            complete_event.update(source_metadata)
    emit_ui_event(complete_event)

    return {"messages": [AIMessage(content=final_text)]}

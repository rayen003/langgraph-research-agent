"""Conversational subgraph — ReAct agent with tool access, no HITL."""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from documents import _session_ctx
from memory_context import format_memory_context_prompt
from reference_broker import collect_refs, completion_gaps, merge_refs, resolve_ref
from turn_context import format_turn_context_prompt
from evidence_memory import format_evidence_pack_prompt
from execution_trace import make_execution_step
from capability_dispatch import get_capability_dispatcher
from tool_catalog import artifact_render_request_for_results, select_capability_tools
from tool_runtime import unwrap_tool_result_message
from tools import (
    ALL_TOOLS,
    calculator,
    execute_python,
    fetch_sec_filing,
    get_company_financials,
    get_stock_price_history,
    query_knowledge_graph,
    render_financial_chart,
    render_normalized_stock_comparison,
    render_stock_price_chart,
    retrieve_tool_result,
    run_dcf_workflow,
    run_deck_workflow,
    search_web,
)
import agent_log
from graphs.workflows.dcf.state import filter_user_assumption_overrides
from utils import console, emit_ui_event, get_run_dir, list_artifact_paths, list_deck_artifact_paths, relative_run_path, set_dcf_hitl_payload

logger = logging.getLogger(__name__)

dotenv.load_dotenv()

ANALYST_MODEL = os.getenv("ANALYST_MODEL", os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1"))

llm = ChatOpenAI(
    model=ANALYST_MODEL,
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30,
    max_retries=0,
)

news_synthesis_llm = ChatOpenAI(
    model=os.getenv("NEWS_SYNTHESIS_MODEL", "gpt-4o-mini"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=8,
    max_retries=0,
    max_tokens=450,
)

MAX_CHAT_ROUNDS = 4

# ---------------------------------------------------------------------------
# Tools (canonical definitions in tools.py)
# ---------------------------------------------------------------------------

CHAT_TOOLS = select_capability_tools("chat", ALL_TOOLS)
CHAT_TOOLS_BY_NAME = {t.name: t for t in CHAT_TOOLS}
_CANONICAL_CHAT_TOOLS_BY_NAME = dict(CHAT_TOOLS_BY_NAME)
_CAPABILITY_DISPATCHER = get_capability_dispatcher()


def _chat_tool_overrides() -> dict:
    return {
        name: tool
        for name, tool in CHAT_TOOLS_BY_NAME.items()
        if tool is not _CANONICAL_CHAT_TOOLS_BY_NAME.get(name)
    }
chat_agent_llm = llm.bind_tools(CHAT_TOOLS)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_CHAT_SYSTEM = (
    "You are a knowledgeable financial research assistant with tool access.\n\n"
    "## Tools available\n"
    "- For explicit listed-stock/share-price history, return, or market-performance charts, use get_stock_price_history, then pass its tool_result_id to render_stock_price_chart. "
    "For normalized or side-by-side listed-stock comparisons, call get_stock_price_history once per ticker, then call render_normalized_stock_comparison with every result ID. "
    "Do not call retrieve_tool_result or execute_python for market-price charts when these renderers are available. "
    "Financial performance means statements; use get_company_financials. Call render_financial_chart only when user requests a chart, plot, or visualization. "
    "Never fabricate, simulate, or infer market prices with execute_python.\n"
    "- search_documents returns a relevance verdict with the relevant passages INLINE: "
    "{status: relevant|partial|mismatch|none, covered: [...], missing: [...], chunks: [{text, citation_id, citation_label, source, page, ticker}, ...]}. "
    "ALWAYS call this BEFORE search_web for any factual query. The `chunks` array already holds the full passage text — "
    "answer directly from it; there is NO separate fetch step. "
    "When any claim comes from uploaded documents, cite it inline with the exact citation_id, e.g. [doc:doc_abc:p12:c4]. "
    "Do not cite uploaded-doc claims with vague source names only. "
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
    "- get_company_financials: fetch multi-period structured financial statements using the same provider adapter as DCF. Returns a compact manifest and result ID. Prefer this over web snippets for reported financial metrics.\n"
    "- render_financial_chart: create a chart from get_company_financials result ID. Pass input_result_id directly; do not retrieve and copy the raw table into chart arguments.\n"
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
    "- run_memo_workflow: generate a durable, source-linked investment memo. "
    "For explicit memo requests, call this tool after workflow setup approval. "
    "Pass a brief; selected workspace objects and documents come from approved workflow context. "
    "Never draft final memo directly in chat when this tool is available.\n\n"
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
    "- For memo requests: always call run_memo_workflow. Memo does not require DCF, but must use selected source objects. "
    "Use hitl_mode='review' unless user settings disable validation.\n"
    "- When user message starts with [DECK_COMPLETE], parse the JSON after the colon. "
    "Tell the user the deck is ready with slide count and deck title. "
    "Do NOT include filesystem paths or markdown download links — the UI renders "
    "Preview and Download controls automatically.\n"
    "- Keep answers focused and well-structured. Use markdown when helpful.\n"
    "- A written document, comparison, or analyst report requires substantive markdown with actual evidence values and analysis. "
    "Do not substitute charts for requested prose, and do not invent headings with placeholder descriptions.\n"
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

_DOC_FOCUSED_RE = re.compile(
    r"\b("
    r"this\s+(doc|document|pdf|file|upload)|"
    r"attached\s+(doc|document|pdf|file)|"
    r"uploaded\s+(doc|document|pdf|file)|"
    r"analy[sz]e\s+(this\s+)?(doc|document|pdf|file)|"
    r"summari[sz]e\s+(this\s+)?(doc|document|pdf|file)|"
    r"what\s+does\s+(this\s+)?(doc|document|pdf|file)\s+say"
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


def _latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _is_doc_focused_request(text: str) -> bool:
    return bool(_DOC_FOCUSED_RE.search(text or ""))


def _ready_docs_for_session(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        from documents import list_docs  # noqa: PLC0415
        return [d for d in list_docs(session_id) if d.get("status") == "ready"]
    except Exception:  # noqa: BLE001
        return []


def _doc_search_query(user_text: str) -> str:
    lowered = (user_text or "").lower()
    if any(term in lowered for term in ("revenue", "margin", "guidance", "earnings", "risk", "outlook")):
        return user_text
    return "key financial highlights revenue growth margins guidance risks outlook"


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


def _strip_generated_artifact_markdown(final_text: str, artifact_paths: list[str]) -> str:
    """Let chat UI own generated artifacts instead of rendering local disk paths."""
    cleaned = final_text
    for artifact_path in artifact_paths:
        if not artifact_path:
            continue
        escaped = re.escape(str(artifact_path))
        cleaned = re.sub(
            rf"!\[[^\]]*\]\((?:sandbox:)?{escaped}(?:\s+[^)]*)?\)",
            "",
            cleaned,
        )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


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
    return []


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


_NEWS_SOURCE_WEIGHTS = {
    "apple.com": 8,
    "reuters.com": 8,
    "apnews.com": 8,
    "bloomberg.com": 7,
    "ft.com": 7,
    "wsj.com": 7,
    "cnbc.com": 6,
    "bbc.com": 6,
    "nbcnews.com": 5,
    "cbsnews.com": 5,
}
_NEWS_TITLE_STOPWORDS = {
    "a", "an", "and", "apple", "for", "in", "is", "of", "on", "the", "to", "with",
}


def _latest_exa_payload(history: list) -> dict | None:
    """Dereference latest persisted Exa result into normalized payload."""
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
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                continue
        if not isinstance(result, dict) or result.get("provider") != "exa":
            continue
        return result
    return None


def _news_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host


def _news_source_name(item: dict) -> str:
    domain = _news_domain(str(item.get("url") or ""))
    known = {
        "apple.com": "Apple Newsroom",
        "reuters.com": "Reuters",
        "apnews.com": "Associated Press",
        "bloomberg.com": "Bloomberg",
        "ft.com": "Financial Times",
        "wsj.com": "Wall Street Journal",
        "cnbc.com": "CNBC",
        "bbc.com": "BBC",
        "nbcnews.com": "NBC News",
        "cbsnews.com": "CBS News",
        "thestar.com.my": "The Star",
    }
    for suffix, label in known.items():
        if domain == suffix or domain.endswith(f".{suffix}"):
            return label
    return domain or str(item.get("author") or "Unknown source")


def _published_timestamp(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_generic_news_page(item: dict) -> bool:
    title = str(item.get("title") or "").lower()
    path = urlparse(str(item.get("url") or "")).path.rstrip("/").lower()
    generic_title = any(marker in title for marker in (
        "today's latest updates", "breaking news on", "newsroom - apple", "latest news and updates",
    ))
    generic_path = path in {"/newsroom", "/tech/apple", "/tag/apple", "/topic/apple"}
    return not item.get("published_date") and (generic_title or generic_path)


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", title.lower())
        if len(token) > 2 and token not in _NEWS_TITLE_STOPWORDS
    }


def _rank_news_results(payload: dict | None, *, limit: int = 5) -> list[dict]:
    """Rank fresh story pages and remove generic pages and duplicate coverage."""
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    now = time.time()
    candidates: list[tuple[float, dict]] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict) or not raw.get("title") or not raw.get("url"):
            continue
        item = dict(raw)
        timestamp = _published_timestamp(item.get("published_date"))
        age_days = max(0.0, (now - timestamp) / 86_400) if timestamp else 30.0
        domain = _news_domain(str(item.get("url") or ""))
        source_weight = max(
            (weight for suffix, weight in _NEWS_SOURCE_WEIGHTS.items() if domain == suffix or domain.endswith(f".{suffix}")),
            default=2,
        )
        score = source_weight + max(0.0, 7.0 - min(age_days, 7.0))
        if _is_generic_news_page(item):
            score -= 20
        candidates.append((score, item))

    ranked: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: list[set[str]] = []
    for _score, item in sorted(candidates, key=lambda pair: pair[0], reverse=True):
        url = str(item.get("url") or "").split("#", 1)[0]
        tokens = _title_tokens(str(item.get("title") or ""))
        duplicate = any(
            tokens and prior and len(tokens & prior) / min(len(tokens), len(prior)) >= 0.75
            for prior in seen_titles
        )
        if url in seen_urls or duplicate or _is_generic_news_page(item):
            continue
        highlights = item.get("highlights") or []
        snippet = highlights[0] if highlights else item.get("text") or ""
        snippet = " ".join(str(snippet).split())[:1_000]
        ranked.append({
            "source_id": f"S{len(ranked) + 1}",
            "title": str(item.get("title") or "Untitled story"),
            "url": url,
            "published_date": item.get("published_date"),
            "source": _news_source_name(item),
            "snippet": snippet,
        })
        seen_urls.add(url)
        seen_titles.append(tokens)
        if len(ranked) >= limit:
            break
    return ranked


def _news_date_label(value: object) -> str:
    timestamp = _published_timestamp(value)
    if timestamp is None:
        return "date unavailable"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%b %d, %Y").replace(" 0", " ")


def _render_news_citations(answer: str, sources: list[dict]) -> str:
    by_id = {str(source["source_id"]): source for source in sources}

    def replace(match: re.Match[str]) -> str:
        source = by_id.get(match.group(1))
        if source is None:
            return ""
        label = f"{source['source']}, {_news_date_label(source.get('published_date'))}"
        return f"[{label}]({source['url']})"

    return re.sub(r"\[(S\d+)\]", replace, answer).strip()


def _deterministic_news_brief(query: str, sources: list[dict], *, error: str | None = None) -> str:
    if not sources:
        suffix = f" Search failed: {error}" if error else ""
        return f"No reliable current sources were returned for `{query}`.{suffix}".strip()
    lines = [f"Latest coverage contains {len(sources)} distinct company development(s) worth reviewing:"]
    for source in sources:
        snippet = source.get("snippet") or "No usable article summary was returned."
        if len(snippet) > 360:
            snippet = snippet[:360].rsplit(" ", 1)[0] + "..."
        citation = f"[{source['source']}, {_news_date_label(source.get('published_date'))}]({source['url']})"
        lines.append(f"- **{source['title']}:** {snippet} {citation}")
    return "\n\n".join(lines)


def _synthesize_news_answer(query: str, sources: list[dict]) -> str:
    """Run one tool-free, source-bounded synthesis call."""
    source_packet = [
        {
            "id": source["source_id"],
            "title": source["title"],
            "source": source["source"],
            "published_date": source.get("published_date"),
            "snippet": source["snippet"],
        }
        for source in sources
    ]
    messages = [
        SystemMessage(content=(
            "You write fast, grounded company-news briefs for finance professionals. "
            "Use only supplied source packet. Do not add facts, numbers, or events. "
            "Start with a direct one-sentence summary. Do not use a fixed label such as 'Bottom line'. "
            "Then write up to 3 concise, distinct bullets. "
            "Each bullet must explain what happened and why it matters. Group duplicate stories. "
            "Cite claims using exact tokens [S1], [S2], etc. Never print URLs or say you found sources. "
            "Flag weak or conflicting evidence instead of resolving it yourself. Stay below 250 words."
        )),
        HumanMessage(content=json.dumps({
            "query": query,
            "current_date": datetime.now(timezone.utc).date().isoformat(),
            "sources": source_packet,
        }, ensure_ascii=False)),
    ]
    parts: list[str] = []
    for chunk in news_synthesis_llm.stream(messages):
        token = chunk.content if isinstance(chunk.content, str) else ""
        if not token:
            continue
        parts.append(token)
        emit_ui_event({"type": "chat_token", "token": token})
    answer = "".join(parts).strip()
    valid_citation = any(f"[{source['source_id']}]" in answer for source in sources)
    if not answer or not valid_citation:
        raise ValueError("news synthesis failed output contract")
    return _render_news_citations(answer, sources)


def _fallback_answer_from_tool_results(history: list) -> str:
    """Create grounded deterministic brief when normal synthesis is unavailable."""
    payload = _latest_exa_payload(history)
    if payload is None:
        return ""
    query = str(payload.get("query") or "latest company news")
    sources = _rank_news_results(payload)
    return _deterministic_news_brief(query, sources, error=payload.get("error"))


def _tool_message_has_error(message: ToolMessage) -> bool:
    try:
        payload = json.loads(str(message.content))
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("error"))


def _workflow_review_payload(message: ToolMessage) -> dict | None:
    """Read structured HITL control payloads, with legacy markdown support."""
    content = str(message.content or "")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        if payload.get("__dcf_hitl__"):
            return {**payload, "workflow": "dcf"}
        if payload.get("__deck_hitl__"):
            return {**payload, "workflow": "deck"}
    if "Draft Deck Outline" in content:
        return {"workflow": "deck", "summary": "Deck outline ready for review"}
    if "DCF Assumptions for" in content or (
        "STOP" in content and "assumption" in content.lower()
    ):
        match = re.search(r"DCF Assumptions for ([A-Z0-9.-]+)", content, re.IGNORECASE)
        return {
            "workflow": "dcf",
            "ticker": match.group(1).upper() if match else "?",
            "summary": "DCF assumptions ready for review",
        }
    return None


def _latest_news_query(user_text: str, state: dict) -> str:
    """Build direct micro-playbook query without another model round."""
    text = str(user_text or "").strip()
    entity_groups = [
        (state.get("turn_context") or {}).get("active_entities", []),
        [
            ref
            for card in (state.get("memory_context") or {}).get("retrieved_object_cards", [])
            for ref in (card.get("entity_refs") or [])
        ],
    ]
    for entities in entity_groups:
        for ref in entities:
            ticker = str(ref.get("ticker") or "").strip()
            name = str(ref.get("name") or "").strip()
            if ticker or name:
                return f"latest company news for {name or ticker} {ticker}".strip()
    from kg.query import resolve_known_company_ticker  # noqa: PLC0415

    ticker = resolve_known_company_ticker(text)
    if ticker:
        return f"latest company news for {ticker} stock"
    if re.search(r"\b(?:latest|recent|current|today|news)\b", text, re.IGNORECASE):
        if not re.search(r"\b(?:said company|the company)\b", text, re.IGNORECASE):
            return text
    return text or "latest company news"


def _can_use_latest_news_fast_path(selected_playbook: dict, required_outputs: list[str]) -> bool:
    return (
        selected_playbook.get("id") == "latest_company_news"
        and set(required_outputs) <= {"answer"}
    )


def _is_quota_error(exc: Exception) -> bool:
    """Detect rate-limit / quota exhaustion via error message."""
    msg = str(exc).lower()
    return "insufficient_quota" in msg or "rate limit" in msg or "429" in msg or "quota" in msg


def _is_timeout_error(exc: Exception) -> bool:
    """Detect provider/network timeouts without importing provider-specific classes."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in msg or "timed out" in msg or "readtimeout" in msg


_QUOTA_FALLBACK_MSG = (
    "OpenAI API quota exhausted (HTTP 429 insufficient_quota). "
    "Tool and workflow outputs produced before the failure remain saved, but final synthesis could not run. "
    "Add credits to the OpenAI account or configure a funded OPENAI_API_KEY."
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

    history: list = []
    try:
        result_message = _CAPABILITY_DISPATCHER.invoke([{
            "id": "dcf_approved_direct",
            "name": "run_dcf_workflow",
            "args": args,
            "type": "tool_call",
        }], surface="chat", context={"tool_execution_context": {"scope": "chat", "step_id": "chat"}},
            tool_overrides=_chat_tool_overrides() or None)[0]
        result = unwrap_tool_result_message(result_message).content
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
    if ready:
        lines.append(
            "Ready documents are already indexed. When the user asks about their content, "
            "call search_documents — do NOT ask the user to upload again."
        )
    elif pending:
        lines.append(
            "Documents are still indexing. If the user asks about their content, say indexing is not complete yet."
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


_DOC_CITATION_RE = re.compile(r"\[doc:[^\]\s]+\]")


def _doc_citations_from_payload(payload: dict) -> list[dict]:
    citations: list[dict] = []
    seen: set[str] = set()
    for chunk in payload.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        citation_id = str(chunk.get("citation_id") or "")
        if not citation_id.startswith("doc:") or citation_id in seen:
            continue
        seen.add(citation_id)
        citations.append({
            "citation_id": citation_id,
            "citation_label": chunk.get("citation_label") or chunk.get("source") or "",
            "source": chunk.get("source") or "",
            "page": chunk.get("page"),
        })
    return citations


def _doc_citations_from_history(history: list) -> list[dict]:
    """Extract uploaded-document citation IDs returned by search_documents."""
    citations: list[dict] = []
    seen: set[str] = set()
    for message in history:
        if not isinstance(message, (ToolMessage, HumanMessage)):
            continue
        try:
            payload = json.loads(str(message.content))
        except (json.JSONDecodeError, TypeError):
            text = str(getattr(message, "content", "") or "")
            if "search_documents result:" not in text:
                continue
            raw = text.rsplit("search_documents result:", 1)[-1].strip()
            raw = raw.split("\n\nNow answer", 1)[0].strip()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(payload, dict):
            continue
        for citation in _doc_citations_from_payload(payload):
            citation_id = citation["citation_id"]
            if citation_id in seen:
                continue
            seen.add(citation_id)
            citations.append(citation)
    return citations


def _ensure_doc_citations(final_text: str, history: list, citation_history: list | None = None) -> str:
    """One repair pass if answer used uploaded docs but omitted doc citations."""
    citations = _doc_citations_from_history(citation_history or history)
    if not citations or _DOC_CITATION_RE.search(final_text):
        return final_text

    allowed = json.dumps(citations[:12], ensure_ascii=False)
    repair_history = history + [
        HumanMessage(content=(
            "Revise the final answer below so every claim supported by uploaded documents "
            "has an inline citation marker using ONLY these exact citation_id values. "
            "Do not add new claims. Keep wording concise. Return only the revised answer.\n\n"
            f"Allowed citations:\n{allowed}\n\n"
            f"Final answer:\n{final_text}"
        ))
    ]
    try:
        repaired = llm.invoke(repair_history)
        repaired_text = repaired.content if isinstance(repaired.content, str) else ""
        if _DOC_CITATION_RE.search(repaired_text):
            return repaired_text
    except Exception:  # noqa: BLE001
        logger.warning("chat_node: document citation repair failed", exc_info=True)

    fallback_sources = " ".join(f"[{c['citation_id']}]" for c in citations[:4])
    return f"{final_text.rstrip()}\n\nSources: {fallback_sources}".strip()


def _chat_tools_for_policy(tool_policy: dict | None) -> list:
    if not isinstance(tool_policy, dict):
        return CHAT_TOOLS
    allowed = tool_policy.get("allowed_tools")
    if allowed is None:
        return CHAT_TOOLS
    allowed_set = {str(name) for name in allowed}
    return [tool for tool in CHAT_TOOLS if tool.name in allowed_set]


def _chat_llm_for_policy(tool_policy: dict | None):
    selected_tools = _chat_tools_for_policy(tool_policy)
    if not selected_tools:
        return llm
    if len(selected_tools) == len(CHAT_TOOLS):
        return chat_agent_llm
    return llm.bind_tools(selected_tools)


def _invoke_chat_tools(
    response: AIMessage,
    *,
    allowed_tool_names: set[str] | None = None,
    remaining_tool_calls: int | None = None,
    execution_context: dict | None = None,
) -> list[ToolMessage]:
    """Execute independent calls through compiled ToolNode batch concurrency."""
    calls = [{
        **tc,
        "id": str(tc.get("id") or ""),
        "name": str(tc.get("name") or ""),
        "args": _normalize_args(tc.get("args", {})),
        "type": "tool_call",
    } for tc in response.tool_calls]

    context = {
        **(execution_context or {}),
        "tool_execution_context": {
            "scope": "chat",
            "step_id": "chat",
            **((execution_context or {}).get("tool_execution_context") or {}),
        },
    }
    overrides = _chat_tool_overrides()
    batch_messages = _CAPABILITY_DISPATCHER.invoke(
        calls,
        surface="chat",
        context=context,
        allowed_capability_ids=(
            allowed_tool_names if allowed_tool_names is not None else set(CHAT_TOOLS_BY_NAME)
        ),
        max_calls=remaining_tool_calls,
        tool_overrides=overrides or None,
    )
    return [unwrap_tool_result_message(message) for message in batch_messages]


def _recover_missing_artifact_tools(
    *,
    required_outputs: list[str],
    current_turn: dict,
    allowed_tool_names: set[str] | None,
    remaining_tool_calls: int | None,
    execution_context: dict,
) -> tuple[AIMessage, list[ToolMessage]] | None:
    """Execute declared result-to-artifact dependency when ReAct stops early."""
    if "chart" not in required_outputs:
        return None
    resolved_results: list[tuple[str, str]] = []
    for result_id in current_turn.get("result_refs") or []:
        try:
            resolved = resolve_ref(str(result_id))
            stored = resolved.get("payload") if isinstance(resolved, dict) else None
            result_schema = str(stored.get("result_schema") or "") if isinstance(stored, dict) else ""
        except Exception:  # noqa: BLE001
            continue
        if result_schema:
            resolved_results.append((str(result_id), result_schema))

    render_request = artifact_render_request_for_results(resolved_results, "chart")
    if render_request is None:
        return None
    renderer = str(render_request["tool_id"])
    if allowed_tool_names is not None and renderer not in allowed_tool_names:
        return None

    # ReAct budget limits discretionary model-selected work. Declared output
    # materialization is a deterministic dependency and gets its own one-call
    # completion allowance after source results already exist.
    recovery_call = AIMessage(content="", tool_calls=[{
        "id": f"completion_chart_{renderer}",
        "name": renderer,
        "args": dict(render_request["args"]),
        "type": "tool_call",
    }])
    return recovery_call, _invoke_chat_tools(
        recovery_call,
        allowed_tool_names=allowed_tool_names,
        remaining_tool_calls=1,
        execution_context=execution_context,
    )


_FINANCIAL_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "free_cash_flow",
    "ebitda",
    "diluted_eps",
)
_GENERIC_ANALYST_PHRASES = re.compile(
    r"\b(detailed analysis showing|insights? into|overview of|comprehensive (?:revenue|financial) analysis|"
    r"evaluation of|summary of free cash flow)\b",
    re.IGNORECASE,
)


def _parse_stored_result(stored: dict) -> dict | list | None:
    raw = stored.get("result")
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _compact_synthesis_evidence(current_turn: dict, query: str) -> list[dict]:
    """Hydrate selected result refs into bounded evidence for final synthesis."""
    requested_years = set(re.findall(r"\b(?:19|20)\d{2}\b", query or ""))
    evidence: list[dict] = []
    for result_id in current_turn.get("result_refs") or []:
        try:
            resolved = resolve_ref(str(result_id))
        except (KeyError, ValueError):
            continue
        stored = resolved.get("payload") if isinstance(resolved, dict) else None
        if not isinstance(stored, dict):
            continue
        schema = str(stored.get("result_schema") or "")
        payload = _parse_stored_result(stored)
        if schema == "company_financial_history.v1" and isinstance(payload, dict):
            rows: list[dict] = []
            for row in payload.get("series") or []:
                if not isinstance(row, dict):
                    continue
                period = str(row.get("fiscal_year") or row.get("date") or row.get("calendar_year") or "")
                if requested_years and not any(year in period for year in requested_years):
                    continue
                compact_row = {
                    key: row.get(key)
                    for key in ("date", "fiscal_year", "calendar_year", "period", *_FINANCIAL_METRICS)
                    if row.get(key) is not None
                }
                if compact_row:
                    rows.append(compact_row)
            if not rows and not requested_years:
                rows = [
                    {
                        key: row.get(key)
                        for key in ("date", "fiscal_year", "calendar_year", "period", *_FINANCIAL_METRICS)
                        if row.get(key) is not None
                    }
                    for row in (payload.get("series") or [])[:6]
                    if isinstance(row, dict)
                ]
            evidence.append({
                "result_id": result_id,
                "tool_name": stored.get("tool_name"),
                "result_schema": schema,
                "ticker": stored.get("ticker") or payload.get("ticker"),
                "company_name": payload.get("company_name"),
                "currency": payload.get("currency"),
                "period": stored.get("period") or payload.get("period"),
                "series": rows[:6],
                "source_refs": (payload.get("source_refs") or [])[:8],
            })
        elif schema == "market_price_history.v1" and isinstance(payload, dict):
            evidence.append({
                "result_id": result_id,
                "tool_name": stored.get("tool_name"),
                "result_schema": schema,
                "ticker": stored.get("ticker") or payload.get("ticker"),
                "period": stored.get("period") or payload.get("period"),
                "start_date": payload.get("start_date"),
                "end_date": payload.get("end_date"),
                "adjusted_close_return_pct": payload.get("adjusted_close_return_pct"),
            })
    return evidence


def _synthesis_evidence_message(current_turn: dict, query: str) -> HumanMessage | None:
    evidence = _compact_synthesis_evidence(current_turn, query)
    if not evidence:
        return None
    return HumanMessage(content=(
        "Resolved evidence packet for current request follows. Use exact figures and periods from it. "
        "Result IDs are provenance pointers, not claims. Do not use placeholder section text. "
        "Answer requested deliverable; a written report does not imply a chart.\n\n"
        f"{json.dumps(evidence, ensure_ascii=False, default=str)}"
    ))


def _analyst_quality_gaps(
    required_outputs: list[str],
    final_text: str,
    current_turn: dict,
    query: str,
) -> list[str]:
    """Check answer substance against hydrated evidence and deliverable contract."""
    gaps = completion_gaps(
        required_outputs,
        final_text=final_text,
        artifact_paths=current_turn.get("artifact_paths") or [],
        artifact_refs=current_turn.get("artifact_refs") or [],
    )
    evidence = _compact_synthesis_evidence(current_turn, query)
    financial = [item for item in evidence if item.get("result_schema") == "company_financial_history.v1"]
    if financial:
        if _GENERIC_ANALYST_PHRASES.search(final_text) or not re.search(r"\d", final_text):
            gaps.append("grounded_analysis")
        tickers = {str(item.get("ticker") or "").upper() for item in financial if item.get("ticker")}
        if len(tickers) > 1 and any(ticker not in final_text.upper() for ticker in tickers):
            gaps.append("entity_coverage")
    if "report" in required_outputs and len(final_text.split()) < 80:
        gaps.append("report_depth")
    return list(dict.fromkeys(gaps))


def _chat_node_inner(state: dict) -> dict:
    messages = state.get("messages", [])
    session_memory = state.get("session_memory") or ""
    current_turn = dict(state.get("current_turn") or {})
    route_payload = current_turn.get("route") or state.get("route_decision") or {}
    required_outputs = list(
        current_turn.get("required_outputs")
        or route_payload.get("requested_outputs")
        or ["answer"]
    )
    current_turn.setdefault("required_outputs", required_outputs)
    current_turn.setdefault("result_refs", [])
    current_turn.setdefault("artifact_refs", [])
    current_turn.setdefault("artifact_paths", [])
    _session_ctx.set(state.get("session_id") or "")
    _restore_hitl_from_approval(messages)

    approved_context = state.get("approved_workflow_context") or {}
    if (
        approved_context.get("workflow_id") == "memo"
        and approved_context.get("triggering_text") == _latest_user_text(messages)
    ):
        company = str(approved_context.get("company_name") or "Investment").strip(" .")
        output_requirements = approved_context.get("output_requirements") or {}
        result_message = _CAPABILITY_DISPATCHER.invoke([{
            "id": "memo_approved_direct",
            "name": "run_memo_workflow",
            "args": {
            "brief": {
                "title": f"{company} investment memo",
                "company_name": company,
                "audience": output_requirements.get("audience") or "investment_committee",
                "objective": approved_context.get("user_intent") or "Produce decision-ready investment memo.",
                "focus_areas": approved_context.get("focus_areas") or [],
                "hitl_mode": "review",
            },
            "workflow_context": approved_context,
            "session_id": state.get("session_id") or "",
            },
            "type": "tool_call",
        }], surface="chat", context={"tool_execution_context": {"scope": "chat", "step_id": "chat"}},
            tool_overrides=_chat_tool_overrides() or None)[0]
        result = unwrap_tool_result_message(result_message).content
        emit_ui_event({
            "type": "execution_step",
            "stage": "dispatch_workflow_tool",
            "route_level": "workflow",
            "selected_workflow": "memo",
            "tool_calls": ["run_memo_workflow"],
            "status": "completed",
        })
        return {"messages": [AIMessage(content=str(result))]}

    # Approved DCF runs are deterministic — run them directly instead of letting
    # the ReAct loop improvise (it would re-trigger the assumption-review HITL
    # and fire redundant workflow calls). Returns None for non-approval turns.
    direct = _direct_dcf_approval(messages)
    if direct is not None:
        return direct

    system_content = _CHAT_SYSTEM
    system_content += _build_today_anchor()
    system_content += _build_user_settings_prompt(state.get("user_settings") or {})
    selected_playbook = state.get("selected_playbook") or {}
    tool_policy = state.get("tool_policy") or {}
    if selected_playbook.get("id") and selected_playbook.get("id") != "base":
        playbook_prompt = {
            "id": selected_playbook.get("id"),
            "kind": selected_playbook.get("kind"),
            "version": selected_playbook.get("version"),
            "summary": selected_playbook.get("summary"),
            "allowed_tools": selected_playbook.get("allowed_tools") or [],
            "tool_budget": selected_playbook.get("tool_budget") or {},
            "required_outputs": selected_playbook.get("required_outputs") or {},
            "validators": selected_playbook.get("validators") or [],
            "instructions": selected_playbook.get("instructions") or "",
        }
        system_content += (
            "\n\n## Selected runtime playbook\n"
            "Follow this selected playbook for the current request. "
            "Do not use tools outside its allowed_tools list. "
            "Do not exceed its tool budget.\n"
            f"{json.dumps(playbook_prompt, ensure_ascii=False)}"
        )
    doc_inventory = _build_doc_inventory(state.get("session_id") or "")
    if doc_inventory:
        system_content += doc_inventory
    if session_memory:
        system_content += f"\n\n## Prior research in this session\n{session_memory}"
    system_content += format_turn_context_prompt(state.get("turn_context"))
    system_content += format_memory_context_prompt(state.get("memory_context"))
    system_content += format_evidence_pack_prompt(state.get("evidence_pack"))
    approved_workflow_context = state.get("approved_workflow_context") or {}
    if approved_workflow_context.get("workflow_id"):
        system_content += (
            "\n\n## Approved workflow setup\n"
            "A workflow setup card was approved for the latest user request. "
            "Use the selected sources, objects, focus areas, and output requirements below. "
            "Do not ask for setup confirmation again. "
            "Do not treat this as long-term memory beyond the current request.\n"
            f"{json.dumps(approved_workflow_context, ensure_ascii=False)}"
        )
    deck_nudge = _build_deck_workflow_nudge(messages, state.get("user_settings") or {})
    if deck_nudge:
        system_content += deck_nudge
    latest_user_text = _latest_user_text(messages)
    doc_focused = _is_doc_focused_request(latest_user_text)
    ready_docs = _ready_docs_for_session(state.get("session_id") or "") if doc_focused else []
    forced_doc_search_done = False
    if doc_focused and ready_docs:
        system_content += (
            "\n\n## Current uploaded-document request\n"
            "The latest user request refers to an uploaded document. "
            "You MUST use search_documents before answering. Do not ask the user to upload again."
        )

    # ── KG state injection: tell the LLM what data it already has ────────
    # Injected into the first user message (not the system prompt) to preserve
    # KV-cache stability for the system prefix across different queries.
    messages_list = list(messages)
    if messages_list:
        last_user_index = next(
            (index for index in range(len(messages_list) - 1, -1, -1) if isinstance(messages_list[index], HumanMessage)),
            None,
        )
        if last_user_index is not None and isinstance(messages_list[last_user_index].content, str):
            last_user_msg = messages_list[last_user_index]
            kg_state = _build_kg_state_injection(last_user_msg.content)
            if kg_state:
                messages_list[last_user_index] = last_user_msg.model_copy(
                    update={"content": kg_state + "\n" + last_user_msg.content}
                )

    history = [SystemMessage(content=system_content)] + messages_list[-20:]
    turn_history_start = len(history)

    # Current-news micro turns have one deterministic action: search once and
    # render persisted sources. Calling an LLM here adds a second latency
    # budget before doing work whose route is already semantically resolved.
    if _can_use_latest_news_fast_path(selected_playbook, required_outputs):
        chat_t = agent_log.chat_start()
        emit_ui_event({"type": "chat_start"})
        query = _latest_news_query(latest_user_text, state)
        if "search_web" not in CHAT_TOOLS_BY_NAME:
            final_text = "Current-news search is unavailable."
        else:
            tool_messages = _invoke_chat_tools(
                AIMessage(content="", tool_calls=[{
                    "id": "search_web_direct",
                    "name": "search_web",
                    "args": {"query": query},
                    "type": "tool_call",
                }]),
                allowed_tool_names={"search_web"},
                remaining_tool_calls=1,
                execution_context={
                    "thread_id": state.get("thread_id") or "",
                    "session_id": state.get("session_id") or "",
                    "user_id": state.get("user_id"),
                },
            )
            history.extend(tool_messages)
            current_turn = merge_refs(current_turn, collect_refs(tool_messages))
            payload = _latest_exa_payload(history)
            sources = _rank_news_results(payload)
            if sources:
                try:
                    final_text = _synthesize_news_answer(query, sources)
                except Exception:  # noqa: BLE001
                    logger.warning("news synthesis failed; using grounded fallback", exc_info=True)
                    final_text = _deterministic_news_brief(
                        query,
                        sources,
                        error=payload.get("error") if isinstance(payload, dict) else None,
                    )
            else:
                final_text = _deterministic_news_brief(
                    query,
                    [],
                    error=payload.get("error") if isinstance(payload, dict) else None,
                )
        agent_log.chat_done(final_text, chat_t)
        emit_ui_event({"type": "chat_complete", "content": final_text})
        current_turn["response"] = {
            **(current_turn.get("response") or {}),
            "status": "complete" if final_text.strip() else "incomplete",
            "missing_outputs": [] if final_text.strip() else ["answer"],
            "artifact_refs": list(current_turn.get("artifact_refs") or []),
        }
        return {"messages": [AIMessage(content=final_text)], "current_turn": current_turn}

    _chat_t = agent_log.chat_start()
    emit_ui_event({"type": "chat_start"})
    used_tools = False
    chat_llm = _chat_llm_for_policy(tool_policy)
    allowed_tools = tool_policy.get("allowed_tools")
    allowed_tool_names = {str(name) for name in allowed_tools} if isinstance(allowed_tools, list) else None
    max_tool_calls = int(tool_policy.get("max_tool_calls") or 0)
    executed_tool_calls = 0
    fast_news_playbook = _can_use_latest_news_fast_path(selected_playbook, required_outputs)
    evidence_signature: tuple[str, ...] = ()
    quality_repair_used = False

    # ── ReAct loop ────────────────────────────────────────────────────────────
    for round_idx in range(MAX_CHAT_ROUNDS):
        try:
            response = chat_llm.invoke(history)
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
            if doc_focused and ready_docs and not forced_doc_search_done:
                forced_doc_search_done = True
                used_tools = True
                query = _doc_search_query(latest_user_text)
                result_message = _CAPABILITY_DISPATCHER.invoke([{
                    "id": "forced_document_search",
                    "name": "search_documents",
                    "args": {"query": query, "skip_gate": True},
                    "type": "tool_call",
                }], surface="chat", context={"tool_execution_context": {"scope": "chat", "step_id": "chat"}},
                    tool_overrides=_chat_tool_overrides() or None)[0]
                raw_result = unwrap_tool_result_message(result_message).content
                history.append(HumanMessage(content=(
                    "search_documents result:\n"
                    f"{raw_result}\n\n"
                    "Now answer the user's document request from these chunks. "
                    "Use exact [doc:...] citation markers for document-supported claims. "
                    "Do not ask the user to upload again."
                )))
                continue
            gaps = completion_gaps(
                required_outputs,
                final_text=response.content if isinstance(response.content, str) else "",
                artifact_paths=current_turn.get("artifact_paths") or [],
                artifact_refs=current_turn.get("artifact_refs") or [],
            )
            if gaps and round_idx < MAX_CHAT_ROUNDS - 1:
                remaining_tool_calls = None
                if max_tool_calls > 0:
                    remaining_tool_calls = max(0, max_tool_calls - executed_tool_calls)
                recovery = _recover_missing_artifact_tools(
                    required_outputs=required_outputs,
                    current_turn=current_turn,
                    allowed_tool_names=allowed_tool_names,
                    remaining_tool_calls=remaining_tool_calls,
                    execution_context={
                        "thread_id": state.get("thread_id") or "",
                        "session_id": state.get("session_id") or "",
                        "user_id": state.get("user_id"),
                    },
                )
                if recovery is not None:
                    recovery_call, recovery_messages = recovery
                    used_tools = True
                    history.append(recovery_call)
                    history.extend(recovery_messages)
                    current_turn = merge_refs(current_turn, collect_refs(recovery_messages))
                    executed_tool_calls += sum(
                        1
                        for message in recovery_messages
                        if not _tool_message_has_error(message)
                    )
                    emit_ui_event(make_execution_step(
                        "completion_recovery",
                        route_level=route_payload.get("route_level"),
                        playbook_id=route_payload.get("playbook_id"),
                        status="completed",
                        object_ids=current_turn.get("artifact_refs") or [],
                        reason=f"rendered_missing_outputs={','.join(gaps)}",
                        next_node="chat",
                    ))
                    continue
                history.append(HumanMessage(content=(
                    "Runtime completion check: missing requested outputs "
                    f"{json.dumps(gaps)}. Continue using available tools and result IDs. "
                    "Do not repeat completed work."
                )))
                emit_ui_event(make_execution_step(
                    "completion_gate",
                    route_level=route_payload.get("route_level"),
                    playbook_id=route_payload.get("playbook_id"),
                    status="incomplete",
                    object_ids=current_turn.get("artifact_refs") or [],
                    reason=f"missing_outputs={','.join(gaps)}",
                    next_node="chat",
                ))
                continue
            quality_gaps = _analyst_quality_gaps(
                required_outputs,
                response.content if isinstance(response.content, str) else "",
                current_turn,
                latest_user_text,
            )
            repairable_quality_gaps = [gap for gap in quality_gaps if gap not in gaps]
            if repairable_quality_gaps and not quality_repair_used and round_idx < MAX_CHAT_ROUNDS - 1:
                quality_repair_used = True
                evidence_message = _synthesis_evidence_message(current_turn, latest_user_text)
                if evidence_message is not None:
                    history.append(evidence_message)
                history.append(HumanMessage(content=(
                    "Quality check rejected prior draft for "
                    f"{json.dumps(repairable_quality_gaps)}. Rewrite it using actual evidence values, "
                    "cover every requested entity and deliverable, and remove generic placeholder prose. "
                    "Do not call more tools unless evidence is missing."
                )))
                emit_ui_event(make_execution_step(
                    "answer_quality_repair",
                    route_level=route_payload.get("route_level"),
                    playbook_id=route_payload.get("playbook_id"),
                    status="incomplete",
                    reason=f"quality_gaps={','.join(repairable_quality_gaps)}",
                    next_node="chat",
                ))
                continue
            # Final answer reached
            break

        # Process tool calls
        if response.tool_calls:
            used_tools = True
            # Pre-process workflow arguments before execution.
            for tc in response.tool_calls:
                if tc["name"] == "run_dcf_workflow":
                    args = _normalize_args(tc.get("args", {}))
                    context = state.get("approved_workflow_context") or {}
                    if context.get("workflow_id") == "dcf":
                        args["workflow_context"] = context
                        if context.get("ticker") and not args.get("ticker"):
                            args["ticker"] = context["ticker"]
                        if context.get("horizon_years") and not args.get("horizon_years"):
                            args["horizon_years"] = context["horizon_years"]
                    tc["args"] = args
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
                if tc["name"] == "run_memo_workflow":
                    args = _normalize_args(tc.get("args", {}))
                    context = state.get("approved_workflow_context") or {}
                    if context.get("workflow_id") == "memo":
                        args["workflow_context"] = context
                    tc["args"] = args

            remaining_tool_calls = None
            if max_tool_calls > 0:
                remaining_tool_calls = max(0, max_tool_calls - executed_tool_calls)
            tool_messages = _invoke_chat_tools(
                response,
                allowed_tool_names=allowed_tool_names,
                remaining_tool_calls=remaining_tool_calls,
                execution_context={
                    "thread_id": state.get("thread_id") or "",
                    "session_id": state.get("session_id") or "",
                    "user_id": state.get("user_id"),
                },
            )
            for message in tool_messages:
                if isinstance(message, ToolMessage):
                    try:
                        parsed = json.loads(str(message.content))
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    if not (isinstance(parsed, dict) and parsed.get("error")):
                        executed_tool_calls += 1
            history.extend(tool_messages)
            current_turn = merge_refs(current_turn, collect_refs(tool_messages))
            current_signature = tuple(current_turn.get("result_refs") or [])
            if current_signature != evidence_signature:
                evidence_message = _synthesis_evidence_message(current_turn, latest_user_text)
                if evidence_message is not None:
                    history.append(evidence_message)
                    evidence_signature = current_signature
                    emit_ui_event(make_execution_step(
                        "hydrate_result_evidence",
                        route_level=route_payload.get("route_level"),
                        playbook_id=route_payload.get("playbook_id"),
                        status="completed",
                        object_ids=list(current_signature),
                        reason="resolved compact synthesis evidence from result references",
                        next_node="chat",
                    ))

            # Post-process: check for DCF reports or HITL
            hitl_found = False
            for tm in tool_messages:
                if not isinstance(tm, ToolMessage):
                    continue
                if _workflow_review_payload(tm) is not None:
                    hitl_found = True
                    break
            if hitl_found or _extract_dcf_report(history):
                break  # HITL or DCF report found → exit the round loop
            if fast_news_playbook and any(
                isinstance(message, ToolMessage)
                and getattr(message, "name", "") == "search_web"
                and not _tool_message_has_error(message)
                for message in tool_messages
            ):
                # Micro current-news turns already have structured, persisted
                # source content. Do not spend another model round retrieving
                # or synthesizing the same result.
                break

    # ── Emit final response ───────────────────────────────────────────────────
    last_ai = next((m for m in reversed(history) if isinstance(m, AIMessage)), None)
    final_text = (last_ai.content if last_ai and isinstance(last_ai.content, str) else "") or ""

    review_payload = None
    for _m in reversed(history):
        if isinstance(_m, ToolMessage):
            review_payload = _workflow_review_payload(_m)
            if review_payload is not None:
                break

    if review_payload and review_payload.get("workflow") == "deck":
        # Server keeps SSE open for deck outline review (deck_hitl_payload).
        return {"messages": [AIMessage(content="Deck outline ready for review.")]}

    if review_payload and review_payload.get("workflow") == "dcf":
        # Do NOT emit chat_complete here — bridge already set status=awaiting_assumptions
        # from the dcf_assumptions_review event, and we need the SSE stream to stay open
        # so the user's /dcf-decision response can stream valuation events back.
        agent_log.chat_hitl(str(review_payload.get("ticker") or "?"))
        return {"messages": [AIMessage(content="DCF assumptions ready for review.")]}

    dcf_report = _extract_dcf_report(history)
    if dcf_report:
        final_text = dcf_report
    elif used_tools and not fast_news_playbook and not final_text.strip():
        history.append(HumanMessage(content=(
            "Now write the final answer from the tool results above. "
            "Do not call more tools. Do not list raw sources or links. "
            "Synthesize what happened, why it matters, and cite source names inline."
        )))
        streamed = _stream_final_answer(history)
        final_text = streamed if streamed.strip() else final_text

    if not final_text.strip() and not fast_news_playbook:
        history.append(HumanMessage(content="Use the available tool results above to produce the final answer now. Do not call more tools."))
        streamed = _stream_final_answer(history)
        final_text = streamed if isinstance(streamed, str) else ""
    if not final_text.strip():
        final_text = _fallback_answer_from_tool_results(history)
    if not final_text.strip():
        final_text = "I could not generate a final answer from the tool results. Check the backend log at agent_project/runs/server.log."

    final_text = _ensure_doc_citations(final_text, history, history[turn_history_start:])

    final_text = _strip_generated_artifact_markdown(
        final_text,
        list(current_turn.get("artifact_paths") or []),
    )
    agent_log.chat_done(final_text, _chat_t)
    final_gaps = _analyst_quality_gaps(
        required_outputs,
        final_text,
        current_turn,
        latest_user_text,
    )
    emit_ui_event(make_execution_step(
        "validate_answer",
        route_level=route_payload.get("route_level"),
        playbook_id=route_payload.get("playbook_id"),
        status="incomplete" if final_gaps else "completed",
        object_ids=(current_turn.get("artifact_refs") or []) + (current_turn.get("result_refs") or []),
        reason=f"quality_gaps={','.join(final_gaps)}" if final_gaps else "deliverables and grounding satisfied",
    ))
    if final_gaps:
        final_text = (
            f"{final_text.rstrip()}\n\n"
            f"Incomplete requested outputs: {', '.join(final_gaps)}."
        ).strip()
    complete_event: dict = {"type": "chat_complete", "content": final_text}
    if current_turn.get("artifact_paths"):
        complete_event["artifact_paths"] = list(current_turn["artifact_paths"])
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

    current_turn["response"] = {
        **(current_turn.get("response") or {}),
        "status": "incomplete" if final_gaps else "complete",
        "missing_outputs": final_gaps,
        "artifact_refs": list(current_turn.get("artifact_refs") or []),
    }
    return {"messages": [AIMessage(content=final_text)], "current_turn": current_turn}

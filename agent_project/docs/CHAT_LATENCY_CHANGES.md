# Chat Latency Optimization — Changes & Issues (2026-06-07/08)

---

## RESOLUTION (2026-06-08, later session)

The CRITICAL output-quality regression (#1) and staleness-metadata gap (#3) are
**fixed**, plus the date-blindness bug they masked, plus streaming for perceived
latency. Verified on "aws latest news and its financials for the year":
news = current (Jun 2026, web-sourced), financials = FY2025 (not stale FY2023),
latency 53.6s → 27.9s.

### Core engine — KG querying is now temporally aware
- **`kg/query.py`** — new `_fmt_temporal(node)` → `as_of=<period> age=<m/h/d>`;
  added a `recency` column to both serializers (`_serialize`, `_serialize_subgraph`);
  documented temporal fields + period-coexistence in `_KG_SCHEMA`. The reasoning
  LLM now SEES when each datapoint is from (was temporally blind).
- **`kg/deep_research.py`** — `HopDecision` gains `needs_external` + `external_reason`;
  planner prompt reframed ("KG is a MEANS, not the goal") with a temporal-honesty
  block; `run_deep_research` threads the signal out (empty graph ⇒ needs_external).
- **`tools.py`** — `query_knowledge_graph` surfaces `needs_external`/`external_reason`
  so the chat agent knows to supplement stale KG with web (still returns the best
  stale answer + flag, per design — agent merges cached + fresh).

### Agent orchestration — Layer C
- **C1** KG injection reheadered `## Background — cached KG data (a HINT, not the
  answer)` with per-item ⚠ stale flags (news >24h, financials period).
- **C2** softened "KG FIRST" → "fast cache; recent ⇒ use, stale/empty ⇒ web".
- **C3** mandatory web gap-fill + honor `needs_external`.
- **Date anchor** — `_build_today_anchor()` injects today's date + fiscal-year
  guidance. ROOT cause of the FY2023-financials miss: the agent was date-blind, so
  "financials for the year" had no anchor and fell back to the cached year.

### Streaming (#2 perceived latency)
- **`_stream_final_answer(history)`** streams the post-tool synthesis token-by-token
  via `chat_token`; ThinkingDots persist until the first token (no `chat_token`
  during tool-routing rounds). Falls back to non-streaming `invoke` on stream error.
  Not the DCF junk path (that was workflow narration, removed separately).

### Still open
- Conceptual (no-tool) direct answers not yet streamed (fast single call).
- Model routing for turn-1 (future-work #4), fast-mode toggle (#2).
- `retrieve_tool_result` pointer kept (Manus philosophy — intentional).

---

## ORIGINAL NOTES (pre-resolution)

## Summary

Attempted to reduce chat response latency (33-54s) for simple factual queries by making the agent aware of what data already exists in the Knowledge Graph. The changes improved routing but introduced an output quality regression: the agent over-trusts stale KG data and stops searching.

---

## What was changed

### 1. KG state injection (`conversational.py`)

**File**: `agent_project/graphs/conversational.py`

**New function**: `_build_kg_state_injection(query: str) -> str`

Before the ReAct loop starts, this function:
- Queries `storage.list_kg_nodes()` to discover all known tickers in the KG
- Uses regex whole-word matching to find which known tickers appear in the user's message
- For each found ticker, builds an f-string line like:
  ```
  AMZN · 12 news (latest 6h ago) · 2 filings · no financials · 1 prior DCF runs
  ```
- Injects into the **first HumanMessage** (not the SystemMessage) to preserve KV-cache stability
- Wrapped in try/except — never crashes the chat

**Injection point**: In `_chat_node_inner`, right before building `history`:
```python
kg_state = _build_kg_state_injection(last_user_msg.content)
if kg_state:
    last_user_msg.content = kg_state + "\n" + last_user_msg.content
```

### 2. ToolNode migration (replaced manual tool loop)

**Before**: Sequential for-loop through `response.tool_calls`, one tool at a time, with `track_tool` context managers per tool:
```python
for tc in response.tool_calls:
    with track_tool(...) as span:
        result = tool_fn.invoke(args)
    history.append(ToolMessage(...))
```

**After**: LangGraph prebuilt `ToolNode` with native parallel execution:
```python
from langgraph.prebuilt import ToolNode
chat_tool_node = ToolNode(CHAT_TOOLS)

# In the ReAct loop:
tool_result = chat_tool_node.invoke({"messages": [response]})
history.extend(tool_result["messages"])
```

**Activity telemetry preserved**: `track_tool` context managers wrap the ToolNode call — all tools enter before invoke, exit after, and summaries are populated from matching ToolMessage results by `tool_call_id`.

### 3. Prompt changes

| Change | Line | Effect |
|---|---|---|
| `query_knowledge_graph` tool description rewritten | ~75 | "For ANY company-specific question — check the KG FIRST" — too strong |
| `search_web` tool description | ~77 | "Always check KG first... only search_web for breaking news (<24h old)" |
| "This is chat mode" | ~95 | Explicitly states chat handles most queries |
| Tool batching instruction | ~118 | "When you need multiple independent sources, call them all in a SINGLE turn" |
| Never-duplicate rule | ~124 | "Never call the same tool more than once for the same ticker" |
| Financial source guidance | ~124 | "For financial metrics when KG is empty: use search_web, not fetch_sec_filing" |

### 4. DCF fact-safety fixes (separate topic, same session)

- `filter_user_assumption_overrides()` in `state.py` — strips Tier A canonical fields (base_revenue, shares_outstanding, net_debt) from user overrides
- Applied consistently across conversational.py, graph.py, assumptions.py, memo.py
- Fast path now requires HITL snapshot
- Net debt FMP fix (prefers financial debt over totalDebt)
- KG lookup fix (market_metric_fund instead of structured_fundamental)

---

## Results

| Metric | Before changes | After changes |
|---|---|---|
| **Latency** (AMZN news+financials) | ~33s (7 turns) | 37-54s (7 turns) |
| **Tool calls per query** | 3-8 | 1-2 |
| **Redundant fetches** | fetch_sec_filing ×3 | 0 |
| **Output quality** | Good — web search for current data | **Regressed** — trusts stale KG, skips web |

---

## Known Issues

### 1. Output quality regression (CRITICAL)

**Symptom**: Agent treats KG data as authoritative even when stale. User asks "latest Amazon news and financials" → agent queries KG → KG returns DCF-era data → agent reports "no recent qualitative news" and gives FY2023 financials. Never calls `search_web`.

**Root cause**: The KG-first prompt bias ("check KG FIRST for any company question" + "if KG has recent data: answer from it directly") is too strong. The agent interprets "data in KG" as "answer complete" instead of "cache hit, verify freshness."

**Example output** (from 2026-06-08 run):
> "Currently, there are no recent qualitative news updates specifically regarding Amazon. The latest quantitative analysis indicates an unfavorable outlook based on a Discounted Cash Flow (DCF) assessment... Amazon's Financials for FY2023..."

The financials are 2 years old. The agent should have called `search_web` for current data but didn't.

### 2. Latency not improved

**Symptom**: Despite ToolNode parallelization and KG state injection, latency is 37-54s (same or worse than baseline ~33s).

**Root cause**: The bottleneck is gpt-4o-mini's deliberation overhead — 4 LLM calls before the first tool execution. ToolNode can't help when the model only calls one tool per turn. The KG state injection removed one discovery turn but the model replaced it with more deliberation.

**LLM call breakdown** (typical run):
```
Turn 1 (3s): Model sees KG state, thinks about what to do
Turn 2 (2s): Still thinking, no tool calls yet
Turn 3 (5s): "I should query KG"
Turn 4 (11s): KG deep research executes (only actual work)
Turn 5 (2s): Sees KG result, thinks
Turn 6 (15s): Synthesis
Turn 7 (15s): Final answer
```

6 of 7 turns are LLM inference with no tool execution. Parallelization doesn't help here.

### 3. KG state injection lacks staleness metadata

The injection says `"12 news"` but doesn't tell the agent whether those news items are 2 hours old or 2 weeks old. Adding timestamps (`"12 news (latest 5d ago, oldest 30d ago)"`) would let the agent decide freshness.

### 4. ToolNode telemetry gap

Tool execution results are ToolMessage objects — the `tool_result_id` pointer pattern means agent still needs `retrieve_tool_result` for web search results. This adds one extra turn that could be eliminated by inlining short results.

---

## What to keep

- **ToolNode**: Correct architecture — parallel execution is the right approach even though the current model doesn't use it yet. As models improve (or we upgrade), this pays off.
- **KG state injection**: The concept is sound — telling the agent what it already knows saves discovery turns. The implementation needs staleness metadata.
- **Tool batching prompt**: Good for future model versions that support parallel tool_calls.
- **DCF fact-safety fixes**: Clean, consistent, correctly prevent canonical fact contamination.

## What to roll back / rebalance

- **KG-first prompt bias**: Soften from "check KG FIRST → answer from it" to "KG is a fast cache → check it → if data is recent and sufficient, use it → otherwise web search."
- **KG state injection wording**: Add staleness timestamps. Change header from "Knowledge Graph state" to "Background: KG cache status (verify freshness)."
- **Restore natural fallback behavior**: The original agent's "I don't know → I search → I synthesize" loop produced better answers. Don't eliminate thinking turns — eliminate redundant API calls.

---

## Future work

1. **Staleness-aware KG injection**: Add `as_of` / `created_at` timestamps to the injection lines
2. **Effort/fast-mode toggle**: User-configurable (max 3 turns, forced tool_choice on turn 1, forced synthesis on turn 3)
3. **Eliminate `retrieve_tool_result` for short results**: Inline web search results directly instead of requiring a pointer fetch
4. **Model routing**: Use larger/faster model for the first tool-selection turn, gpt-4o-mini for synthesis
5. **Fix HITL substeps in right sidebar**: Substeps from the DCF fast path don't render in chat mode after HITL approval (tracked in `HITL_SUBSTEPS_ISSUE.md`)

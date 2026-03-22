# Agent Project — Living Document

> **Purpose:** Single source of truth for architecture, decisions, and next steps.  
> **Update policy:** Edit this file whenever a feature is completed or a decision changes.  
> **Guiding start:** Follow **Guiding roadmap** (below) for implementation order — planner → graph → financial tools / paper trading — with **no real money** and **free-first** data.

---

## Project Overview

A LangGraph-based agent with a plan-then-execute flow, human-in-the-loop (HITL) approval, local Python execution for charts/data fetching, and a React + FastAPI streaming UI.

**Direction (evolving):** Move from **pure research** (text + citations) toward a **trading-style agent** that can take structured actions (paper orders, fundamentals, charts) and eventually proactive behaviors — always with **no real money** in dev (paper / simulation only) to stress-test **robustness** of the design.

**Stack:**
- **Orchestration:** LangGraph + LangChain
- **Model:** OpenAI `gpt-5-nano` (easily swappable)
- **Search:** Tavily (`search_web` tool)
- **Python execution:** Local subprocess execution via `execute_python` (`pandas`/`matplotlib`/`requests`/`yfinance`)
- **UI:** React 19 + Vite + Tailwind + Motion, backed by FastAPI SSE
- **Package manager:** `uv`

---

## Repository Layout

```
lca-reliable-agents/
├── agent_project/
│   ├── agent/           # Graph, nodes, prompts, executor, state
│   ├── tools/           # Tool modules and registry
│   ├── memory/          # Session memory node(s)
│   ├── utils/           # Persistence, formatting, UI event hooks
│   ├── server.py        # FastAPI backend + SSE orchestration
│   ├── ui/                # React/Vite frontend (Vite dev → proxies to FastAPI)
│   └── runs/            # Per-thread run dirs (plans, tool_results, artifacts)
├── pyproject.toml       # uv dependencies
└── AGENT_PROJECT.md     # ← this file
```

---

## Architecture

### Graph Flow

```
START → plan → review_plan (HITL interrupt) → execute_plan → synthesize → update_memory → END
                    ↓ (rejected)
                   END
```

### AgentState

```python
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]   # append-only per plan; reset on new plan
    session_memory: str          # persists across plans within a session
```

### Context Management (Manus-inspired)

- **Append-only `context_stack`:** after each step, a `{step_id, summary, tool_result_ids}` entry is pushed — never modified.
- **Full results on disk:** tool outputs saved as JSON under `runs/<thread_id>/tool_results/`.
- **Pointer pattern:** tools return a `tool_result_id` pointer; agent calls `retrieve_tool_result(id)` to fetch the full payload only when needed.

### Tools

| Tool | Purpose |
|---|---|
| `search_web` | Tavily web search → persists result, returns pointer |
| `calculator` | Safe math eval via `simpleeval` |
| `retrieve_context` | Look up a prior step's summary + tool_result_ids from saved plan |
| `retrieve_tool_result` | Fetch full content of any stored tool result by ID |
| `execute_python` | Run local Python for data fetching, analysis, and plot generation |
| `fetch_url` | Fetch a specific URL, extract main text, and return a pointer |

### Persistence Layout (per thread)

```
runs/<thread_id>/
├── plans/               # plan JSON snapshots (written on every state change)
├── tool_results/        # full tool output payloads (JSON, one file per call)
├── artifacts/           # downloaded sandbox files (PNG plots, etc.)
└── final_report.md      # synthesized markdown report
```

---

## React UI

### Frontend (`agent_project/ui/`)

The React app uses:
- React 19 + Vite + Tailwind + Motion
- A Perplexity-style dark layout with:
  - compact plan review card
  - live step timeline
  - nested tool-call rows
  - streamed final markdown report
  - inline artifact images served by FastAPI

### UI Event System (`utils/events.py`)

`emit_ui_event(event)` is called from `agent/executor.py` and `agent/nodes.py`.  
`server.py` bridges those events into Server-Sent Events (SSE) for the React app.

Events emitted:
- `step_start` — step begins (step_id, description, index, total)
- `tool_call_start` — tool invoked (tool_name, args_preview)
- `tool_call_end` — tool returned (tool_name, summary)
- `tool_error` — tool failed (tool_name, error)
- `step_complete` — step done (step_id, result_preview, tool_result_ids)
- `synthesis_start` / `synthesis_complete`

### HITL Flow

1. `POST /runs` invokes the graph until `review_plan`
2. Backend returns `{thread_id, plan}`
3. React shows the draft plan and approval buttons
4. `POST /runs/{thread_id}/resume` continues execution
5. `GET /runs/{thread_id}/events` streams step/tool/report events over SSE
6. React updates the step timeline and final report incrementally

---

## What's Working

- [x] Plan-then-execute flow with HITL approval
- [x] Append-only context stack (Manus-inspired)
- [x] All current tools functional, including `fetch_url`
- [x] Local Python execution for data + matplotlib
- [x] Artifact download + final report as markdown
- [x] React UI with plan review, step timeline, tool calls, and streamed report
- [x] Per-step highlighting + tool call streaming in UI
- [x] Show/Hide reasoning toggle after report completes
- [x] FastAPI backend for run orchestration, artifact serving, and SSE
- [x] Static system prompt for KV-cache (step 1 complete)
- [x] Multi-turn session memory (step 2 complete)

---

## Known Issues / Limitations

- ~~**KV-cache: NONE.**~~ Fixed in step 1 — static system prompt now fully cache-eligible.
- ~~**Single-turn only.**~~ Fixed in step 2 — persistent thread_id + session_memory across messages.
- **Latency:** Many plans use **4–5 sequential steps**; each step is one LLM+tool loop → total time stacks. Mitigation is in the **Guiding roadmap** (planner first, then graph parallelism).
- **Tool ceiling:** Complex financial work still often goes through `execute_python`; dedicated market/fundamental tools will improve quality and speed.
- **No document ingestion.** Agent can search/fetch the web, but still can't reason over uploaded PDFs/CSVs.
- **Global UI event handler is process-wide.** Fine for one active run, but needs per-thread routing for true concurrency.

---

## Guiding roadmap (implementation order)

> **Use this section as the single checklist for what to build next.** Update checkboxes and notes as work completes.

### Principles

| Principle | Meaning |
|-----------|--------|
| **No real money** | Paper trading / simulation / dry-run APIs only. Goal is **robustness** of flows (guards, HITL, audit), not funding a live strategy. |
| **Data: free → reliable** | Prefer **free** data/APIs first (e.g. `yfinance`, Stooq CSVs). When two free options exist, pick the **more reliable** for the task; document trade-offs in code comments. |
| **Planner before graph surgery** | Fix **over-decomposition** and step count **before** investing in parallel execution — bad plans get faster, not better, when parallelized. |

### Phase A — Planner & prompts `[DONE — v1]`

**Goal:** Fewer, denser steps; align plan length with task difficulty and available tools (one step can host multiple tool calls).

**Implemented:**

- **`PlanDraft`** (`agent/state.py`): `steps` is constrained to **1–5** items with a schema `description` that tells the model to prefer 2–4 steps and merge logical units.
- **`plan_node`** (`agent/nodes.py`): Replaced the old “**3–6 steps**” anchor with **`PLANNER_INSTRUCTIONS`** — step budget (2–4 default, 5 max), explicit **merge rules** (search→retrieve, fetch_url→retrieve, data+chart in one `execute_python`), and tool hints aligned with `STATIC_SYSTEM_PROMPT`.

**Still optional (if v1 isn’t enough):**

- Stronger structured fields (e.g. `complexity: low|medium|high` + post-process caps), or a **second-pass compress** if the model still returns 5 thin steps.

**Success:** Lower median wall-clock per run for typical queries **without** changing the graph yet — **measure** on a few representative queries before Phase B.

---

### Phase B — Graph & execution `[AFTER A]`

**Goal:** Cut latency where work is independent.

**Order of attack:**

1. **Parallel tool calls within a step** — when the model emits multiple tool calls in one turn, execute independent tools concurrently (thread pool or `asyncio`), then return all `ToolMessage`s. Big win, localized to `agent/executor.py` (and prompt nudges).
2. **Parallel steps (LangGraph)** — only where the plan’s dependency graph allows (e.g. two tickers with no shared step ordering). Requires richer routing / fan-out-fan-in; do after (1).

**Success:** Same quality as today with measurably shorter execute phase.

---

### Phase C — Financial & “doing” tools `[AFTER A OR IN PARALLEL WITH SMALL SCOPE]`

**Goal:** First-class primitives so the model rarely hand-writes fragile pandas in `execute_python` for standard tasks.

**Candidate tools (names indicative; refine in implementation):**

| Tool | Role | Data preference |
|------|------|-----------------|
| `fetch_stock_prices` | OHLCV for a ticker, date range, frequency | Free: yfinance and/or Stooq; same pointer pattern as other tools |
| `analyze_financials` (or split) | Key fundamentals / statement-derived metrics as structured JSON | Free tier APIs or yfinance statements; document limitations |

**Paper trading (still no real money):**

- Integrate a **paper** brokerage API (e.g. Alpaca paper) for place/cancel/list orders — only after tools + guards + HITL story are clear.
- Treat as **robustness** testing: idempotency, error paths, audit logs under `runs/<thread_id>/`.

---

### Phase D — Proactive agent `[DISCUSS BEFORE BUILDING]`

**Goal:** Agent that **initiates** work (watchlists, schedules, alerts), not only answers one-shot queries.

**Implications:** Persistent preferences, cron or event loop, possibly separate “job” graph — **design session recommended** before coding.

---

## Next Steps (historical / backlog)

**Note:** Prefer the **Guiding roadmap** for ordering new work. The sections below document completed milestones and a small backlog (RAG, error context, cost tracking) that can be scheduled alongside or after Phase A–C as needed.

### 1. ✅ Static system prompt for KV-cache  `[DONE]`

**Goal:** Separate stable rules (system prompt, never changes) from dynamic context (human message, changes per step).

**What changed in `file.py`:**
- Removed `build_system_prompt()` — it was fully dynamic, invalidating cache on every step.
- Added `STATIC_SYSTEM_PROMPT` module-level constant — identity, tool rules, output rules. Never changes at runtime. Fully KV-cache eligible.
- Added `build_step_message()` — all dynamic context (objective, plan trajectory, step info, context stack, feedback) formatted into the HumanMessage only.
- `execute_step()` now sends `[SystemMessage(STATIC_SYSTEM_PROMPT), HumanMessage(step_message)]`.

**Effect:** The system prompt prefix is identical across every step call in every run → near-100% KV-cache hit on the static prefix → ~10x cost reduction on cached tokens (e.g. Claude Sonnet: $0.30/MTok cached vs $3/MTok uncached).

---

### 2. ✅ Multi-turn session memory  `[DONE]`

**Goal:** Agent remembers prior research within a chat session; planner avoids repeating work.

**What changed:**

`file.py`:
- Added `session_memory: str` field to `AgentState` — persists across plans via MemorySaver.
- Added `update_memory_node` after `synthesize`: compresses the completed plan's objective + step findings into structured text (no LLM call; bounded to ~2000 chars).
- Graph edge: `synthesize → update_memory → END`.
- `plan_node` now reads `session_memory` and injects it into the planner prompt: *"Prior research completed in this session (use as context if relevant, don't repeat work already done)"*.

`app.py`:
- `thread_id` created once in `on_chat_start`, stored in `cl.user_session`, reused across all messages.
- Same `config` (same LangGraph thread) used for every `ainvoke` in the session.
- Each message still triggers a fresh plan (context_stack resets) but the planner sees prior findings.

**Design choices:**
- Memory is structured text, not an LLM summarization — zero latency, zero cost, deterministic.
- Truncation from the front keeps the most recent entries when memory exceeds 2000 chars.
- The planner is the gatekeeper: it decides what prior context is relevant to the new plan. Execution nodes never see raw memory — only what the planner chose to embed in step descriptions.

---

### 2b. Streaming UI  `[DONE]`

**Goal:** Stream the final report token-by-token into Chainlit, and make sure tool call events appear live during execution.

**What changed:**

`file.py` — `synthesize_node`:
- Replaced `llm.invoke()` with `llm.stream()` — iterates over chunks as they arrive.
- Each chunk emits a `synthesis_token` event via `emit_ui_event`.

`app.py`:
- `_process_event` now accepts a `ui_state` dict to track mutable state across events (e.g., the streamed report message).
- `synthesis_start` creates a `cl.Message` and stores it in `ui_state["report_msg"]`.
- `synthesis_token` calls `msg.stream_token(token)` to append each token live.
- `synthesis_complete` can now split the final report into `before artifacts` / `after artifacts` sections and place generated chart images between them.
- Approval prompt bubble is removed immediately after user action (`AskActionMessage.remove()`), so no extra "Selected: Approve" message remains in chat.
- Fallback logic still creates a regular report if streaming wasn't used.

**Design choices:**
- Report streaming is the highest-value UX improvement — users see the report build token-by-token.
- Tool-call/step events stream into `StepTracker` continuously through the event bridge.
- Artifact placement uses an explicit `[ARTIFACTS]` anchor when available, with a `before Limitations` fallback, so charts appear mid-report rather than only at the end.

---

### 3. Session-doc RAG  `[PENDING]`

**Goal:** Let users attach PDFs/CSVs in Chainlit; agent can search them with a new tool.

**Approach:**
- Chainlit file upload → LangChain document loaders → FAISS in-memory index per session
- New tool: `search_documents(query: str) → str` — same pointer pattern as `search_web`
- Store FAISS index in `cl.user_session`
- Tool description added to static system prompt

**Files to change:** `file.py` (new tool), `app.py` (file upload handling, index creation).

---

### 4. Error recovery in context  `[PENDING]`

**Goal:** Per Manus blog — leave failed tool calls in context rather than silently retrying. Improves self-correction.

**Approach:** Currently errors produce a JSON error payload that goes into `ToolMessage`. This is already correct — the issue is that failed steps set `step["status"] = "failed"` but don't influence future steps' context_stack. Add failed step summaries to context_stack with a `failed:` prefix so downstream steps know what was attempted.

**Files to change:** `file.py` — `execute_plan_node`.

---

### 5. Token / cost tracking  `[PENDING]`

**Goal:** Know how much each run costs.

**Approach:** `ChatOpenAI` returns `response.usage_metadata`. Accumulate across all LLM calls in a thread-local counter. Print at end of run. Optionally save to `runs/<thread_id>/usage.json`.

---

## Key Design Decisions (Log)

| Decision | Rationale |
|---|---|
| Append-only context_stack (not full message injection) | KV-cache friendly; follows Manus principle |
| Tool results stored on disk, pointer in message | Keeps context short; full data retrievable on demand |
| `execute_python` banned from HTTP fetching | Sandbox SSL issues; cleaner separation of concerns |
| React + FastAPI UI (primary) over ad-hoc UIs | SSE + typed events; Chainlit-era path removed once parity achieved |
| React step timeline + plan review | Full control over layout; streaming report and tool rows |
| FAISS (not ChromaDB) for RAG | Zero infrastructure; in-memory per session is sufficient for educational scope |

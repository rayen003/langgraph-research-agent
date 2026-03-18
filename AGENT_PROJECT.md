# Agent Project — Living Document

> **Purpose:** Single source of truth for architecture, decisions, and next steps.  
> **Update policy:** Edit this file whenever a feature is completed or a decision changes.

---

## Project Overview

A LangGraph-based research agent with a plan-then-execute flow, human-in-the-loop (HITL) approval, local Python execution for charts/data fetching, and a Chainlit UI.

**Stack:**
- **Orchestration:** LangGraph + LangChain
- **Model:** OpenAI `gpt-5-nano` (easily swappable)
- **Search:** Tavily (`search_web` tool)
- **Python execution:** Local subprocess execution via `execute_python` (`pandas`/`matplotlib`/`requests`/`yfinance`)
- **UI:** Chainlit 2.10 with custom React elements
- **Package manager:** `uv`

---

## Repository Layout

```
lca-reliable-agents/
├── agent_project/
│   ├── file.py          # Core agent: graph, tools, prompts, nodes
│   ├── utils.py         # Persistence helpers, Rich formatting, UI event hooks
│   ├── app.py           # Chainlit entrypoint
│   ├── server.py        # FastAPI backend (artifact serving, plan endpoint)
│   ├── runs/            # Per-thread run dirs (plans, tool_results, artifacts)
│   ├── chainlit.md      # Chainlit welcome message
│   └── public/
│       └── elements/
│           ├── PlanCard.jsx     # Collapsible plan card custom element
│           └── StepTracker.jsx  # Live vertical timeline custom element
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
| `execute_python` | Run Python in Daytona sandbox; download artifacts (plots) |

### Persistence Layout (per thread)

```
runs/<thread_id>/
├── plans/               # plan JSON snapshots (written on every state change)
├── tool_results/        # full tool output payloads (JSON, one file per call)
├── artifacts/           # downloaded sandbox files (PNG plots, etc.)
└── final_report.md      # synthesized markdown report
```

---

## Chainlit UI

### Custom React Elements (`public/elements/`)

**`PlanCard.jsx`**
- Collapsible card: "Execution Plan" + step count badge
- Status dot: draft (blue) → approved (green) → running (amber pulse) → completed (green)
- Expands to show numbered step list with dependency annotations

**`StepTracker.jsx`**
- Processing-card layout inspired by modern agent UIs
- Left-side `Step N` labels with color accents + larger right-hand step panels
- Tool calls live inside each step block, vertically stacked with result summaries and expandable details
- `args_preview` decoded to show query/expression inline (e.g., `→ "Apple latest news"`)
- "Show/Hide reasoning" toggle — auto-collapses when report is ready

### UI Event System (`utils.py`)

`emit_ui_event(event)` is called from `file.py` at key points.  
`set_ui_event_handler(callback)` registers the Chainlit async bridge.

Events emitted:
- `step_start` — step begins (step_id, description, index, total)
- `tool_call_start` — tool invoked (tool_name, args_preview)
- `tool_call_end` — tool returned (tool_name, summary)
- `tool_error` — tool failed (tool_name, error)
- `step_complete` — step done (step_id, result_preview, tool_result_ids)
- `synthesis_start` / `synthesis_complete`

### HITL Flow in app.py

1. `ainvoke` → hits `review_plan` interrupt → renders `PlanCard`
2. `AskActionMessage` → Approve / Reject buttons
3. On approve: `ainvoke(Command(resume=...))` via async event loop
4. Async queue bridges sync graph events → `_process_event()` → `_update_element()` → React re-render
5. Final report rendered with `cl.Image` elements for any artifacts

---

## What's Working

- [x] Plan-then-execute flow with HITL approval
- [x] Append-only context stack (Manus-inspired)
- [x] All 5 tools functional
- [x] Daytona sandbox for Python/matplotlib execution
- [x] Artifact download + final report as markdown
- [x] Chainlit UI with live custom elements (PlanCard, StepTracker)
- [x] Per-step blue highlighting + tool call streaming in UI
- [x] Show/Hide reasoning toggle after report completes
- [x] FastAPI backend for artifact serving
- [x] Static system prompt for KV-cache (step 1 complete)
- [x] Multi-turn session memory (step 2 complete)

---

## Known Issues / Limitations

- ~~**KV-cache: NONE.**~~ Fixed in step 1 — static system prompt now fully cache-eligible.
- ~~**Single-turn only.**~~ Fixed in step 2 — persistent thread_id + session_memory across messages.
- **No document ingestion.** Agent can only search the web; can't reason over user-provided PDFs/CSVs.
- **`execute_plan` is one monolithic graph node.** All steps run sequentially inside a single node, so LangGraph can't stream individual step updates — only the whole batch completes at once.
- **Synthesize node returns final answer but doesn't stream.** The final report appears all at once.

---

## Next Steps

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
| Chainlit over AG-UI/CopilotKit | Pure Python, native LangGraph integration, faster to build |
| CustomElement (React JSX) over TaskList for tracker | Full control over layout; supports live prop updates via `updateElement` |
| FAISS (not ChromaDB) for RAG | Zero infrastructure; in-memory per session is sufficient for educational scope |

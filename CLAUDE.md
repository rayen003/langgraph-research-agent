# Research Agent — North Star

> **Purpose:** Single source of truth for what's been built, what's broken, and where we're going.
> **Update policy:** Edit this file whenever a phase is completed or a decision changes.

---

## Project Vision

A financial-institution-grade research agent — think Rogo — that can:
- Conduct **deep research** across web, filings, and market data
- **Draft reports**, presentations, Excel models, and Word memos
- Run as a **multi-turn conversational assistant** with persistent memory
- Spawn **background agents** for long-running tasks while the user continues chatting
- Serve analysts, associates, and PMs at banks, PE firms, and asset managers

---

## What's Been Built

### Backend (`agent_project/`)

#### Subgraph modules — `graphs/`
- **`graphs/research.py`** — all research tools + nodes (plan, review_plan, execute_plan, synthesize, update_memory); includes `run_dcf_workflow` tool
- **`graphs/conversational.py`** — `chat_node`: streams direct answer, emits `chat_token`/`chat_complete` events; includes `run_dcf_workflow` for chat-mode DCF requests
- **`graphs/workflows/dcf.py`** — deterministic DCF LangGraph subgraph (`dcf_workflow_app`); see “DCF workflow” below

#### Parent graph — `file.py`
- **LangGraph graph:** `START → intent → [research path | chat path] → END`
- **Research path:** `intent → plan → review_plan (HITL) → execute_plan → synthesize → update_memory → END`
- **Chat path:** `intent → chat → END`
- **`intent_node`:** `gpt-4o-mini` for auto classification; passthrough for forced research/chat mode
- **AgentState:** messages, mode, resolved_intent, plan, objective, approved, review_feedback, context_stack, session_memory
- **7 tools:** `search_web` (Exa), `search_documents`, `calculator`, `retrieve_context`, `retrieve_tool_result`, `execute_python`, `run_dcf_workflow`

#### DCF workflow — `graphs/workflows/dcf.py`

Standalone LangGraph: `START → normalize_input → hydrate_fundamentals → build_assumptions → review_assumptions → [optional END if rejected] → collect_market_data → project_cashflows → compute_valuation → sensitivity → finalize → END`.

- **Canonical fundamentals (Tier A):** `base_revenue`, `shares_outstanding`, `net_debt` — primarily from **Financial Modeling Prep** (`/stable/` API, `FMP_API_KEY`), then **yfinance** fallback; external web/doc hints cannot overwrite Tier A once canonical data exists (`_filter_tier_a_conflicts`).
- **Assumption merge order:** defaults → web (Exa snippets, optional) → documents (RAG hybrid search) → canonical fundamentals (including Tier B hints such as `fcff_margin`, `tax_rate`) → `assumption_overrides`.
- **FCFF margin from FMP:** unlevered proxy `FCF + InterestExpense × (1 − effective tax) / revenue` via `_compute_fcff_components`; naive FCF/revenue only as lower-confidence fallback.
- **Profile priors:** `_classify_profile(sector, market_cap)` → buckets (`mega_cap_tech`, etc.) driving `_check_assumption_plausibility`; output sanity in `_check_valuation_sanity` (e.g. implied vs spot ratio, terminal value share of EV).
- **Trust outputs:** `assumption_flags`, `valuation_flags`, `confidence_label` (`high`/`medium`/`low`); persisted in `dcf_output.json` and `workflow_complete` SSE; tool wrappers use `summarize_dcf_payload()` so the LLM sees confidence + flag counts in the persisted tool summary.
- **HITL:** `assumption_review_mode` + LangGraph `interrupt`; server resumes via `POST .../assumptions-decision`. Sync agent path (`run_dcf_workflow_sync`) forbids review mode — use the workflow HTTP API for reviewed runs.
- **Static system prompt** — fully KV-cache eligible; all dynamic context in HumanMessage only
- **`execute_python` prelude** — `get_stock_data(ticker, period)` helper injected into every script; handles yfinance MultiIndex, auto_adjust, column normalization — model just calls the helper
- **Session memory** — deterministic compression of completed plans into structured text; persisted in SQLite by `session_id`; injected into planner/chat on later turns; no LLM call, bounded to 2000 chars
- **Synthesis streaming** — `llm.stream()` emits `synthesis_token` events token-by-token
- **Artifact placement** — `[ARTIFACTS]` marker in report → images inserted inline at correct position
- **Timeouts:** LLM `timeout=60`, `SANDBOX_EXEC_TIMEOUT=60`, `MAX_TOOL_ROUNDS=6`

#### Server — `server.py`
- **FastAPI** with SSE, run registry, and HITL endpoints
- `POST /runs` — creates thread, starts `_run_agent_task` as asyncio Task
- `GET /runs/{thread_id}/events` — SSE stream backed by SQLite event polling; ping keepalive every 25s
- `GET /runs/{thread_id}/events?after_id=N` — replays persisted events after `event_id=N`, then streams live events when the run is active
- `POST /runs/{thread_id}/decision` — resolves HITL `asyncio.Future` (approve/reject)
- `GET /runs/{thread_id}/plan` — latest plan JSON
- `GET /artifacts/{thread_id}/{filename}` — serves generated files (PNG, PPTX, etc.)
- **SQLite persistence** — `runs/agent.db` stores jobs, final reports, per-session memory, and uploaded document metadata
- **Durable event log** — `job_events` table stores append-only SSE payloads for reconnect/replay
- **Durable step state** — `job_steps` table mirrors plan step status, results, tool_result_ids, timings, and errors
- **Research resume** — approved/in-progress interrupted research jobs resume from the latest persisted plan; completed steps are skipped, unfinished steps are reset to pending
- **Durable jobs list** — `/jobs` is backed by SQLite, so completed/error/interrupted jobs survive server restarts
- **Report fallback** — `/runs/{thread_id}/report` reads from disk first, then SQLite-stored report content
- **Event bridge:** sync `emit_ui_event` → `loop.call_soon_threadsafe` → SSE queue
- **Full traceback** in error events so the frontend can display the actual crash
- **DCF workflow HTTP API** — `POST /workflows/dcf/runs`, `POST /workflows/dcf/runs/{thread_id}/assumptions-decision`, `GET /workflows/dcf/runs/{thread_id}/result`; SSE events include `workflow_started`, `workflow_step`, `workflow_complete` (with `confidence_label`, `flag_count` where applicable)

#### Utilities — `utils.py`
- Per-thread run directory: `runs/<thread_id>/{plans,tool_results,artifacts,context_items}/`
- SQLite database: `runs/agent.db`
- `persist_tool_result()` — writes full payload to disk, returns pointer JSON (keeps context short)
- `emit_ui_event` / `set_ui_event_handler` — sync event bus consumed by Chainlit or FastAPI bridge; **per-run isolation via `contextvars`** for thread id and UI handler (reduces cross-run clobbering vs old globals)
- Rich console formatting for terminal logs

#### Documents / RAG — `documents.py`
- **ChromaDB persistence** — chunks and embeddings stored under `runs/chroma/`
- **Raw upload persistence** — original files stored under `runs/uploads/<doc_id>/` for preview/download
- **SQLite metadata persistence** — `documents` table stores doc_id, filename, session_id, status, chunk/page counts, error, upload_path, created_at
- **Registry hydration** — `_doc_registry` loads from SQLite on import so documents survive backend restart
- **Backfill path** — if SQLite is empty but Chroma has chunks, metadata can be rebuilt from Chroma metadatas and upload files
- **Hybrid retrieval** — dense Chroma query + BM25 over candidates + RRF merge

### Frontend (`agent_project/frontend/`)

**Stack:** Vite + React + TypeScript + Tailwind CSS

#### Layout
- **Idle state:** centered hero with query input and example chips
- **Active state:** two-pane layout — report (left, flex-1) + execution sidebar (right, 360px fixed)

#### Components
| Component | Role |
|---|---|
| `useAgentRun.ts` | Core hook: SSE connection, state accumulation from events, approve/reject/reset |
| `App.tsx` | Root — idle hero vs two-pane split |
| `QueryInput.tsx` | Auto-resizing textarea, Enter to submit, Shift+Enter for newline |
| `ReportPane.tsx` | Streaming markdown, inline artifact placement at `[ARTIFACTS]` marker, skeleton loaders |
| `MarkdownRenderer.tsx` | Custom ReactMarkdown component map — h1/h2/h3, styled lists, tables, code blocks, links |
| `ExecutionSidebar.tsx` | Progress bar, step count, current step label, HITL approve/reject footer, error display |
| `StepCard.tsx` | Timeline step with animated status dot; tool labels via `lib/toolLabels.ts` (`getToolDisplay`, `cleanToolSummary`) |
| `ActivityTrace.tsx` | Collapsible audit trail for tool calls (chat + nested research tools) |
| `MessageThread.tsx` | On run completion, snapshots `toolTrace` / `researchSteps` into committed messages for auditing after synthesis |
| `lib/toolLabels.ts` | Human-readable labels for all tools and `workflow:*` substeps (e.g. DCF) |

#### Key UX details
- SSE `done` event closes `EventSource` gracefully before `onerror` fires (prevents false "Connection lost")
- `[ARTIFACTS]` / `[ARTIFACT]` / `[CHART]` markers split report → images render inline
- Blinking cursor during synthesis streaming
- Error traceback displayed in sidebar (not just "Execution failed")
- Tailwind custom animations: `pulse-ring`, `fade-up`, `slide-in`, `blink`

### Infrastructure
- **`start.sh`** — launches backend (`:8080`) + frontend (`:5174`) with colored prefixed logs, auto npm install, graceful `Ctrl+C` shutdown
- **No `--reload`** on uvicorn — prevents mid-run restarts when files are saved
- **`PYTHONUNBUFFERED=1`** — real-time backend logs
- **`uv`** for Python env management; vite proxy forwards `/runs`, `/artifacts`, `/health` to `:8080`

---

## Known Issues / Limitations

- **Concurrency:** thread id and UI handlers use **`contextvars`** (`utils.py`), improving isolation for overlapping runs; extreme parallelism or multi-worker backends are still not formally coordinated (single recommended worker for jobs).
- **`execute_plan` still runs as one graph node** — resume wrapper can continue from last completed step after restart, but the interrupted in-progress step restarts from scratch.
- **Exa search is better but still not enough** — current web tool returns highlighted excerpts; analyst-grade work still needs source-content fetching, SEC filings, market data, and citations.
- **Durable memory is basic** — session memory persists in SQLite, but there is no entity/fact graph or semantic retrieval across old reports yet.
- **Worker model is still single-process** — no DB claim lock / multi-worker coordination yet; only one backend process should run jobs.
- **Document ingestion does not resume** — SQLite preserves metadata/status, but an upload interrupted mid-embedding may remain `processing`/`error`; no retry queue yet.
- **Completed job report opens in new tab** — clicking a completed job in `JobsPanel` opens a Blob URL; not yet loaded into the main research view
- **No document generation** — no PPTX, DOCX, or XLSX output yet

---

## Roadmap

### Phase 1 — Intent Router + Conversational Mode `[COMPLETE]`

**Goal:** Agent can answer follow-up questions without spawning a full plan-execute cycle.

**What was built:**
- `graphs/research.py` — all research nodes (plan, HITL, execute, synthesize, memory)
- `graphs/conversational.py` — direct-answer `chat_node` with streaming tokens
- `file.py` — parent graph: `START → intent_node → [research | chat] → END`
- `intent_node` uses `gpt-4o-mini` for fast classification (auto mode) or passthrough (research/chat forced)
- `server.py` — extended: `mode` in RunRequest, `/jobs` endpoint, `/runs/{id}/report` endpoint
- Frontend: mode selector pill (Auto/Research/Chat), `ChatPane`, `JobsPanel`, `useJobs` hook
- Adaptive layout: chat mode → centered chat pane; research mode → two-pane with HITL
- Multi-turn chat: persistent `CHAT_THREAD_ID` per browser session; LangGraph MemorySaver accumulates context
- Background research: jobs panel shows all research runs; user can chat while research runs in background

---

### Phase 2 — Research Quality `[IN PROGRESS]`

**Goal:** Replace Tavily with a stack that can actually support financial research.

**New tools:**
| Tool | Source | Use case |
|---|---|---|
| `search_exa` | Exa API | Semantic search + highlighted excerpts; analyst reports, news |
| `fetch_sec_filing` | SEC EDGAR (free) | 10-K, 10-Q, 8-K, DEF 14A filings by ticker or CIK |
| `get_market_data` | Polygon.io or Alpha Vantage | Prices, fundamentals, options, earnings |
| `search_transcripts` | Earnings Whispers / Motley Fool | Earnings call transcripts |

**Changes needed:**
- Add source-content fetcher for exact URLs / Exa result IDs
- Add `fetch_sec_filing`
- Add `get_market_data` (FMP partially covers fundamentals for DCF; broader market-data tool still Phase 2)
- Add `search_transcripts`
- `.env`: add `POLYGON_API_KEY`

---

### Phase 3 — Background Agents + Jobs Panel `[PARTIALLY COMPLETE]`

**Goal:** User starts a long research task, continues chatting, gets notified when done.

**What was built (Phase 1 sprint):**
- `GET /jobs` endpoint returns persisted run states (thread_id, status, query, intent, created_at)
- `JobsPanel` component — floating button with badge showing running job count; opens list of research jobs
- Research runs tracked live in `_run_registry` and durably in SQLite
- User can type chat queries in the inline input shown during research execution
- `GET /runs/{thread_id}/report` endpoint to read completed report markdown
- SQLite-backed job persistence in `runs/agent.db`
- Append-only `job_events` table for durable SSE payloads
- `job_steps` table mirrors plan steps and allows resume from the latest completed step
- `/runs/{thread_id}/events?after_id=N` replays missed events and supports browser `Last-Event-ID`
- Completed/error/interrupted jobs survive server restarts
- Approved/in-progress interrupted research jobs auto-resume on startup
- Awaiting-approval research jobs survive restart and can be approved later
- Final reports are stored in SQLite as fallback content
- Stale running jobs are marked `interrupted` on startup before resume

**Still needed:**
- Push notification / toast when a background job completes
- Click a completed job → load its full report into the main research view (currently opens in new tab)
- Cancel/retry endpoints
- DB claim lock for multi-worker execution

---

### Phase 4 — Persistent Memory Store `[PARTIALLY COMPLETE]`

**Goal:** Agent remembers across sessions; can reference prior reports by name.

**Built:**
- SQLite database at `runs/agent.db`
- `session_memory` table stores deterministic per-session memory
- Completed research updates memory in `update_memory_node`
- Server injects stored memory into later runs by `session_id`

**Still needed:**
- Tables: `conversations`, `reports`, `entities`, `facts`
- `entities` — companies, people, tickers mentioned across all sessions
- `facts` — key assertions extracted from completed reports (with source report_id)
- Planner reads relevant entities + facts before building plan
- User can ask "what did we find about Apple last week?" → retrieval from store

---

### Finance workflows + copilot quality `[IN PROGRESS]`

**Goal:** Domain subgraphs (DCF first) with deterministic math, traceable steps, and narrative trust signals.

**Built:**
- Standalone DCF subgraph + agent tool `run_dcf_workflow`; FMP-first fundamentals; tiered assumption merge; flags + `confidence_label`; UI-friendly workflow step labels and collapsible activity traces.

**Still needed:**
- Sector-tuned default WACC / growth when no external hints; richer market snapshot than yfinance-only spot; surface flags/confidence in execution sidebar (data already in events/payload).
- Additional workflows (comps, LBO, deck generation) reusing `PROFILE_PRIORS`-style patterns.

---

### Phase 5 — Document Generation `[MEDIUM]`

**Goal:** Agent produces presentation-ready deliverables, not just markdown reports.

**Outputs:**
| Format | Library | Use case |
|---|---|---|
| `.pptx` | `python-pptx` | Pitch decks, comp tables, investment memos |
| `.docx` | `python-docx` | Credit memos, research notes, formal reports |
| `.xlsx` | `openpyxl` | DCF models, financial comps, data exports |

**Approach:**
- New `generate_document(format, content, template)` tool in `file.py`
- Templates stored in `agent_project/templates/`
- Artifacts served via existing `/artifacts/{thread_id}/{filename}` endpoint
- Frontend: download button on artifact cards

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Append-only context_stack | KV-cache friendly; Manus-inspired; full data on disk |
| Tool result pointer pattern | Keeps context short; retrieve on demand |
| Static system prompt | Identical across all steps → near-100% KV-cache hit |
| Deterministic memory compression | Zero latency, zero cost, no LLM summarisation |
| `get_stock_data()` prelude | Model-generated yfinance code is unreliable; inject a working helper |
| SSE + asyncio Queue | Clean async bridge; no WebSocket complexity |
| `done` event closes EventSource | Prevents false "Connection lost" from normal stream end |
| No `--reload` in production | Prevents mid-run uvicorn restarts |
| `python-pptx` over Google Slides CLI | No OAuth headache; runs locally; full control |
| Exa over Tavily | Better semantic search and excerpts; still needs full source-content fetch for deep work |
| FMP + yfinance for DCF levels | Canonical scale and margins from statements; web/docs only refine rates, not Tier A |
| Assumption / valuation flags | Deterministic “warn vs block” bands; `confidence_label` gates how hard the LLM should sell the number |

---

## Environment Variables (`.env` in `agent_project/`)

```
OPENAI_API_KEY=sk-proj-...
TAVILY_API_KEY=tvly-...          # legacy; no longer used by current search_web
EXA_API_KEY=...                  # current search
FMP_API_KEY=...                  # Financial Modeling Prep (DCF canonical fundamentals; /stable API)
POLYGON_API_KEY=...              # Phase 2
```

## Running

```bash
cd /Users/rayengallas/Project/langgraph-research-agent
./start.sh
# Backend:  http://localhost:8080
# Frontend: http://localhost:5174
```

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

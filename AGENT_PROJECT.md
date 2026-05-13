# Agent Project — Living Document

> **Purpose:** Single source of truth for architecture, decisions, and next steps.  
> **Update policy:** Edit this file whenever a feature is completed or a decision changes.

---

## Project Overview

A LangGraph-based research agent with a plan-then-execute flow, human-in-the-loop (HITL) approval, multi-turn conversational chat, local Python execution, DCF valuation workflow, and a React frontend.

**Stack:**
- **Orchestration:** LangGraph + LangChain
- **Model:** OpenAI `gpt-5-nano` (easily swappable)
- **Search:** Exa (`search_web` tool)
- **Python execution:** Local subprocess via `execute_python` (`pandas`/`matplotlib`/`requests`/`yfinance`)
- **Documents:** ChromaDB + BM25 hybrid RAG
- **DCF valuation:** Deterministic subgraph with SEC EDGAR, FMP/yfinance, CAPM WACC, sensitivity
- **UI:** Vite + React + TypeScript + Tailwind CSS
- **Package manager:** `uv`

---

## Repository Layout

```
agent_project/
├── file.py                # Parent graph: intent routing, state, compilation
├── tools.py               # Canonical tool definitions (shared by all subgraphs)
├── plan_store.py          # Single seam for plan persistence (disk + SQLite)
├── activity.py            # Unified activity-event contract
├── utils.py               # UI event bridge, tool result persistence, formatting
├── storage.py             # SQLite persistence (jobs, events, steps, sessions, docs)
├── documents.py           # RAG pipeline: upload → chunk → embed → ChromaDB
├── web_search.py          # Exa search client
├── server.py              # FastAPI backend (SSE, HITL, artifacts, jobs, DCF workflow endpoint)
├── app.py                 # Chainlit entrypoint
├── graphs/
│   ├── __init__.py
│   ├── research.py        # Research subgraph: plan, HITL review, execute, synthesize, memory
│   ├── conversational.py  # Chat subgraph: ReAct loop with streaming
│   └── workflows/
│       └── dcf/           # DCF valuation subgraph (13 modules)
│           ├── graph.py   # Graph wiring + public API
│           ├── state.py   # DCFState TypedDict + constants
│           ├── evidence.py    # Evidence assembly (5 tiers: filing > api > doc > news > web)
│           ├── fundamentals.py # FMP/yfinance fetchers
│           ├── sec_filings.py  # SEC EDGAR integration
│           ├── synthesis.py    # LLM semantic synthesis (CompanyState)
│           ├── memo.py         # LLM assumption memo (proposals + rationale)
│           ├── wacc.py         # CAPM WACC estimation
│           ├── valuation.py    # Deterministic FCFF math (project → PV → TV → equity)
│           ├── priors.py       # Profile priors + confidence breakdown
│           ├── review.py       # HITL assumption review gate
│           ├── assumptions.py  # Legacy regex-merge heuristics (unused)
│           └── activity.py     # DCF workflow activity emitters
├── frontend/              # Vite + React + TypeScript + Tailwind
│   └── src/
│       ├── App.tsx        # Root: idle hero vs two-pane
│       ├── types.ts       # Shared TypeScript contracts
│       └── components/    # QueryInput, ReportPane, ExecutionSidebar, StepCard,
│                           # ActivityTrace, DcfHitlSection, MessageThread, ChatBubble…
└── public/
    └── elements/          # Chainlit custom React elements (legacy)
```

---

## Architecture

### Graph Flow

```
START → intent
          │
      route_intent
        ↙         ↘
     chat        plan
      │            │
     END       review_plan (HITL interrupt)
                  │
            route_after_review
               ↙            ↘
       execute_one_step     END (rejected)
              │
        route_after_step
         ↙              ↘
  execute_one_step   synthesize
  (more pending)          │
                     update_memory → END
```

Each `execute_one_step` is a real LangGraph node invocation → per-step checkpointing, streaming, and interrupt support.

### AgentState

```python
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Routing
    mode: str           # "auto" | "research" | "chat" — user's selected mode
    resolved_intent: str | None  # "research" | "chat" — set by intent_node
    # Research subgraph fields
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]   # append-only per plan; reset on new plan
    # Shared memory (persists across turns in the same LangGraph thread)
    session_memory: str
    # RAG session scope — used to filter uploaded documents
    session_id: str
```

### Intent Classification

`intent_node` uses `gpt-4o-mini` for auto-classification (fast, cheap). User can force `"research"` or `"chat"` mode via a pill selector. Auto mode classifies from the last 6 messages of conversation history.

### Context Management (Manus-inspired)

- **Append-only `context_stack`:** after each step, a `{step_id, summary, tool_result_ids}` entry is pushed — never modified.
- **Full results on disk:** tool outputs saved as JSON under `runs/<thread_id>/tool_results/`.
- **Pointer pattern:** tools return a `tool_result_id` pointer; agent calls `retrieve_tool_result(id)` to fetch the full payload only when needed.

### Tools (canonical definitions in `tools.py`, shared by all subgraphs)

| Tool | Purpose |
|---|---|
| `search_web` | Exa semantic search → persists result, returns pointer |
| `search_documents` | ChromaDB + BM25 hybrid retrieval over uploaded PDFs/CSVs |
| `fetch_sec_filing` | Free SEC EDGAR 10-K/10-Q section extraction |
| `calculator` | Safe math eval via `simpleeval` |
| `retrieve_context` | Look up a prior step's summary + tool_result_ids from saved plan (research-only) |
| `retrieve_tool_result` | Fetch full content of any stored tool result by ID |
| `execute_python` | Run Python locally with `yfinance`/`matplotlib`/`pandas`/`requests` |
| `run_dcf_workflow` | Deterministic DCF valuation subgraph (shared across chat and research) |

### DCF Workflow — Unified Across Chat and Research

The canonical `run_dcf_workflow` tool (in `tools.py`) emits `dcf_assumptions_review` SSE events on HITL, storing the payload via `set_dcf_hitl_payload()`. Both chat and research modes use the **same** tool instance — zero duplication.

| Mode | Pause mechanism | Resume |
|------|----------------|--------|
| Chat | ReAct loop detects `"⛔ STOP"` in tool output → breaks loop | Server injects `[DCF_APPROVED]` message → new `ainvoke` |
| Research | `execute_one_step_node` detects `get_dcf_hitl_payload()` → calls `interrupt()` | `Command(resume={"approved": True, "assumption_overrides": {...}})` |

Both show the same `DcfHitlSection` component (assumptions table, EvidencePanel, ConfidenceBreakdownPanel, ImpliedWaccDetail, approve/reject buttons) via the shared `dcf_assumptions_review` SSE event on the frontend.

### PlanStore — Single Seam for Persistence

`plan_store.py` unifies the dual-write pattern (disk JSON + SQLite) behind three functions:

| Function | Does |
|----------|------|
| `save_plan(thread_id, plan)` | Disk write + SQLite `sync_job_steps` |
| `update_step(thread_id, plan, step_id, *, status, result, tool_result_ids)` | Mutates plan dict, saves to disk, updates SQLite row |
| `save_report(thread_id, session_id, objective, content)` | Disk write + SQLite `store_report` |

Callers never know about disk vs SQLite — one call does both. Tool results stay disk-only (`utils.persist_tool_result`), session memory stays SQLite-only.

### Persistence Layout (per thread)

```
runs/<thread_id>/
├── plans/               # plan JSON snapshots (written on every state change)
├── tool_results/        # full tool output payloads (JSON, one file per call)
├── artifacts/           # downloaded sandbox files (PNG plots, etc.)
└── final_report.md      # synthesized markdown report

runs/agent.db            # SQLite: jobs, job_events, job_steps, reports, session_memory, documents
runs/chroma/             # ChromaDB: document embeddings
```

### Activity Telemetry

Unified `ActivityEvent` contract (`activity.py`) describes every unit of agent work. Legacy `tool_call_start/end/error` and `workflow_step` events have been removed — the frontend has a single store and renderer (`ActivityTrace`).

---

## What's Working

- [x] Intent router with auto/forced research/chat modes
- [x] Plan-then-execute flow with HITL approval
- [x] Per-step LangGraph node execution → checkpointing, streaming, resume
- [x] Multi-turn chat with streaming tokens
- [x] Append-only context stack (Manus-inspired)
- [x] All 8 tools functional, defined once in `tools.py`, shared by all subgraphs
- [x] DCF valuation workflow with unified HITL across chat and research
- [x] PlanStore — single seam for plan persistence (disk + SQLite)
- [x] SEC EDGAR integration for free 10-K/10-Q extraction
- [x] Session-doc RAG (ChromaDB + BM25 hybrid)
- [x] Python execution with matplotlib artifacts
- [x] React frontend with streaming reports, DcfHitlSection, ActivityTrace, jobs panel
- [x] FastAPI backend with SSE, HITL endpoints, job resume on restart
- [x] Static system prompt for KV-cache
- [x] Multi-turn session memory
- [x] Unified activity contract (single event store/renderer)

---

## Known Issues / Limitations

- **No doc generation.** No PPTX, DOCX, or XLSX output yet.
- **Standalone DCF endpoint (`POST /workflows/dcf/runs`) HITL resume is broken.** The dedicated HTTP endpoint's `Command(resume=...)` path may not find saved interrupt state in MemorySaver. The agent-tool path (chat and research) works correctly.
- **Completed job report opens in new tab.** Clicking a completed job in `JobsPanel` opens a Blob URL; not yet loaded into the main research view.
- **Worker model is single-process.** No DB claim lock / multi-worker coordination yet.
- **Document ingestion does not resume.** Uploads interrupted mid-embedding may remain `processing`/`error`; no retry queue yet.
- **Exa search is good but not deep enough for financial research.** Still needs full source-content fetching and citations for deep work.

---

## Key Design Decisions (Log)

| Decision | Rationale |
|---|---|
| Append-only context_stack (not full message injection) | KV-cache friendly; follows Manus principle |
| Tool results stored on disk, pointer in message | Keeps context short; full data retrievable on demand |
| Static system prompt | Identical across all steps → near-100% KV-cache hit |
| Deterministic memory compression | Zero latency, zero cost, no LLM summarisation |
| One tool definition in `tools.py`, shared by all subgraphs | Eliminates duplication; single source of truth |
| `execute_one_step_node` loop in parent graph (not subgraph) | Per-step checkpointing, streaming, and interrupt — not possible with monolithic node |
| PlanStore wraps disk + SQLite behind one seam | No dual-write bugs; swap adapter for tests or Postgres |
| DCF HITL unified across chat and research | Same tool, same events, same frontend components — different pause/resume mechanism per mode |
| Chat uses ReAct break for DCF HITL; research uses `interrupt()` | Chat is free-form conversation; research is step-based. Both benefit from their native pause pattern |
| Exa over Tavily | Better semantic search and excerpts |
| FMP + yfinance for DCF levels | Canonical scale and margins from statements; web/docs only refine rates |

---

## Environment Variables

`.env` in `agent_project/`:

```
OPENAI_API_KEY=sk-proj-...
EXA_API_KEY=...                    # web search
FMP_API_KEY=...                    # Financial Modeling Prep (DCF fundamentals)
# Optional — DCF CAPM calibration (defaults 0.045 / 0.055 if unset)
DCF_RISK_FREE_RATE=0.045
DCF_EQUITY_RISK_PREMIUM=0.055
# Optional — DCF LLM model overrides (defaults to gpt-4o)
DCF_SYNTHESIS_MODEL=gpt-4o
DCF_MEMO_MODEL=gpt-4o
```

---

## Running

```bash
cd /Users/rayengallas/Project/langgraph-research-agent
./start.sh
# Backend:  http://localhost:8080
# Frontend: http://localhost:5174
```

`start.sh` launches the FastAPI backend (`:8080`) and Vite dev server (`:5174`) with colored prefixed logs, auto npm install, and graceful `Ctrl+C` shutdown. `PYTHONUNBUFFERED=1` for real-time backend logs. No `--reload` on uvicorn — prevents mid-run restarts.

### LangSmith Studio

Root `langgraph.json` registers two graphs. From the **repo root**:

```bash
uv sync --extra studio
uv run --extra studio langgraph dev
```

| Graph ID | Module | What you see |
|----------|--------|--------------|
| `agent` | `file.py:app` | Full agent — intent → research path or chat path |
| `dcf_workflow` | `graphs/workflows/dcf:dcf_workflow_app` | Standalone DCF subgraph |

Studio UI: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`. Set `LANGSMITH_TRACING=false` in `agent_project/.env` if you don't want traces sent to LangSmith.

---

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore the codebase.** The graph is faster, cheaper (fewer tokens), and gives you structural context (callers, dependents, test coverage) that file scanning cannot.

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

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

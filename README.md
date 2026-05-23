# Agent Project

> **Single source of truth.** Architecture, design decisions, features, testing, roadmap.
> Update this file whenever a feature ships or a decision changes.

---

## Table of Contents

1. [What this is](#what-this-is)
2. [Quick start](#quick-start)
3. [Architecture overview](#architecture-overview)
4. [Modes](#modes)
5. [Workflows](#workflows)
6. [Tools](#tools)
7. [DCF workflow (deep dive)](#dcf-workflow-deep-dive)
8. [State + persistence](#state--persistence)
9. [Knowledge Graph layer](#knowledge-graph-layer)
10. [Frontend](#frontend)
11. [Code structure](#code-structure)
12. [Testing](#testing)
13. [Configuration](#configuration)
14. [Design decisions](#design-decisions)
15. [What works](#what-works)
16. [Known limitations](#known-limitations)
17. [Future work](#future-work)
18. [Development](#development)

---

## What this is

LangGraph-based research agent for financial analysis. Core capability: **deterministic DCF valuation with LLM-assisted assumption derivation, scenario modelling, and adversarial self-review**. Wrapped in a plan-then-execute orchestration layer with multi-turn chat, human-in-the-loop approval, and a React frontend.

**Three guiding principles:**

1. **LLMs find problems, Python applies fixes.** Every numeric output passes through deterministic math. LLM never mutates floats directly.
2. **Evidence-grounded.** Every assumption has a provenance trail (source URL, page, confidence). UI surfaces this on click.
3. **Cache-first.** Knowledge Graph layer caches everything expensive (fundamentals, theses, syntheses) with TTL + confidence floor. Repeat DCF runs are nearly free.

**Stack:**

| Layer | Choice |
|-------|--------|
| Orchestration | LangGraph + LangChain |
| LLM | OpenAI `gpt-4o` family (configurable per node via env) |
| Web search | Exa (`search_web` tool) |
| Python exec | Local subprocess (`pandas`, `matplotlib`, `yfinance`, `requests`) |
| Documents | ChromaDB + BM25 hybrid RAG |
| Fundamentals | Financial Modeling Prep API + yfinance fallback |
| Filings | SEC EDGAR (free, no key) |
| Persistence | SQLite + disk JSON + ChromaDB |
| Backend | FastAPI + SSE |
| Frontend | Vite + React + TypeScript + Tailwind |
| Package mgr | `uv` |

---

## Quick start

```bash
# from repo root
uv sync
cp agent_project/.env.example agent_project/.env  # fill in OPENAI_API_KEY, EXA_API_KEY, FMP_API_KEY
./start.sh
# backend  → http://localhost:8080
# frontend → http://localhost:5174
```

`start.sh` kills stale processes on ports 8080 + 5174-5178, clears `__pycache__`, then launches uvicorn + Vite with coloured logs and graceful shutdown.

To run a DCF programmatically:

```python
from agent_project.graphs.workflows.dcf.graph import run_dcf_workflow_sync
result = run_dcf_workflow_sync(ticker="AAPL", horizon_years=5)
print(result["valuation"]["implied_share_price"])
```

---

## Architecture overview

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

Parent graph (`agent_project/file.py`) routes between **chat** and **research** subgraphs based on intent classification. Research mode runs plan-then-execute with HITL plan approval. Each `execute_one_step` is a real LangGraph node invocation → per-step checkpointing, streaming, interrupt support.

Subgraphs:
- `research.py` — plan → HITL review → execute → synthesize → memory
- `conversational.py` — ReAct loop with streaming
- `workflows/dcf/` — DCF subgraph (16 nodes, deep-dive below)

---

## Modes

The system has three orthogonal mode dimensions:

### 1. Intent mode (user choice or auto-classified)

| Mode | When | Behaviour |
|------|------|-----------|
| `auto` | Default | `intent_node` classifies last 6 messages with `gpt-4o-mini` → routes to chat or research |
| `research` | User forces | Plan-then-execute with HITL plan approval |
| `chat` | User forces | Streaming ReAct loop, can still call tools (including DCF) |

### 2. DCF execution mode

| Mode | Trigger | Effect |
|------|---------|--------|
| `assumption_review_mode=True` | Default for full workflow | Runs to assumption review gate, returns HITL payload |
| `assumption_review_mode=False` | After approval / programmatic | Auto-approves, runs to completion |
| Fast path | All 8 assumption fields provided in `assumption_overrides` | Skips evidence/synthesis/memo, uses `dcf_valuation_app` (math only) |

### 3. KG cache mode (transparent)

`cache_check_node` probes the Knowledge Graph for cached fundamentals, evidence, synthesis, thesis. Hits → downstream nodes short-circuit. Misses → full LLM/API path runs and writes back on `finalize`.

---

## Workflows

### Research workflow (`graphs/research.py`)

Plan-then-execute with append-only context stack (Manus-inspired):
1. **Plan node** — LLM produces a structured plan (list of steps, each with goal + tools).
2. **HITL review** — user approves, edits, or rejects plan.
3. **Execute one step** — single LangGraph node invocation per step. Streams tool calls + activity events. Can interrupt for DCF HITL.
4. **Synthesize** — LLM combines step results into a markdown report.
5. **Update memory** — session memory captured for next conversation.

Tool results stored on disk (`runs/<thread_id>/tool_results/`) as JSON; pointer (`tool_result_id`) goes into the agent message. `retrieve_tool_result(id)` fetches full payload only when needed → keeps context short.

### Chat workflow (`graphs/conversational.py`)

Streaming ReAct loop. Can call any tool including `run_dcf_workflow`. DCF HITL detected via `⛔ STOP` sentinel in tool output → loop breaks → server injects `[DCF_APPROVED]` on resume → new `ainvoke`. When a DCF run completes, the chat path returns the **verbatim markdown report** from `summarize_dcf_payload()` — the LLM does not rewrite it.

### DCF workflow (`graphs/workflows/dcf/`)

Deterministic valuation subgraph. See [DCF deep dive](#dcf-workflow-deep-dive).

---

## Tools

All tool definitions live in `tools.py` — **shared by all subgraphs**, no duplication.

| Tool | Purpose |
|------|---------|
| `search_web` | Exa semantic search → persists result, returns pointer |
| `search_documents` | ChromaDB + BM25 hybrid retrieval over uploaded PDFs/CSVs |
| `fetch_sec_filing` | Free SEC EDGAR 10-K/10-Q section extraction |
| `calculator` | Safe math eval via `simpleeval` |
| `retrieve_context` | Look up a prior step's summary + tool_result_ids from saved plan |
| `retrieve_tool_result` | Fetch full content of any stored tool result by ID |
| `execute_python` | Run Python locally with `yfinance`/`matplotlib`/`pandas`/`requests` |
| `run_dcf_workflow` | Deterministic DCF with scenarios, thesis, review loop |

---

## DCF workflow (deep dive)

The most complex subgraph and the main feature. 16 nodes organised into three layers.

### Graph flow

```
START → normalize_input → cache_check ──┐    (KG cache hits skip downstream)
    │                                    ├─→ formulate_thesis (if synthesis cached)
    └─→ assemble_evidence → semantic_synthesis → formulate_thesis
    → propose_assumptions → scenario_generator
    → review_assumptions (HITL interrupt)
    → scenario_runner → project_cashflows → compute_valuation
    → compute_market_signals → sensitivity
    → review_subgraph ──────────────────────────────────────────┐
        ├── review_deep_dive (adversarial LLM — findings only)  │
        └── synthesize_adjustments (deterministic Python)       │
    → detect_divergences → analysis → convergence_gate           │
        ├── scenario_runner (loop, max 2 iterations) ───────────┘
        └── finalize → kg_backwrite → END
```

### Three layers

**Evidence layer** — `assemble_evidence` → `semantic_synthesis`
Turns messy sources (SEC filings, FMP API, web, uploaded docs, market data) into structured company understanding. Each evidence item has a tier (`FILING`, `API`, `WEB`, `DOC`, `MKT`) and is sent as a lightweight preview in SSE events for UI inspection.

**Thesis layer** — `formulate_thesis` → `propose_assumptions` → `scenario_generator` → `review_assumptions`
Forms investment thesis (bull/bear narrative + key drivers), derives base-case assumptions with full provenance, generates bear/base/bull scenarios with monotonicity validation, presents to user for HITL approval.

**Valuation + review layer** — `scenario_runner` → math → `review_subgraph` → finalize
Runs the deterministic math pipeline per scenario, then enters adversarial review loop (max 2 iterations or delta < 0.5% convergence). Finalize emits markdown report + writes Knowledge Graph artefacts.

### Key sub-systems

**Scenario modelling** — `scenarios.py`
LLM generates bear + bull variants from base case + thesis. Monotonicity check (`_violates_monotonicity`): for each scenario field, bull ≥ base ≥ bear must hold. Probabilities sum to 1.0. Expected value = Σ(probability × implied_price); range = min–max.

**Adversarial review subgraph** — `review_graph.py`, `review_state.py`
Isolated `ReviewState` (one-way snapshot from `DCFState`, can't pollute upstream). Two nodes:
- `review_deep_dive_node` — one LLM call, finds contradictions across three layers: evidence↔memo, thesis↔assumptions, market signals. **Receives no valuation output** — prevents backward anchoring. Structured output via `ReviewFindings` Pydantic model.
- `synthesize_adjustments_node` — deterministic Python. Votes across findings, applies convergence damping (same-direction repeat adjustments halved), hard-clamps via `_FIELD_CLAMP`. **LLM never mutates numbers.**

**Convergence gate** — `analysis.py`
`detect_divergences_node` finds unexplained gaps between scenarios and base. `analysis_node` runs LLM critique of any unexplained divergences. `convergence_gate_node` sets `reconciliation_status: structural_gap` when market-implied WACC/growth/margin diverge from model assumptions but the DCF math is intact — the model stays **`valid`**. Only solver failures or critical unresolved issues flip `model_validity` to **`invalid`**.

**Confidence breakdown** — `priors.py`
Per-component scoring (`data_quality`, `revenue_growth`, `margin_stability`, `wacc_reliability`, `terminal_assumptions`) weighted to aggregate `[0,1]` score → label `high/medium/low`. Validity gate multiplies aggregate when `model_validity == invalid` (×0.3) or `adjusting` (×0.7). Unexplained divergences subtract up to 0.20.

**Profile priors** — `priors.py`
Sector-aware plausibility bands. Profiles: `mega_cap_tech`, `large_cap_tech`, `mature_consumer_or_industrial`, `default`. Each field gets `{soft_min, soft_max, hard_min, hard_max}` — soft violations → `warn` flag, hard violations → `block` flag (forces confidence to low).

**Assumption journey** — `finalize_node`
When review loop ran, emits per-scenario diff table (initial assumptions → iteration 1 → iteration 2 → final) with field-level deltas. Surfaced in UI as `AssumptionJourneyDetail` panel.

### DCF report delivery

After HITL approval, the fast path (`dcf_valuation_app`) runs deterministic math only. The full workflow already collected evidence, thesis, scenarios, and memo during the review pass.

**HITL snapshot restore.** On approval, the server stores a full snapshot (`hitl_snapshot.py`) — evidence items, provenance, scenarios, thesis, company state — and restores it before the fast-path re-invoke. Without this, post-approval runs lose `[n]` citations and fall back to `user_provided` provenance.

**Report builder.** `payload.summarize_dcf_payload()` assembles the user-facing markdown:

| Section | Notes |
|---------|-------|
| Company Profile / Recent Developments | From evidence pack + FMP profile |
| Executive Summary | Validity, reconciliation posture, implied price vs spot |
| Sensitivity Matrix | WACC × TGR table + `[SENSITIVITY_CHART]` marker (before thesis) |
| Investment Thesis / Analysis Journey | Structured bullets |
| Assumptions | `\| Field \| Value \| Basis \| Refs \|` with inline `[n]` citations |
| WACC Decomposition / Market Reconciliation | Gap table vs market-implied signals |
| Assumption Rationale / Company Context | Bullets + `###` subsections for risks/drivers/conflicts |
| Valuation Detail / Consistency Checks | Table + bullet checks |
| References | Bulleted, hyperlinked sources (SEC, web, FMP summary) |

LLM-only instruction blocks (e.g. “do not say overvalued”) are emitted only when `for_display=False` — never shown to users.

**Source registry.** `sources.py` assigns stable `[1]`, `[2]`, … numbers via `SourceRegistry`. References link to original URLs where available (SEC EDGAR, Exa web excerpts) or FMP company summary for structured fundamentals.

**Artifacts.** `sensitivity_node` writes `artifacts/sensitivity_{ticker}.png`. The UI renders it inline at the `[SENSITIVITY_CHART]` marker. Artifact paths are emitted on `chat_complete`.

**Download.** Report card footer has a PDF / Markdown dropdown:

| Endpoint | Returns |
|----------|---------|
| `GET /runs/{thread_id}/dcf-report.md` | Markdown from `dcf_output.json` |
| `GET /runs/{thread_id}/dcf-report.pdf` | ReportLab PDF with wrapped tables + embedded heatmap |
| `GET /artifacts/{thread_id}/{filename}` | PNG and other run artifacts |

PDF export lives in `report_export.py`. Assumptions and reconciliation tables use fixed column widths and word-wrapped cells so content stays within A4 margins.

### Three compiled graphs

`graph.py` compiles three variants from the same node set:

| Graph | When | Composition |
|-------|------|-------------|
| `dcf_workflow_app` | Main | Full 16-node graph with HITL interrupt + review loop |
| `dcf_valuation_app` | Fast path (post-HITL, all assumptions known) | Math + review loop; skips evidence/synthesis/memo. Does **not** re-run `scenario_runner` (avoids chat token pollution). |
| `dcf_scenario_val_app` | Per-scenario inside `scenario_runner` | Math only, no analysis loop |

---

## State + persistence

### AgentState

```python
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    mode: str           # "auto" | "research" | "chat"
    resolved_intent: str | None
    plan: dict | None
    plan_path: str | None
    objective: str
    approved: bool
    review_feedback: str | None
    context_stack: list[dict]   # append-only per plan; reset on new plan
    session_memory: str
    session_id: str
```

### DCFState (abbreviated)

```python
class DCFState(TypedDict):
    ticker: str
    horizon_years: int
    assumptions: dict[str, float]
    assumption_provenance: dict[str, dict[str, Any]]
    scenarios: list[dict]              # bear/base/bull with probabilities
    scenario_results: list[dict]       # per-scenario valuation outputs
    thesis: dict | None
    evidence_pack: dict
    company_state: dict | None
    assumption_memo: dict | None
    valuation: dict
    sensitivity_table: list[dict]
    wacc_components: dict
    confidence_label: str
    confidence_breakdown: dict | None
    wacc_sanity: dict | None
    # Review loop
    assumption_history: list[dict]     # one record per completed review iteration
    initial_assumptions: dict          # snapshot before any review adjustments
    analysis_iteration: int
    model_validity: str                # "valid" | "invalid" | "adjusting"
    invalidation_reason: str
    # ... ~30 more fields, see state.py
```

### PlanStore — single seam for plan persistence

`plan_store.py` wraps the dual-write pattern (disk JSON + SQLite) behind three functions:

| Function | Does |
|----------|------|
| `save_plan(thread_id, plan)` | Disk write + SQLite `sync_job_steps` |
| `update_step(thread_id, plan, step_id, *, status, result, tool_result_ids)` | Mutates plan dict, saves to disk, updates SQLite row |
| `save_report(thread_id, session_id, objective, content)` | Disk write + SQLite `store_report` |

Callers never know about disk vs SQLite. Swap adapter for tests or Postgres without touching callers.

### Persistence layout (per thread)

```
runs/<thread_id>/
├── plans/               # plan JSON snapshots (every state change)
├── tool_results/        # full tool output payloads (one file per call)
├── artifacts/           # sensitivity heatmaps, sandbox plots, etc.
├── dcf_output.json      # full DCF payload (written by finalize_node)
└── final_report.md      # synthesized markdown report (research mode)

runs/agent.db            # SQLite: jobs, job_events, job_steps, reports, session_memory, documents
runs/chroma/             # ChromaDB: document embeddings
```

### Append-only context stack

After each research step:
- `{step_id, summary, tool_result_ids}` pushed to `context_stack` — never modified.
- Tool outputs persist to disk via `utils.persist_tool_result`.
- Agent message contains `tool_result_id` pointer; agent calls `retrieve_tool_result(id)` for full payload only when needed.

KV-cache friendly. Follows Manus principle of immutable append-only context.

---

## Knowledge Graph layer

Typed graph centred on the `company` anchor. Three-layer model defining what is immutable, what is rebuildable, and what is historical-only.

**Storage:** SQLite (`kg_nodes`, `kg_edges`, `kg_traversals`) durable source of truth. `KGCache` in-process dict singleton = write-through cache for O(1) hot path.

### Three-layer model

```
                    company (anchor — Layer 0, never expires)
                    /         |         \
            fundamentals   filings    news_items        ← LAYER 1: ANCHORED FACTS
            (current snap) (immutable) (immutable)         (additive — never invalidate)
                    \         |         /
                       synthesis · thesis · drivers      ← LAYER 2: DERIVED INFERENCES
                       (rebuildable, cached, hash-checked)
                                |
                             dcf_run                      ← LAYER 3: RUN ARTIFACTS
                            /   |   \                        (immutable history,
                  assumptions outputs scenarios               NEVER input to future runs)
```

**Why three layers:** the value of a 10-K filed in Nov 2024 doesn't change. Fetching news in May does not invalidate news from January — it adds to the corpus. A DCF run done last week is what it was; a new run is a separate analytical event, not a "refresh" of the old one. Confusing these breaks reproducibility and forces wasteful re-work.

### Layer 1 — Anchored facts (additive, infinite TTL)

| Type | What it stores | Field key | Source |
|------|----------------|-----------|--------|
| `company` | Entity anchor | `anchor` | inferred |
| `filing` | 10-K/10-Q/8-K section text | `{filing_type}::{as_of}::{section}` | SEC EDGAR |
| `news_item` | Article (text + url + published_at) | `{published_at}::{url_hash}` | web_search |
| `market_metric_fund` | Current FY fundamental snapshot | `base_revenue`, `net_debt`, etc. | FMP / yfinance |
| `market_metric_price` | Current price | `price` | yfinance |
| `person` | Executive / analyst entity | name / role | inferred |

**Write semantics:** `put()` is a no-op for `filing` and `news_item` when an existing node with the same ID is found. Deterministic ID + immutable content = re-fetch never overwrites. Corpus only grows.

`market_metric_*` keep TTL because they represent the CURRENT snapshot (refreshable). Historical snapshots could move into a separate `historical_fundamental` anchored type — future work.

### Layer 2 — Derived inferences (rebuildable, finite TTL)

| Type | TTL | Hash-checked vs inputs |
|------|-----|-----------------------|
| `driver`, `theme`, `risk` | 7 days | no |
| `company_synthesis` | 7 days | yes — `evidence_hash(ticker)` |
| `thesis` | 7 days | yes — `evidence_hash(ticker)` |
| `company_lifecycle` | 30 days | no (lifecycle changes slowly) |

Refresh when TTL expires OR upstream evidence hash changes (for compound types). These ARE the input to runs; they DO matter when stale.

### Layer 3 — Run artifacts (immutable, infinite TTL)

| Type | What it stores |
|------|----------------|
| `dcf_run` | Run metadata (horizon, profile, confidence, validity) |
| `run_assumption` | One node per (run_id, field) — the exact values used |
| `run_output` | Implied price, EV, equity bridge |
| `run_scenario` | Per-scenario inputs + results |

**Key rule:** Layer 3 nodes are NEVER read as inputs to future runs. They exist for audit + comparison (compare-runs UI, run-diff). A new run with different assumptions is a NEW `dcf_run`; the old one stays in history.

### Node IDs

Deterministic — `"{ticker}::{node_type}::{field}"` (shared) or `"{ticker}::{node_type}::{run_id}::{field}"` (run-scoped). Pure dict lookup, no graph traversal on the hot path.

### Cache rules per layer

- **Layer 0/1 anchored** (`filing`, `news_item`): infinite TTL + write-once. New fetch with existing ID = no-op.
- **Layer 1 refreshable** (`market_metric_*`): TTL-based. Hit if fresh, refresh on miss.
- **Layer 2 derived**: TTL + optional input-hash check (`thesis`, `company_synthesis` only).
- **Confidence floor 0.7** — below this, treat as miss.
- **`user_stated` lock** — auto-writes never overwrite user-stated nodes (controlled by `respect_user_lock`).

### Write triggers

- **Evidence assembly** (`assemble_evidence_node`): writes Layer 1 anchored items (filings + news) — additive corpus growth.
- **Synthesis** (`semantic_synthesis_node`): writes Layer 2 `company_synthesis` + `company_lifecycle`.
- **Thesis** (`formulate_thesis_node`): writes Layer 2 `thesis`.
- **Finalize** (`finalize_node`): writes Layer 3 `dcf_run` + `run_assumption` + `run_output` + refreshes Layer 1 `market_metric_*`.
- **User edit** (`PATCH /kg/{session_id}/nodes/{id}`): source becomes `user_stated`, confidence=1.0.

### Read triggers (inside DCF)

- `cache_check_node`:
  - Probes Layer 1 refreshable + Layer 2 derived for skip flags.
  - Loads Layer 1 anchored corpus (`get_anchored_corpus`) — surfaces counts to state so agent knows corpus depth.
  - Injects `company_state`, `thesis`, `kg_lifecycle_hint` into state when cached.
- `formulate_thesis_node`: short-circuits if `skip_formulate_thesis` set.
- Memo: reads injected `company_state` (lifecycle fields drive optional Tier B selection).

### REST API

**Runs & DCF**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/runs` | Start a chat or research run |
| `GET` | `/runs/{thread_id}/events` | SSE activity stream |
| `POST` | `/runs/{thread_id}/dcf-decision` | Approve/edit DCF assumptions (chat HITL) |
| `GET` | `/runs/{thread_id}/dcf-report.md` | Download DCF report (markdown) |
| `GET` | `/runs/{thread_id}/dcf-report.pdf` | Download DCF report (PDF) |
| `GET` | `/artifacts/{thread_id}/{filename}` | Serve run artifacts (e.g. sensitivity PNG) |
| `GET` | `/workflows/dcf/runs/{thread_id}/result` | Raw `dcf_output.json` |

**Knowledge Graph**

`GET /kg/{session_id}` · `GET /kg/{session_id}/subgraph/{ticker}` · `POST/PATCH/DELETE /kg/{session_id}/nodes(/{id})` · `POST/DELETE /kg/{session_id}/edges(/{id})` · `POST /kg/{session_id}/query` · `GET /kg/{session_id}/traversal/{run_id}`.

**Natural-language query:** `POST /kg/{session_id}/query` → LLM extracts `KGQuery` (intent, ticker, node_type, field) → deterministic executor walks the cache → returns answer + matched nodes + traversal path. Intents: `lookup`, `compare_runs`, `why_assumption`, `list_drivers`, `recent_changes`.

### Recency policy (Layer 1 news)

News is additive — old news stays valid as historical context. Agent decides when to fetch fresh news based on `kg_anchored_corpus_meta.newest_news_ts`:
- newest > 24h old + question is time-sensitive → fetch new (adds to corpus)
- otherwise → re-use existing corpus
- never delete or invalidate old news

---

## Frontend

Vite + React + TypeScript + Tailwind. Single-page app with two main views:

- **Idle hero** — empty state, mode selector, input box.
- **Two-pane** — `MessageThread` (left) + `ExecutionSidebar` (right) with collapsible activity trace.

**Key components:**

| Component | Purpose |
|-----------|---------|
| `ActivityTrace` | Expandable per-step detail panels for all DCF nodes |
| `DcfHitlSection` | Assumption review UI with evidence panel + approve/reject |
| `ConfidenceBreakdownPanel` | Per-component scores + reasons |
| `EvidencePanel` | Source items with tier badges + content previews |
| `ImpliedWaccDetail` | Market-implied WACC vs CAPM gap |
| `AssumptionJourneyDetail` | Per-scenario diff table across review iterations |
| `ScenarioRunnerDetail` | Bear/base/bull table with expected value + range |
| `DcfReportCard` | Verbatim DCF markdown + inline sensitivity chart + PDF/MD download |
| `KnowledgePanel` | Right-panel React Flow graph viz with NL query drawer |
| `KgNodeCard` | Inspect/edit a KG node |
| `KgQueryDrawer` | NL query with traversal highlight |

**KG visualization:** Custom React Flow nodes coloured by `node_type` + `source` (green=user_stated, blue=company/metric, amber=driver). Query results highlight traversed subgraph with teal animated edges. Floating 🧠 button (bottom-right) toggles the panel.

**Activity badges:**
- Teal `⚡ KG` pill on cache-hit substeps.
- Amber `⚠ fallback` pill on `formulate_thesis` when thesis LLM failed.
- `×N` re-run badge on `scenario_runner` when review loop iterates.

---

## Code structure

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
├── server.py              # FastAPI backend (SSE, HITL, artifacts, jobs, DCF download)
├── report_export.py       # DCF report PDF/MD export (ReportLab)
├── app.py                 # Chainlit entrypoint (legacy)
├── graphs/
│   ├── research.py        # Research subgraph
│   ├── conversational.py  # Chat subgraph
│   └── workflows/
│       └── dcf/           # 24 modules
│           ├── graph.py            # Wiring + run_dcf_workflow_sync (public API)
│           ├── lifecycle.py        # normalize_input, cache_check, routing
│           ├── scenarios.py        # scenario_generator + monotonicity
│           ├── execution.py        # formulate_thesis + scenario_runner
│           ├── review_loop.py      # run_review_subgraph gateway + routing
│           ├── refinement.py       # analyze_result + refine_assumptions
│           ├── payload.py          # summarize_dcf_payload + consistency checks
│           ├── sources.py          # SourceRegistry, citations, section builders
│           ├── hitl_snapshot.py    # HITL context serialize/restore for fast path
│           ├── state.py            # DCFState TypedDict + field specs
│           ├── analysis.py         # convergence_gate + divergence detection
│           ├── evidence.py         # 5-tier evidence assembly
│           ├── fundamentals.py     # FMP / yfinance fetchers
│           ├── sec_filings.py      # SEC EDGAR integration
│           ├── synthesis.py        # LLM semantic synthesis
│           ├── memo.py             # LLM assumption memo
│           ├── wacc.py             # CAPM WACC estimation
│           ├── valuation.py        # Deterministic FCFF math + finalize
│           ├── priors.py           # Profile priors + confidence
│           ├── review.py           # HITL assumption review gate
│           ├── review_graph.py     # Adversarial review subgraph
│           ├── review_state.py     # Isolated ReviewState + Pydantic findings
│           ├── activity.py         # DCF-specific activity emitters
│           └── assumptions.py      # Legacy heuristics (unused)
├── kg/
│   ├── cache.py           # KGCache singleton
│   └── query.py           # NL→KGQuery + deterministic executor
├── tests/                 # See "Testing" section
│   ├── conftest.py
│   ├── helpers.py
│   ├── fixtures/
│   │   ├── payloads/      # captured DCF output snapshots
│   │   └── golden/        # hand-curated DCF records (ground truth)
│   ├── unit/              # 143 deterministic unit tests
│   └── golden/            # 8 golden tests per record (AAPL captured)
├── frontend/              # Vite + React + TypeScript + Tailwind
└── public/elements/       # Legacy Chainlit custom elements
```

---

## Testing

**213 tests, 0 LLM calls, runs in ~2s.**

Three tiers of testing, each with different ground truth:

| Tier | What it catches | Ground truth | Cost |
|------|----------------|--------------|------|
| **Unit** | Code regressions, math identities | Thresholds in source + accounting identities | ~2s, free |
| **Golden** | Model errors (vs hand-vetted records) | SEC filings + Damodaran + analyst consensus | 1s, free |
| **LangSmith evals** *(future)* | LLM output drift | Curated datasets | $$, slow |

### Unit tests (`tests/unit/`)

213 deterministic tests. No LLM, no network.

| File | Tests | Covers |
|------|-------|--------|
| `test_routing.py` | 13 | All router functions |
| `test_scenarios_validation.py` | 8 | Monotonicity validator |
| `test_deterministic_flags.py` | 14 | Severity thresholds for all signals |
| `test_consistency_checks.py` | 9 | EV reconciliation, TGR vs Rf, evidence coverage |
| `test_humanize_refs.py` | 9 | Evidence ref formatting (all 5 kinds) |
| `test_sources.py` | 12 | SourceRegistry, citation numbering, Reference hyperlinks |
| `test_convergence_gate.py` | 3 | `structural_gap` vs `invalid` validity policy |
| `test_report_export.py` | 3 | PDF/MD export, table wrapping |
| `test_initial_state.py` | 11 | DCFState factory + required keys |
| `test_fcff_math.py` | 18 | FCFF projection + valuation math (+ 200-example hypothesis property tests) |
| `test_payload_invalid.py` | 10 | Invalid model banner + fixture contracts |
| `test_priors.py` | 32 | Profile classify, bands, confidence breakdown, validity gate |
| `test_refine_assumptions.py` | 13 | Bounded adjustment application + flag-derived fallbacks |
| `test_memo_validation.py` | 22 | Assumption memo schema contracts |
| `test_kg_anchored.py` | 16 | KG cache anchoring rules |
| `test_synthesis_lifecycle.py` | 13 | Evidence synthesis lifecycle |

**Refactor resistance** — tests assert *contracts* not *implementations*:
- Use `<=` / `<` / `in` not `==` for thresholds
- `pytest.approx` for floats
- `build_test_state(**overrides)` factory — adding a state key doesn't break every test

### Golden dataset (`tests/fixtures/golden/`)

Hand-curated DCF records with full provenance. Each record encodes:
- Assumptions extracted from real SEC filings (Tier A factuals)
- Damodaran sector WACC + analyst consensus (Tier B/C)
- Expected implied-price *range* (not point estimate — DCF inherently imprecise)
- Audit trail in `sources/<ticker>_<fy>_notes.md`

**Current records:**
- `aapl_fy2024.json` — AAPL FY2024 from 10-K + Damodaran Jan 2026. Hand DCF = $124.63; range [$80, $280]; analyst consensus $308 (market premium documented).

**Extrapolation recipe:** See `tests/fixtures/golden/README.md` for 10-step process to add a ticker. Variant handling for mega-cap, high-growth, startup (ARR), cyclical.

**Auto-discovery:** `test_golden_math.py` globs `golden/*.json` (skips `_template.json`) → parametrizes 8 tests per record:
1. Schema validation (4 tests) — required keys, valid tiers, range well-formed
2. Math correctness — implied price ∈ expected range
3. Gordon model precondition — TGR < WACC
4. Accounting identity — EV = PV(CF) + PV(TV)
5. Suite non-empty

### What's not tested yet

- **LLM nodes** (`formulate_thesis`, `propose_assumptions`, `semantic_synthesis`, `scenario_generator`, `analyze_result` LLM step) — require LangSmith evaluator datasets. Marked `@pytest.mark.llm` so they don't run in CI by default.
- **External APIs** (yfinance, FMP, Tavily) — mocked or skipped.
- **Full workflow integration** — captured in `valid_aapl.json` payload fixture as regression baseline.

### Running tests

```bash
.venv/bin/python -m pytest agent_project/tests/ -v       # all
.venv/bin/python -m pytest agent_project/tests/unit/ -q  # unit only
.venv/bin/python -m pytest agent_project/tests/golden/   # golden only
.venv/bin/python -m pytest -k aapl                       # filter
```

See `tests/README.md` for full layout + design rationale.

---

## Configuration

`.env` in `agent_project/`:

```env
OPENAI_API_KEY=sk-proj-...
EXA_API_KEY=...                    # web search
FMP_API_KEY=...                    # Financial Modeling Prep (DCF fundamentals)
LANGSMITH_API_KEY=lsv2_...         # optional, for tracing

# DCF CAPM calibration (defaults 0.045 / 0.055)
DCF_RISK_FREE_RATE=0.045
DCF_EQUITY_RISK_PREMIUM=0.055

# DCF LLM model overrides (default gpt-4o)
DCF_SYNTHESIS_MODEL=gpt-4o
DCF_MEMO_MODEL=gpt-4o
DCF_THESIS_MODEL=gpt-4o
DCF_ANALYSIS_MODEL=gpt-4o
DCF_SCENARIO_MODEL=gpt-4o
DCF_REVIEW_MODEL=gpt-4o
KG_QUERY_MODEL=gpt-4o-mini         # NL→KGQuery (cheap classifier)
```

---

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Append-only `context_stack` | KV-cache friendly; follows Manus principle |
| Tool results on disk, pointer in message | Keeps context short; full data retrievable on demand |
| Static system prompt | Identical across all steps → near-100% KV-cache hit |
| Deterministic memory compression | Zero latency, zero cost, no LLM summarisation |
| One tool definition in `tools.py`, shared by all subgraphs | Eliminates duplication |
| `execute_one_step_node` loop in parent graph | Per-step checkpointing, streaming, interrupt — not possible with monolithic node |
| PlanStore wraps disk + SQLite behind one seam | No dual-write bugs; swap adapter for tests or Postgres |
| DCF HITL unified across chat and research | Same tool, same events, same frontend |
| Scenarios added as separate nodes, not inside memo | Keeps memo node stable; scenario_generator is a clean new layer |
| Scenario valuation uses separate compiled graph | Avoids running analysis loop per-scenario |
| Evidence items sent as lightweight previews in SSE | Enables UI inspection without extra API calls |
| Exa over Tavily | Better semantic search and excerpts |
| FMP + yfinance for DCF levels | Canonical scale and margins from statements |
| **ReviewState isolated from DCFState (one-way snapshot)** | Prevents state pollution; subgraph can't accidentally mutate upstream state |
| **LLM finds problems, Python applies fixes** | LLM better at judgment than arithmetic; deterministic rule table prevents hallucinated deltas |
| **No valuation output passed to reviewer** | Prevents backward anchoring — reviewer critiques assumptions, not implied price |
| Convergence damping (same-direction repeat halved) | Prevents oscillation without explicit history check |
| `_fallback: True` marker on thesis + HIGH severity flag | Quality propagates to review subgraph without special-casing |
| `KeyDriver` nested Pydantic model with `extra='forbid'` | Required for OpenAI structured output (`additionalProperties: false`) |
| KG uses SQLite as source of truth, in-process dict as cache | User edits between runs require durable store; in-process gives O(1) |
| KG nodes use deterministic IDs (`ticker::type::field`) | No graph traversal for cache lookups — pure dict access |
| KG separates shared from run-scoped nodes | Avoids overwrite on repeat runs; multiple runs coexist; comparable in UI |
| LLM only translates NL→`KGQuery`, never traverses | Prevents hallucinated answers; all facts come from the actual graph |
| User-stated nodes are sticky | Analyst conviction must never be silently overwritten |
| `model_validity` invalid → suppress point estimate | Honesty over confidence; better to flag uncertainty than fake precision |
| Golden test range ±50% around hand DCF | DCF inherently imprecise; tighter range = false positives from legitimate variation |
| `build_test_state(**overrides)` factory pattern | Adding a state key doesn't break every test |

---

## What works

- [x] Intent router with auto / forced research / chat modes
- [x] Plan-then-execute with HITL approval
- [x] Per-step LangGraph node execution → checkpointing, streaming, resume
- [x] Multi-turn chat with streaming tokens
- [x] Append-only context stack (Manus-inspired)
- [x] All 8 tools functional, shared across subgraphs
- [x] DCF valuation with thesis node, scenario modelling (bear/base/bull), adversarial review subgraph
- [x] DCF HITL unified across chat and research (same tool, same events, same UI)
- [x] PlanStore — single seam for plan persistence
- [x] SEC EDGAR integration (free 10-K/10-Q extraction)
- [x] Session-doc RAG (ChromaDB + BM25 hybrid)
- [x] Python execution with matplotlib artefacts
- [x] React frontend with streaming reports, DcfHitlSection, ActivityTrace with per-step expanders
- [x] ActivityTrace nested sub-steps (`review_deep_dive` / `synthesize_adjustments` under `review_subgraph`)
- [x] ActivityTrace `×N` re-run badge on `scenario_runner` when review loop iterates
- [x] AssumptionJourneyDetail panel — per-scenario diff table across iterations
- [x] Thesis fallback amber badge
- [x] Knowledge Graph layer — SQLite + in-process cache, cache-first DCF, back-write on finalize
- [x] KG REST API — full CRUD, NL query, traversal replay
- [x] KG visualization panel — React Flow with custom typed nodes, inspect/edit drawer
- [x] KG cache badges in ActivityTrace
- [x] Evidence inspection with clickable sources
- [x] FastAPI + SSE + HITL endpoints, job resume on restart
- [x] Static system prompt (KV-cache)
- [x] Multi-turn session memory
- [x] Unified activity contract (single event store/renderer)
- [x] `start.sh` kills stale processes
- [x] **Test suite: 143 unit + 8 golden tests, runs in 1.4s, zero LLM calls**
- [x] **Golden dataset workflow (AAPL FY2024) with extrapolation recipe**
- [x] **DCF report with numbered citations, hyperlinked References, sensitivity heatmap**
- [x] **DCF report PDF + Markdown download (ReportLab, wrapped tables)**
- [x] **HITL snapshot restore — fast path preserves evidence/provenance after approval**
- [x] **Verbatim DCF report in chat (no LLM re-synthesis)**
- [x] **Validity vs reconciliation policy — structural market gaps ≠ invalid model**

---

## Known limitations

- **No document generation beyond DCF PDF/MD.** No PPTX, DOCX, or XLSX output yet.
- **Standalone DCF endpoint (`POST /workflows/dcf/runs`) HITL resume is broken.** The dedicated HTTP endpoint's `Command(resume=...)` path may not find saved interrupt state in MemorySaver. The agent-tool path (chat + research) works correctly.
- **Completed job report opens in new tab.** Clicking a completed job in `JobsPanel` opens a Blob URL; not loaded into main research view.
- **Worker model is single-process.** No DB claim lock / multi-worker coordination.
- **Document ingestion does not resume.** Uploads interrupted mid-embedding may stay `processing` / `err`; no retry queue.
- **Exa search is good but not deep enough for financial research.** Still needs full source-content fetching + citations for deep work.
- **Review adjustments sparse in fast path.** When `scenario_results` is empty (fast-path, no scenarios), `synthesize_adjustments` has no scenario deltas to apply — adjustments target base only.
- **Market-implied signals incomplete.** Only implied WACC computed; implied growth and margin needed for deeper critique.
- **LLM nodes not unit-tested.** Require LangSmith eval datasets — golden dataset only covers the math pipeline.

---

## Future work

### Phase 3 — Market signals + report quality (partially shipped)

**Shipped:**
- Reverse-solved implied growth/margin/WACC via `compute_market_signals_node`
- `reconciliation_status: structural_gap` when price embeds different expectations but DCF math is valid
- Numbered `[n]` citations throughout report + hyperlinked References appendix
- Sensitivity matrix + heatmap artifact + inline chart marker
- PDF/Markdown export with table width fixes
- Scannable report formatting (bullets, subsections, valuation table)

**Remaining:**
- Conviction label surfaced more prominently in UI footer
- Inline assumption-row click-through to evidence side panel (References links exist; table rows do not yet open a drawer)

~200 lines of market-signal math shipped; citation/export layer added on top.

### Phase 4 — Confidence gating + exit modes

Confidence mechanically derived from diagnostics. Exit modes (clean / refined / unstable) surfaced in UI based on review loop outcome.

### Phase 5 — Assumption memory

Store historical assumptions per ticker. Flag deviations from prior runs.

### LangSmith evaluator datasets

For each LLM node, curate ~20 input examples with expected output properties:
- `formulate_thesis` — does output have `direction` + `confidence`? Are key drivers grounded in evidence?
- `propose_assumptions` — are values within profile bands? Does every field have `evidence_refs`?
- `semantic_synthesis` — does output reference base_revenue, margins, growth signals from input filings?
- `scenario_generator` — monotonicity holds? Probabilities sum to 1.0?
- `review_deep_dive_node` — does it identify planted contradictions in synthetic test inputs?

### Golden dataset expansion

Currently AAPL only. Targets: MSFT, GOOGL (other mega-cap tech), KO (mature consumer), F (cyclical), SHOP (high-growth), PLTR (story stock), one SPAC, one small-cap miner (edge cases). Per ticker: ~20 min using the documented extrapolation recipe.

### Comparable comps workflow

Reuses: HITL pattern, validity gate, KG cache (peer set), confidence breakdown, payload summariser. Different: no projection math — peer-multiple selection + outlier detection. Validates architecture generalises.

### Citations in UI

Backend ships numbered `[n]` refs in the report and hyperlinked References. **Remaining:** assumption table row → click → side panel with 1-3 evidence sources (inline refs in markdown are clickable via References links today).

### Prompt extraction + versioning

Move every inline prompt to `prompts/` as `.md` or `.j2`. Each prompt gets `THESIS_PROMPT_V = "2025-05-01"`. Log version alongside run output → diff "which prompt produced this."

### Streaming via LangGraph native

Port research-mode streaming to DCF: `app.astream_events()` per node → existing activity stream. Thesis / scenario / analysis LLMs are each 5–15s — perceived latency drops ~3×.

### Run-diff view

"Compare run A vs run B" — assumption table side-by-side, delta column, which divergence flipped. Higher analyst utility than the current force-directed KG graph.

### Confidence breakdown tooltip

Surface `confidence_breakdown.components` as expandable card on hover/click of confidence chip. e.g. "HIGH because: WACC reliable (0.85), 12 evidence refs (0.78); LOW because: solver failed (−0.30)."

### RAG into evidence layer

`documents.py` + ChromaDB exist but unused in `assemble_evidence_node`. User uploads PDF (10-K, transcript, broker note) → indexed → assumptions cite retrieved chunks.

### File splits (engineering hygiene)

- `valuation.py` 953 lines → `compute/`, `finalize/`, `kg_write/` sub-modules
- `ActivityTrace.tsx` 2101 lines → split per activity-type renderer
- `MessageThread.tsx` 653 lines → extract `Bubble`, `DegradedBanner`, `CommittedMessage`

### Latency budget + telemetry

Log per-node duration to run output JSON. Surface in UI footer: "DCF took 47s (thesis 8s, scenarios 12s, valuation 3s, analysis 19s, review 5s)."

### Output delta on rerun

Extend monospace diff message: "You changed `tax_rate` 15.6% → 12%, implied price moved $182 → $194 (+6.6%)."

---

## Development

### Running the full stack

```bash
./start.sh
# Syncs Python deps (uv sync), kills stale processes, then launches:
# backend  → http://localhost:8080  (FastAPI + SSE)
# frontend → http://localhost:5174  (Vite dev server)
```

### LangSmith Studio (graph debugger)

Root `langgraph.json` registers two graphs. From repo root:

```bash
uv sync --extra studio
uv run --extra studio langgraph dev
```

| Graph ID | Module | What you see |
|----------|--------|--------------|
| `agent` | `file.py:app` | Full agent — intent → research path or chat path |
| `dcf_workflow` | `graphs/workflows/dcf:dcf_workflow_app` | Standalone DCF subgraph (16 nodes) |

Studio UI: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`.

### MCP tools: code-review-graph

This project has a code knowledge graph. **Use the graph tools before Grep/Glob/Read** when exploring:

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | High-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

Fall back to Grep/Glob/Read only when the graph doesn't cover what you need.

### Adding a new DCF node

1. Define node function in the appropriate module (`evidence.py`, `valuation.py`, etc.). Signature: `(state: DCFState) -> dict`. Return only the keys it mutates.
2. Add `emit_step(...)` calls at start + complete for activity trace.
3. Register node in `graph.py` via `graph.add_node(...)` and wire edges.
4. If LLM call, use Pydantic structured output (see `ScenarioOutput`, `ThesisOutput` patterns).
5. Add unit tests in `tests/unit/` — at minimum, test the routing and any pure functions extracted.

### Adding a new golden record

See `tests/fixtures/golden/README.md`. 10-step process; ~20 min per ticker.

### Adding a new tool

1. Define in `tools.py` with `@tool` decorator + clear docstring (LLM sees this).
2. Wire into the tool list at the bottom of `tools.py` — both chat and research subgraphs pick it up automatically.
3. If it persists data, use `utils.persist_tool_result(...)` and return a pointer.

---

## License + attribution

Personal research project. Built on LangGraph, LangChain, OpenAI, Exa, FMP, SEC EDGAR, Damodaran sector data, ChromaDB.

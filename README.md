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

## Recent changes (2026-06)

**Multi-hop KG deep research + tool consolidation (`kg/deep_research.py`)**
- The KG is now the agent's **own memory, queryable as a tool**. `query_knowledge_graph(question, ticker?)` is in the shared tool set → both chat *and* research subgraphs consult prior DCF runs, theses, fundamentals, drivers, and filings *before* hitting the web. Precedence: **KG → documents (RAG) → web**.
- The engine reasons hop-by-hop instead of dumping the whole subgraph: seed (company + 1-hop) → LLM decides *answer now* or *which real edges to expand* → expand → repeat (bounded: ≤4 hops, ≤80 nodes). The planner only ever picks from relations that actually exist on the frontier, so it can't hallucinate a hop. Handles cross-run and cross-ticker questions ("compare wacc across runs", "AAPL vs META tax rate", "do the assumptions match the thesis?").
- Adjacency read from durable storage (not the partially-hydrated in-memory cache), so a fresh query process sees the full graph.
- **One engine**: the manual `/kg/{session}/query` panel routes through the same `run_deep_research` — the visual traversal panel benefits from multi-hop too. The tool returns a synthesized answer inline + a `tool_result_id` for the full hop/node/edge trail, and emits a `kg_traversal` UI event. (Idea adapted from QuantMind's multi-hop DeepResearch.)

KG ingestion, rendering, and audit hardening — driven by real upload/DCF sessions.

**Document → KG ingestion**
- **Fixed cross-document fact contamination.** Fact extraction fetched chunks with a no-op `where` clause (`hasattr(collection, "_metadata")` always False) → it sampled the first chunks of the *whole* Chroma collection, so one company's filing extracted another's numbers. Now filters by `doc_id` and samples evenly across the document (financials live mid-doc, not on the cover page).
- **Hardened the extraction prompt.** Income-statement lines are now first-class `fact_type`s (`net_income`, `operating_income`, `gross_profit`, `eps`, `shares_outstanding`) — no more net-income mislabeled as revenue (which would corrupt a DCF). Unmapped lines fall to `other` instead of a wrong label.
- **Filing persistence reworked.** One `filing` node per document (was 7 noisy per-page nodes) carrying `source_doc_id` + a short lead excerpt. `FilingCard` renders an **"Open document ↗"** link → `GET /documents/{doc_id}/file` so the original upload is retrievable later.
- **Temporal fundamentals (backend).** Document facts are keyed by `metric::period` (`net_income::Q2 2026`) so multiple reporting periods **coexist** instead of overwriting — the YoY series an analyst can compare. Same metric + same period still upserts idempotently.

**KG write path**
- **Fixed silent fundamentals loss.** `ingest_fact` assumed `value` was a dict (`value.get("as_of")`) but `market_metric_fund` writes a bare float → `AttributeError`, swallowed by finalize's non-fatal handler. Fundamentals never persisted on *any* run. Now guards scalar vs dict.
- **Raw fundamentals persist regardless of confidence.** `market_metric_fund` / beta / profile / period are facts (sourced from FMP/yfinance) — their validity is independent of the valuation's confidence. Only *derived* inferences (synthesis/thesis/drivers) stay gated by `write_shared`. A low-confidence DCF no longer leaves the KG with zero Fundamentals.
- Added `KGCache.query()`, `get_nearest()`, `delete()` — previously missing methods that silently broke the audit and ingest dedup/contradiction checks.
- **Canonical value schemas (`kg/schemas.py`).** Per-node_type Pydantic models + `validate_kg_value()` (advisory, never drops data) are now the single source of truth for value shapes, plus `SCALAR_NODE_TYPES` / `RUN_SCOPED_NODE_TYPES` used by ingest + audit. Validates payloads at the write boundary to prevent the scalar-vs-dict crash class; `extra="allow"` keeps the KG additive. (Idea adapted from QuantMind's Pydantic knowledge standardization.)

**Frontend / KG panel**
- **App-level KG notifications.** Write toasts (`KgNotificationPanel`) moved out of the KG panel to app root → they fire even when the panel is closed (uploads from the chat composer). localStorage watermark survives close/reopen; StrictMode-safe (deferred persistence).
- **Financials hub → tabbed dock panel** (`KgFinancialsPanel`). Replaced the cluttered on-canvas category sub-hubs with category tabs (Fundamentals / Drivers / Filings / Thesis / Beliefs / …). Canvas now renders only company + News + Financials + DCF-run hubs.
- **DCF report button** on the run node (`KgRunInspector`) → opens the persisted PDF inline in a new tab (`dcf-report.pdf?inline=1`; `dcf_run` node now stores its `thread_id`).
- **Timeline brush-to-zoom** (`KgTimeline`). Drag across the strip to zoom into a timeframe; range readout + clear; dots filter to the window (fixes overlapping date labels).

**KG Audit**
- **Audit was always returning 0** — every check called the missing `cache.query`, crashed, and was swallowed by a per-check `except`. Now functional.
- **Cross-source check rewritten.** Excludes run-scoped nodes (per-run by design), uses ticker-keyed grouping (no cross-company false positives), and correct distinct-value logic (the old code flagged *identical* values). Auto-fix wired (`cache.delete`), staleness handles float epoch timestamps.
- **Audit ticker selection** — choose which tickers to audit via chips (backend accepts a `tickers` list).

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
- `conversational.py` — ReAct loop with streaming (`gpt-4o-mini` chat model)
- `workflows/dcf/` — DCF valuation subgraph (see [DCF deep dive](#dcf-workflow-deep-dive))
- `workflows/deck/` — slide-deck compiler (see [Deck workflow](#deck-workflow))

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

### Deck workflow

Standalone slide-deck compiler. Primary entry: `run_deck_workflow` tool, invoked from chat after a completed DCF.

```
START
  → validate_sources
  → normalize_all          (adapters: dcf_output, document, chart_artifact, …)
       └─ dcf_expectations_blocks.py prepends 6 institutional block kinds
  → generate_outline       (one LLM call — structure only)
  → outline_review         (HITL when hitl_mode ≠ disabled)
  → per_slide_generate     (one LLM call per slide; retry on blank/missing required field)
  → assemble_pptx          (python-pptx; DeckTheme throughout; layout_spec overlay)
  → finalize_deck          (deck_output.json + KG snapshot: deck_run + HAS_DECK edge)
END
```

**Outputs:** `runs/<thread>/decks/<title>.pptx` + `deck_output.json`.

**KG integration:** `finalize_deck` writes a `deck_run` node anchored to the company ticker (not session), linked to its parent `dcf_run` via `HAS_DECK`. Each slide gets a `deck_slide` node linked via `HAS_SLIDE`. Both edges added so deck nodes are visible in the KG hub model.

**Institutional block kinds** (`dcf_expectations_blocks.py`): `expectations_table`, `three_box`, `debate`, `capital_flow`, `variable_impact`, `decision` — framed as market expectations vs fundamental reality, matching buy-side analyst presentation style.

**Theme tokens (B1):** `DeckBrief.{density, accent, font_scale}` → `DeckTheme` resolved once per run. Audience presets: `board`, `ic`, `internal`, `client`, `generic`.

**Layout spec (B2):** LLM can emit `layout_spec` (list of fractional-coord `LayoutRegion`s). Pure overlay — canonical fields always required. `is_renderable()` strict gate; non-renderable spec gracefully dropped, canonical fallback fires.

**Key decisions:**
- Compiler pipeline (not ReAct loop) — deterministic, testable, HITL-friendly.
- Auto-resolve `sources` from `dcf_output.json` on disk — models pass garbage in sources arg.
- LLM structural validation + retry — new layouts blank `columns`/`flow_steps` ~20% of time; one nudge resolves most.
- Deterministic fallback with `_HUMAN_LABEL` dict — raw snake_case keys unacceptable in slides.

**API:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/runs/{thread_id}/deck-decision` | Approve/edit/reject outline |
| `GET` | `/runs/{thread_id}/decks/{filename}` | PPTX download |
| `GET` | `/runs/{thread_id}/deck-output` | Preview JSON |

**Tests:** `test_deck_inputs.py`, `test_deck_artifact_paths.py`, `test_deck_outline.py`, `test_deck_theme.py`, `test_deck_layout_spec.py`, `test_deck_citations.py`, `test_deck_structured_output.py`, `test_conversational_deck_routing.py`, `test_dcf_expectations_blocks.py`.

**Known deck gaps:** empty `section_header` slides when outline honors `must_cover` but adapters produce no matching blocks (e.g. `scenario_results` empty on DCF payload, sensitivity PNG path not on `dcf_output.json`). See [Known limitations](#known-limitations).

---

## Tools

All tool definitions live in `tools.py` — **shared by all subgraphs**, no duplication.

| Tool | Purpose |
|------|---------|
| `search_web` | Exa semantic search → persists result, returns pointer |
| `search_documents` | ChromaDB + BM25 hybrid retrieval with entity-aware gate model → returns relevance verdict (`relevant`/`partial`/`mismatch`) + chunk IDs. Agent passes `skip_gate=True` when doc landscape is known. |
| `fetch_sec_filing` | Free SEC EDGAR 10-K/10-Q section extraction |
| `calculator` | Safe math eval via `simpleeval` |
| `retrieve_context` | Look up a prior step's summary + tool_result_ids from saved plan |
| `retrieve_tool_result` | Fetch full content of any stored tool result by ID |
| `execute_python` | Run Python locally with `yfinance`/`matplotlib`/`pandas`/`requests` |
| `run_dcf_workflow` | Deterministic DCF with scenarios, thesis, review loop |
| `run_deck_workflow` | Slide-deck compiler from DCF output + other sources |

### Entity-aware RAG (`documents.py`)

Document uploads now trigger automatic entity extraction (`gpt-4o-mini`), identifying company name, ticker, document type, and fiscal period from the first few chunks. This metadata is stored in ChromaDB chunk metadata and the in-memory doc registry.

**Gate model** — Before returning RAG results to the agent, `_classify_rag_results()` runs a small LLM call that classifies retrieved chunks against the user's query:

| Status | Meaning | Agent action |
|--------|---------|-------------|
| `relevant` | Chunks cover everything needed | Fetch chunks, answer from docs |
| `partial` | Chunks cover some topics, others missing | Fetch relevant chunks + web search for missing |
| `mismatch` | Chunks are about completely different entities | Tell user about discrepancy, ask for clarification |
| `gate_skipped` | Agent passed `skip_gate=True` | Agent evaluates relevance from chunk metadata itself |
| `none` | No docs or no matches | Proceed with web search |

**`skip_gate` parameter** — The `search_documents` tool exposes `skip_gate: bool = False`. The ReAct agent can pass `True` when it already knows the document landscape from prior turns, saving ~1-2s of gate model latency. When skipped, chunk metadata (`company`, `ticker`, `doc_type`) is returned inline so the agent can evaluate relevance directly.

**Document inventory in planning** — `plan_node` queries `list_docs(session_id)` and injects uploaded document filenames + extracted entities into the planning prompt. The planner sees *"Uploaded: Earnings-Presentation-Q1-2026.pdf (Meta Platforms, META, earnings_call, Q1 2026)"* before generating steps.

**Coloured terminal logging** — The RAG pipeline emits coloured `rich` console output at each stage: 📤 upload, 📄 entity extraction, ✅ ready, 🔍 hybrid search, ⚡ dense candidates, 🔀 RRF fusion, 🚦 gate verdict (green/yellow/red for relevant/partial/mismatch), ⚡ gate skipped.

**Database** — Entity metadata columns (`company`, `ticker`, `doc_type`, `fiscal_period`, `subjects`, `stage`) added to `documents` table with auto-migration for existing databases.

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
├── decks/               # deck_output.json + generated .pptx (deck workflow)
├── dcf_output.json      # full DCF payload (written by finalize_node)
└── final_report.md      # synthesized markdown report (research mode)

runs/agent.db            # SQLite: jobs, job_events, job_steps, reports, session_memory, documents
runs/chroma-openai/      # ChromaDB store (OpenAI embeddings) — provider-namespaced
runs/chroma-local/       # ChromaDB store (local embeddings) — never collides with openai
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

- **Shared:** `"{ticker}::{node_type}::{field}"`
- **Run-scoped:** `"{ticker}::{node_type}::{run_id}::{field}"` where `run_id = kg_run_id` (unique per run — see Run identity below).

Pure dict lookup, no graph traversal on the hot path.

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
| `POST` | `/runs/{thread_id}/deck-decision` | Approve/edit/reject deck outline (chat HITL) |
| `GET` | `/runs/{thread_id}/decks/{filename}` | Download generated deck PPTX |
| `GET` | `/runs/{thread_id}/deck-output` | Deck JSON snapshot for in-app slide preview |

**Knowledge Graph**

`GET /kg/{session_id}` · `GET /kg/{session_id}/subgraph/{ticker}` · `POST/PATCH/DELETE /kg/{session_id}/nodes(/{id})` · `POST/DELETE /kg/{session_id}/edges(/{id})` · `POST /kg/{session_id}/query` · `GET /kg/{session_id}/traversal/{run_id}`.

**Natural-language query:** `POST /kg/{session_id}/query` → `_llm_answer_subgraph` serializes the full ticker subgraph → LLM answers + returns exact node ids → validated ids drive graph highlight (never hallucinated ids). `_KG_SCHEMA` injected into prompt (3-layer model, node-type meanings, relations). For causal questions ("why/justify/rationale"), `_augment_with_evidence` folds in `company_synthesis` + `driver` nodes so their hubs light up alongside the matched assumption. Fallback intents (keyword executor): `lookup`, `compare_runs`, `why_assumption`, `list_drivers`, `recent_changes`.

### Run identity (`graphs/workflows/dcf/idgen.py`)

Historically `run_id = parent_step_id = "workflow_dcf"` → identical every run → upsert **overwrote** all prior nodes. Fixed in Phase 0.

**`kg_run_id`** is now generated once per run in `normalize_input_node` (single chokepoint for all entry paths):

```
Root run:    AAPL_20260530145809_0492
Derived:     AAPL_20260530145809_51f7__from_1458090492
```

Format: `{TICKER}_{YYYYMMDDHHMMSS}_{rand4}`. Derived runs (clone/rerun) encode lineage via `__from_{ts_tail+rand4}` suffix — collision-resistant, readable. `parent_step_id` unchanged (activity streaming unaffected). Runs now **accumulate** — every rerun produces a new `dcf_run` node with `{run_id, created_at, parent_run_id, trigger, label, implied_share_price}` in its value. `label: null` reserved for future user-editable registry labels.

The `parent_run_id` threads through: `tools.py` → `[DCF_APPROVED]` payload → `conversational.py` → `run_dcf_workflow_sync` → `_build_initial_state` → `state["parent_run_id"]` → `finalize_node`.

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
| `DeckOutlineReview` | Deck outline HITL — editable titles, approve/reject |
| `DeckArtifactCard` | PPTX preview + download after deck completes |
| `DeckPreview` | Right-panel slide viewer from `/deck-output` |
| `KnowledgePanel` | Full-screen KG hub graph (canvas) + NL query drawer |
| `KgCanvas` | HTML5 canvas static radial layout; drag-to-reposition; d3-zoom pan/zoom |
| `KgValueView` | Structured node value renderer (thesis cards, news chips, source refs, status chips) |
| `KgHubPanel` | News hub detail — members grouped by type, colored left-stripe cards |
| `KgTablePanel` | Category table — drivers (dedup+filter), metrics rows, beliefs composer |
| `KgRunInspector` | DCF run assumptions/outputs table; inline editable; rerun + clone |
| `KgCompareRuns` | Cross-run diff matrix — assumptions × runs, delta green▲/red▼ |
| `KgTimeline` | Collapsible bottom strip — runs by date, color=ticker, click→inspector |
| `KgFilterSidebar` | Ticker filter + hub legend |
| `KgQueryPanel` | NL query input; answer + traversal route display |

**KG visualization (hub model):** Canvas renders hub nodes only — company, News, Financials, N×DCF Run per ticker (~10 nodes vs 300+ raw). Clicking Financials expands category sub-hubs (Drivers, Thesis, Fundamentals, Lifecycle, Beliefs). Each category opens a table panel. DCF run nodes: date in label, recency-coded alpha, violet badge dot on newest. Query matches roll up to their hub (via `hubForRaw`); matched rows glow teal inside open panels.

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
│       ├── dcf/           # DCF valuation subgraph (27 modules)
│       │   ├── graph.py            # Wiring + run_dcf_workflow_sync (public API)
│       │   ├── lifecycle.py        # normalize_input, cache_check, routing
│       │   ├── idgen.py            # Unique kg_run_id per run
│       │   ├── scenarios.py        # scenario_generator + monotonicity
│       │   ├── execution.py        # formulate_thesis + scenario_runner
│       │   ├── review_loop.py      # run_review_subgraph gateway + routing
│       │   ├── refinement.py       # analyze_result + refine_assumptions
│       │   ├── payload.py          # summarize_dcf_payload + consistency checks
│       │   ├── sources.py          # SourceRegistry, citations, section builders
│       │   ├── hitl_snapshot.py    # HITL context serialize/restore for fast path
│       │   ├── state.py            # DCFState TypedDict + field specs
│       │   ├── analysis.py         # convergence_gate + divergence detection
│       │   ├── evidence.py         # 5-tier evidence assembly
│       │   ├── fundamentals.py     # FMP / yfinance fetchers
│       │   ├── sec_filings.py      # SEC EDGAR integration
│       │   ├── synthesis.py        # LLM semantic synthesis
│       │   ├── memo.py             # LLM assumption memo
│       │   ├── wacc.py             # CAPM WACC estimation
│       │   ├── valuation.py        # Deterministic FCFF math + finalize
│       │   ├── priors.py           # Profile priors + confidence
│       │   ├── review.py           # HITL assumption review gate
│       │   ├── review_graph.py     # Adversarial review subgraph
│       │   ├── review_state.py     # Isolated ReviewState + Pydantic findings
│       │   ├── activity.py         # DCF-specific activity emitters
│       │   └── assumptions.py      # Legacy heuristics (unused)
│       └── deck/            # Slide-deck compiler (21 modules)
│           ├── graph.py            # Wiring + run_deck_workflow_sync
│           ├── inputs.py           # Sanitize/resolve LLM tool args
│           ├── normalize.py        # Source adapters → NormalizedBlock
│           ├── outline.py          # Outline LLM + repair pass
│           ├── review.py           # Outline HITL interrupt
│           ├── slides.py           # Per-slide LLM + deterministic fallback
│           ├── assemble.py         # python-pptx renderers + layout_spec
│           ├── theme.py            # DeckTheme tokens (audience presets)
│           ├── finalize.py         # deck_output.json + KG deck_run snapshot
│           └── adapters/           # dcf_output, chart_artifact, document, …
├── kg/
│   ├── cache.py           # KGCache singleton
│   └── query.py           # NL→KGQuery + deterministic executor
├── docs/
│   └── adr/               # Architecture decision records
├── tests/                 # See "Testing" section
│   ├── conftest.py
│   ├── helpers.py
│   ├── fixtures/
│   │   ├── payloads/      # captured DCF output snapshots
│   │   └── golden/        # hand-curated DCF records (7 tickers)
│   ├── unit/              # 356 deterministic unit tests
│   ├── golden/            # 50 parametrized golden math tests
│   └── e2e/               # 1 browser/app trace test
├── frontend/              # Vite + React + TypeScript + Tailwind
└── public/elements/       # Legacy Chainlit custom elements
```

---

## Testing

**407 tests total** (356 unit + 50 golden + 1 e2e). Zero LLM calls in CI; full suite runs in ~10s locally.

Three tiers of testing, each with different ground truth:

| Tier | Count | What it catches | Ground truth | Cost |
|------|-------|----------------|--------------|------|
| **Unit** | 356 | Code regressions, math identities, deck input/theme/layout | Thresholds in source + accounting identities | ~8s, free |
| **Golden** | 50 | Model errors (vs hand-vetted records) | SEC filings + Damodaran + analyst consensus | ~2s, free |
| **E2E** | 1 | App trace smoke | Captured run artifacts | slow, optional |
| **LangSmith evals** *(future)* | — | LLM output drift | Curated datasets | $$, slow |

### Unit tests (`tests/unit/`)

356 deterministic tests across 34 files. No LLM, no network.

| File | Covers |
|------|--------|
| `test_routing.py` | All router functions |
| `test_scenarios_validation.py` | Monotonicity validator |
| `test_deterministic_flags.py` | Severity thresholds for all signals |
| `test_consistency_checks.py` | EV reconciliation, TGR vs Rf, evidence coverage |
| `test_humanize_refs.py` | Evidence ref formatting (all 5 kinds) |
| `test_sources.py` | SourceRegistry, citation numbering, Reference hyperlinks |
| `test_convergence_gate.py` | `structural_gap` vs `invalid` validity policy |
| `test_report_export.py` | PDF/MD export, table wrapping |
| `test_initial_state.py` | DCFState factory + required keys |
| `test_fcff_math.py` | FCFF projection + valuation math (+ hypothesis property tests) |
| `test_payload_invalid.py` | Invalid model banner + fixture contracts |
| `test_priors.py` | Profile classify, bands, confidence breakdown, validity gate |
| `test_refine_assumptions.py` | Bounded adjustment application + flag-derived fallbacks |
| `test_memo_validation.py` | Assumption memo schema contracts |
| `test_kg_anchored.py` | KG cache anchoring rules |
| `test_synthesis_lifecycle.py` | Evidence synthesis lifecycle |
| `test_deck_inputs.py` | Deck source sanitization + DCF auto-inject |
| `test_deck_artifact_paths.py` | PPTX path resolution + `_default` adoption |
| `test_deck_theme.py` | DeckTheme audience presets |
| `test_deck_layout_spec.py` | Declarative layout_spec render smoke |
| `test_deck_outline.py` | Outline repair / empty-slide policy |
| `test_conversational_deck_routing.py` | Chat nudge + deck tool routing |
| `test_dcf_expectations_blocks.py` | Institutional DCF block kinds for deck |

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

**Current records (7 tickers):** AAPL, MSFT, META, NVDA, F, KO, WMT — see `tests/fixtures/golden/*.json`.

**Extrapolation recipe:** See `tests/fixtures/golden/README.md` for 10-step process to add a ticker. Variant handling for mega-cap, high-growth, startup (ARR), cyclical.

**Auto-discovery:** `test_golden_math.py` globs `golden/*.json` (skips `_template.json`) → parametrized tests per record (schema, math, Gordon precondition, accounting identity).

### What's not tested yet

- **LLM nodes** (`formulate_thesis`, `propose_assumptions`, `semantic_synthesis`, `scenario_generator`, `analyze_result` LLM step) — require LangSmith evaluator datasets. Marked `@pytest.mark.llm` so they don't run in CI by default.
- **External APIs** (yfinance, FMP, Tavily) — mocked or skipped.
- **Full workflow integration** — captured in `valid_aapl.json` payload fixture as regression baseline.

### Running tests

```bash
uv run pytest agent_project/tests/ -v       # all 407
uv run pytest agent_project/tests/unit/ -q  # unit only
uv run pytest agent_project/tests/golden/   # golden only
uv run pytest -k aapl                       # filter
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

# Deck workflow model overrides (defaults shown)
DECK_OUTLINE_MODEL=gpt-4o-mini
DECK_MODEL_DEFAULT=gpt-4o-mini
# DECK_MODEL_THESIS=gpt-4o          # per-layout override example
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
| `kg_run_id` separate from `parent_step_id` | Step id (activity) and run id (KG identity) are different concerns; conflating caused silent overwrite |
| `kg_run_id` minted in `normalize_input_node` | Single chokepoint — both server `ainvoke` and sync entry paths flow through it |
| Lineage encoded in `kg_run_id` suffix (`__from_…`) | Derivation chain visible without edge lookup; human-readable in DB |
| LLM reads full serialized subgraph, not a narrow query | Narrow NL→KGQuery was brittle (wrong field, wrong type); full subgraph + schema = better retrieval |
| `_KG_SCHEMA` injected into query prompt | LLM didn't know synthesis/drivers justify assumptions; explicit schema bridges the structural gap |
| View-model layer, not DB change, for hub model | Hub nodes are synthetic React-side objects; DB schema unchanged; presentation ≠ persistence |
| Two-tier drill-down (hub → category → table) | Mirrors analyst scan pattern; not storage layout |
| Ephemeral comparison artifacts | Storage design for workspace objects needs usage data before being set; premature persistence = clutter |
| Beliefs category always present (even 0 members) | Composer must be reachable before beliefs exist; data-conditional visibility hides the feature |
| User-stated nodes are sticky | Analyst conviction must never be silently overwritten |
| `model_validity` invalid → suppress point estimate | Honesty over confidence; better to flag uncertainty than fake precision |
| Golden test range ±50% around hand DCF | DCF inherently imprecise; tighter range = false positives from legitimate variation |
| `build_test_state(**overrides)` factory pattern | Adding a state key doesn't break every test |
| Deck compiler pipeline (not chat ReAct) | Deterministic, testable, HITL-friendly; expensive runs should not fail on validation |
| Auto-resolve deck `sources` from `dcf_output.json` | Models pass garbage in `sources`; disk is source of truth after DCF |
| `set_thread_id` inside deck HITL executor | `run_in_executor` drops contextvars; decks were saved under `runs/_default/` without this |
| Adopt `_default` deck artifacts on API read | Recover decks written before executor context fix without re-running workflow |

---

## What works

- [x] Intent router with auto / forced research / chat modes
- [x] Plan-then-execute with HITL approval
- [x] Per-step LangGraph node execution → checkpointing, streaming, resume
- [x] Multi-turn chat with streaming tokens
- [x] Append-only context stack (Manus-inspired)
- [x] All 9 tools functional, shared across subgraphs
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
- [x] KG hub visualization — static canvas (no physics), analyst-oriented hub model, two-tier drill-down
- [x] KG NL query — schema-injected prompt, causal question evidence augmentation, row-level glow
- [x] KG run identity — unique `kg_run_id` per run (accumulate, not overwrite), lineage in id
- [x] KG cache badges in ActivityTrace
- [x] Deck workflow — compiler pipeline, institutional block kinds, theme tokens, layout_spec, HITL outline, PPTX download + preview
- [x] Deck KG integration — deck_run + deck_slide nodes, HAS_DECK + HAS_SLIDE edges
- [x] Cross-run diff matrix (KgCompareRuns) — client-side, ephemeral
- [x] Run timeline strip (KgTimeline) — chronological, color=ticker, click→inspector
- [x] Beliefs composer — user_belief nodes writable from KG panel
- [x] Evidence inspection with clickable sources
- [x] FastAPI + SSE + HITL endpoints, job resume on restart
- [x] Static system prompt (KV-cache)
- [x] Multi-turn session memory
- [x] Unified activity contract (single event store/renderer)
- [x] `start.sh` kills stale processes
- [x] **Test suite: 407 tests (356 unit + 50 golden + 1 e2e), zero LLM calls in CI**
- [x] **Golden dataset workflow (AAPL FY2024) with extrapolation recipe**
- [x] **DCF report with numbered citations, hyperlinked References, sensitivity heatmap**
- [x] **DCF report PDF + Markdown download (ReportLab, wrapped tables)**
- [x] **HITL snapshot restore — fast path preserves evidence/provenance after approval**
- [x] **Verbatim DCF report in chat (no LLM re-synthesis)**
- [x] **Validity vs reconciliation policy — structural market gaps ≠ invalid model**
- [x] **Entity-aware RAG — document entity extraction at upload, gate model for relevance classification, mismatch detection, skip_gate for fast-path**
- [x] **Coloured RAG pipeline logging — entity extraction, hybrid search stages, gate verdict, document upload progress**
- [x] **Document → KG fact ingestion — doc_id-scoped extraction (no cross-doc contamination), typed income-statement facts, period-scoped keys for YoY coexistence**
- [x] **Filing persistence — one node per upload with "Open document" link to the original file**
- [x] **Raw fundamentals always persisted (facts decoupled from valuation confidence); derived inferences gated**
- [x] **App-level KG write notifications (fire with panel closed), Financials tabbed dock panel, DCF report button (inline PDF), timeline brush-to-zoom**
- [x] **KG Audit functional — cross-source/staleness/orphan/entity-coherence, ticker subset selection, auto-fix**

---

## Known limitations

- **Deck content gaps.** Scenario/sensitivity slides can render as title-only `section_header` when DCF payload lacks `scenario_results` or `sensitivity_chart` path (PNG may exist under `artifacts/` but not linked). Outline repair still allows empty section slides for `must_cover` topics without backing blocks.
- **Deck formatting is programmatic only.** No master `.pptx` template library yet; visual polish lives in `assemble.py` renderers + `DeckTheme`.
- **No DOCX or XLSX export.** DCF PDF/MD and deck PPTX are supported; other office formats are not.
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

### Deck workflow

**Content (Phase A):**
- DCF adapter fallbacks: build scenario blocks from `scenarios` when `scenario_results` empty; discover sensitivity PNG on disk
- Persist `scenario_results` + `sensitivity_chart` path in `dcf_output.json` upstream
- Outline policy: drop `section_header` slides with empty `block_refs`

**Formatting (Phase B–D):**
- Expand declarative `layout_spec` vocabulary + theme presets (shipped: B1 `DeckTheme`, B2 `layout_spec` overlay)
- Optional sandbox render script with curated python-pptx helper API (per-run layout, bounded)
- Master `.pptx` template + placeholder fill for firm-quality decks

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

Currently 7 tickers (AAPL, MSFT, META, NVDA, F, KO, WMT). Targets: additional edge cases (high-growth, cyclical, story stock). Per ticker: ~20 min using the documented extrapolation recipe.

### Comparable comps workflow

Reuses: HITL pattern, validity gate, KG cache (peer set), confidence breakdown, payload summariser. Different: no projection math — peer-multiple selection + outlier detection. Validates architecture generalises.

### Citations in UI

Backend ships numbered `[n]` refs in the report and hyperlinked References. **Remaining:** assumption table row → click → side panel with 1-3 evidence sources (inline refs in markdown are clickable via References links today).

### Prompt extraction + versioning

Move every inline prompt to `prompts/` as `.md` or `.j2`. Each prompt gets `THESIS_PROMPT_V = "2025-05-01"`. Log version alongside run output → diff "which prompt produced this."

### Streaming via LangGraph native

Port research-mode streaming to DCF: `app.astream_events()` per node → existing activity stream. Thesis / scenario / analysis LLMs are each 5–15s — perceived latency drops ~3×.

### KG run registry (Phase 1)

List all `dcf_run` nodes across sessions in a sortable registry. Now feasible since `kg_run_id` is unique per run and carries `{created_at, trigger, parent_run_id, implied_share_price}`.

### Comparison by drag (Phase 2)

Drag a `dcf_run` hub from the KG canvas into the compare artifact to assemble a diff. Only runs are draggable (not assumption nodes). Comparison artifact: ephemeral (UI state only).

### Comparison side-chat (Phase 3)

Side-chat on comparison artifact — LLM receives structured diff `{field, run_a, run_b, delta, pct}[]` as context. Cowork step 1 (bounded; doesn't require full session context).

### Workspace registry (Phase 4)

Durable, labeled objects (saved comparisons, pinned run sets) in a separate store — NOT the KG, NOT session chat. Design after Phase 3 usage reveals the right shape.

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

---

## KG Audit & Quality System (v2.1)

### Architecture

The Knowledge Graph now has a **defense-in-depth quality architecture** with three layers:

| Layer | Module | When | Cost |
|-------|--------|------|------|
| Write-time validation | `kg/ingest.py` | Every `kg_write()` call | Near-zero (deterministic) |
| Periodic audit | `kg/audit.py` | On-demand via API or scheduled | Low (LLM spot-checks only) |
| Human review | Frontend delete buttons | Manual | Human |

### New Modules

**`kg/audit.py`** — Standalone audit module (not a LangGraph workflow). Five check types:

1. **Cross-source consistency** — Same (ticker, field) with different values from different sources. Higher-tier source wins.
2. **Staleness detection** — Layer 2 facts past their TTL. Auto-deletes when `auto_fix=True`.
3. **Orphan detection** — Facts referencing deleted doc_ids.
4. **Entity coherence** — Filing ticker ≠ node ticker.
5. **Hallucination spot-check** — LLM re-extracts facts from ChromaDB source chunks and compares to KG (on-demand only).

All findings write to a **separate `kg_audit_log` SQLite table** — never back into the KG itself (avoids circular contamination).

**API endpoints:**
- `POST /kg/audit` — Run audit suite
- `GET /kg/audit/findings?ticker=AAPL&severity=warning` — Query historical findings

**`kg/ingest.py`** — Unified single write path (`kg_write()`) replacing all scattered `cache.put()` calls:

- **Source-tier precedence**: `user_stated(6) > filing(5) > structured_api(4) > document_extraction(3) > dcf_derived(2) > web_search(1)`
- **Confidence floors** per source (document_fact: 0.70, guidance: 0.65, web_search: 0.50)
- **Fast-path** for run-scoped and user nodes (bypass quality gates)
- **Structured logging** with `[KG ⚡]` (fast-path), `[KG ✓]` (accepted), `KG PUT` (cache write) prefixes

### Source-Tier Precedence

| Tier | Sources | Precedence |
|------|---------|------------|
| 6 | user_stated, user_override | Highest — human edits always win |
| 5 | filing (10-K, 10-Q) | SEC EDGAR data |
| 4 | structured_api (FMP, yfinance) | Live market data |
| 3 | document_extraction | Uploaded docs, LLM-extracted |
| 2 | dcf_derived | Computed from DCF model |
| 1 | web_search | Exa search results |

---

## RAG → KG Document Pipeline (v2.1)

### Document Ingestion Flow

```
Upload PDF → Parse → Chunk → Embed → Extract Entities (LLM) → Index in ChromaDB
                                                                    ↓
                                                          Extract Facts (LLM)
                                                                    ↓
                                                          Ingest via kg_write()
                                                                    ↓
                                                          Routed to KG sub-hubs
```

### Fact Extraction & Routing

Document facts are extracted via `gpt-4o-mini` (`_extract_document_facts()` in `documents.py`) and classified into semantic types. The frontend view model (`kgViewModel.ts`) routes them into **existing Financials sub-categories** — no separate "Documents" bucket:

| LLM fact_type | KG sub-hub | Sits alongside |
|---------------|-----------|----------------|
| `revenue`, `margin`, `eps`, `growth_rate`, `effective_tax_rate` | **Fundamentals** (metrics) | FMP/yfinance market data |
| `risk_factor`, `competitive_moat`, `wacc_signal` | **Drivers** (risks) | DCF-derived risk nodes |
| `guidance` | **Thesis** | DCF thesis narrative |
| `other` (unclassified) | **Synthesis** | Company summary |

### Filing Auto-Ingestion

When a document is classified as `sec_filing` or `annual_report`, a `filing` node is auto-created in the KG under **Financials → Filings** with metadata (filing_type, fiscal_period, filename, chunk count, page count). Deterministic filename detection (`10-K`, `10-Q`, `8-K`, `S-1`, `20-F`) overrides LLM classification for reliability.

### Key Bug Fixes

- **Key mismatch** — `extract_and_ingest_facts()` used wrong keys to read entity metadata (was `entity_ticker`, should be `ticker`). Fixed — every document now correctly flows facts to KG.

### Known Limitations

- **Thesis/synthesis nodes from RAG** — document facts with `guidance` land in the Thesis sub-hub but don't integrate into the thesis bull/bear narrative (they appear as generic field rows). The rendering doesn't match the rich thesis card format.
- **Number formatting** — document fact numeric values use the same `toLocaleString()` formatting as market data, but may differ in scale (billions vs raw dollars) since unit information isn't extracted.
- **Chat mode doesn't write to KG** — research/chat queries successfully find document chunks via hybrid search but discovered facts aren't persisted to the KG after tool calls. This is a planned enhancement.

---

## Frontend: KG Notification Widget (v2.1)

Phone-style floating notification cards in the bottom-right corner of the KG canvas. Aggregates new KG nodes by category with color-coded cards:

| Category | Color | Node types |
|----------|-------|-----------|
| DCF | Emerald | dcf_run, run_assumption, run_output |
| Financial Data | Sky | financials_hub, market_metric, structured_fundamental |
| Doc Facts | Amber | document_fact, key_fact |
| News | Purple | news_item |
| User Override | Rose | user_belief, user_stated |
| Filing | Indigo | filing |
| Risk | Red | risk, driver, risk_factor |

Cards auto-dismiss after 6 seconds with slide-in animation. Per-card ✕ dismiss and "Clear all" for bulk dismiss.

---

## Frontend: Per-Node Delete (v2.1)

Every KG node now has a hover-reveal ✕ delete button across all rendering paths:

- **Metrics table** — right side of each row
- **Driver table** — right side of each row
- **Beliefs section** — always visible ✕
- **Thesis/Synthesis/Filings** — top-right of each card

Backed by `kg.deleteNode` → `storage.delete_kg_node()` + cache invalidation. No confirmation dialog — deletion is immediate but reversible by re-running the source computation.

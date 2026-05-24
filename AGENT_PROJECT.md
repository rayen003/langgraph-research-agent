# Agent Project — Living Document

> **Purpose:** Single source of truth for architecture, decisions, and next steps.  
> **Update policy:** Edit this file whenever a feature is completed or a decision changes.

---

## Project Overview

A LangGraph-based research agent with a plan-then-execute flow, human-in-the-loop (HITL) approval, multi-turn conversational chat, local Python execution, DCF valuation with scenario analysis, and a React frontend.

**Stack:**
- **Orchestration:** LangGraph + LangChain
- **Model:** OpenAI `gpt-5-nano` (easily swappable)
- **Search:** Exa (`search_web` tool)
- **Python exec:** Local subprocess via `execute_python` (`pandas`/`matplotlib`/`requests`/`yfinance`)
- **Documents:** ChromaDB + BM25 hybrid RAG
- **DCF valuation:** Deterministic subgraph with SEC EDGAR, FMP/yfinance, CAPM WACC, scenario modeling, self-critique analysis loop
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
├── server.py              # FastAPI backend (SSE, HITL, artifacts, jobs, DCF download)
├── report_export.py       # DCF report PDF/MD export (ReportLab)
├── app.py                 # Chainlit entrypoint
├── graphs/
│   ├── __init__.py
│   ├── research.py        # Research subgraph: plan, HITL review, execute, synthesize, memory
│   ├── conversational.py  # Chat subgraph: ReAct loop with streaming
│   └── workflows/
│       └── dcf/           # DCF valuation subgraph (26 modules)
│           ├── graph.py        # Graph wiring + run_dcf_workflow_sync (public API) only
│           ├── lifecycle.py    # normalize_input_node, cache_check_node, route_after_cache_check
│           ├── scenarios.py    # scenario_generator_node, _violates_monotonicity, ScenarioOutput
│           ├── execution.py    # formulate_thesis_node, scenario_runner_node, ThesisOutput
│           ├── review_loop.py  # run_review_subgraph, route_after_review, route_after_review_val
│           ├── refinement.py   # analyze_result_node, refine_assumptions_node, _build_deterministic_flags
│           ├── payload.py      # summarize_dcf_payload, _run_consistency_checks
│           ├── sources.py      # SourceRegistry, numbered citations, References hyperlinks
│           ├── hitl_snapshot.py # HITL evidence/thesis/scenario serialize + restore
│           ├── state.py        # DCFState TypedDict + constants
│           ├── analysis.py     # convergence_gate_node, analysis_node, detect_divergences_node
│           ├── evidence.py     # Evidence assembly (5 tiers with inspectable items)
│           ├── fundamentals.py # FMP/yfinance fetchers
│           ├── sec_filings.py  # SEC EDGAR integration
│           ├── synthesis.py    # LLM semantic synthesis (CompanyState)
│           ├── memo.py         # LLM assumption memo (base case proposals)
│           ├── wacc.py         # CAPM WACC + profile quality stack + post-stack audit trail
│           ├── coherence.py    # Pre-valuation ops/WACC coherence gate
│           ├── valuation.py    # Deterministic FCFF math + finalize_node
│           ├── priors.py       # Profile priors + confidence breakdown
│           ├── review.py           # HITL assumption review gate
│           ├── review_graph.py     # Adversarial review subgraph (deep-dive + synthesis)
│           ├── review_state.py     # Isolated ReviewState TypedDict + Pydantic findings models
│           ├── assumptions.py      # Legacy regex-merge heuristics (unused)
│           └── activity.py         # DCF workflow activity emitters (emit_step, emit_review_substep)
├── kg/                          # Knowledge Graph layer (NEW)
│   ├── __init__.py              # Module entry — exports get_cache(), KGNode, KGEdge
│   ├── cache.py                 # KGCache singleton — write-through SQLite, O(1) lookups, TTL+confidence floor
│   └── query.py                 # NL→KGQuery (Pydantic) + deterministic graph executor
├── frontend/              # Vite + React + TypeScript + Tailwind
│   └── src/
│       ├── App.tsx        # Root: idle hero vs two-pane
│       ├── types.ts       # Shared TypeScript contracts
│       └── components/    # ActivityTrace (expandable detail panels for all DCF steps),
│                           # MessageThread, ChatBubble, ExecutionSidebar, DcfHitlSection…
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

Each `execute_one_step` is a real LangGraph node invocation → per-step checkpointing, streaming, and interrupt support (including DCF HITL).

### DCF Subgraph Flow

```
START → normalize_input → cache_check ──┐    (KG cache hits skip downstream)
    │                                    ├─→ formulate_thesis (if synthesis cached)
    └─→ assemble_evidence → semantic_synthesis → formulate_thesis
    → propose_assumptions → scenario_generator
    → review_assumptions (HITL interrupt)
    → scenario_runner → project_cashflows → compute_valuation
    → compute_implied_wacc → sensitivity
    → review_subgraph ──────────────────────────────────────────┐
        ├── review_deep_dive (adversarial LLM — finds only)     │
        └── synthesize_adjustments (deterministic Python fixes)  │
    → route_after_review                                         │
        ├── scenario_runner (loop, max 2 iterations) ───────────┘
        └── [assumption_journey] → finalize ── kg_backwrite ── END
```

Three layers:
- **Evidence layer** (assemble → synthesis): Turn messy sources into structured company understanding.
- **Thesis layer** (formulate_thesis → memo → scenario_generator → review): Form convictions, derive assumptions, produce scenarios, get HITL approval.
- **Valuation + review layer** (scenario_runner → valuation math → review_subgraph loop → finalize): Deterministic math + adversarial cross-check loop across scenarios. Max 2 review iterations; convergence check halts early.

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

### DCFState

```python
class DCFState(TypedDict):
    ticker, horizon_years, session_id
    assumptions, assumption_provenance, assumptions_approved
    # Scenarios
    scenarios: list[dict]           # bear/base/bull with probabilities
    scenario_results: list[dict]    # per-scenario valuation outputs
    # Thesis
    thesis: dict | None             # bull_thesis, bear_thesis, key_drivers, narrative, _fallback
    # Valuation
    valuation, sensitivity_table, wacc_components, confidence_label
    confidence_breakdown: dict | None   # per-component decomposition
    wacc_sanity: dict | None            # market-implied WACC vs CAPM
    # Evidence
    evidence_pack, company_state, assumption_memo, fundamentals
    # Review loop state
    assumption_history: list[dict]      # one record per completed review iteration
    initial_assumptions: dict           # snapshot before any review adjustments (for journey panel)
    analysis_iteration: int
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
| `run_dcf_workflow` | Deterministic DCF valuation with scenarios, thesis, analysis loop |

### DCF Workflow — Unified Across Chat and Research

The canonical `run_dcf_workflow` tool (in `tools.py`) emits `dcf_assumptions_review` SSE events on HITL, storing the payload via `set_dcf_hitl_payload()`. Both chat and research modes use the **same** tool instance — zero duplication.

| Mode | Pause mechanism | Resume |
|------|----------------|--------|
| Chat | ReAct loop detects `"⛔ STOP"` in tool output → breaks loop | Server injects `[DCF_APPROVED]` msg → new `ainvoke` |
| Research | `execute_one_step_node` detects `get_dcf_hitl_payload()` → calls `interrupt()` | `Command(resume={"approved": True, "assumption_overrides": {...}})` |

Both show the same `DcfHitlSection` component (assumptions table, EvidencePanel, ConfidenceBreakdownPanel, ImpliedWaccDetail, approve/reject buttons) via the shared `dcf_assumptions_review` SSE event.

On approval, the server persists a full **HITL snapshot** (`hitl_snapshot.py`) — evidence pack, provenance, scenarios, thesis, company state — and restores it before the fast-path re-invoke. The `[DCF_APPROVED]` message carries this snapshot so post-approval runs keep numbered citations instead of falling back to `user_provided`.

**Evidence persistence fix (May 2026):** `build_hitl_snapshot()` now reads from `evidence_pack.items` via `extract_evidence_items()` (not only the truncated `evidence_items` key on interrupt payloads). `finalize_node` writes both `_evidence_items` and `evidence_pack` into `dcf_output.json`. `dcf_source_metadata()` resolves cited IDs to real items for the citation drawer; falls back to `dcf_output.json` when tool-result JSON is sparse. The frontend drawer shows archived excerpts + URLs instead of generic “metadata not preserved” placeholders when items survive the fast path.

HITL handling is DRYed in `server.py` via `_handle_dcf_hitl()` — one function for both chat and research paths.

### DCF Report Delivery

After HITL approval, chat mode re-invokes via `dcf_valuation_app` (fast path). The full evidence/thesis/scenario work already ran during the review pass; the fast path runs deterministic math + review loop only. **`scenario_runner` is omitted on the fast path** so scenario progress tokens do not overwrite the final chat message.

**Report assembly** — `payload.summarize_dcf_payload(for_display=True)` (default) builds user-facing markdown:

- Executive summary with validity + reconciliation posture (`valid` vs `structural_gap` vs `invalid`)
- Sensitivity matrix (WACC × TGR) + `[SENSITIVITY_CHART]` marker placed before thesis/assumptions
- Assumptions table with Basis + inline `[n]` refs
- Formatted Company Context (bullets + `###` subsections), Valuation Detail table, Consistency Checks
- Hyperlinked References appendix via `sources.py` (`SourceRegistry`, SEC/web/FMP URLs; inferred fallbacks when metadata missing)
- **Shareholder Mechanics** table — initial shares, horizon buyback effect, SBC drag on FCFF margin, perpetual buyback + effective terminal growth (from `valuation` fields; no full share ledger yet)
- **WACC stack audit trail** — Base CAPM → profile quality discounts → post-review/coherence deltas; footer shows **WACC used in valuation** (must match assumptions table + sensitivity center)
- **Assumption Coherence** section — ops/WACC tier classification + auto-corrections when not user-pinned

LLM-only instruction lines (`_assistant_instruction_lines`) are excluded from display output.

**Verbatim delivery** — When a DCF report is present, chat/research skip a second LLM synthesis pass. The markdown from `summarize_dcf_payload` is returned as-is (`dcf_report_verbatim: true` on the tool pointer).

**Artifacts + export**

| Artifact / endpoint | Purpose |
|---------------------|---------|
| `artifacts/sensitivity_{ticker}.png` | Heatmap from `valuation._render_sensitivity_heatmap()` |
| `GET /runs/{thread_id}/dcf-report.md` | Markdown download |
| `GET /runs/{thread_id}/dcf-report.pdf` | ReportLab PDF with wrapped tables + embedded heatmap |
| `GET /artifacts/{thread_id}/{filename}` | Serve PNG and other run artifacts |

PDF generation: `report_export.py` (depends on `reportlab`, `markdown`). Frontend: `DcfReportCard` + `DcfReportDownloadMenu` in `MessageThread.tsx` render inline chart at marker + PDF/MD dropdown.

**Validity policy** — Unexplained market-implied WACC/growth/margin gaps set `reconciliation_status: structural_gap` but leave `model_validity: valid`. Only solver failures or critical unresolved issues mark the model `invalid`.

### WACC stack, coherence gate, and audit trail (May 2026)

Epistemic guardrails added so discount-rate logic is observable and internally consistent in the report.

| Component | Module | Behavior |
|-----------|--------|----------|
| **Profile WACC stack** | `wacc.py` | Bottom-up CAPM → additive quality discounts (mega-cap durability, high FCFF margin, net-cash) → clip to profile soft band (`priors.py`). Writes `wacc_stack` into `wacc_components`. |
| **Coherence gate** | `coherence.py` | Runs before every projection fan-out (`coherence_gate` node). Detects ops/WACC mismatch (e.g. strong growth + high WACC). Auto-pulls WACC toward profile midpoint when not user-pinned; enforces profile band. Review-loop and convergence re-routes go through this gate (not straight to `project_cashflows`). |
| **Post-stack WACC sync** | `wacc.append_wacc_stack_delta()` | When review loop, coherence gate, or refinement adjusts WACC after the stack is built, appends a labeled delta line (e.g. `Review-loop adjustment (pass 1): +1.00%`) and sets `final_wacc` = assumptions WACC. Report footer: **WACC used in valuation** — eliminates stale 8.90% stack vs 9.90% valuation divergence. |
| **Shareholder mechanics** | `payload.py` | Surfaces buyback/SBC/per-share fields already computed in `compute_valuation_node` — no year-by-year share ledger yet. |
| **Report formatting** | `payload.py`, `sources.py` | `shares_outstanding` shows `15,005M` (not `$15,005M`); References use human titles via `infer_evidence_item()` + `format_reference_line()` for missing metadata. |
| **Chat timeout fix** | `conversational.py` | Completed DCF tool results are terminal; timeout fallback emits report verbatim (`test_conversational_dcf_fallback.py`). |
| **Interpretive confidence** | `analysis.py`, `payload.py` | Evidence grounding penalty when memo cites web/news but SEC filings exist in pack; procedural vs interpretive confidence decomposition in executive summary. |

**Graph wiring:** `coherence_gate` sits between HITL/scenario runner and `project_cashflows` on both main and fast-path graphs. Review loop WACC deltas are profile-clipped (`review_loop.py`).

**Tests added:** `test_wacc_stack_coherence.py`, `test_report_shareholder_mechanics.py`, `test_evidence_persistence.py`, `test_conversational_dcf_fallback.py`, `test_dcf_valuation_math.py`, `test_capital_assumption_fields.py`, e2e harness `tests/e2e/test_dcf_app_trace.py` (opt-in `--run-dcf-e2e`). Full unit suite: 262 tests.

**Known philosophical tension (not yet implemented):** Profile bands and coherence midpoint-pull act as active guardrails today. Longer-term intent is CAPM-primary with profile bands as fallback/validator only and coherence flag-first (not auto-pull).

### DCF Scenario-Based Valuation

The memo node proposes a base case. `scenario_generator_node` uses an LLM (configurable via `DCF_SCENARIO_MODEL`) to derive bear and bull variants from the base case + investment thesis. `scenario_runner_node` runs the valuation subgraph 3× (once per scenario) using `dcf_scenario_val_app` — a stripped-down graph without analysis loop.

Output: expected value = Σ(probability × price), range = min–max, scenario table in both the sidebar detail panel and the markdown report.

### DCF Thesis + Review Subgraph

- **`formulate_thesis_node`** — LLM produces structured thesis (`KeyDriver` Pydantic model, `extra='forbid'` for OpenAI structured output compatibility) from evidence + synthesis. Sets `_fallback: True` when LLM fails; this propagates as a `HIGH` severity flag to the review subgraph and renders an amber warning badge in the UI.
- **`review_subgraph`** — adversarial cross-check subgraph (isolated `ReviewState`, one-way data flow from `DCFState`):
  - **`review_deep_dive_node`**: LLM finds contradictions across three layers — evidence↔memo, thesis↔assumptions, market signals. Receives NO valuation output (prevents backward anchoring). Emits structured `ScenarioFinding` objects.
  - **`synthesize_adjustments_node`**: deterministic Python. Votes across findings, applies convergence damping (same-direction repeat adjustments halved), hard-clamps via `_FIELD_CLAMP`. LLM never mutates numbers.
- **`build_deterministic_flags`**: signals fed to the reviewer — terminal weight, implied-vs-spot gap, WACC sanity gap, sensitivity swing, confidence, TGR vs Rf, thesis fallback.
- **Convergence**: stops when max delta < 0.5% across all scenarios, or max 2 iterations.
- **`assumption_journey`**: emitted by `finalize_node` when review ran; shows per-field diff table (initial → after each review iteration → final) in the UI.

### DCF Evidence Inspection

The `Assembling evidence` detail panel in the sidebar shows individual evidence items with tier badges (`FILING`, `API`, `WEB`, `DOC`, `MKT`), titles, dates, and clickable content previews (first 300 chars) + URLs. Items are sent as lightweight previews in the SSE event payload.

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
├── artifacts/           # sensitivity heatmaps, sandbox plots, etc.
├── dcf_output.json      # full DCF payload (written by finalize_node)
└── final_report.md      # synthesized markdown report (research mode)

runs/agent.db            # SQLite: jobs, job_events, job_steps, reports, session_memory, documents
runs/chroma/             # ChromaDB: document embeddings
```

### Knowledge Graph (memory layer)

A typed graph of entities, beliefs, and DCF run artifacts. Acts as:
- **Cache** — DCF nodes check the KG before fetching/computing. Hits skip expensive work.
- **Memory** — chat and DCF write back to the KG; user beliefs persist across sessions.
- **Explainability** — every fact has a `source`, `confidence`, and `updated_at`; runs leave a traversal trail.

**Storage:** SQLite (`kg_nodes`, `kg_edges`, `kg_traversals`) is the durable source of truth. `KGCache` (in-process dict singleton) is a write-through cache for O(1) lookups during runs.

**Node types:**
- *Shared* (merged by confidence): `company`, `driver`, `theme`, `risk`, `user_belief`, `market_metric_fund`, `market_metric_price`, `company_synthesis`, `thesis`, `person`.
- *Run-scoped* (immutable, never merged): `dcf_run`, `run_assumption`, `run_output`, `run_scenario`.

**Node IDs:** Deterministic — `"{ticker}::{node_type}::{field}"` (shared) or `"{ticker}::{node_type}::{run_id}::{field}"` (run-scoped). Enables dict lookups, no graph traversal needed for the hot path.

**Cache rules:**
- `TTL` per node type (1h price · 24h fundamentals · 7d driver/thesis/synthesis · ∞ user_belief/run_*).
- `confidence_floor = 0.7` — below this, treat as miss and refresh.
- Compound types (`thesis`, `company_synthesis`) additionally check `input_hash` against `evidence_hash(ticker)`; stale if inputs changed.
- `user_stated` source is sticky — auto-updates never overwrite (controlled by `respect_user_lock`).

**Update triggers:**
- **T3** (DCF complete): `finalize_node` calls `_write_to_kg` — creates `dcf_run` anchor + `run_assumption` + `run_output` + refreshes shared `market_metric` / `company_synthesis` / `thesis` / `driver` nodes.
- **T4** (User edit): `PATCH /kg/{session_id}/nodes/{id}` — source becomes `user_stated`, confidence=1.0.
- T1 (chat extraction) and T2 (tool result) are future work; not yet wired.

**Injection points inside DCF:**
- **I1** — `cache_check_node` probes KG for `market_metric_fund`, `market_metric_price`, `company_synthesis`, `thesis`. Sets `kg_cache_flags` and pre-fills `state["company_state"]` / `state["thesis"]`.
- **I2** — `formulate_thesis_node` short-circuits if `skip_formulate_thesis` is set; returns cached thesis.
- I3 (assumption hints from `user_belief`) is future work.

**REST API:** `GET /kg/{session_id}` · `GET /kg/{session_id}/subgraph/{ticker}` · `POST/PATCH/DELETE /kg/{session_id}/nodes(/{id})` · `POST/DELETE /kg/{session_id}/edges(/{id})` · `POST /kg/{session_id}/query` · `GET /kg/{session_id}/traversal/{run_id}`.

**DCF report download:** `GET /runs/{thread_id}/dcf-report.md` · `GET /runs/{thread_id}/dcf-report.pdf` · `GET /artifacts/{thread_id}/{filename}`.

**Natural-language query:** `POST /kg/{session_id}/query` → LLM extracts `KGQuery` (intent, ticker, node_type, field) → deterministic executor walks the cache → returns answer + matched nodes + traversal path. Intents supported: `lookup`, `compare_runs`, `why_assumption`, `list_drivers`, `recent_changes`.

**UI:**
- Activity sub-step rows show a teal `⚡ KG` badge when a step was skipped via cache hit (e.g. `formulate_thesis` reads `thesis_quality === 'cached'`).
- `cache_check` step expander shows per-field hit/miss table with ages and actions.
- **🧠 Knowledge Base button** (floating, bottom-right) toggles a right-panel `KnowledgePanel` with React Flow visualization, node inspection drawer (`KgNodeCard`), and NL query drawer (`KgQueryDrawer`). Custom React Flow nodes are colour-coded by `node_type` + `source` (green=user_stated, blue=company/metric, amber=driver, etc.). Query results highlight the traversed subgraph with teal animated edges.

### Activity Telemetry

Unified `ActivityEvent` contract (`activity.py`) describes every unit of agent work. Legacy events removed — single store and renderer (`ActivityTrace`). All DCF substeps have expandable detail panels with step-specific information.

---

## What's Working

- [x] Intent router with auto/forced research/chat modes
- [x] Plan-then-execute flow with HITL approval
- [x] Per-step LangGraph node execution → checkpointing, streaming, resume
- [x] Multi-turn chat with streaming tokens
- [x] Append-only context stack (Manus-inspired)
- [x] All 8 tools functional, defined once in `tools.py`, shared by all subgraphs
- [x] DCF valuation with thesis node, scenario modeling (bear/base/bull), adversarial review subgraph
- [x] DCF HITL unified across chat and research (same tool, same events, same UI)
- [x] PlanStore — single seam for plan persistence (disk + SQLite)
- [x] SEC EDGAR integration for free 10-K/10-Q extraction
- [x] Session-doc RAG (ChromaDB + BM25 hybrid)
- [x] Python execution with matplotlib artifacts
- [x] React frontend with streaming reports, DcfHitlSection, DcfReportCard (inline heatmap + PDF/MD download), ActivityTrace with expandable detail panels for all DCF substeps
- [x] ActivityTrace two-level grouping — `review_subgraph` shows nested `review_deep_dive` / `synthesize_adjustments` sub-steps with progress badge
- [x] ActivityTrace ×N re-run badge on `scenario_runner` when review loop iterates
- [x] AssumptionJourneyDetail panel — per-scenario diff table (initial → iteration N → final) with field-level delta highlights
- [x] Thesis fallback amber badge — amber dot + `⚠ fallback` pill on `formulate_thesis` row; warning banner in expanded detail
- [x] Knowledge Graph layer — SQLite + in-process cache, cache-first DCF execution, back-write on finalize
- [x] KG REST API — full CRUD on nodes/edges, NL query endpoint, traversal replay
- [x] KG visualization panel — React Flow with custom typed nodes, node inspect/edit drawer, NL query drawer with traversal highlight
- [x] KG cache badges in ActivityTrace — teal `⚡ KG` pill on cache-hit substeps, `KgCacheDetail` expander with per-field table
- [x] Evidence inspection — clickable source items with content previews and URLs
- [x] FastAPI backend with SSE, HITL endpoints, job resume on restart
- [x] Static system prompt for KV-cache
- [x] Multi-turn session memory
- [x] Unified activity contract (single event store/renderer)
- [x] DCF report with numbered citations, hyperlinked References, sensitivity matrix + heatmap artifact
- [x] DCF report PDF + Markdown export (`report_export.py`, ReportLab wrapped tables)
- [x] HITL snapshot restore — fast path preserves evidence/provenance after approval
- [x] Evidence pack persisted through HITL → finalize (`extract_evidence_items`, `_evidence_items` + `evidence_pack` in `dcf_output.json`)
- [x] Citation drawer shows real web/FMP excerpts + URLs (not generic inferred placeholders)
- [x] WACC stack audit trail synced with valuation WACC (review/coherence/refinement deltas)
- [x] Shareholder Mechanics report section (buyback, SBC drag, terminal buyback compounding)
- [x] Pre-valuation coherence gate + profile-based WACC stack
- [x] Chat DCF timeout fallback — verbatim report when LLM synthesis times out
- [x] Verbatim DCF report in chat (no LLM re-synthesis of valuation output)
- [x] Validity vs reconciliation policy — structural market gaps ≠ invalid model
- [x] `start.sh` runs `uv sync` and kills stale processes before launch

---

## Known Issues / Limitations

- **No document generation beyond DCF PDF/MD.** No PPTX, DOCX, or XLSX output yet.
- **Standalone DCF endpoint (`POST /workflows/dcf/runs`) HITL resume is broken.** The dedicated HTTP endpoint's `Command(resume=...)` path may not find saved interrupt state in MemorySaver. The agent-tool path (chat and research) works correctly.
- **Completed job report opens in new tab.** Clicking a completed job in `JobsPanel` opens a Blob URL; not yet loaded into the main research view.
- **Worker model is single-process.** No DB claim lock / multi-worker coordination yet.
- **Document ingestion does not resume.** Uploads interrupted mid-embedding may remain `processing`/`err`; no retry queue yet.
- **Exa search is good but not deep enough for financial research.** Still needs full source-content fetching and citations for deep work.
- **Review adjustments sparse in fast path.** When `scenario_results` is empty (fast-path, no scenarios), `synthesize_adjustments` has no scenario deltas to apply — adjustments target base-only state. Meaningful only when full scenario set exists.
- **Market-implied signals incomplete.** Only implied WACC is computed; implied growth and margin are needed for deeper critique.

## Roadmap

### Phase 1 ✅ — Scenarios + Thesis + Analysis Loop

- [x] `formulate_thesis_node` — investment thesis from evidence
- [x] `scenario_generator_node` — bear/base/bull from base case + thesis
- [x] `scenario_runner_node` — per-scenario DCF with expected value + range
- [x] `analyze_result_node` — constrained critique with deterministic flags
- [x] `refine_assumptions_node` — bounded assumption adjustments
- [x] `refine_assumptions_node` — bounded assumption adjustments with mechanical fallbacks
- [x] UI detail panels for all new steps (ThesisDetail, AnalysisDetail, ScenarioRunnerDetail, ScenarioGeneratorDetail)
- [x] Evidence inspection in sidebar (clickable source items)
- [x] Structured LLM output via Pydantic models (ThesisOutput, AnalysisCritique, ScenarioOutput)

### Phase 2 ✅ — Review Subgraph (multi-layer deliberation)

Replaced the overloaded single `analyze_result_node` with a nested `review_dcf` subgraph that separates finding problems from fixing them.

#### What shipped

- **`review_graph.py`** — adversarial subgraph with two nodes:
  - `review_deep_dive_node`: one LLM call, finds contradictions only (evidence↔memo, thesis↔assumptions, market signals). No valuation output shown (prevents backward anchoring). Structured output via `ReviewFindings` Pydantic model with `ScenarioFinding` items.
  - `synthesize_adjustments_node`: deterministic Python. Votes across findings, convergence damping (same-direction repeat halved), hard-clamps via `_FIELD_CLAMP`. LLM never mutates numbers.
- **`review_state.py`** — isolated `ReviewState` TypedDict + `ScenarioFinding` / `ReviewFindings` Pydantic models.
- **`graph.py`** — `run_review_subgraph` gateway: snapshots `DCFState` → `ReviewState` (one-way), applies adjustments back, records `assumption_history`. `route_after_review` / `route_after_review_val` handle loopback vs finalize routing for main graph and fast path.
- **`state.py`** — added `assumption_history: list[dict]`, `initial_assumptions: dict`.
- **`valuation.py`** — `finalize_node` emits `assumption_journey` step when review ran.
- **`activity.py`** — added `emit_review_substep` for sub-step nesting under `review_subgraph`.
- **`build_deterministic_flags`** — thesis-fallback signal (`severity: "severe"`) when `thesis._fallback` is set.
- **Thesis quality gate** — `formulate_thesis_node` marks fallback thesis with `_fallback: True`, emits warning summary, logs HIGH severity warning.
- **Pydantic fix** — `KeyDriver` nested model with `ConfigDict(extra='forbid')` fixes OpenAI structured output 400 error (`additionalProperties` required).

#### Design decisions

| | Old (single node) | New (review subgraph) |
|--|-------------------|-----------------------|
| LLM's job | Find problems AND fix them | Find problems only |
| Adjustment quality | LLM guesses deltas (often empty) | Deterministic rule table |
| Observability | One opaque "interpretation" | Structured findings per layer |
| Stopping | Max iterations | Delta convergence + max iterations |
| State pollution | DCFState accumulates critique | Isolated ReviewState |
| Memo anchoring | Treated as ground truth | Explicitly challengeable |

### Phase 3 — Market Signals + Report Quality (partially shipped)

**Shipped:**
- Reverse-solved implied WACC/growth/margin via `compute_market_signals_node`
- `reconciliation_status: structural_gap` when price embeds different expectations but DCF math is valid
- Numbered `[n]` citations + hyperlinked References appendix (`sources.py`) with human titles for FMP/web/SEC fallbacks
- Sensitivity matrix + heatmap artifact + inline `[SENSITIVITY_CHART]` marker
- PDF/Markdown export with table width fixes (`report_export.py`)
- Scannable report formatting (bullets, subsections, valuation table, Shareholder Mechanics, WACC stack + coherence sections)
- HITL snapshot restore for fast-path citation fidelity + evidence pack serialization fix
- WACC stack / coherence gate / post-adjustment audit trail (see section above)
- Citation drawer wired via `chat_complete` → `citation_map` + `evidence_items` (`dcf_source_metadata`)

**Remaining:**
- Conviction label surfaced more prominently in UI footer
- Assumption table row → click → evidence side panel (References links exist today)

#### Original Phase 3 spec (reference)

- Reverse-solve implied growth/margin from market price via bisection (new pure functions in `valuation.py`)
- New `compute_market_signals_node` replaces `compute_implied_wacc_node`, adds growth + margin
- Thesis strengthened: mandatory structured output with `direction` + `confidence` fields
- Review prompt gains assumption anchoring check (Layer 0) + market-implied signals section
- Routing simplified: single `route_after_review`, conviction is output-only (not routing)
- Conviction computed deterministically in `finalize_node` from gap + dispersion + unanchored count
- ~200 lines total, no new LLM calls

#### Updated graph
```
START → normalize_input → assemble_evidence → semantic_synthesis
 → formulate_thesis (mandatory, structured) → propose_assumptions
 → scenario_generator → review_assumptions (HITL)
 → scenario_runner → project_cashflows → compute_valuation
 → compute_market_signals (NEW: implied WACC + growth + margin) → sensitivity
 → review_subgraph (gains: anchoring check + market signals)
 → route_after_review → [scenario_runner | finalize]
 → finalize (computes conviction, writes shaped report)
```

#### Files changed
`valuation.py` (+3 functions), `state.py` (+2 fields), `review_state.py` (+2 fields + 1 finding attr),
`review_graph.py` (prompt update), `graph.py` (+1 node, +wiring), `tools.py` (summary update),
`server.py` (state update), `toolLabels.ts` (+1 label)

### Phase 4 — Confidence Gating + Exit Modes

Confidence mechanically derived from diagnostics. Exit modes (clean/refined/unstable) surfaced in UI based on review loop outcome.

### Phase 5 — Assumption Memory

Store historical assumptions per ticker. Flag deviations from prior runs.

---

## Interview Sprint Roadmap

Triage for a 2-week interview-readiness sprint. Ordered by interview ROI.

### P0 — Must ship (each blocks a canonical interview question)

#### 1. Golden dataset + eval harness

- **Tickers:** AAPL, META, NVDA (mega-cap), SHOP (high-growth), KO (mature), F (cyclical), PLTR (story stock), TSLA (volatile), 1 SPAC, 1 small-cap miner (edge cases)
- **Per ticker:** expected validity outcome, expected confidence band, sanity bounds on implied price (±50% of street consensus)
- **Harness:** `pytest tests/golden/` — runs DCF, asserts validity matches expected, price in band, no UNEXPLAINED divergences for "valid" cases
- **Snapshots:** key fields to JSON, diffed on regression
- **Why:** every Rogo interviewer asks "how do you know it works." No good answer exists without this.

#### 2. Citations surfaced in UI

- **Shipped:** numbered `[n]` refs in report markdown; hyperlinked References appendix (SEC, web, FMP); click `[n]` in report → `EvidenceSourceDrawer` with title, excerpt, and “Open original source” when metadata archived; FMP “View underlying API data” for structured fundamentals
- **Remaining:** assumption table row → click → evidence side panel (inline row drill-down; References + citation drawer cover most cases today)

#### 3. Prompt extraction + versioning

- Move every inline prompt to `prompts/` directory as `.md` or `.j2` templates
- Each prompt gets a version constant (`THESIS_PROMPT_V = "2025-05-01"`)
- Log version constant alongside run output → diff "which prompt produced this"
- **Why:** every prompt-eval interview question fails without this. Trivial work, huge signal.

---

### P1 — High leverage (1 week each)

#### 4. Streaming via LangGraph native

- Port research-mode streaming to DCF: `app.astream_events()` per node → existing activity stream
- Thesis LLM, scenario LLM, analysis LLM each 5–15s — perceived latency drops ~3×
- **Why:** agentic UX without streaming feels broken in 2025.

#### 5. Second workflow: comparable comps

- Reuses: HITL pattern, validity gate, KG cache (peer set), confidence breakdown, payload summarizer
- Different: no projection math — peer-multiple selection + outlier detection instead
- Validates architecture generalizes; kills "one-trick demo" objection
- **Why:** "I built two workflows on the same chassis" is a different interview story.

#### 6. Defer confidence emission (architecture cleanup)

- Currently `compute_valuation` emits confidence pre-gate, then `finalize_node` re-emits to overwrite (patch)
- Cleaner: `compute_valuation` emits a `pending` badge; `finalize` emits the final value
- **Why:** shows engineering judgment — "I shipped the patch, then fixed the architecture."

---

### P2 — Polish for credibility (2–4 days each)

#### 7. Wire RAG into evidence layer

- `documents.py` + ChromaDB exist but unused in `assemble_evidence_node`
- User uploads PDF (10-K, transcript, broker note) → indexed → assumptions cite retrieved chunks
- **Why:** shows evidence layer is extensible, not just API-consuming.

#### 8. Run-diff view (replace KG canvas)

- "Compare run A vs run B" — assumption table side-by-side, delta column, which divergence flipped
- Higher analyst utility than force-directed graph; frees ~600 lines
- **Cut:** KG canvas visualization

#### 9. Confidence breakdown tooltip

- `confidence_breakdown` already has per-component scores (`wacc_reliability`, `evidence_coverage`, `validity_penalty`)
- Surface as expandable card on hover/click of confidence chip in UI
- e.g. "HIGH because: WACC reliable (0.85), 12 evidence refs (0.78); LOW because: solver failed (−0.30)"
- **Why:** shows confidence is explainable, not vibes.

#### 10. File splits (engineering hygiene)

- `valuation.py` 953 lines → `compute/`, `finalize/`, `kg_write/` sub-modules
- `ActivityTrace.tsx` 2101 lines → split per activity-type renderer
- `MessageThread.tsx` 653 lines → extract `Bubble`, `DegradedBanner`, `CommittedMessage`

---

### P3 — Nice-to-have

#### 11. Latency budget + telemetry

- Log per-node duration to run output JSON
- Surface in UI footer: "DCF took 47s (thesis 8s, scenarios 12s, valuation 3s, analysis 19s, review 5s)"
- Enables cost/perf tradeoff reasoning in interview.

#### 12. Output delta on rerun

- Extend existing monospace diff message to include output delta
- "You changed `tax_rate` 15.6% → 12%, implied price moved $182 → $194 (+6.6%)"

#### 13. User research

- 2 finance users (analyst + student), 30 min Loom each
- "I tested with 2 users, both missed X, I fixed Y" — strong interview answer
- Note ≤2 confusing things per session; fix them before interview

---

### Cut list (do not build)

- More KG visualization
- More chat session features
- Auth / multi-user
- Deployment infra (Rogo has it)
- Mobile UI
- Theme switcher / settings page

---

### Suggested 2-week sprint

| Day | Work |
|-----|------|
| 1–2 | Golden dataset (5 tickers, expected ranges) |
| 3–4 | Pytest harness + run baseline |
| 5 | Prompt extraction to `prompts/` + version constants |
| 6–7 | Citations in UI (assumption row → evidence panel) |
| 8 | Streaming port from research mode → DCF |
| 9–10 | Comps workflow scaffold (reuse 70% of DCF chassis) |
| 11 | User test with 1–2 finance people, capture confusion points |
| 12 | Confidence tooltip + run-diff view (replace KG canvas) |
| 13 | Latency telemetry + writeup |
| 14 | Buffer / fix what user test surfaced |

---

## Key Design Decisions (Log)

| Decision | Rationale |
|---|---|
| Append-only context_stack | KV-cache friendly; follows Manus principle |
| Tool results stored on disk, pointer in message | Keeps context short; full data retrievable on demand |
| Static system prompt | Identical across all steps → near-100% KV-cache hit |
| Deterministic memory compression | Zero latency, zero cost, no LLM summarisation |
| One tool definition in `tools.py`, shared by all subgraphs | Eliminates duplication; single source of truth |
| `execute_one_step_node` loop in parent graph | Per-step checkpointing, streaming, interrupt — not possible with monolithic node |
| PlanStore wraps disk + SQLite behind one seam | No dual-write bugs; swap adapter for tests or Postgres |
| DCF HITL unified across chat and research | Same tool, same events, same frontend |
| Scenarios added as separate nodes, not inside memo | Keeps memo node stable; scenario generator is a clean new layer |
| Scenario valuation uses separate compiled graph | Avoids running analysis loop per-scenario; runs analysis on combined results |
| Evidence items sent as lightweight previews in SSE | Enables UI inspection without extra API calls |
| Exa over Tavily | Better semantic search and excerpts |
| FMP + yfinance for DCF levels | Canonical scale and margins from statements |
| ReviewState isolated from DCFState (one-way snapshot) | Prevents state pollution; subgraph can't accidentally mutate upstream state |
| LLM finds problems, Python applies fixes | LLM is better at judgment than arithmetic; deterministic rule table prevents LLM hallucination on deltas |
| No valuation output passed to reviewer | Prevents backward anchoring — reviewer critiques assumptions, not implied price |
| Convergence damping (same-direction repeat halved) | Prevents oscillation without explicit history check |
| `_fallback: True` marker on thesis + HIGH severity flag | Thesis quality propagates to review subgraph without special-casing; UI surfaces it at the source |
| `KeyDriver` nested Pydantic model with `extra='forbid'` | Required for OpenAI structured output — `additionalProperties: false` must be set on all nested objects |
| KG uses SQLite as source of truth, in-process dict as cache | User edits between runs require durable store; in-process cache gives O(1) hot path |
| KG nodes use deterministic IDs (`ticker::type::field`) | No graph traversal for cache lookups — pure dict access |
| KG separates shared (entity facts) from run-scoped nodes | Avoids overwrite on repeat DCF runs; multiple runs coexist; comparable in UI |
| LLM only translates NL→`KGQuery`, never traverses | Prevents hallucinated answers; all facts come from the actual graph |
| User-stated nodes are sticky (auto-writes respect lock) | Analyst conviction must never be silently overwritten by FMP/LLM |
| Conditional routing on `kg_cache_flags` | Skipped nodes don't run; cache hits flow into state directly |

---

## Environment Variables

`.env` in `agent_project/`:

```env
OPENAI_API_KEY=sk-proj-...
EXA_API_KEY=...                    # web search
FMP_API_KEY=...                    # Financial Modeling Prep (DCF fundamentals)
# Optional — DCF CAPM calibration (defaults 0.045 / 0.055 if unset)
DCF_RISK_FREE_RATE=0.045
DCF_EQUITY_RISK_PREMIUM=0.055
# Optional — DCF LLM model overrides (defaults to gpt-4o)
DCF_SYNTHESIS_MODEL=gpt-4o
DCF_MEMO_MODEL=gpt-4o
DCF_THESIS_MODEL=gpt-4o           # thesis formulation
DCF_ANALYSIS_MODEL=gpt-4o         # analysis loop critique
DCF_SCENARIO_MODEL=gpt-4o         # bear/bull scenario generation
DCF_REVIEW_MODEL=gpt-4o           # adversarial review deep-dive
KG_QUERY_MODEL=gpt-4o-mini        # NL→KGQuery translation (cheap classifier)
```

---

## Running

```bash
cd /Users/rayengallas/Project/langgraph-research-agent
./start.sh
# Backend:  http://localhost:8080
# Frontend: http://localhost:5174
```

`start.sh` runs `uv sync`, kills any existing processes on ports 8080 and 5174-5178, clears `__pycache__`, then launches the FastAPI backend (uvicorn) and Vite dev server with colored logs and graceful shutdown.

### LangSmith Studio

Root `langgraph.json` registers two graphs. From the **repo root**:

```bash
uv sync --extra studio
uv run --extra studio langgraph dev
```

| Graph ID | Module | What you see |
|----------|--------|--------------|
| `agent` | `file.py:app` | Full agent — intent → research path or chat path |
| `dcf_workflow` | `graphs/workflows/dcf:dcf_workflow_app` | Standalone DCF subgraph (16 nodes) |

Studio UI: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`.

---

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore the codebase.**

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

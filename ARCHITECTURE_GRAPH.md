# LangGraph Research Agent — Architecture Graph

> **Generated:** 2026-05-31
> **Source:** README.md + 689 indexed source files from `agent_project/`
> **Project:** Financial analysis agent with deterministic DCF valuation, knowledge graph, and React frontend

---

## Meta

| Property | Value |
|----------|-------|
| **Stack** | Python / LangGraph / LangChain / FastAPI ↔ TypeScript / React / Vite / Tailwind |
| **LLM** | OpenAI `gpt-4o` family (configurable per node via env) |
| **Web Search** | Exa |
| **Fundamentals** | Financial Modeling Prep API + yfinance fallback |
| **Filings** | SEC EDGAR (free, no key) |
| **Documents** | ChromaDB + BM25 hybrid RAG |
| **Persistence** | SQLite + disk JSON + ChromaDB |
| **Tests** | 407 (356 unit + 50 golden + 1 e2e), zero LLM calls in CI |

---

## Layer 1: Entry & Orchestration

```
                    ┌─────────────────────┐
                    │   server.py         │
                    │   FastAPI + SSE     │
                    │   HITL endpoints    │
                    │   KG REST API       │
                    └────────┬────────────┘
                             │ compiles graph
                    ┌────────▼────────────┐
                    │   file.py           │
                    │   Parent graph      │
                    │   intent routing    │
                    └───┬──────────┬──────┘
               routes to │          │ routes to
          ┌──────────────▼──┐  ┌───▼──────────────┐
          │ research.py     │  │ conversational.py │
          │ Plan-then-exec  │  │ Streaming ReAct   │
          │ HITL plan       │  │ gpt-4o-mini       │
          └─────────────────┘  └───────────────────┘
```

| Module | Role | Key Functions |
|--------|------|---------------|
| `file.py` | Parent graph builder, intent classification | `create_agent_graph()`, `intent_node()`, `route_intent()` |
| `server.py` | FastAPI backend, SSE streaming, HITL, KG API | 14 REST endpoints (see API section below) |
| `app.py` | Chainlit entrypoint (legacy) | — |

**State:** `AgentState` — TypedDict with `messages`, `mode`, `plan`, `context_stack`, `session_memory`, `session_id`

---

## Layer 2: Workflows & Subgraphs

### 2a. Research Subgraph (`graphs/research.py`)

```
START → plan_node → review_plan_node (HITL) → execute_one_step_node (loop)
                                                ↓
                          update_memory_node ← synthesize_node
                                                ↓
                                               END
```

Nodes: `plan_node`, `review_plan_node` (HITL interrupt), `execute_one_step_node`, `synthesize_node`, `update_memory_node`

Key patterns: Append-only `context_stack` (Manus-inspired), per-step checkpointing, tool result pointer in message → full payload on disk.

### 2b. Chat Subgraph (`graphs/conversational.py`)

```
START → ReAct loop (gpt-4o-mini) → END
              ↓ (tool calls)
      ┌───────┴──────┐
      │ DCF detected  │ → ⛔ STOP sentinel → HITL → [DCF_APPROVED] → resume
      │ Deck detected │ → deck workflow routing
      └──────────────┘
```

### 2c. DCF Valuation Subgraph (`graphs/workflows/dcf/` — 27 modules)

The core feature. Three compiled graphs from the same node set:

| Graph Variant | When Used | Composition |
|---------------|-----------|-------------|
| `dcf_workflow_app` | Main entry | Full 16-node graph with HITL interrupt + review loop |
| `dcf_valuation_app` | Fast path (post-HITL) | Math + review loop only; skips evidence/synthesis/memo |
| `dcf_scenario_val_app` | Per-scenario inside scenario_runner | Math only, no analysis loop |

**DCF Node Flow (16 nodes):**

```
START
  └→ normalize_input ───── (mints kg_run_id)
      └→ cache_check ────── (KG cache hits skip downstream)
          └→ assemble_evidence ── (5-tier: FILING/API/WEB/DOC/MKT)
              └→ semantic_synthesis ── (LLM: structured company understanding)
                  └→ formulate_thesis ── (bull/bear narrative + key drivers)
                      └→ propose_assumptions ── (base-case with full provenance)
                          └→ scenario_generator ── (bear/base/bull + monotonicity check)
                              └→ review_assumptions ── (HITL interrupt)
                                  ↓
                      ┌───────────┴───────────┐
                      │                       │
               collect_market_data     scenario_runner
                      │                       │
                      └───────────┬───────────┘
                                  ↓
                          project_cashflows ── (FCFF projections)
                              └→ compute_valuation ── (DCF + terminal value)
                                  └→ compute_market_signals ── (implied WACC/growth/margin)
                                      └→ sensitivity ── (WACC×TGR matrix + PNG heatmap)
                                          └→ review_subgraph ── (adversarial review, loop max 2×)
                                              └→ analysis ── (detect_divergences + LLM critique)
                                                  └→ convergence_gate ── (loop or proceed)
                                                      └→ finalize ── (summarize + KG backwrite)
                                                          └→ END
```

**Submodule Responsibilities:**

| Module | Role |
|--------|------|
| `graph.py` | Wiring + public API: `run_dcf_workflow_sync()` |
| `state.py` | `DCFState` TypedDict (~40 fields) |
| `lifecycle.py` | `normalize_input`, `cache_check`, routing |
| `idgen.py` | Unique `kg_run_id` per run (format: `TICKER_YYYYMMDDHHMMSS_rand4`) |
| `evidence.py` | 5-tier evidence assembly (`FILING`, `API`, `WEB`, `DOC`, `MKT`) |
| `fundamentals.py` | FMP + yfinance fetchers |
| `sec_filings.py` | SEC EDGAR 10-K/10-Q extraction |
| `synthesis.py` | LLM semantic synthesis |
| `memo.py` | LLM assumption memo |
| `execution.py` | `formulate_thesis` + `scenario_runner` |
| `scenarios.py` | Scenario generator + monotonicity validation |
| `wacc.py` | CAPM WACC estimation |
| `valuation.py` | Deterministic FCFF math + finalize (953 lines — needs split) |
| `priors.py` | Profile priors (mega_cap_tech, large_cap_tech, mature_consumer, default) + confidence scoring |
| `review.py` | HITL assumption review gate |
| `review_graph.py` | Adversarial review subgraph |
| `review_state.py` | Isolated `ReviewState` + Pydantic `ReviewFindings` |
| `refinement.py` | `analyze_result` + `refine_assumptions` |
| `analysis.py` | Convergence gate + divergence detection |
| `payload.py` | `summarize_dcf_payload` + consistency checks |
| `sources.py` | `SourceRegistry`, numbered `[n]` citations, section builders |
| `hitl_snapshot.py` | HITL context serialize/restore for fast path |
| `activity.py` | DCF-specific activity emitters |
| `assumptions.py` | Legacy heuristics (unused) |

**Key Design Decisions:**
- **LLM finds problems, Python applies fixes** — no LLM-mutated floats. LLM produces findings; deterministic rule table applies adjustments.
- **ReviewState isolated from DCFState** — one-way snapshot, subgraph can't pollute upstream.
- **No valuation output passed to reviewer** — prevents backward anchoring.
- **Convergence damping** — same-direction repeat adjustments halved.
- **`structural_gap` ≠ `invalid`** — market mispricing possible; model stays valid. Only solver failures or critical unresolved issues flip `model_validity` to invalid.

### 2d. Deck Workflow (`graphs/workflows/deck/` — 10 modules)

```
START → validate_sources → normalize_all → generate_outline
        → outline_review (HITL)
        → per_slide_generate → assemble_pptx → finalize → END
```

| Module | Role |
|--------|------|
| `graph.py` | Wiring + `run_deck_workflow_sync()` |
| `state.py` | `DeckState`, `DeckBrief`, `DeckSource` |
| `inputs.py` | Sanitize/resolve LLM tool args |
| `normalize.py` | Source adapters → `NormalizedBlock` |
| `outline.py` | Outline LLM + repair pass |
| `review.py` | Outline HITL interrupt |
| `slides.py` | Per-slide LLM + deterministic fallback |
| `assemble.py` | python-pptx renderers + `layout_spec` overlay |
| `theme.py` | `DeckTheme` tokens (audience presets: board, ic, internal, client, generic) |
| `finalize.py` | `deck_output.json` + KG `deck_run`/`deck_slide` nodes |

---

## Layer 3: Shared Services

```
┌──────────────┐  ┌──────────────┐  ┌───────────────┐
│  tools.py    │  │ plan_store.py│  │ activity.py   │
│  9 tools     │  │ disk+SQLite  │  │ event contract│
│  shared by   │  │ single seam  │  │               │
│  ALL graphs  │  │              │  │               │
└──────┬───────┘  └──────┬───────┘  └───────────────┘
       │                 │
       ├─────────────────┤
       │                 │
┌──────▼───────┐  ┌──────▼───────┐  ┌───────────────┐
│  storage.py  │  │ documents.py │  │ web_search.py │
│  SQLite      │  │ RAG pipeline │  │ Exa client    │
│  persistence │  │ ChromaDB     │  │               │
└──────────────┘  └──────────────┘  └───────────────┘
```

### Tools (`tools.py`) — Shared by All Subgraphs

| Tool | Purpose |
|------|---------|
| `search_web` | Exa semantic search → persist result, return pointer |
| `search_documents` | ChromaDB + BM25 hybrid retrieval over uploaded PDFs/CSVs |
| `fetch_sec_filing` | Free SEC EDGAR 10-K/10-Q section extraction |
| `calculator` | Safe math eval via `simpleeval` |
| `retrieve_context` | Prior step summary + `tool_result_ids` from saved plan |
| `retrieve_tool_result` | Full content of stored tool result by ID |
| `execute_python` | Local subprocess with `yfinance`/`matplotlib`/`pandas`/`requests` |
| `run_dcf_workflow` | Deterministic DCF with scenarios, thesis, review loop |
| `run_deck_workflow` | Slide-deck compiler from DCF output |

### PlanStore → Persistence Bridge

```
plan_store.py
├── save_plan(thread_id, plan) → disk JSON + SQLite sync_job_steps
├── update_step(thread_id, plan, step_id, ...) → disk + SQLite
└── save_report(thread_id, session_id, objective, content) → disk + SQLite
```

---

## Layer 4: Knowledge Graph

```
┌──────────────────────────────────────────────────────────────┐
│                    KG Layer (SQLite + in-process cache)       │
│                                                              │
│  ┌──────────────────────┐   ┌──────────────────────────────┐ │
│  │ kg/cache.py          │   │ kg/query.py                  │ │
│  │ KGCache singleton    │   │ NL → KGQuery + executor       │ │
│  │ O(1) hot path        │   │ Full subgraph serialization  │ │
│  │ TTL + confidence fl. │   │ _KG_SCHEMA injection         │ │
│  │ user_stated lock     │   │ Intent classification         │ │
│  └──────────┬───────────┘   └──────────────────────────────┘ │
│             │                                                │
│  ┌──────────▼───────────┐   ┌──────────────────────────────┐ │
│  │ kg/compare.py        │   │ kg/__init__.py               │ │
│  │ Cross-run diff chat  │   │ Module exports               │ │
│  │ LLM over struct diff │   │                              │ │
│  └──────────────────────┘   └──────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### Three-Layer Data Model

```
                    company (anchor — Layer 0, never expires)
                    /         |         \
            fundamentals   filings    news_items        ← LAYER 1: ANCHORED FACTS
            (current snap) (immutable) (immutable)         additive — never invalidate
                    \         |         /
                       synthesis · thesis · drivers      ← LAYER 2: DERIVED INFERENCES
                       (rebuildable, cached, hash-checked, TTL 7-30d)
                                |
                             dcf_run                      ← LAYER 3: RUN ARTIFACTS
                            /   |   \                        immutable history
                  assumptions outputs scenarios              NEVER input to future runs
```

| Layer | Types | TTL | Semantics |
|-------|-------|-----|-----------|
| **0** | `company` | infinite | Entity anchor |
| **1** | `filing`, `news_item`, `market_metric_fund`, `market_metric_price`, `person` | infinite (immutable) or TTL (snapshots) | Anchored facts — filings/news write-once (corpus grows), market metrics refreshable |
| **2** | `company_synthesis`, `thesis`, `driver`, `theme`, `risk`, `company_lifecycle` | 7–30 days, hash-checked | Derived inferences — rebuildable, input-hash checked |
| **3** | `dcf_run`, `run_assumption`, `run_output`, `run_scenario`, `deck_run`, `deck_slide` | infinite | Run artifacts — historical audit only |

### Node ID Scheme

- **Shared nodes:** `"{ticker}::{node_type}::{field}"`
- **Run-scoped nodes:** `"{ticker}::{node_type}::{run_id}::{field}"` where `run_id` = `kg_run_id` (e.g., `AAPL_20260530145809_0492`)

### Write Triggers

| When | What Gets Written | Layer |
|------|-------------------|-------|
| `assemble_evidence_node` | filings + news items | Layer 1 (additive) |
| `semantic_synthesis_node` | `company_synthesis` + `company_lifecycle` | Layer 2 |
| `formulate_thesis_node` | `thesis` | Layer 2 |
| `finalize_node` | `dcf_run` + `run_assumption` + `run_output` + market_metric refreshes | Layer 3 + Layer 1 |
| `finalize_node` (deck) | `deck_run` + `deck_slide` | Layer 3 |
| User edit (`PATCH /kg/.../nodes/{id}`) | source → `user_stated`, confidence = 1.0 | any |

---

## Layer 5: Frontend (React + TypeScript + Vite + Tailwind)

```
┌────────────────────────────────────────────────────────────────┐
│                        App.tsx (Root)                         │
│                                                               │
│  ┌─────────────────────┐     ┌──────────────────────────────┐ │
│  │ MessageThread.tsx   │     │ ExecutionSidebar.tsx          │ │
│  │ Left pane (653 loc) │     │ Right pane (collapsible)      │ │
│  │                     │     │                               │ │
│  │ DcfReportCard       │     │ ActivityTrace.tsx (2101 loc)  │ │
│  │ DcfHitlSection      │     │  └─ per-step expander panels  │ │
│  │ DeckOutlineReview   │     │     └─ DCF node sub-panels:   │ │
│  │ DeckArtifactCard    │     │        EvidencePanel          │ │
│  └─────────────────────┘     │        ConfidenceBreakdown    │ │
│                              │        AssumptionJourney      │ │
│                              │        ScenarioRunnerDetail   │ │
│                              │        DeckPreview            │ │
│                              └──────────────────────────────┘ │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ KnowledgePanel.tsx (full-screen modal)                   │ │
│  │  ┌────────────┐  ┌─────────────────┐  ┌───────────────┐ │ │
│  │  │KgFilter    │  │ KgCanvas.tsx    │  │ KgQueryPanel  │ │ │
│  │  │Sidebar     │  │ HTML5 canvas    │  │ NL query      │ │ │
│  │  │            │  │ static radial   │  │ input+answer  │ │ │
│  │  │            │  │ d3-zoom pan     │  │               │ │ │
│  │  └────────────┘  └────────┬────────┘  └───────────────┘ │ │
│  │                           │ click hub → drill down        │ │
│  │  ┌────────────────────────▼─────────────────────────────┐ │ │
│  │  │ KgHubPanel │ KgTablePanel  │ KgRunInspector          │ │ │
│  │  │ news cards │ drivers table │ assumptions/outputs      │ │ │
│  │  │            │ metrics rows  │ inline edit, rerun       │ │ │
│  │  │            │ beliefs comp. │                          │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │                                                           │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ KgCompareRuns.tsx (cross-run diff matrix)            │ │ │
│  │  │ KgTimeline.tsx (collapsible bottom strip)            │ │ │
│  │  │ KgValueView.tsx (structured node value renderer)     │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  └──────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

**Dependencies:** d3-drag, d3-force, d3-selection, d3-zoom, lucide-react, react-markdown, rehype-highlight, remark-gfm

---

## Layer 6: Testing

| Tier | Count | Location | Cost | Ground Truth |
|------|-------|----------|------|-------------|
| **Unit** | 356 | `tests/unit/` (34 files) | ~8s, free | Thresholds in source + accounting identities |
| **Golden** | 50 | `tests/golden/` | ~2s, free | SEC filings + Damodaran + analyst consensus |
| **E2E** | 1 | `tests/e2e/` | slow, optional | Captured run artifacts |

**Key Test Files:**

| File | Covers |
|------|--------|
| `test_routing.py` | All router functions |
| `test_scenarios_validation.py` | Monotonicity validator |
| `test_fcff_math.py` | FCFF projection + valuation math |
| `test_priors.py` | Profile classify, bands, confidence breakdown |
| `test_sources.py` | SourceRegistry, citation numbering |
| `test_convergence_gate.py` | `structural_gap` vs `invalid` validity policy |
| `test_report_export.py` | PDF/MD export, table wrapping |
| `test_golden_math.py` | Parametrized golden records (AAPL, MSFT, META, NVDA, F, KO, WMT) |
| `test_deck_*.py` | Deck inputs, theme, outline, layout_spec, citations |

**Golden Records:** 7 tickers — AAPL, MSFT, META, NVDA, F, KO, WMT (see `tests/fixtures/golden/`)

---

## Layer 7: Data Flow & Persistence

### Per-Thread Layout

```
runs/<thread_id>/
├── plans/              # plan JSON snapshots (every state change)
├── tool_results/       # full tool output payloads (one file per call)
├── artifacts/          # sensitivity heatmaps, sandbox plots, etc.
├── decks/              # deck_output.json + generated .pptx
├── dcf_output.json     # full DCF payload (written by finalize_node)
└── final_report.md     # synthesized markdown report (research mode)
```

### Global Persistence

```
runs/agent.db           # SQLite: jobs, job_events, job_steps, reports, session_memory, documents
runs/chroma/            # ChromaDB: document embeddings for RAG
agent_project/kg.db     # SQLite: kg_nodes, kg_edges, kg_traversals
```

### Key Data Patterns

1. **Append-only context_stack** — Manus-inspired, KV-cache friendly. After each research step, `{step_id, summary, tool_result_ids}` pushed — never modified.
2. **Tool results on disk, pointer in message** — keeps context short. Agent message contains `tool_result_id`; `retrieve_tool_result(id)` fetches full payload only when needed.
3. **Static system prompt** — identical across all steps → near-100% KV-cache hit rate.
4. **PlanStore wraps disk+SQLite behind single seam** — no dual-write bugs; swap adapter for tests or Postgres.
5. **KG deterministic node IDs** — `ticker::type::field` → O(1) dict lookup, no graph traversal on hot path.

---

## Complete REST API

### Runs & DCF

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/runs` | Start a chat or research run |
| `GET` | `/runs/{thread_id}/events` | SSE activity stream |
| `POST` | `/runs/{thread_id}/dcf-decision` | Approve/edit DCF assumptions (chat HITL) |
| `GET` | `/runs/{thread_id}/dcf-report.md` | Download DCF report (markdown) |
| `GET` | `/runs/{thread_id}/dcf-report.pdf` | Download DCF report (PDF) |
| `GET` | `/artifacts/{thread_id}/{filename}` | Serve run artifacts (e.g. sensitivity PNG) |
| `GET` | `/workflows/dcf/runs/{thread_id}/result` | Raw `dcf_output.json` |
| `POST` | `/runs/{thread_id}/deck-decision` | Approve/edit/reject deck outline |
| `GET` | `/runs/{thread_id}/decks/{filename}` | Download generated deck PPTX |
| `GET` | `/runs/{thread_id}/deck-output` | Deck JSON snapshot |

### Knowledge Graph

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/kg/{session_id}` | Full KG (all nodes + edges) |
| `GET` | `/kg/{session_id}/subgraph/{ticker}` | Ticker-scoped subgraph |
| `POST` / `PATCH` / `DELETE` | `/kg/{session_id}/nodes(/{id})` | CRUD on nodes |
| `POST` / `DELETE` | `/kg/{session_id}/edges(/{id})` | CRUD on edges |
| `POST` | `/kg/{session_id}/query` | Natural-language query |
| `GET` | `/kg/{session_id}/traversal/{run_id}` | Traversal replay |
| `POST` | `/kg/compare` | Cross-run diff side-chat |

### Jobs

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/jobs` | Job list |
| `POST` | `/jobs/{id}/resume` | Resume interrupted job |

---

## Intent Routing Flow

```
User message
      │
      ▼
intent_node (gpt-4o-mini classifier, last 6 messages)
      │
      ▼
route_intent
    ↙         ↘
 chat        research
  │            │
ReAct loop   plan → HITL review → execute → synthesize → memory
  │            │
 END          END
```

### Mode Dimensions

| Dimension | Options | Effect |
|-----------|---------|--------|
| **Intent mode** | `auto`, `research`, `chat` | User choice or auto-classified routing |
| **DCF execution** | `assumption_review_mode=True/False`, Fast path (all 8 fields provided) | HITL gate, auto-approve, or math-only |
| **KG cache** | transparent | Cache hits → downstream nodes short-circuit; misses → full LLM/API path |

---

## Design Decisions Reference

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
| **LLM finds problems, Python applies fixes** | LLM better at judgment than arithmetic; deterministic rule table prevents hallucinated deltas |
| **ReviewState isolated from DCFState (one-way snapshot)** | Prevents state pollution; subgraph can't accidentally mutate upstream state |
| **No valuation output passed to reviewer** | Prevents backward anchoring — reviewer critiques assumptions, not implied price |
| Convergence damping (same-direction repeat halved) | Prevents oscillation without explicit history check |
| KG deterministic node IDs (`ticker::type::field`) | O(1) dict lookup on hot path |
| KG separates shared from run-scoped nodes | Avoids overwrite on repeat runs; multiple runs coexist; comparable in UI |
| `kg_run_id` separate from `parent_step_id` | Step id (activity) and run id (KG identity) are different concerns |
| LLM reads full serialized subgraph, not narrow query | Narrow NL→KGQuery was brittle; full subgraph + schema = better retrieval |
| Golden test range ±50% around hand DCF | DCF inherently imprecise; tighter range = false positives from legitimate variation |
| Deck compiler pipeline (not chat ReAct) | Deterministic, testable, HITL-friendly |

---

## Known Limitations

- Deck content gaps — scenario/sensitivity slides can render as empty `section_header`
- Deck formatting is programmatic only — no master `.pptx` template library yet
- No DOCX or XLSX export (only PDF/MD and PPTX)
- Standalone DCF endpoint HITL resume is broken (agent-tool path works)
- Worker model is single-process — no DB claim lock / multi-worker coordination
- Document ingestion does not resume (no retry queue for interrupted uploads)
- LLM nodes not unit-tested (require LangSmith eval datasets)
- Market-implied signals incomplete (only implied WACC; growth and margin needed)

---

## Future Work Roadmap

| Phase | Topic | Status |
|-------|-------|--------|
| A | Deck adapter fallbacks + outline empty-slide policy | planned |
| B–D | Expanded layout_spec vocabulary + template library | planned |
| 3 | Market signals + report quality | ✅ shipped (with remaining surface items) |
| 4 | Confidence gating + exit modes | planned |
| 5 | Assumption memory (historical assumptions per ticker) | planned |
| — | LangSmith evaluator datasets for LLM nodes | planned |
| — | Golden dataset expansion (edge cases: high-growth, cyclical, story stocks) | planned |
| — | Comps workflow (peer-multiple selection + outlier detection) | planned |
| — | Prompt extraction + versioning | planned |
| — | Streaming via LangGraph native | planned |
| — | KG run registry (Phase 1) + comparison by drag (Phase 2) | planned |
| — | Workspace registry (Phase 4) | planned |
| — | File splits (valuation.py, ActivityTrace.tsx, MessageThread.tsx) | planned |
| — | Latency budget + telemetry | planned |

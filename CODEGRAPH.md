# Codegraph

Purpose: working map of the current agent system. Use this before large changes.

Repo root: `/Users/rayengallas/Desktop/langgraph-research-agent`

Current head verified: `3ebe552 feat(dcf): Tier 3 review rigor — causal adjustments, driver scenarios, true convergence`

## System Shape

```text
React UI
  -> FastAPI server.py
    -> parent LangGraph file.py
      -> intent_node
        -> chat workflow graphs/conversational.py
        -> research workflow graphs/research.py
          -> optional DCF workflow graphs/workflows/dcf/graph.py
          -> optional deck workflow graphs/workflows/deck/graph.py
    -> documents.py RAG ingestion/retrieval
    -> kg/* knowledge graph cache/query/audit
    -> storage.py SQLite persistence
```

Main runtime protocol:

```text
POST /runs
  -> create RunState
  -> background asyncio task
  -> LangGraph invoke
  -> events persisted in SQLite + pushed over SSE
GET /runs/{thread_id}/events
  -> resumable SSE stream
  -> frontend state reducer updates UI
```

## Core Backend Entrypoints

### API Layer

File: `agent_project/server.py`

Owns:
- run registry and resumable SSE
- HITL futures for research, DCF, deck review
- document upload/status/list/delete endpoints
- DCF direct workflow endpoints
- deck artifact endpoints
- KG CRUD/query/audit endpoints
- session layout persistence endpoints

Important routes:
- `POST /runs` -> normal chat/research run
- `GET /runs/{thread_id}/events` -> SSE stream
- `POST /runs/{thread_id}/decision` -> research plan approval
- `POST /runs/{thread_id}/dcf-decision` -> DCF HITL decision
- `POST /runs/{thread_id}/deck-decision` -> deck HITL decision
- `POST /documents` -> upload + async ingest
- `GET /documents?session_id=...` -> session document list
- `GET /kg/{session_id}` -> full session KG
- `POST /kg/{session_id}/query` -> NL KG query
- `POST /workflows/dcf/runs` -> direct DCF run

High-risk function:
- `_run_agent_task(...)`: bridges frontend run request to parent graph, handles interrupts, persists errors.

### Parent Graph

File: `agent_project/file.py`

Graph:

```text
START
  -> intent
    -> research: planning -> review_plan -> execute_one_step* -> synthesize -> update_memory -> END
    -> chat: chat -> END
```

State keys:
- `messages`
- `mode`: `auto | research | chat`
- `resolved_intent`
- `session_id`
- research fields: `plan`, `objective`, `context_stack`, `approved`, `review_feedback`
- `session_memory`
- `user_settings`

Decision:
- forced `mode=research` bypasses classifier
- forced `mode=chat` bypasses classifier
- `mode=auto` uses small classifier model

## Chat Workflow

File: `agent_project/graphs/conversational.py`

Role: fast ReAct agent inside unified chat.

Capabilities:
- normal answer
- tool calls
- DCF workflow launch
- deck workflow launch
- RAG search
- KG query
- date anchor / temporal reasoning
- final answer streaming

Key functions:
- `chat_node(state)`
- `_chat_node_inner(state)`
- `_build_doc_inventory(session_id)`
- `_build_kg_state_injection(query)`
- `_stream_final_answer(history)`
- `_fallback_answer_from_tool_results(history)`

Tool pattern:

```text
LLM -> tool_call -> tool result -> repeat bounded rounds -> final answer
```

Important design:
- chat uses same action surface as research, but no plan/HITL unless specific workflow triggers it.
- DCF/deck tools can interrupt through server bridge.
- committed chat messages live in frontend session state; backend session memory lives in SQLite.

## Research Workflow

File: `agent_project/graphs/research.py`

Role: slower plan-execute-synthesize workflow.

Graph:

```text
planning
  -> review_plan
    -> execute_one_step
      -> execute_one_step while pending steps exist
      -> synthesize
        -> update_memory
```

Important functions:
- `plan_node`
- `review_plan_node`
- `execute_one_step_node`
- `execute_step`
- `synthesize_node`
- `update_memory_node`

Important tools:
- document search
- web search
- Python execution
- calculator
- context/tool-result retrieval
- workflow tools from `tools.py` including DCF/deck routes

Persistence pattern:
- tool output saved under `runs/<thread_id>/tool_results`
- context stack stores summaries + result pointers
- report saved as `final_report.md`

## DCF Workflow

Folder: `agent_project/graphs/workflows/dcf`

Main file: `graph.py`

Main graph:

```text
START
  -> normalize_input
  -> cache_check
    -> assemble_evidence (when cache miss / needs evidence)
    -> semantic_synthesis
    -> formulate_thesis
    -> propose_assumptions
    -> scenario_generator
    -> review_assumptions
    -> coherence_gate
    -> scenario_runner
    -> project_cashflows
    -> compute_valuation
    -> compute_market_signals
    -> sensitivity
    -> review_subgraph
      -> detect_divergences
      -> analysis
      -> convergence_gate
        -> refine/re-run valuation OR finalize
```

Subgraphs:
- validation graph: direct scenario/base valuation loop after assumptions exist
- scenario graph: lightweight per-scenario compute path
- review subgraph: adversarial findings + deterministic bounded adjustments

Key modules:
- `lifecycle.py`: normalize ticker/horizon/run metadata
- `evidence.py`: filings, web, structured API evidence pack
- `sec_filings.py`: EDGAR retrieval
- `synthesis.py`: company state synthesis
- `execution.py`: thesis generation
- `memo.py`: assumption generation, hard-band clamps, peer validation
- `priors.py`: profile bands, confidence logic, forecast confidence
- `wacc.py`: CAPM/profile-adjusted WACC stack
- `coherence.py`: assumption bundle checks
- `scenarios.py`: driver-based scenarios
- `valuation.py`: market data, FCFF projection, valuation, sensitivity, final payload write
- `analysis.py`: market reconciliation / divergence analysis
- `review_graph.py`: adversarial review and deterministic adjustment synthesis
- `review_loop.py`: applies bounded adjustments, tracks true convergence
- `payload.py`: report assembly
- `peers.py`: peer-based validation
- `sources.py`: citation/source registry
- `state.py`: `DCFState`

Recent DCF tiers already landed:
- Tier 0: hard-band floor enforcement; no negative valuation from sub-floor margins
- Tier 1: forecast table, value bridge, WACC stack, evidence vs forecast confidence
- Tier 2: causal chains + peer validation
- Tier 3: finding-linked adjustments, driver scenarios, severity-based convergence

Current DCF risk:
- AMZN still needs better profile/segment-aware normalized margin grounding.
- Peer validation is warn-only and best-effort.
- Causal chains are prompted and surfaced, but not strictly quality-gated.

## Deck Workflow

Folder: `agent_project/graphs/workflows/deck`

Main file: `graph.py`

Graph:

```text
START
  -> validate_sources
  -> normalize_all
  -> generate_outline
  -> outline_review
    -> per_slide_generate when approved
    -> END when rejected
  -> assemble_pptx
  -> finalize_deck
  -> END
```

Key modules:
- `inputs.py`: source/brief models
- `normalize.py`: source normalization
- `outline.py`: structured outline
- `review.py`: HITL outline approval
- `slides.py`: deterministic slide content/layout
- `assemble.py`: PPTX assembly
- `finalize.py`: artifact output
- `adapters/`: DCF/report-to-deck source adapters

Current role:
- can draft presentation artifacts from DCF/report inputs.
- integrated into chat via deck routing and HITL.

## RAG / Documents

File: `agent_project/documents.py`

Pipeline:

```text
upload
  -> register document metadata
  -> parse file
  -> chunk pages/tables
  -> embed into Chroma
  -> extract facts
  -> ingest document facts into KG
```

Supported:
- PDF via `pdfplumber`
- DOCX via `python-docx`
- CSV/XLSX via pandas
- TXT/MD fallback

Retrieval:
- Chroma dense search
- BM25 sparse score over candidates
- reciprocal rank fusion
- session-scoped `search_documents` tool

Important functions:
- `ingest_document`
- `hybrid_search`
- `delete_document`
- `_make_search_documents_tool`
- `extract_and_ingest_facts`

Known edge:
- frontend must proxy `/documents` to backend in dev.
- upload UX depends on `useDocuments` + `QueryInput` paperclip.

## Knowledge Graph

Folder: `agent_project/kg`

Purpose:
- persistent fact cache
- temporal querying
- run comparison
- audit findings
- DCF evidence reuse

Core:
- `cache.py`: `KGCache`, TTL, get/put/query, anchored corpus
- `ingest.py`: duplicate/contradiction-aware fact ingestion
- `query.py`: natural-language KG query
- `compare.py`: run comparison
- `audit.py`: quality checks
- `deep_research.py`: KG-backed research helpers
- `schemas.py`: node/fact schema

Runtime relation:
- DCF writes filings/news/market metrics/assumptions/results into KG.
- Chat can query KG as cache before web/data calls.
- UI visualizes KG and supports query/audit/compare.

## Persistence

File: `agent_project/storage.py`

SQLite stores:
- jobs
- events
- job steps
- reports
- session memory
- document metadata
- KG nodes/edges/traversals
- session layout

Filesystem stores:
- `agent_project/runs/<thread_id>/...`
- artifacts, final reports, DCF outputs, deck files, uploaded docs
- Chroma under runs-managed path

Important persistence path:

```text
Run event -> storage.append_job_event -> SSE replay via /runs/{id}/events?after_id=N
```

## Frontend

Folder: `agent_project/frontend/src`

Root:
- `App.tsx`

Main UI shape:

```text
SessionsSidebar
  + MessageThread
  + ExecutionSidebar
  + KnowledgePanel
  + JobsPanel
  + DocumentPreview / DeckPreview / SettingsPanel
```

Important hooks:
- `useAgentRun`: run lifecycle, SSE reducer, approvals, DCF/deck state
- `useSessionManager`: local session/chat history
- `useDocuments`: upload/list/delete/poll docs
- `useJobs`: background jobs
- `useKnowledgeGraph`: KG fetch/query state
- `useKgRerun`: rerun DCF from KG/history
- `usePanelHidden`, `useResizable`: layout state

Important components:
- `MessageThread.tsx`: unified chat/research/deck/DCF rendering; large hub
- `ExecutionSidebar.tsx`: live steps, activity trace, HITL panels
- `KnowledgePanel.tsx`: KG canvas, audit, compare, financials
- `QueryInput.tsx`: mode selector, attachments/upload
- `ActivityTrace.tsx`: visible activity log
- `MarkdownRenderer.tsx`: report/chat markdown rendering

Frontend data flow:

```text
QueryInput submit
  -> App.handleSubmit
  -> useAgentRun.startRun
  -> POST /runs
  -> SSE events
  -> useAgentRun reducer
  -> MessageThread + ExecutionSidebar update
  -> run completes
  -> App commits final message/report to session
```

## High-Churn / High-Risk Files

Change carefully:
- `agent_project/server.py`: many endpoints + bridge logic; easy to break SSE/HITL.
- `agent_project/frontend/src/components/MessageThread.tsx`: huge rendering hub; easy UI regressions.
- `agent_project/graphs/conversational.py`: chat tool routing, DCF/deck triggers, streaming.
- `agent_project/graphs/workflows/dcf/payload.py`: giant report assembler; many downstream tests.
- `agent_project/graphs/workflows/dcf/valuation.py`: valuation math + final payload.
- `agent_project/graphs/workflows/dcf/memo.py`: assumptions, clamps, peer validation.
- `agent_project/storage.py`: schema/backwards compatibility risk.

## Verified Checks

Last verified during this map:

```text
uv run python -m py_compile DCF touched modules
npm run build
uv run pytest agent_project/tests/unit/test_dcf_valuation_math.py \
  agent_project/tests/unit/test_dcf_expectations_blocks.py \
  agent_project/tests/unit/test_kg_query.py -q
uv run pytest agent_project/tests/unit/test_peers.py \
  agent_project/tests/unit/test_priors.py \
  agent_project/tests/unit/test_review_tier3.py -q
```

Results:
- frontend build passed
- 52 DCF/KG tests passed
- 61 peer/prior/review tests passed

## Next-Step Codegraph

### Next 1: Fix PDF Upload End-to-End

Likely path:

```text
QueryInput paperclip
  -> useDocuments.upload
  -> POST /documents
  -> server.upload_document
  -> documents.ingest_document
  -> Chroma + SQLite document rows
  -> search_documents tool available to chat/research
```

Files:
- `agent_project/frontend/vite.config.ts`
- `frontend/src/hooks/useDocuments.ts`
- `frontend/src/components/QueryInput.tsx`
- `frontend/src/components/MessageThread.tsx`
- `agent_project/server.py`
- `agent_project/documents.py`
- `agent_project/storage.py`

Checks:
- Vite proxy includes `/documents`
- server accepts multipart form with `file` and `session_id`
- document row appears in SQLite
- Chroma collection accepts embedding dimension
- uploaded PDF can be searched through `search_documents`

### Next 2: AMZN / Segment-Aware Margin Grounding

Problem:
- AMZN cannot use pure `mega_cap_tech` margin band.
- Current clamp prevents degenerate math, but normalized margin still too low/uncertain.

Path:

```text
evidence_pack + SEC filings + FMP cash flows
  -> synthesis company_state
  -> memo propose assumptions
  -> peer validation
  -> valuation forecast model
```

Files:
- `dcf/evidence.py`
- `dcf/sec_filings.py`
- `dcf/synthesis.py`
- `dcf/memo.py`
- `dcf/priors.py`
- `dcf/peers.py`
- `dcf/valuation.py`
- `dcf/payload.py`

Implementation direction:
- add `profile="hybrid_platform"` or `retail_cloud_platform`
- split margin logic: retail/ecommerce drag + AWS/ads margin expansion
- use filings to ground capex cycle and normalized FCF margin
- peer validation should compare AMZN to mixed peer set, not only broad FMP peers

### Next 3: Enforce Causal Chain Quality

Problem:
- causal chains displayed, but shallow LLM chains can pass.

Path:

```text
AssumptionProposal.causal_chain
  -> validate_memo
  -> assumption_flags
  -> confidence/payload
```

Files:
- `dcf/memo.py`
- `dcf/priors.py`
- `dcf/payload.py`
- tests: `test_memo_validation.py`, `test_priors.py`

Implementation direction:
- require 3+ links for `revenue_growth`, `fcff_margin`, `terminal_growth`
- reject chains containing only observed metric -> assumption
- downgrade evidence/forecast confidence if chain weak
- surface quality flag in report

### Next 4: Split MessageThread

Problem:
- `MessageThread.tsx` is too large and high-risk.

Target decomposition:

```text
MessageThread
  -> UserBubble
  -> ChatBubble
  -> DcfReportCard
  -> ResearchReportCard
  -> DeckArtifactCard
  -> EvidenceSourceDrawer
  -> ThreadInputBar
```

Files:
- `frontend/src/components/MessageThread.tsx`
- new folder: `frontend/src/components/thread/*`

Reason:
- easier UI iteration
- lower regression risk
- smaller build errors

### Next 5: Job / Workflow State Unification

Problem:
- research, DCF, deck, document ingest, KG rerun each have different state conventions.

Target:

```text
workflow_run
  id
  kind: chat | research | dcf | deck | document_ingest | kg_audit
  status
  parent_session_id
  parent_thread_id
  activity_events
  artifacts
```

Files:
- `server.py`
- `storage.py`
- `useAgentRun.ts`
- `useJobs.ts`
- `ActivityTrace.tsx`

Reason:
- background agents become first-class
- easier recovery/replay
- one timeline model for UI

## Operating Notes

- Prefer code-review-graph tools first for impact review.
- Use `/Users/rayengallas/Desktop/langgraph-research-agent`, not stale `/Users/rayengallas/Project/...`.
- After changing graph/workflow code, run focused unit tests plus `npm run build`.
- For frontend changes, inspect `MessageThread.tsx` blast radius before editing.
- For DCF changes, inspect `payload.py`, `memo.py`, `valuation.py`, and relevant tests together.

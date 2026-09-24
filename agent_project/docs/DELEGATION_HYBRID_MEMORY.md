# Delegation And Hybrid Memory

## Execution Levels

```text
user turn
   |
semantic router (strong scope decision)
   |
   +-- direct/tool ----> conversational agent
   +-- small_task -----> bounded research workflow
   +-- workflow -------> named workflow + HITL
   +-- case -----------> dynamic case planner
```

Only `case` creates a dynamic task DAG. Router prompt requires cheapest sufficient
route, preventing one-tool questions from paying orchestration overhead.

## Dynamic Case Runtime

```text
plan_case (strong orchestrator model)
   |
prepare_case_dispatch
   |
   +-- Send(execute_case_task, TaskPacket) --+
   +-- Send(execute_case_task, TaskPacket) --+--> collect_case_results
   +-- Send(execute_case_task, TaskPacket) --+            |
                                                          +-- more ready tasks
                                                          +-- synthesize_case
```

Planner selects capabilities from registry. It cannot invent capability IDs. DAG
validator rejects duplicate IDs, missing dependencies, self-dependencies, cycles,
and plans over ten tasks. Independent tasks become parallel LangGraph `Send`
branches. `case_task_results` uses stable-ID reducer; collector remains sole canonical
join point.

Memo and deck capabilities accept multiple input types. Neither requires DCF.
Requested deliverables and available evidence determine graph shape.

## Delegation Contracts

- `TaskSpec`: objective, capability, dependencies, input versions, required outputs,
  context query, execution policy, acceptance criteria.
- `TaskPacket`: task plus dependency results, bounded evidence pack, allowed tools,
  session/thread identity.
- `TaskResult`: status, summary, immutable output version IDs, citations,
  discovered requirements, warnings, metrics.

Workers receive isolated context and capability-scoped tools. Large outputs persist
as workspace object versions; orchestrator receives summaries and references.

## Canonical Turn State

```text
current_turn
   +-- route
   +-- playbook
   +-- execution_policy
   +-- memory_selection
   +-- evidence_selection
   +-- delegation
   |     +-- case_id
   |     +-- plan_version_id
   |     +-- active_task_ids
   |     +-- completed_task_ids
   |     +-- status
   +-- response
```

`pending_task_packets` exposes prepared `TaskPacket` values before fan-out. Durable
case, task, and result history lives in append-only workspace object versions.

## Hybrid Evidence Retrieval

```text
query
  |
  +-- document chunks: dense + BM25 + RRF
  +-- versioned financial facts
  +-- KG entities and relationships
  |
EvidencePack
  +-- chunk_refs
  +-- fact_refs
  +-- entity_refs
  +-- relationship_refs
  +-- coverage / missing / contradictions
```

`EvidencePack` is injected into conversational, research-planning, research-step,
and delegated-worker prompts. Direct/tool turns skip document-vector retrieval unless
query names documents, contracts, statements, filings, clauses, PDFs, or uploads.
Case, workflow, and small-task turns retrieve documents automatically.

## Persistence

Uploaded source receives stable `document_version_id` (`{doc_id}:v1`). Re-upload is
a new document ID. Extracted facts use logical IDs based on subject, predicate, and
fiscal period:

```text
fact logical object
   +-- v1: extracted value + exact evidence citation
   +-- v2: changed/restated value + evidence citation
```

Unchanged extraction retries are idempotent. Changed values append versions.
Fact states are `candidate`, `extracted`, `verified`, `disputed`, or `superseded`.
KG remains fast query projection; workspace fact versions remain durable source of
truth.

## Main Files

- `domain/execution.py`: DAG and task contracts/reducer.
- `capabilities.py`: discoverable capability registry.
- `case_orchestration.py`: planning, scheduling, workers, merge, synthesis.
- `domain/evidence.py`: evidence/fact/entity/relationship contracts.
- `evidence_memory.py`: fact persistence and hybrid retrieval.
- `file.py`: parent LangGraph wiring and canonical state.

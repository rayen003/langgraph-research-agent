# State, Object, and Storage Audit

## Purpose

Define ownership boundaries before adding collaboration, durable memory, background
tasks, or subagents. This audit covers runtime state, workflow state, domain objects,
knowledge graph records, persistence, identity, actions, and retrieval.

## Current Data Layers

```text
User request
    |
    v
AgentState (main LangGraph execution state)
    |-- routing/playbook/tool policy
    |-- messages/session memory
    |-- workflow context and HITL decisions
    |-- research plan fields
    |
    +--> DCFState / DeckState (nested workflow execution state)
    |       |-- workflow inputs and control flags
    |       |-- intermediate evidence and analysis
    |       |-- HITL decisions
    |       +-- final outputs and artifact paths
    |
    +--> AgentObject / workspace_objects (durable product objects)
    |
    +--> KGNode / KGEdge (durable financial facts and relations)
    |
    +--> jobs / events / steps (execution and action history)
    |
    +--> LangGraph checkpointer (thread and interrupt continuation)
```

## Inventory

| Layer | Current schema | Persistence | Intended lifetime |
|---|---|---|---|
| Main graph | `file.AgentState` | durable SQLite checkpointer | cross-process thread |
| DCF graph | `dcf.state.DCFState` | durable SQLite plus result JSON | cross-process workflow run |
| Deck graph | `deck.state.DeckState` | durable SQLite plus artifacts | cross-process workflow run |
| Memo graph | `memo.state.MemoState` | durable SQLite plus artifact versions | cross-process workflow run |
| Domain object | `domain.objects.AgentObject` | `workspace_objects` | durable |
| Memory extraction | `memory_writer.WorkspaceObjectDraft` | converted directly to row dict | durable |
| Research case | `domain.cases.*` | no complete repository boundary | durable, planned |
| KG | `KGNode`, `KGEdge`, typed KG value models | `kg_nodes`, `kg_edges` | durable/cache-dependent |
| Jobs/actions | ad hoc dicts and server models | `jobs`, `job_events`, `job_steps` | durable audit history |
| Documents | registry dicts plus Chroma metadata | SQLite plus Chroma | durable |
| Session UI | server/frontend models | SQLite plus local storage | durable per user/workspace |

## Duplication and Boundary Problems

### Object identity

`object_id`, `object_type`, `session_id`, `thread_id`, `run_id`, timestamps,
creator, status, entities, sources, and provenance appear across domain models,
workspace rows, KG records, memory drafts, workflow payloads, and server models.
No shared identity type or repository contract enforces consistency.

### Entity references

`EntityCard`, `domain.cases.EntityRef`, and `memory_writer.EntityRef` represent
same company/entity concept with separate models. KG uses ticker fields directly.

### Relationships

`AgentObject.references` is typed and relation-aware, but storage flattens it to
`source_object_ids`. Relation, sequence, required flag, and note are lost.
KG has separate edges with another relation vocabulary. Dependency traversal cannot
reliably cross workspace objects and KG.

### Status

Object status currently models execution (`running`, `failed`) and knowledge lifecycle
(`draft`, `archived`) in one field. Jobs and workflow state also carry execution status.
Collaboration needs separate lifecycle, execution, and review states.

### Provenance and human decisions

DCF stores per-assumption provenance inside workflow state and result payload. KG stores
source/confidence at node level. Workspace objects have source refs and creator fields.
HITL approvals exist in graph state and events, but no common immutable action record
links actor, target, prior version, new version, and rationale.

### Workflow state size

`DCFState` combines request, control flow, evidence, cache hints, intermediate analysis,
human decisions, final valuation, and artifact location. `AgentState` combines routing,
conversation, research, memory, workflow setup, and UI preferences. Both are useful
execution snapshots but poor durable domain models.

### Storage concentration

`storage.py` owns jobs, reports, memory, documents, KG, UI layout, workspace objects,
and audit records in one module and one SQLite database. One database is acceptable
now; one unbounded module and untyped row dictionaries are not.

## Target Ownership

```text
Execution state
  LangGraph checkpoint only
  messages, node cursor, retries, interrupts, transient intermediate values

Application database
  identity: users, workspaces, memberships, roles
  objects: objects, versions, relations, sources, entities
  collaboration: actions, reviews, comments, assignments
  operations: runs, tasks, events, tool calls, artifact records

Knowledge store
  financial facts, observations, derived claims, semantic relationships
  every record linked to source object and producing run

Vector index
  searchable projections/chunks only; never source of truth

Artifact store
  reports, decks, models, uploaded files; database stores metadata and URI
```

## Database Decision

Do not create separate physical databases for users, objects, and agent actions yet.
These records require foreign keys, transactions, authorization joins, and versioned
audit writes. Start with one application database split into explicit modules/tables.

Keep these stores physically separate because lifecycles differ:

1. Application database: users, workspaces, objects, relationships, actions, runs.
2. LangGraph checkpoint database: thread snapshots and interrupt continuation.
3. Vector index: document/object embeddings and retrieval projections.
4. Artifact storage: generated and uploaded binary files.

Move from SQLite to Postgres before multi-user production or concurrent background
workers. Logical repository interfaces should make migration mechanical.

## Proposed Core Records

### Object

Stable identity and current version pointer. Contains workspace ownership, object type,
visibility, lifecycle status, review status, current version, creator, and timestamps.

### ObjectVersion

Immutable content snapshot. Contains title, summary, payload, schema version, search
text, confidence, quality, provenance, created-by actor, and creation action.

### ObjectRelation

Typed directed edge with source object/version, target object/version, relation,
sequence, required flag, note, and creator. Replaces flattened `source_object_ids`.

### Actor

Typed reference to `user`, `agent`, `workflow`, or `system`. Used consistently by
object versions, actions, reviews, tool calls, and KG writes.

### Action

Immutable event: actor, action type, target, run/thread/task, previous version, resulting
version, decision payload, rationale, timestamp, and idempotency key.

### Run and Task

Operational records for foreground/background execution. LangGraph checkpoint IDs are
references, not domain state. Tasks may be assigned to agents or users and depend on
other tasks/objects.

### Evidence/Source

Canonical source identity and retrieval metadata. Workspace objects, claims, KG facts,
and assumptions refer to same source records instead of embedding incompatible copies.

## State Rule

Graph state may carry IDs and transient working copies. Durable data must be written
through repositories and referenced by ID:

```text
graph state = execution context + durable IDs + pending writes
database    = durable truth + versions + permissions + actions
KG          = queryable financial knowledge linked to durable sources/objects
```

## Runtime Ownership Decision

Do not introduce a separate `ExecutionState` model. Current execution ownership is:

```text
AgentState.current_turn
  control ledger for current request
  - goal and route
  - required outputs
  - result_refs
  - artifact_refs
  - artifact_paths
  - response completion status

messages
  interaction log and LangGraph conversation continuity

ToolInvocation / ToolResult
  per-call execution telemetry and compact result references

workspace object + immutable object version
  durable user-visible output and lineage
```

`current_turn` contains references, not copied financial series, deck payloads, code,
or binary artifacts. Router replaces control fields each turn. Durable route/action
history appends in application database. This keeps current state small while letting
subsequent turns retrieve prior objects by metadata.

Reference resolution is one broker over existing stores:

```text
tool_result_id ---------> runs/<thread>/tool_results/<id>.json
object_id --------------> current workspace object version
object_id:v000123 ------> exact immutable workspace object version
```

Broker is an access layer, not another persistence model or schema hierarchy.

Graph checkpoint must never become workspace database. Workspace object must never
be required to resume interrupted graph.

## Audit Deliverables Before Migration

- Field-level matrix for `AgentState`, `DCFState`, `DeckState`, `AgentObject`, KG,
  server models, and SQLite tables.
- Classification for each field: transient, checkpointed, durable, derived, cached,
  secret, user-owned, or artifact reference.
- Canonical identifier map for user, workspace, session, thread, run, task, case,
  object, object version, source, entity, and KG record.
- Relation vocabulary shared across objects and KG where semantics overlap.
- Lifecycle, review, execution, and visibility transition rules.
- Authorization matrix for user, collaborator, agent, workflow, and system actors.
- Migration map preserving current DCF objects and KG lineage.

## First Implementation Slice

1. Introduce canonical ID and actor types.
2. Split object lifecycle, review status, and execution status.
3. Add `object_versions`, `object_relations`, and immutable `actions` tables.
4. Add repository interfaces; keep existing SQLite backend.
5. Adapt workspace object writes and DCF synchronization first.
6. Preserve compatibility view returning current `workspace_objects` API shape.
7. Add tests for versioning, approval, supersession, permissions, action history,
   idempotency, and graph resume independence.

Background workers and subagents follow after this slice. They should consume object
IDs, create versioned writes, and emit actions rather than mutating shared payloads.

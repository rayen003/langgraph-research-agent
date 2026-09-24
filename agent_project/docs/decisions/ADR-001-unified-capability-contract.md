# ADR-001: Unified Capability Contract and Registry

## Status

Accepted

## Date

2026-09-07

## Context

Agent exposes atomic tools, deterministic LangGraph workflows, and bounded
subagents. Before this decision, executable behavior was described separately
by `ToolSpec`, `Capability`, chat tool lists, research tool lists, and case
worker allowlists. This caused caller-specific behavior: workflows available in
chat were absent from research or case execution, while overlapping schemas
could drift without failing tests.

Long-running cases need every caller to invoke same capability through same
policy and result contracts. Adding separate workflow or scheduler schemas
would deepen current duplication.

## Decision

Use one `CapabilitySpec` and one `CapabilityRegistry` as source of truth for
atomic tools, deterministic workflows, and bounded agent capabilities.

`ToolSpec` and `ToolRegistry` remain temporary import aliases for compatibility;
they do not define separate schemas or registries.

Capability kinds:

- `read`, `compute`, and `write`: atomic executable operations.
- `workflow`: deterministic multi-step LangGraph capability.
- `agent`: bounded reasoning capability with explicit child capability allowlist.

Caller surfaces (`chat`, `research`, `case`) are catalog metadata. Runtime tool
lists must be generated from registry plus implementation map. Callers must not
maintain independent capability lists.

Top-level execution envelope and capability kind are independent:

- `direct`: controller answers without capability invocation.
- `react`: controller can invoke atomic or workflow capabilities.
- `case`: durable DAG tasks can invoke atomic, workflow, or agent capabilities.

Workflow remains capability, not terminal routing mode. Existing workflow route
and setup nodes remain compatibility paths until controller routing is migrated.

## Canonical Contracts

| Concern | Canonical contract | Purpose |
|---|---|---|
| Executable definition | `CapabilitySpec` | Semantics, schemas, policy, exposure, accepted and produced types |
| Registry | `CapabilityRegistry` | Discovery, plan validation, caller exposure |
| Case planning | `CasePlan` and `TaskSpec` | Objectives, dependencies, acceptance criteria |
| Execution attempt | Future generalized `Invocation` | Status, parentage, retries, HITL, timing |
| Execution observation | Future generalized `Result` | Summary, payload and object refs, citations, artifacts |
| Durable business output | `AgentObject` version | Append-only semantic object history |

No new `WorkflowResult`, `SubagentResult`, or scheduler-specific result envelope
may be introduced. Existing invocation/result contracts will be generalized in
later migration.

## Migration Map

| Previous construct | Migration |
|---|---|
| `ToolSpec` | Compatibility alias of `CapabilitySpec` |
| `ToolRegistry` | Compatibility alias of `CapabilityRegistry` |
| `Capability` dataclass | Removed; catalog uses `CapabilitySpec` |
| `_CAPABILITIES` case catalog | Merged into canonical capability catalog |
| Chat `CHAT_TOOLS` list | Generated for `chat` surface |
| Research `TOOLS` list | Generated for `research` surface |
| Case global tool map | Generated for `case` surface |
| `dcf_valuation` capability | Canonical `run_dcf_workflow` capability |
| `memo_generation` capability | Canonical `run_memo_workflow` capability |
| `deck_generation` capability | Canonical `run_deck_workflow` capability |

## Invariants

1. Every exposed executable has exactly one capability specification.
2. Same capability ID resolves to same semantics and execution policy for every caller.
3. Workflows can be invoked from bounded ReAct or case tasks.
4. Case planner sees plannable capabilities only; atomic tools remain worker actions.
5. Missing declared implementation fails during caller exposure construction.
6. Capability results use references for large payloads and durable objects.
7. HITL workflow cannot be marked complete while waiting for human decision.
8. Prompt text cannot grant capability absent from registry policy.

## Temporary Bridge

Case workflow tasks currently enter bounded worker loop with workflow itself as
sole allowed child capability. Unified dispatcher now executes that call through
same middleware as chat and research. Workflow-as-single-child bridge remains
until workers can invoke workflow graphs directly without an extra model turn.

## Alternatives Considered

### Separate Tool, Workflow, and Agent Registries

Rejected. Clear local types, but three discovery and policy authorities recreate
caller drift and require adapters at every orchestration boundary.

### Add Capability Wrapper Around Existing Registries

Rejected. Wrapper would become another schema while underlying authorities kept
drifting.

### Keep Manual Caller Tool Lists

Rejected. Existing chat/research mismatch already proves manual lists unsafe.

## Consequences

- New capabilities require one catalog entry plus implementation binding.
- Chat, research, and case receive consistent workflow visibility.
- Existing names remain compatible during incremental migration.
- Catalog now mixes execution policy and semantic planning metadata intentionally.
- Unified dispatch and durable SQLite graph checkpoints are implemented.
- Durable invocation persistence and nested workflow HITL remain next work.

## Verification

- Registry rejects duplicate IDs and unknown plan capabilities.
- Every surface resolves all declared executable implementations.
- Planner cards contain only plannable workflows and agents.
- Memo and deck workflows remain independent from DCF.
- Chat, research, and case expose same workflow IDs.

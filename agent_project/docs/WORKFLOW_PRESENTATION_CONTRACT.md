# Workflow presentation contract

All domain workflows use one presentation path. DCF, deck, memo, and future
workflows must not return protocol payloads as visible status text.

## Separation

```text
workflow/tool result
  |-- machine payload --------> LangGraph state, result store, object versions
  |-- activity summary -------> compact chat status
  |-- activity detail --------> expandable audit panel
  |-- review_ref -------------> separate HITL event and interactive review card
  `-- artifact/object refs ---> durable renderer and lineage views
```

`summary` is one short human-readable sentence. It must never contain JSON,
Markdown tables, filesystem dumps, or full artifacts.

`detail` has stable optional categories:

```json
{
  "inputs": {},
  "outputs": {},
  "metrics": {},
  "evidence_refs": [],
  "object_refs": [],
  "artifact_refs": [],
  "notes": []
}
```

`review_ref` identifies review type and workflow. Review content travels in its
own SSE event, such as `memo_draft_review`; frontend renders dedicated card.

## Producer API

Use `workflow_activity.emit_workflow_step()` for every graph node and
`workflow_activity.emit_workflow()` for root lifecycle. Emit stable pair:

```text
start -> complete | skipped | error | awaiting_input
```

Put display sentence in `payload.summary_line`. Put audit fields in
`payload.detail`. Existing workflow-specific values may remain in metadata
during migration, but new workflows should use categorized detail directly.

## Frontend behavior

- Root opens while running or awaiting input.
- Root collapses after completion unless user manually toggled it.
- Every terminal step is expandable.
- Specialized views may render known financial structures.
- Generic audit view always renders lifecycle, duration, categorized fields,
  and durable references.
- HITL content renders as review card, never activity summary.

## Live delivery

Interactive runs wait up to 750ms for SSE subscription before graph execution.
SSE connection releases run immediately, sends anti-buffer prelude, then
delivers each lifecycle event as producer emits it. Background/API-only runs do
not wait for subscriber. This prevents early workflow steps arriving as replay
batch while preserving unattended execution.

## Tool boundary

Tool middleware treats structured output as machine protocol. Summary priority:

```text
summary -> message -> error -> type/status label -> "Tool completed"
```

Serialized dict/list content is never valid activity copy.

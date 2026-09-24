---
id: memo
kind: workflow
version: 0.1
title: Investment Memo Workflow
summary: Generate source-linked investment memo through typed workflow and review gate.
route_levels:
  - workflow
allowed_tools:
  - run_memo_workflow
allowed_workflows:
  - memo
tool_budget:
  max_calls: 1
  target_latency_s: 60
object_policy:
  creates_objects: true
  default_object_type: memo
required_outputs:
  schema: workflows.memo.MemoOutput
  citations_required: true
validators:
  - route_level_allowed
  - workflow_allowed
  - memo_payload
  - source_lineage
stop_conditions:
  - memo draft review reached
  - memo output persisted
---

# Investment Memo Workflow

## Objective

Produce decision-ready memo from selected workspace objects, documents, or analyst notes.

## Procedure

Use workflow context gate to select sources, audience, focus, and depth. Call `run_memo_workflow` with typed brief. Let memo graph generate draft, pause for review, then persist approved output.

## Guardrails

DCF remains optional. Never invent unsupported metrics. Preserve citations and exact source-version lineage. Never replace memo workflow with chat prose.

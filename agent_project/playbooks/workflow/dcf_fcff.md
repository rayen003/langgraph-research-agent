---
id: dcf_fcff
kind: workflow
version: 0.1
title: DCF / FCFF Workflow
summary: Prepare and run existing deterministic DCF workflow.
route_levels:
  - workflow
allowed_tools:
  - run_dcf_workflow
allowed_workflows:
  - dcf
tool_budget:
  max_calls: 1
  target_latency_s: 60
object_policy:
  creates_objects: true
  default_object_type: valuation_run
required_outputs:
  schema: valuation_run
  citations_required: true
validators:
  - route_level_allowed
  - workflow_allowed
  - valuation_payload
  - citation_presence
stop_conditions:
  - DCF review gate reached
  - DCF output persisted
---

# DCF / FCFF Workflow

## Objective

Run intrinsic valuation through existing deterministic DCF workflow.

## Procedure

Use current `workflow_context_review` gate for company, source, focus, audience, depth, and format. Then call `run_dcf_workflow`.

## Guardrails

Do not calculate DCF manually in chat. Do not rewrite completed DCF report. Present workflow output through existing DCF path.

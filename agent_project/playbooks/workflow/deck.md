---
id: deck
kind: workflow
version: 0.1
title: Deck Workflow
summary: Prepare and run existing deck compiler workflow.
route_levels:
  - workflow
allowed_tools:
  - run_deck_workflow
allowed_workflows:
  - deck
tool_budget:
  max_calls: 1
  target_latency_s: 60
object_policy:
  creates_objects: true
  default_object_type: deck
required_outputs:
  schema: deck
  citations_required: true
validators:
  - route_level_allowed
  - workflow_allowed
  - deck_payload
stop_conditions:
  - deck outline review reached
  - deck artifact persisted
---

# Deck Workflow

## Objective

Generate PPTX deck from selected workspace objects, DCF output, documents, or analyst notes.

## Procedure

Use existing deck workflow. Build `DeckBrief` and typed `DeckSource[]`. Let compiler produce outline, review gate, and PPTX.

## Guardrails

Do not invent slide deck in chat when deck workflow is available.

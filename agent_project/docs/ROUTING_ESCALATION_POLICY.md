# Routing Escalation Policy

> Transitional routing contract. ADR-001 supersedes separation of `tool` and
> `workflow` as capability boundaries. These route values remain compatibility
> fields until execution-envelope migration.

## Status

Implemented routing contract with dependency guard; further calibration remains.

## Date

2026-09-01

## Purpose

Prevent agent autonomy from turning simple questions into slow workflows while still allowing rigorous behavior through small playbooks.

The router decides execution intensity. The playbook decides behavior. These are separate.

## Core Rule

Use the cheapest execution level that can satisfy the user request.

```text
direct -> tool -> small_task -> workflow -> case
```

Escalate only when lower level cannot satisfy user intent, evidence needs, artifact requirements, or persistence requirements.

## Execution Levels

| Level | Use When | Tool Budget | Creates Objects | User Experience |
|-------|----------|-------------|-----------------|-----------------|
| `direct` | Concept, clarification, follow-up from visible context | 0 | No | Immediate answer |
| `tool` | One independent batch: current news, price, filing/doc search, calculations | 1-4 | No by default | Concurrent calls, short answer |
| `small_task` | Bounded multi-round ReAct tools and synthesis, no explicit task DAG | 3-6 | Optional task result | Brief progress, then answer |
| `workflow` | DCF, deck, memo, comparison, document analysis artifact | Workflow-defined | Yes | Setup/review gate, progress, artifact |
| `case` | Work with logical or chronological dependencies between delegated tasks | Case-defined | Yes | Case intake, dependency DAG, background work |

Multiple independent calls do not justify a case. `ToolBatchExecutor` runs calls from each agent turn concurrently through `ToolNode`. `small_task` uses bounded conversational ReAct rounds, not research-plan nodes. If case planner produces no `dependency_task_ids`, runtime downgrades route to this small-task loop before creating a `ResearchCase` object.

## Dropdown Cap

User-facing mode dropdown sets maximum ceremony. Semantic router still chooses exact route inside cap.

| UI Mode | Allowed Levels | Rule |
|---------|----------------|------|
| `Auto` | `direct`, `tool`, `small_task`, `workflow`, `case` | Choose cheapest sufficient level; confirm uncertain workflow/case |
| `Quick` | `direct`, `tool` | No setup gates, no durable case/workflow |
| `Workflow` | `direct`, `tool`, `small_task`, `workflow` | Allow structured workflow; no long-running case |
| `Case` | all levels, case preferred | Open/resume ResearchCase when request is underspecified |

If user asks for a blocked level in a capped mode, router should explain cap and offer switch. Example: Quick mode plus "build a DCF" should ask to switch to Workflow, not silently run DCF.

## Router Output Contract

```json
{
  "route_level": "tool",
  "playbook_id": "latest_company_news",
  "selected_workflow": null,
  "selected_case_type": null,
  "confidence": 0.91,
  "latency_class": "short",
  "max_tool_calls": 2,
  "creates_objects": false,
  "needs_confirmation": false,
  "object_policy": "none",
  "speech_act": "fresh_lookup",
  "context_reference": "none",
  "data_requirement": "fresh_external",
  "reason": "current company news lookup"
}
```

### Fields

- `route_level`: execution intensity, not task type.
- `playbook_id`: instruction profile loaded for behavior. Can be micro, task, workflow, or case playbook.
- `selected_workflow`: concrete workflow when `route_level=workflow`, e.g. `dcf`, `deck`, `memo`, `comparison`.
- `selected_case_type`: concrete case playbook when `route_level=case`, e.g. `listed_company_equity_research`.
- `confidence`: router confidence in route.
- `latency_class`: `instant`, `short`, `medium`, `long`, `background`.
- `max_tool_calls`: hard budget for non-workflow execution.
- `creates_objects`: whether durable workspace objects should be created.
- `needs_confirmation`: whether user must approve escalation before execution.
- `object_policy`: `none`, `ephemeral`, `task_result`, `artifact`, `case`.
- `speech_act`: semantic action requested now, independent of words found in prior turns.
- `context_reference`: thread/workspace referent needed to interpret current request.
- `data_requirement`: `none`, `thread`, `workspace`, or `fresh_external`.
- `reason`: short user-visible or loggable explanation.

## Context Before Route

Router receives a typed `TurnContextSnapshot` before selecting execution level:

- `latest_user_turn`: current instruction, kept separate from history.
- `previous_user_turn` and `previous_assistant_turn`: exact short-term recall anchors.
- `recent_messages`: bounded role-labelled checkpoint history.
- `session_memory`: compact durable session summary.
- `workspace_candidates`: bounded recent object metadata, never full payloads.
- `active_entities`: entity references carried by candidate objects.

This ordering prevents prior requests from being re-executed when current turn
asks for recall, clarification, correction, or continuation. Deep object graph,
document RAG, and dependency expansion remain post-route because they are more
expensive and route-specific.

## Playbook Is Not Workflow

Playbook is guidance and constraints. Workflow is stateful execution.

Micro-playbooks are allowed for simple tasks:

```yaml
id: latest_company_news
kind: micro
allowed_tools:
  - query_knowledge_graph
  - search_web
tool_budget:
  max_calls: 2
  target_latency_s: 8
rules:
  - use KG only as cache or hint
  - use web when query asks latest or current and KG is stale/empty
  - mention date scope
  - return concise bullets
object_policy:
  create: false
```

This gives quality without workflow overhead.

## Confirmation Rules

Require confirmation when:

- `route_level=case` and user did not explicitly ask for project/case/pack/end-to-end workflow.
- `route_level=workflow` and router confidence is below `0.80`.
- estimated latency is `long` or `background` and user asked a short question.
- durable objects will be created from an ambiguous prompt.
- workflow needs HITL or expensive API calls and mode is `Auto`.

Do not require confirmation when:

- user explicitly selects Workflow or Case mode.
- user says "build DCF", "generate deck", "draft memo", "create research case".
- level is `direct` or `tool`.

## Route Examples

| User Request | UI Mode | Router Result | Why |
|--------------|---------|---------------|-----|
| "latest news for Apple" | Auto | `tool`, `latest_company_news` | Current-data lookup only |
| "what is WACC?" | Auto | `direct`, `explain_finance_concept` | No fresh data needed |
| "Apple price today?" | Auto | `tool`, `current_market_snapshot` | One market-data lookup |
| "summarize Meta latest quarter and stock reaction" | Auto | `small_task`, `earnings_update` | Several sources, no artifact |
| "build DCF for Meta" | Auto | `workflow`, `dcf_fcff` | Structured valuation artifact |
| "make this DCF into a deck" | Auto | `workflow`, `deck` | Existing artifact workflow |
| "build full equity research case on Meta" | Auto | `case`, `listed_company_equity_research` | Multi-step long-running case |
| "analyze Apple" | Auto | `small_task` or ask case confirmation | Ambiguous scope |

## Current Code Integration

Short term:

- Keep `file.py` as top-level router.
- Replace binary `research/chat` classification with router output contract behind current modes.
- Keep direct/chat path for `direct` and `tool`.
- Keep existing `workflow_context_review` gate for `workflow`.
- Add `case` route only after ResearchCase persistence and playbook contract exist.

Medium term:

- `chat` becomes command layer over cases, tasks, and artifacts.
- `research` becomes small-task execution or gets deprecated.
- `case_orchestrator.py` handles long-running case DAGs.

## Implementation Tasks

### Task 1: Router Contract Models

Add Pydantic contract for router output and tests.

Acceptance:

- validates all route levels
- rejects impossible combinations, e.g. `route_level=workflow` with no `selected_workflow`
- allows `tool` route with `playbook_id` and no workflow
- carries mode cap and object policy

### Task 2: Escalation Policy Function

Add deterministic policy function that applies UI mode cap, confirmation rules, latency class, and object policy to router candidate.

Acceptance:

- Quick mode blocks workflow/case
- Auto mode prefers cheaper route when confidence is low
- ambiguous case requests require confirmation
- explicit workflow requests do not require extra confirmation

### Task 3: Semantic Router Prompt

Update intent classification to produce router contract, not only `research` or `chat`.

Acceptance:

- "latest Apple news" routes to `tool` with micro-playbook
- "build DCF for Meta" routes to `workflow`
- "build full research case on Meta" routes to `case`
- malformed LLM output falls back to safe `tool` or `direct`

### Task 4: Runtime Integration

Integrate router result into `file.py` without changing DCF/deck internals.

Acceptance:

- existing chat still works
- existing DCF workflow still reaches setup/HITL
- simple web-search question does not hit workflow setup
- router result emitted as UI/debug event

### Task 5: Telemetry

Track route decisions and misroutes.

Acceptance:

- route_level, playbook_id, latency_class, confidence, and confirmation flag are logged
- later eval can compare expected vs actual route

## Non-Goals

- No new UI in this step.
- No case orchestrator in this step.
- No DDM/comps implementation in this step.
- No provider connector rewrite in this step.
- No replacement of DCF/deck workflows.

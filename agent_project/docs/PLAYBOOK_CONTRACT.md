# Playbook Contract

> Capability discovery and caller exposure now follow ADR-001. Manual tool or
> workflow lists described below are migration context, not extension points.

## Status

Partially implemented for top-level semantic routing, local playbook loading, workflow dispatch, and dynamic chat tool binding. Validators, case orchestration, and research-task playbook injection remain future work.

## Date

2026-09-01

## Purpose

Playbooks encode analyst behavior without forcing every task into a compiled LangGraph workflow.

They let simple tasks stay fast, let complex tasks stay structured, and let future subagents receive narrow task guidance without reading the full project documentation or chat history.

## Core Decision

Playbooks are trusted local markdown files with machine-readable YAML frontmatter.

They are not always loaded into the global system prompt. They are selected by the router or orchestrator, then injected into the specific execution prompt for that turn/task.

```text
user request
  -> router output
  -> playbook_id
  -> playbook registry lookup
  -> playbook packet
  -> task prompt / workflow input
  -> validators from frontmatter
```

## Why Not Global System Prompt

Do not put all playbooks into the invariant system prompt.

Reasons:

- quick questions would pay context/latency cost for irrelevant guidance
- stale or conflicting playbooks would pollute unrelated tasks
- orchestration would become prompt-only instead of contract-driven
- future playbook library could grow without bound

Global prompt should only say:

- obey selected playbook
- respect tool budget
- return required output schema
- stop when playbook stop condition is met
- do not escalate execution level without router/orchestrator approval

## Why Not Vector Retrieval First

Initial access should be deterministic registry lookup, not semantic retrieval.

Use semantic retrieval later only for discovery among playbook cards when router lacks confidence.

```text
deterministic:
  route says playbook_id=latest_company_news
  registry loads playbooks/micro/latest_company_news.md

semantic fallback:
  route lacks playbook_id or confidence low
  search compact playbook cards
  choose candidate
  apply routing policy / confirmation
```

## File Layout

```text
agent_project/playbooks/
  README.md
  micro/
    latest_company_news.md
    current_market_snapshot.md
    finance_concept_explainer.md
  task/
    source_acquisition.md
    earnings_update.md
    peer_set_build.md
    business_analysis.md
  workflow/
    dcf_fcff.md
    memo.md
    deck.md
    comparison.md
  case/
    listed_company_equity_research.md
```

## Playbook Kinds

| Kind | Purpose | Creates Objects | Example |
|------|---------|-----------------|---------|
| `micro` | fast answer discipline for direct/tool routes | no by default | latest company news |
| `task` | autonomous research step | optional, usually `TaskResult` inside case | source acquisition |
| `workflow` | prepare/drive stateful workflow artifact | yes | DCF, deck, memo |
| `case` | long-running case DAG and golden path | yes | listed-company equity research |

## Frontmatter Schema

```yaml
id: latest_company_news
kind: micro
version: 0.1
title: Latest Company News
summary: Fast current-news lookup for a public company.
route_levels: [tool]
applies_when:
  - user asks for latest/current/recent company news
  - answer can fit in concise bullets
reject_when:
  - user asks for full research case
  - user asks for valuation, memo, or deck
allowed_tools:
  - query_knowledge_graph
  - search_web
allowed_workflows: []
tool_budget:
  max_calls: 2
  target_latency_s: 8
object_policy:
  creates_objects: false
  default_object_type: null
required_inputs:
  - company_or_ticker
required_outputs:
  schema: short_answer
  citations_required: true
validators:
  - route_level_allowed
  - tool_budget
  - freshness
  - citation_presence
stop_conditions:
  - enough current sources found
  - max tool calls reached
fallback:
  on_no_sources: say no reliable current sources found and state query date
```

### Required Frontmatter Fields

- `id`: stable unique identifier used by router.
- `kind`: `micro`, `task`, `workflow`, or `case`.
- `version`: prompt/behavior version.
- `title`: human-readable name.
- `summary`: compact card for router or orchestrator.
- `route_levels`: allowed execution levels.
- `applies_when`: matching criteria.
- `allowed_tools`: tool names permitted under this playbook.
- `tool_budget`: max calls and latency target for non-workflow routes.
- `object_policy`: persistence behavior.
- `required_outputs`: output contract.
- `validators`: deterministic checks to run after execution.
- `stop_conditions`: when agent should stop instead of expanding scope.

## Markdown Body Schema

```markdown
## Objective

What task means in analyst terms.

## Procedure

Preferred reasoning/tool sequence. Guidance, not hardcoded graph.

## Source Policy

Which source types count as acceptable support.

## Output Shape

Expected answer or object payload shape.

## Guardrails

Rules agent must not violate.

## Escalation

When to ask user or request higher execution level.
```

## Runtime Access Model

### Direct / Tool / Small Task

```text
router chooses route_level + playbook_id
  -> registry loads one playbook
  -> prompt builder injects compact PlaybookPacket
  -> chat/small-task executor runs with allowed tools + budget
  -> validators check output
```

Prompt receives only selected playbook, not full playbook directory.

### Workflow

```text
router chooses workflow + playbook_id
  -> workflow setup receives playbook constraints
  -> workflow tool receives typed request
  -> workflow output wrapped as artifact / TaskResult
```

DCF and deck internals stay unchanged. Playbook prepares inputs and validates outputs.

### Case

```text
orchestrator loads case playbook
  -> expands task DAG
  -> each CaseTask references task/workflow playbook_id
  -> subagent receives TaskBrief + selected playbook
  -> result validator persists TaskResult
```

Case playbook defines golden path. Task playbooks define execution behavior.

## Playbook Packet

Runtime should not inject raw file blindly. Build compact packet:

```json
{
  "id": "latest_company_news",
  "kind": "micro",
  "version": "0.1",
  "summary": "Fast current-news lookup for a public company.",
  "allowed_tools": ["query_knowledge_graph", "search_web"],
  "tool_budget": {"max_calls": 2, "target_latency_s": 8},
  "required_outputs": {"schema": "short_answer", "citations_required": true},
  "validators": ["route_level_allowed", "tool_budget", "freshness", "citation_presence"],
  "instructions": "Use KG only as cache/hint. Use web for latest/current queries. Return 3-5 concise bullets with source dates."
}
```

## Validator Boundary

Markdown guides. Code enforces.

Examples:

- `route_level_allowed`: selected route is in `route_levels`.
- `tool_budget`: max tool calls not exceeded.
- `freshness`: latest/current questions did not rely only on stale KG.
- `citation_presence`: material claims include source refs.
- `schema`: result matches required schema.
- `object_policy`: durable objects created only when allowed.

## Integration With Current Code

Implemented:

- `routing.py` defines `RouteDecision` and parser/fallback helpers.
- `playbooks/registry.py` loads trusted local markdown/frontmatter and returns compact playbook packets.
- `file.py` top-level graph now routes through `semantic_router -> load_playbook -> apply_runtime_policy -> execution path`.
- `conversational.py` injects selected playbook packets into chat prompts and dynamically binds allowed tools.
- execution trace events are emitted at `semantic_router`, `load_playbook`, `apply_runtime_policy`, `route_execution`, and `case_not_implemented`.
- `case` routes end in an explicit placeholder node until `case_orchestrator.py` exists.
- `tools.py` tool definitions remain unchanged.
- DCF/deck workflows remain unchanged and are reached through existing workflow setup/executor paths.

Medium term:

- `research.py` uses task playbooks for small research steps
- `case_orchestrator.py` uses case + task playbooks for long-running cases
- deterministic validators enforce playbook budgets, freshness, citations, schemas, and object policy

## Non-Goals

- no vector database for playbooks in first version
- no UI playbook picker in first version
- no replacement of system prompt
- no automatic workflow escalation from playbook text alone
- no provider/API integration in playbook contract

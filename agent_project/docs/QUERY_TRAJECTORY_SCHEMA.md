# Query Trajectory Schema

Design drawing for one user query moving through routing, playbooks, memory,
tools, workflows, and persistence.

Core correction: avoid hardcoded mode explosion. The graph should have two main
execution lanes, with route policy tuning behavior inside each lane.

## Two-Lane Model

```text
User message
  |
  v
LangGraph thread state
  - messages
  - UI mode: auto | research | chat
  - session_id
  - short-term checkpoint state
  |
  v
Pre-route turn context
  - exact latest user turn
  - exact previous user and assistant turns
  - bounded recent message window
  - session memory summary
  - recent workspace object cards and entity refs
  |
  v
Semantic router
  |
  v
Route policy
  - trajectory_lane: answer | work
  - effort: instant | short | medium | long | background
  - tool_budget
  - playbook_id
  - selected_workflow
  - selected_case_type
  - object_policy
  - memory_policy
  - speech_act
  - context_reference
  - data_requirement
  - needs_confirmation
  |
  +-------------------------------+
  |                               |
  v                               v
Answer lane                    Work lane
```

Short-term thread memory is unconditional router input. Route cannot decide
whether prior conversation exists. Route only controls deeper retrieval after
intent and references have been resolved.

```text
checkpoint messages + bounded object metadata
  -> TurnContextSnapshot
  -> one semantic interpretation/router call
  -> playbook and runtime policy
  -> route-aware deep memory/RAG expansion
  -> execution
```

Example:

```text
turn 1: "what are Apple's latest news?"
  -> fresh_lookup, tool, latest_company_news

turn 2: "what did I just ask you?"
  -> context_recall, previous_user_turn, direct
  -> no playbook, no web search
```

## Answer Lane

Handles old `direct` and old `tool`.

```text
Answer lane
  |
  v
Micro/base playbook
  - optional
  - tunes prompt/tool policy
  - does not change graph path
  |
  v
Light memory build
  |
  +-- instant effort
  |     current thread messages
  |
  +-- short effort
        current thread messages
        session summary
        workspace object cards if useful
  |
  v
Chat/tool loop
  |
  +-- model answers directly
  |
  +-- model calls allowed tools
        - search_web
        - query_knowledge_graph
        - search_documents when doc-focused
  |
  v
Final answer
  |
  v
LangGraph checkpoint
```

## Turn Ledger and Result References

No third execution-state object sits between graph and tools.

```text
AgentState.current_turn
  |
  +-- goal
  +-- route / playbook / execution policy
  +-- required_outputs
  +-- result_refs --------------------+
  +-- artifact_refs ------------------|----+
  +-- artifact_paths -----------------|----|----+
  +-- response status                 |    |    |
                                      v    v    v
                                ToolResult  object versions  artifact store
```

References append during one turn. New user turn replaces current route and starts new
result lists. Prior durable objects remain retrievable through pre-route workspace cards;
route/action history remains append-only in application database.

## Generic Tool Composition

```text
"find five years of Apple financials and graph revenue/net income"
  |
  v
semantic route
  - small_task
  - required_outputs: answer, chart
  |
  v
bounded ReAct controller
  |
  +-- round 1 ToolNode batch
  |     get_company_financials -> tool_result_id
  |     optional independent context lookup -> tool_result_id
  |
  +-- round 2 dependent call
  |     render_financial_chart(input_result_id=<financial result ID>)
  |       -> chart object version + PNG path
  |
  +-- completion gate
        answer exists? chart artifact exists?
        yes -> finish
        no  -> continue within budget or return explicit incomplete status
```

Controller does not hardcode this sequence. Model chooses calls from typed tools; runtime
provides concurrency, reference passing, budgets, persistence, and output completion.
Explicit DAG starts only when dependency-bearing work needs scheduling, delegation,
resumption, or background execution beyond bounded loop.

## Minimal Analyst Loop

Normal analyst work uses one memory-first controller loop. Existing `AgentState`,
`current_turn`, `RouteDecision`, `ToolResult`, and workspace object versions remain
the contracts; no parallel execution-state schema is introduced.

```text
user message + short-term thread history
  |
  v
build turn context
  |
  v
retrieve memory cards + evidence summaries
  |
  v
strong analyst controller (ORCHESTRATOR_MODEL)
  |  chooses scope, deliverables, tool calls, workflow/case escalation
  |  route is policy + telemetry, not substitute for reasoning
  |
  +--> answer directly
  |
  +--> independent ToolNode calls run concurrently
  |      |
  |      v
  |    ToolResult IDs + compact observations
  |      |
  |      v
  |    resolve only synthesis-relevant fields from result IDs
  |      |
  |      +--> controller continues with dependent calls
  |      +--> controller drafts grounded answer/report
  |
  +--> explicit workflow with HITL
  |
  +--> dependency-bearing case DAG
  |
  v
completion validator
  - requested outputs present
  - required artifacts exist
  - named entities covered
  - financial answer contains evidence values, not placeholder prose
  |
  +--> pass -> persist turn object + finish
  +--> fail -> one evidence-backed repair pass -> persist explicit status
```

Model policy:

- `ORCHESTRATOR_MODEL` defaults to `gpt-4.1` for scope and routing decisions.
- `ANALYST_MODEL` defaults to `ORCHESTRATOR_MODEL` for tool selection and synthesis.
- Specialized workers may remain cheaper when their output contract is narrow and validated.
- Current-news fast path keeps bounded search latency and separate grounded synthesis.

```text
"what is WACC?"
  -> answer lane, instant effort, no tools

"latest news for Apple"
  -> answer lane, short effort, search_web allowed

Same graph lane. Prompt/tool policy changes.
```

## Work Lane

Handles old `small_task`, old `workflow`, and old `case`.

```text
Work lane
  |
  v
Work playbook
  |
  +-- lightweight research task
  |     examples: earnings update, source acquisition, market snapshot
  |
  +-- deterministic workflow
  |     examples: DCF, deck, memo, comparison, document analysis
  |
  +-- research case
        examples: listed-company equity research, private-company diligence
  |
  v
Memory build
  |
  +-- medium effort
  |     workspace cards
  |     KG nodes
  |     document chunks
  |     shallow dependencies
  |
  +-- long/background effort
        broad object graph
        KG
        docs
        LangGraph long-term store
        lazy full-payload expansion
  |
  v
Work controller
  |
  +-- selected_workflow exists
  |      workflow setup card
  |        - sources
  |        - objects
  |        - focus areas
  |        - output requirements
  |      |
  |      v
  |      deterministic workflow graph
  |
  +-- selected_case_type exists
  |      case orchestrator
  |        - creates tasks
  |        - delegates later
  |        - merges TaskResult objects
  |
  +-- no selected workflow/case
         research loop
           - plan
           - execute
           - synthesize
  |
  v
Object envelope
  |
  v
Persistence
  - LangGraph checkpoint
  - workspace object store
  - KG/source refs/artifacts when present
```

```text
"summarize Apple quarter and stock reaction"
  -> work lane, medium effort, research loop, TaskResult object optional

"build a DCF for Apple"
  -> work lane, long effort, selected_workflow=dcf, artifact object

"build full Apple equity research case"
  -> work lane, background effort, selected_case_type=listed_company_equity_research

Same work lane. Work controller policy changes.
```

## Route Policy Shape

Router should choose lane + policy, not many hardcoded graph modes.

```text
route_policy
  |
  +-- trajectory_lane
  |     answer | work
  |
  +-- effort
  |     instant | short | medium | long | background
  |
  +-- playbook_id
  |     latest_company_news | dcf_fcff | deck | listed_company_equity_research | null
  |
  +-- selected_workflow
  |     dcf | deck | memo | comparison | document_analysis | null
  |
  +-- selected_case_type
  |     listed_company_equity_research | private_company_diligence | null
  |
  +-- tool_budget
  |     max_calls
  |
  +-- object_policy
  |     none | ephemeral | task_result | artifact | case
  |
  +-- memory_policy
        thread_only | cards | scoped | expanded | broad
```

Compatibility aliases:

```text
direct      -> answer lane + instant effort + thread_only memory
tool        -> answer lane + short effort + cards memory + tools allowed
small_task  -> work lane + medium effort + scoped memory + research loop
workflow    -> work lane + long effort + expanded memory + selected_workflow
case        -> work lane + background effort + broad memory + selected_case_type
```

These aliases should not become separate graph branches.

## Playbook Binding

```text
route_policy
  |
  v
playbook registry
  |
  v
selected_playbook
  |
  +-- instructions
  +-- allowed_tools
  +-- allowed_workflows
  +-- tool_budget
  +-- validators
  +-- required_outputs
  +-- object_policy
```

Playbooks tune behavior. They should not replace deterministic graph nodes for:

- validation
- calculations
- workflow setup
- persistence
- HITL gates

## Memory Build

```text
route_policy + selected_playbook + session_id + thread state
  |
  v
memory policy
  |
  +-- thread_only
  |     current messages
  |
  +-- cards
  |     current messages
  |     session summary
  |     workspace object cards
  |
  +-- scoped
  |     cards
  |     matching KG nodes
  |     relevant documents
  |
  +-- expanded
  |     scoped context
  |     selected object dependencies
  |     source refs
  |
  +-- broad
        object graph
        KG/docs/store
        dependency expansion
        full payloads only when explicitly needed
```

## Context Pack

Downstream chat, tools, workflows, and future subagents receive same compact
context pack.

```text
context_pack
  |
  +-- query
  +-- route_policy
  +-- selected_playbook_card
  +-- memory_summary
  +-- retrieved_object_cards
  +-- expanded_dependencies
  +-- source_refs
  +-- kg_node_ids
  +-- artifact_paths
  +-- gaps
```

Object cards are cheap and safe. Full payloads stay lazy unless work policy
requires them.

## Object Flow

```text
Workflow/task output
  |
  v
Universal object envelope
  - object_id
  - object_type
  - title
  - summary
  - search_text
  - entity_refs
  - source_refs
  - references
  - kg_node_ids
  - artifact_paths
  - quality
  - payload
  |
  v
Workspace object store
  |
  +--> UI object rail
  +--> memory retrieval
  +--> dependency expansion
  +--> future subagent handoff
```

## LangGraph Memory Mapping

```text
Short-term memory
  |
  v
LangGraph checkpointer
  - thread-scoped state
  - messages
  - route policy
  - selected playbook
  - pending workflow review
  - current context pack

Long-term memory
  |
  v
LangGraph store + app SQLite
  - cross-thread facts
  - workspace objects
  - KG nodes
  - source refs
  - artifacts
```

Design constraint: LangGraph stores memory, but graph nodes still decide what to
retrieve and inject. Memory retrieval remains explicit.

## Main Graph

```text
START
  |
  v
build_turn_context
  |
  v
build_memory_context
  |
  v
semantic_router / analyst controller
  |
  v
load_playbook -> apply_runtime_policy
  |
  v
route_lane
  |
  +--> answer_controller
  |      |
  |      v
  |    bounded ReAct -> validate -> persist -> END
  |
  +--> work_controller
         |
         +--> research_loop
         +--> workflow_setup -> workflow_graph
         +--> case_orchestrator
         |
         v
       validate + persist_outputs
         |
         v
       END
```

# Tool Execution Runtime

This layer standardizes tool metadata, invocation lifecycle, and observations around LangGraph `ToolNode` execution.

## Contracts

`domain/tool_execution.py` defines:

- `ToolSpec`: stable execution policy for one registered tool.
- `ToolInvocation`: one call's identity, context, argument hash, attempts, deadline, status, and result reference.
- `ToolResult`: normalized observation returned to the agent.
- `ToolProgress`: ordered partial event written to LangGraph's custom stream.
- `ToolCachePolicy` and `ToolRetryPolicy`: validated subordinate policies.

These contracts do not replace `AgentState`. `AgentState.current_turn` owns turn-level
control and carries only IDs gathered from `ToolResult`; `ToolResult` owns one call's
observation; workspace object versions own durable outputs.

Each `ToolSpec` selects an `interactive` or `background` execution lane. Streaming tools may set a heartbeat interval. Non-idempotent tools cannot enable cache or automatic retries. First-event SLO and progress interval cannot exceed execution deadline. Unknown specification fields are rejected.

## Middleware

`ToolExecutionMiddleware` creates a LangGraph `ToolNode` with synchronous and asynchronous wrappers. It provides:

- strict registry validation at graph construction;
- invocation and result lifecycle events;
- existing activity and runtime trace spans;
- argument redaction in invocation telemetry;
- deterministic raw-argument hashing and cache keys without exposing redacted values;
- TTL cache for successful idempotent calls;
- bounded retries for explicitly named exception types;
- hard wall-clock deadlines on asynchronous execution;
- post-execution deadline detection on synchronous execution;
- per-key serialization for shared providers or mutation domains;
- separate interactive and background concurrency limits;
- queue-wait timing inside invocation metadata;
- ordered `started`, `attempt`, `heartbeat`, `partial`, and terminal events through LangGraph `StreamWriter`;
- normalized `ToolResult` JSON inside returned `ToolMessage`;
- error recognition from both `ToolMessage.status` and structured error payloads;
- telemetry failure isolation from tool outcomes;
- unchanged propagation of LangGraph `Command` results.

Independent tool calls emitted in one AI message remain parallel through `ToolNode`. Calls sharing a `concurrency_key` are serialized by middleware.

`ToolBatchExecutor` compiles middleware and `ToolNode` into a reusable one-node graph. `tool` and `small_task` conversational batches use its async path, so independent calls run concurrently and network tools use their nonblocking implementations. `small_task` remains a bounded multi-round ReAct loop; it does not create a task DAG. A presentation adapter unwraps normalized output for the existing chat prompt while preserving complete `ToolResult` in message artifacts and runtime events.

Current-news micro execution performs one web search, ranks and deduplicates story results, removes generic topic pages, then makes one tool-free synthesis call. Synthesis receives no URLs, cites bounded source IDs, and has an eight-second provider deadline. Answer tokens stream while synthesis runs; final response replaces valid source IDs with named, dated links. Contract failure or timeout falls back to a deterministic grounded brief instead of exposing raw search payloads.

## Bounded Composition Loop

Conversational `small_task` execution uses same bounded ReAct loop as short tool turns:

```text
model
  -> zero or more independent calls in one ToolNode batch
  -> normalized ToolResult messages
  -> collect result/object/artifact references into current_turn
  -> model may issue dependent call using prior tool_result_id
  -> completion gate checks requested outputs
  -> final answer
```

No task DAG is created for this path. Explicit DAGs remain reserved for work with real
dependencies, delegation, resumability, or background scheduling beyond bounded loop.
Independent calls in one model response execute concurrently through `ToolNode`.

Completion gate is output-based, not query-specific. Router records requested output
types such as `answer` and `chart`; loop cannot accept prose-only completion while a
requested artifact is absent. Gate feeds missing outputs back into next ReAct round.

Route metadata controls cost, playbook selection, and execution lane; it is not a hard
capability gate for normal conversation. Base `direct`, `tool`, and `small_task` turns
retain a bounded set of non-workflow tools, allowing the ReAct model to recover when
semantic routing understates a request. DCF, deck, and memo tools remain excluded until
an explicit workflow route reaches their HITL setup. Router parse/provider failures use
a bounded `small_task` fallback instead of a tool-free answer path.

Named playbooks remain restrictive allowlists. Runtime rejects a selected playbook when
its tool set cannot produce an explicit requested artifact, then returns to the base
bounded loop. This is output-capability validation, not phrase-based routing.

## Result Reference Flow

`reference_broker.py` resolves existing `tool_result_id`, workspace `object_id`, and
immutable object-version IDs. It also collects normalized and legacy pointer fields.
No raw result expansion is injected into model unless model or downstream tool asks for
it. Tools can consume references directly, keeping large financial series out of model
context.

Current financial composition path:

```text
get_company_financials(ticker, period, limit)
  -> existing DCF FMP client
  -> company_financial_history.v1 in ToolResult store
  -> compact tool_result_id returned to model

render_financial_chart(input_result_id, metrics, chart_type)
  -> broker resolves financial payload server-side
  -> PNG artifact
  -> immutable chart workspace-object version
  -> ToolResult containing artifact path + object version ID
```

Default lane capacities are eight interactive calls and two background calls. Limits are configurable through `ToolLaneLimits`. Long workflows therefore cannot consume interactive slots. Async execution deadline includes lane queue time.

## Async Network Boundaries

- `search_web` uses native `httpx.AsyncClient` I/O.
- SEC filing retrieval, document RAG, and knowledge-graph synthesis expose async tool coroutines and move their remaining synchronous libraries into worker threads.
- Sync functions remain attached to each tool for existing HITL and command-line callers.

Tools call `write_tool_progress(...)` for domain partials. Middleware binds this helper to current invocation's LangGraph `StreamWriter`, adds invocation identity and sequence, and mirrors events onto existing UI event bus. Graph consumers receive them with `stream_mode="custom"`; current SSE UI receives same envelope without polling graph state.

## Current Catalog

`tool_catalog.py` registers policies for all tools currently exposed by chat:

- calculator;
- web search and stored-result retrieval;
- document search and SEC retrieval;
- knowledge-graph query;
- Python execution;
- structured company financial history and financial chart rendering;
- DCF, deck, and memo workflows.

Workflow and write tools are non-idempotent and uncached. Network reads are eligible for short TTL caching. Catalog deadlines are initial policy values and must be calibrated from runtime traces.

## Deliberate Boundaries

- Conversational and direct current-news calls use `ToolBatchExecutor`. Research step execution still uses its older manual loop.
- `fallback_tool_ids` records recovery options and appears in timeout/error observations; scheduler does not execute fallbacks yet.
- `stale_while_revalidate_seconds` is contract metadata; background refresh is not implemented yet.
- Arbitrary synchronous Python cannot be safely terminated. Production graph must use asynchronous `ToolNode` execution for hard deadlines.
- Sync and async compatibility paths maintain separate process-local lane semaphores. Production graph should use async execution exclusively.
- HITL remains owned by workflow interrupts. Generic `requires_confirmation` enforcement belongs in dispatcher migration.

## Next Migration

1. Repeat batch execution migration for research workers.
2. Add dispatcher-level budgets, native interrupts, fallbacks, and durable cache.

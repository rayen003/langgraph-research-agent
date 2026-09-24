# Runtime Tracing

Runtime tracing records where user-visible latency is spent. It is diagnostic data, separate from activity events shown as user-facing tool/workflow progress.

## Span Model

Every run receives a unique `trace_id` and root `span_id`. Child spans use this envelope:

```json
{
  "type": "trace_span",
  "trace_id": "trace_...",
  "span_id": "node_...",
  "parent_span_id": "run_...",
  "category": "run|transport|node|model|tool|memory",
  "name": "semantic_router",
  "status": "started|running|completed|error",
  "started_at": 1788460000.0,
  "ended_at": 1788460001.2,
  "duration_ms": 1200,
  "metadata": {},
  "error": null
}
```

Lifecycle events share one `span_id`. Consumers merge later status events into the original span. Prompts, tool results, and model outputs are not copied into traces; metadata contains operational fields only.

## Coverage

- `frontend_run_request`: browser POST latency.
- `submit_to_first_event`: user submit to first SSE event.
- `agent_run` / `dcf_workflow_run`: full backend task.
- `sse_connection`: stream lifetime and replay/poll counters.
- Parent graph nodes: router, playbook, policy, memory, controller, chat/research/case nodes.
- Workflow nodes: DCF, deck, memo, and DCF review subgraph.
- `chat_model`: model latency, first-output timestamp when streaming, model name, token usage when supplied by provider.
- Tool spans: tool duration, scope, step, argument preview, and summary.

## Access

Live events flow through `GET /runs/{thread_id}/events`. Persisted spans are available from:

```text
GET /runs/{thread_id}/trace
GET /runs/{thread_id}/trace?trace_id=trace_...
```

Without `trace_id`, endpoint returns latest turn in thread. UI merges spans live and stores final trace with session message so completed runs remain inspectable in Trace sidebar.

## Simple News Contract

Opt-in live E2E test treats current-company news as short path:

- first trace event under 1 second;
- router under 5 seconds;
- total graph execution under 25 seconds;
- no workflow selection;
- at most two model calls;
- exactly one web-search activity.

Run with:

```bash
uv run --extra dev pytest agent_project/tests/e2e/test_thread_memory_workflow_e2e.py \
  -q -s --run-live-agent-e2e -k live_simple_current_news
```

Budget failure is product regression even when answer-quality assertions pass.

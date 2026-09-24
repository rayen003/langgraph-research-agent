"""Canonical runtime tracing for agent runs.

Trace spans are diagnostic events, separate from user-facing activity events.
They are persisted through the existing job-event stream and merged by span_id.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler


TraceCategory = Literal["run", "transport", "node", "model", "tool", "memory"]
TraceStatus = Literal["started", "running", "completed", "error"]

_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_active_span_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_span_id", default=None)


def make_trace_span(
    *,
    trace_id: str,
    span_id: str,
    category: TraceCategory,
    name: str,
    status: TraceStatus,
    parent_span_id: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    duration_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "trace_span",
        "trace_id": trace_id,
        "span_id": span_id,
        "category": category,
        "name": name,
        "status": status,
    }
    if parent_span_id:
        payload["parent_span_id"] = parent_span_id
    if started_at is not None:
        payload["started_at"] = started_at
    if ended_at is not None:
        payload["ended_at"] = ended_at
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    if metadata:
        payload["metadata"] = metadata
    if error:
        payload["error"] = error
    return payload


def set_trace_context(trace_id: str, root_span_id: str | None = None) -> None:
    _trace_id_ctx.set(trace_id)
    _active_span_ctx.set(root_span_id)


def current_trace_id() -> str:
    return _trace_id_ctx.get()


def current_span_id() -> str | None:
    return _active_span_ctx.get()


def emit_trace_span(payload: dict[str, Any]) -> None:
    if not payload.get("trace_id"):
        return
    # Local import avoids utils -> tracing -> utils import cycle.
    from utils import emit_ui_event  # noqa: PLC0415

    emit_ui_event(payload)


@contextlib.contextmanager
def track_span(
    *,
    category: TraceCategory,
    name: str,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    trace_id = current_trace_id()
    resolved_span_id = span_id or f"{category}_{uuid4().hex[:12]}"
    resolved_parent = parent_span_id if parent_span_id is not None else current_span_id()
    started_at = time.time()
    details: dict[str, Any] = dict(metadata or {})
    emit_trace_span(make_trace_span(
        trace_id=trace_id,
        span_id=resolved_span_id,
        parent_span_id=resolved_parent,
        category=category,
        name=name,
        status="started",
        started_at=started_at,
        metadata=details,
    ))
    token = _active_span_ctx.set(resolved_span_id)
    try:
        yield details
    except Exception as exc:  # noqa: BLE001
        ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=trace_id,
            span_id=resolved_span_id,
            parent_span_id=resolved_parent,
            category=category,
            name=name,
            status="error",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=(ended_at - started_at) * 1000,
            metadata=details,
            error=f"{type(exc).__name__}: {exc}",
        ))
        raise
    else:
        ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=trace_id,
            span_id=resolved_span_id,
            parent_span_id=resolved_parent,
            category=category,
            name=name,
            status="completed",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=(ended_at - started_at) * 1000,
            metadata=details,
        ))
    finally:
        _active_span_ctx.reset(token)


def traced_node(name: str, fn: Callable[..., Any], *, category: TraceCategory = "node") -> Callable[..., Any]:
    """Wrap LangGraph node with start/end/error trace events."""
    def restore_from_state(args: tuple[Any, ...]) -> None:
        if current_trace_id() or not args or not isinstance(args[0], dict):
            return
        state = args[0]
        if state.get("trace_id"):
            set_trace_context(str(state["trace_id"]), state.get("root_span_id"))

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            restore_from_state(args)
            with track_span(category=category, name=name):
                return await fn(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        restore_from_state(args)
        with track_span(category=category, name=name):
            return fn(*args, **kwargs)

    return wrapper


def _uuid_text(value: UUID | str | None) -> str | None:
    return str(value) if value is not None else None


class TraceCallbackHandler(BaseCallbackHandler):
    """Capture LangChain model latency and token usage without prompt content."""

    def __init__(self, trace_id: str, root_span_id: str | None = None) -> None:
        self.trace_id = trace_id
        self.root_span_id = root_span_id
        self._starts: dict[str, tuple[float, str | None, dict[str, Any]]] = {}
        self._first_tokens: set[str] = set()
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        run_key = str(run_id)
        params = kwargs.get("invocation_params") or {}
        metadata = {
            "model": params.get("model_name") or params.get("model") or serialized.get("name") or "chat_model",
            "message_count": sum(len(batch) for batch in messages),
            "streaming": bool(params.get("stream")),
        }
        parent = current_span_id() or _uuid_text(parent_run_id) or self.root_span_id
        started_at = time.time()
        with self._lock:
            self._starts[run_key] = (started_at, parent, metadata)
        emit_trace_span(make_trace_span(
            trace_id=self.trace_id,
            span_id=run_key,
            parent_span_id=parent,
            category="model",
            name="chat_model",
            status="started",
            started_at=started_at,
            metadata=metadata,
        ))

    def on_llm_new_token(self, token: str, *, run_id: UUID, **kwargs: Any) -> None:
        run_key = str(run_id)
        with self._lock:
            if run_key in self._first_tokens or run_key not in self._starts:
                return
            self._first_tokens.add(run_key)
            started_at, parent, metadata = self._starts[run_key]
        now = time.time()
        emit_trace_span(make_trace_span(
            trace_id=self.trace_id,
            span_id=run_key,
            parent_span_id=parent,
            category="model",
            name="chat_model",
            status="running",
            started_at=started_at,
            duration_ms=(now - started_at) * 1000,
            metadata={**metadata, "first_output_at": now},
        ))

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        run_key = str(run_id)
        with self._lock:
            record = self._starts.pop(run_key, None)
        if record is None:
            return
        started_at, parent, metadata = record
        ended_at = time.time()
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        completed_meta = dict(metadata)
        if usage:
            completed_meta["token_usage"] = usage
        if llm_output.get("model_name"):
            completed_meta["model"] = llm_output["model_name"]
        emit_trace_span(make_trace_span(
            trace_id=self.trace_id,
            span_id=run_key,
            parent_span_id=parent,
            category="model",
            name="chat_model",
            status="completed",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=(ended_at - started_at) * 1000,
            metadata=completed_meta,
        ))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        run_key = str(run_id)
        with self._lock:
            record = self._starts.pop(run_key, None)
        if record is None:
            return
        started_at, parent, metadata = record
        ended_at = time.time()
        emit_trace_span(make_trace_span(
            trace_id=self.trace_id,
            span_id=run_key,
            parent_span_id=parent,
            category="model",
            name="chat_model",
            status="error",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=(ended_at - started_at) * 1000,
            metadata=metadata,
            error=f"{type(error).__name__}: {error}",
        ))

"""Universal execution middleware for LangGraph ToolNode."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, StreamWriter

from domain.tool_execution import CapabilitySpec, ToolInvocation, ToolProgress, ToolResult, ToolSpec
from tracing import current_span_id, current_trace_id
from utils import emit_ui_event, track_tool
import agent_log


ToolResponse = ToolMessage | Command
SyncExecutor = Callable[[ToolCallRequest], ToolResponse]
AsyncExecutor = Callable[[ToolCallRequest], Awaitable[ToolResponse]]
EventSink = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolLaneLimits:
    """Independent capacity limits keep slow work from starving user-facing calls."""

    interactive: int = 8
    background: int = 2

    def __post_init__(self) -> None:
        if self.interactive < 1 or self.background < 1:
            raise ValueError("tool lane limits must be positive")

    def capacity(self, lane: str) -> int:
        return self.background if lane == "background" else self.interactive


@dataclass
class _ToolStreamContext:
    invocation: ToolInvocation
    writer: StreamWriter | None
    event_sink: EventSink | None = None


_tool_stream_context: contextvars.ContextVar[_ToolStreamContext | None] = contextvars.ContextVar(
    "tool_stream_context",
    default=None,
)


def write_tool_progress(
    summary: str,
    *,
    partial: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit domain progress from inside a wrapped tool through LangGraph custom stream."""
    context = _tool_stream_context.get()
    if context is None:
        return
    _write_progress_event(
        context,
        phase="partial",
        summary=summary,
        partial=partial,
        metadata=metadata,
    )


def _write_progress_event(
    context: _ToolStreamContext,
    *,
    phase: str,
    summary: str,
    partial: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    writer = context.writer
    if writer is None and context.event_sink is None:
        return
    invocation = context.invocation
    sequence = int(invocation.metadata.get("stream_event_count") or 0) + 1
    invocation.metadata["stream_event_count"] = sequence
    progress = ToolProgress(
        invocation_id=invocation.invocation_id,
        tool_id=invocation.tool_id,
        phase=phase,
        sequence=sequence,
        summary=summary,
        partial=partial,
        metadata=metadata or {},
    )
    payload = {"type": "tool_progress", "progress": progress.model_dump(mode="json", exclude_none=True)}
    if writer is not None:
        try:
            writer(payload)
        except Exception:  # noqa: BLE001
            logger.exception("LangGraph tool progress writer failed")
    if context.event_sink is not None:
        try:
            context.event_sink(payload)
        except Exception:  # noqa: BLE001
            logger.exception("Tool progress event sink failed")


class CapabilityRegistry:
    """Single registry for atomic tools, workflows, and bounded agents."""

    def __init__(self, specs: Sequence[CapabilitySpec] = ()) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: CapabilitySpec, *, replace: bool = False) -> None:
        if spec.capability_id in self._specs and not replace:
            raise ValueError(f"CapabilitySpec already registered: {spec.capability_id}")
        self._specs[spec.capability_id] = spec

    def get(self, tool_id: str) -> CapabilitySpec:
        try:
            return self._specs[tool_id]
        except KeyError as exc:
            raise KeyError(f"No CapabilitySpec registered for capability: {tool_id}") from exc

    def cards(self, *, plannable_only: bool = False) -> list[dict[str, Any]]:
        specs = self._specs.values()
        if plannable_only:
            specs = (spec for spec in specs if spec.plannable)
        return [spec.model_dump(mode="json") for spec in specs]

    def specs_for_surface(self, surface: str) -> list[CapabilitySpec]:
        return [spec for spec in self._specs.values() if surface in spec.exposed_in]

    def validate_plan_capabilities(self, capability_ids: list[str]) -> None:
        unknown = sorted(set(capability_ids) - set(self._specs))
        if unknown:
            raise ValueError(f"Unsupported capabilities: {unknown}")
        non_plannable = sorted({
            capability_id for capability_id in capability_ids
            if not self.get(capability_id).plannable
        })
        if non_plannable:
            raise ValueError(f"Capabilities are not plannable: {non_plannable}")

    def validate_dependencies(self, tasks: list, *, initial_types: set[str] | None = None) -> None:
        by_id = {task.task_id: task for task in tasks}
        initial = set(initial_types or set())
        for task in tasks:
            capability = self.get(task.capability_id)
            available = set(initial)
            for dependency_id in task.dependency_task_ids:
                available.update(by_id[dependency_id].required_output_types)
            has_external_inputs = bool(task.input_object_version_ids)
            missing_all = set(capability.requires_all) - available
            if missing_all and not has_external_inputs:
                raise ValueError(f"Task {task.task_id} missing required inputs: {sorted(missing_all)}")
            if capability.requires_any and not (set(capability.requires_any) & available) and not has_external_inputs:
                raise ValueError(f"Task {task.task_id} requires one of: {sorted(capability.requires_any)}")

    def validate_tools(self, tools: Sequence[BaseTool | Callable[..., Any]]) -> None:
        missing = sorted(
            name for tool in tools
            if (name := getattr(tool, "name", getattr(tool, "__name__", ""))) not in self._specs
        )
        if missing:
            raise ValueError(f"Missing CapabilitySpec registrations: {missing}")
        unknown_fallbacks = sorted({
            fallback_id
            for spec in self._specs.values()
            for fallback_id in spec.fallback_tool_ids
            if fallback_id not in self._specs
        })
        if unknown_fallbacks:
            raise ValueError(f"Unknown fallback CapabilitySpec registrations: {unknown_fallbacks}")

    def validate_capability_links(self) -> None:
        unknown_children = sorted({
            child_id
            for spec in self._specs.values()
            for child_id in spec.allowed_tools
            if child_id not in self._specs
        })
        if unknown_children:
            raise ValueError(f"Unknown child capability registrations: {unknown_children}")


# Transitional import alias. Runtime has one registry implementation.
ToolRegistry = CapabilityRegistry


@dataclass
class _CacheEntry:
    result: ToolResult
    expires_at: float


class InMemoryToolCache:
    """Small process-local TTL cache used by execution middleware."""

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> ToolResult | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            return entry.result.model_copy(deep=True)

    def put(self, key: str, result: ToolResult, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = _CacheEntry(
                result=result.model_copy(deep=True),
                expires_at=time.monotonic() + ttl_seconds,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class _ToolBatchState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    thread_id: str
    session_id: str
    user_id: str | None
    tool_execution_context: dict[str, Any]


class ToolExecutionMiddleware:
    """Policy, telemetry, cache, retry, and normalization boundary for ToolNode."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        cache: InMemoryToolCache | None = None,
        event_sink: EventSink | None = None,
        lane_limits: ToolLaneLimits | None = None,
    ) -> None:
        self.registry = registry
        self.cache = cache or InMemoryToolCache()
        self.event_sink = event_sink or emit_ui_event
        self.lane_limits = lane_limits or ToolLaneLimits()
        self._sync_locks: dict[str, threading.Lock] = {}
        self._sync_locks_guard = threading.Lock()
        self._async_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._async_locks_guard = threading.Lock()
        self._sync_lanes = {
            "interactive": threading.BoundedSemaphore(self.lane_limits.interactive),
            "background": threading.BoundedSemaphore(self.lane_limits.background),
        }
        self._async_lanes: dict[tuple[int, str], asyncio.Semaphore] = {}
        self._async_lanes_guard = threading.Lock()

    def create_tool_node(
        self,
        tools: Sequence[BaseTool | Callable[..., Any]],
        *,
        name: str = "tools",
        messages_key: str = "messages",
        handle_tool_errors: bool | str | Callable[..., str] | type[Exception] | tuple[type[Exception], ...] = False,
    ) -> ToolNode:
        self.registry.validate_tools(tools)
        return ToolNode(
            tools,
            name=name,
            messages_key=messages_key,
            handle_tool_errors=handle_tool_errors,
            wrap_tool_call=self.wrap_tool_call,
            awrap_tool_call=self.awrap_tool_call,
        )

    def wrap_tool_call(self, request: ToolCallRequest, execute: SyncExecutor) -> ToolResponse:
        spec = self.registry.get(str(request.tool_call.get("name") or ""))
        invocation = self._new_invocation(request, spec)
        invocation.metadata["deadline_mode"] = "post_execution"
        cached = self._cached_result(invocation, spec)
        if cached is not None:
            return self._complete_cached_with_trace(request, invocation, cached, spec)

        started_monotonic = time.monotonic()
        self._start_invocation(request, invocation, spec)
        lock = self._sync_lock(spec.concurrency_key)
        lane = self._sync_lanes[spec.execution_lane]
        try:
            with track_tool(
                activity_id=invocation.invocation_id,
                name=spec.tool_id,
                scope=self._scope(request.state),
                step_id=self._step_id(request.state),
                args_preview=self._args_preview(invocation.arguments),
            ) as span:
                try:
                    queued_at = time.monotonic()
                    with lane:
                        invocation.metadata["lane_queue_wait_ms"] = round(
                            (time.monotonic() - queued_at) * 1000,
                            3,
                        )
                        if lock is None:
                            response, attempts = self._run_sync_attempts(request, execute, spec, invocation)
                        else:
                            with lock:
                                response, attempts = self._run_sync_attempts(request, execute, spec, invocation)
                    elapsed_ms = (time.monotonic() - started_monotonic) * 1000
                    if elapsed_ms > spec.timeout_ms:
                        result = self._timeout_result(invocation, spec, attempts, elapsed_ms)
                        span.update(status="error", error=result.error, summary=result.summary)
                        return self._finish_with_result(request, invocation, result)
                    if isinstance(response, Command):
                        self._finish_command(request, invocation, attempts, started_monotonic)
                        span["summary"] = "Tool returned graph command"
                        return response
                    result = self._result_from_message(invocation, response, attempts, started_monotonic)
                    span["summary"] = result.summary
                    self._cache_result(invocation, spec, result)
                    return self._finish_with_result(request, invocation, result)
                except Exception as exc:  # noqa: BLE001
                    result = self._error_result(invocation, exc, spec, started_monotonic)
                    span.update(status="error", error=result.error, summary=result.summary)
                    return self._finish_with_result(request, invocation, result)
        finally:
            if invocation.status == "running":
                self._finish_invocation(invocation, "error", error="Tool middleware ended without result")

    async def awrap_tool_call(self, request: ToolCallRequest, execute: AsyncExecutor) -> ToolResponse:
        spec = self.registry.get(str(request.tool_call.get("name") or ""))
        invocation = self._new_invocation(request, spec)
        invocation.metadata["deadline_mode"] = "hard_async"
        cached = self._cached_result(invocation, spec)
        if cached is not None:
            return self._complete_cached_with_trace(request, invocation, cached, spec)

        started_monotonic = time.monotonic()
        self._start_invocation(request, invocation, spec)
        lock = self._async_lock(spec.concurrency_key)
        lane = self._async_lane(spec.execution_lane)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = (
            asyncio.create_task(self._stream_heartbeats(request, invocation, spec, heartbeat_stop))
            if spec.supports_streaming and spec.progress_interval_ms > 0
            else None
        )
        try:
            with track_tool(
                activity_id=invocation.invocation_id,
                name=spec.tool_id,
                scope=self._scope(request.state),
                step_id=self._step_id(request.state),
                args_preview=self._args_preview(invocation.arguments),
            ) as span:
                try:
                    async with asyncio.timeout(spec.timeout_ms / 1000):
                        queued_at = time.monotonic()
                        async with lane:
                            invocation.metadata["lane_queue_wait_ms"] = round(
                                (time.monotonic() - queued_at) * 1000,
                                3,
                            )
                            if lock is None:
                                response, attempts = await self._run_async_attempts(request, execute, spec, invocation)
                            else:
                                async with lock:
                                    response, attempts = await self._run_async_attempts(request, execute, spec, invocation)
                    if isinstance(response, Command):
                        self._finish_command(request, invocation, attempts, started_monotonic)
                        span["summary"] = "Tool returned graph command"
                        return response
                    result = self._result_from_message(invocation, response, attempts, started_monotonic)
                    span["summary"] = result.summary
                    self._cache_result(invocation, spec, result)
                    return self._finish_with_result(request, invocation, result)
                except TimeoutError:
                    elapsed_ms = (time.monotonic() - started_monotonic) * 1000
                    result = self._timeout_result(invocation, spec, max(invocation.attempt, 1), elapsed_ms)
                    span.update(status="error", error=result.error, summary=result.summary)
                    return self._finish_with_result(request, invocation, result)
                except Exception as exc:  # noqa: BLE001
                    result = self._error_result(invocation, exc, spec, started_monotonic)
                    span.update(status="error", error=result.error, summary=result.summary)
                    return self._finish_with_result(request, invocation, result)
        finally:
            heartbeat_stop.set()
            if heartbeat_task is not None:
                await heartbeat_task
            if invocation.status == "running":
                self._finish_invocation(invocation, "error", error="Tool middleware ended without result")

    def _new_invocation(self, request: ToolCallRequest, spec: ToolSpec) -> ToolInvocation:
        state = self._state_dict(request.state)
        raw_arguments = request.tool_call.get("args") or {}
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {"value": raw_arguments}
        safe_arguments = {
            key: "[REDACTED]" if key in spec.sensitive_arg_names else value
            for key, value in arguments.items()
        }
        invocation = ToolInvocation(
            tool_id=spec.tool_id,
            tool_version=spec.version,
            tool_call_id=str(request.tool_call.get("id") or ""),
            trace_id=current_trace_id(),
            parent_span_id=current_span_id(),
            thread_id=str(state.get("thread_id") or ""),
            session_id=str(state.get("session_id") or ""),
            user_id=str(state["user_id"]) if state.get("user_id") is not None else None,
            arguments=safe_arguments,
            arguments_hash=self._raw_arguments_hash(arguments),
            metadata={
                "execution_kind": spec.execution_kind,
                "latency_class": spec.latency_class,
                "execution_lane": spec.execution_lane,
                "cache_disabled": bool(
                    (state.get("tool_execution_context") or {}).get("disable_cache")
                ),
            },
        )
        cache_scope = str(state.get("session_id") or state.get("thread_id") or "global")
        invocation.cache_key = (
            f"{spec.tool_id}:{spec.version}:{cache_scope}:{self._raw_arguments_hash(arguments)}"
        )
        return invocation

    def _start_invocation(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        spec: ToolSpec,
    ) -> None:
        invocation.status = "running"
        invocation.started_at = time.time()
        invocation.deadline_at = invocation.started_at + spec.timeout_ms / 1000
        first_event_delay_ms = (invocation.started_at - invocation.created_at) * 1000
        invocation.metadata["first_event_delay_ms"] = round(first_event_delay_ms, 3)
        invocation.metadata["first_event_slo_ms"] = spec.first_event_slo_ms
        invocation.metadata["first_event_slo_breached"] = first_event_delay_ms > spec.first_event_slo_ms
        invocation.metadata["terminal_log_started_at"] = invocation.started_at
        agent_log.tool_call(spec.tool_id, self._args_preview(invocation.arguments))
        self._emit_invocation(invocation)
        self._stream_progress(
            request,
            invocation,
            phase="started",
            summary=f"{spec.title} started",
            metadata={"lane": spec.execution_lane},
        )

    def _finish_invocation(
        self,
        invocation: ToolInvocation,
        status: str,
        *,
        result_id: str | None = None,
        error: str | None = None,
    ) -> None:
        invocation.status = status  # type: ignore[assignment]
        invocation.ended_at = time.time()
        if invocation.started_at is not None:
            invocation.duration_ms = (invocation.ended_at - invocation.started_at) * 1000
        invocation.result_id = result_id
        invocation.error = error
        self._emit_invocation(invocation)

    def _finish_with_result(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        result: ToolResult,
    ) -> ToolMessage:
        status = "completed" if result.status in {"success", "warning", "partial"} else result.status
        self._finish_invocation(
            invocation,
            status,
            result_id=result.result_id,
            error=result.error,
        )
        terminal_phase = "timeout" if result.status == "timeout" else (
            "error" if result.status == "error" else "completed"
        )
        self._stream_progress(
            request,
            invocation,
            phase=terminal_phase,
            summary=result.summary,
            partial={
                "result_id": result.result_id,
                "status": result.status,
                "payload_ref": result.payload_ref,
                "artifact_paths": result.artifact_paths,
                "object_version_ids": result.object_version_ids,
            },
        )
        self._emit_event({"type": "tool_result", "result": result.model_dump(mode="json", exclude_none=True)})
        started_at = float(invocation.metadata.get("terminal_log_started_at") or time.time())
        if result.status in {"timeout", "error"}:
            agent_log.tool_error(invocation.tool_id, result.error or result.summary)
        else:
            agent_log.tool_done(invocation.tool_id, result.summary, started_at)
        for artifact_path in result.artifact_paths:
            agent_log.artifact_created(artifact_path, source_tool=invocation.tool_id)
        return ToolMessage(
            content=result.as_tool_message_content(),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=str(request.tool_call.get("name") or "") or None,
            status="error" if result.status in {"timeout", "error"} else "success",
            artifact={"tool_result": result.model_dump(mode="json", exclude_none=True)},
        )

    def _complete_cached(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        cached: ToolResult,
    ) -> ToolMessage:
        started_at = invocation.started_at or time.time()
        cached.result_id = f"result_{invocation.invocation_id.removeprefix('inv_')}"
        cached.invocation_id = invocation.invocation_id
        cached.cached = True
        cached.started_at = started_at
        cached.ended_at = time.time()
        cached.duration_ms = (cached.ended_at - started_at) * 1000
        cached.metadata = {**cached.metadata, "cache": "hit"}
        return self._finish_with_result(request, invocation, cached)

    def _complete_cached_with_trace(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        cached: ToolResult,
        spec: ToolSpec,
    ) -> ToolMessage:
        with track_tool(
            activity_id=invocation.invocation_id,
            name=spec.tool_id,
            scope=self._scope(request.state),
            step_id=self._step_id(request.state),
            args_preview=self._args_preview(invocation.arguments),
        ) as span:
            invocation.cached = True
            self._start_invocation(request, invocation, spec)
            span["summary"] = cached.summary
            span["meta"] = {"cache": "hit"}
            return self._complete_cached(request, invocation, cached)

    def _finish_command(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        attempts: int,
        started_monotonic: float,
    ) -> None:
        invocation.attempt = attempts
        duration_ms = (time.monotonic() - started_monotonic) * 1000
        result = ToolResult(
            invocation_id=invocation.invocation_id,
            tool_id=invocation.tool_id,
            status="success",
            summary="Tool returned graph command",
            output=None,
            attempt_count=attempts,
            started_at=invocation.started_at,
            duration_ms=duration_ms,
            metadata={"response_type": "command"},
        )
        self._finish_invocation(invocation, "completed", result_id=result.result_id)
        self._stream_progress(
            request,
            invocation,
            phase="completed",
            summary=result.summary,
            partial={"result_id": result.result_id, "response_type": "command"},
        )
        self._emit_event({"type": "tool_result", "result": result.model_dump(mode="json", exclude_none=True)})

    def _result_from_message(
        self,
        invocation: ToolInvocation,
        message: ToolMessage,
        attempts: int,
        started_monotonic: float,
    ) -> ToolResult:
        parsed = self._parse_content(message.content)
        summary = self._summary(parsed, message.content)
        reported_status = parsed.get("status") if isinstance(parsed, dict) else None
        if message.status == "error" or (isinstance(parsed, dict) and parsed.get("error")):
            status = "error"
        elif reported_status in {"warning", "partial", "waiting"}:
            status = reported_status
        else:
            status = "success"
        duration_ms = (time.monotonic() - started_monotonic) * 1000
        metadata: dict[str, Any] = {"response_type": "tool_message"}
        payload_ref = None
        artifact_paths: list[str] = []
        object_version_ids: list[str] = []
        citation_refs: list[dict[str, Any]] = []
        next_actions: list[str] = []
        if isinstance(parsed, dict):
            payload_ref = parsed.get("payload_ref") or parsed.get("stored_at")
            artifact_paths = self._string_list(parsed.get("artifact_paths"))
            object_version_ids = self._string_list(parsed.get("object_version_ids"))
            citation_refs = parsed.get("citation_refs") if isinstance(parsed.get("citation_refs"), list) else []
            next_actions = self._string_list(parsed.get("next_actions"))
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_id=invocation.tool_id,
            status=status,
            summary=summary,
            output=parsed,
            payload_ref=str(payload_ref) if payload_ref else None,
            artifact_paths=artifact_paths,
            object_version_ids=object_version_ids,
            citation_refs=citation_refs,
            next_actions=next_actions,
            attempt_count=attempts,
            started_at=invocation.started_at,
            duration_ms=duration_ms,
            error=str(parsed.get("error")) if status == "error" and isinstance(parsed, dict) else None,
            error_type=str(parsed.get("error_type")) if status == "error" and isinstance(parsed, dict) and parsed.get("error_type") else None,
            metadata=metadata,
        )

    def _timeout_result(
        self,
        invocation: ToolInvocation,
        spec: ToolSpec,
        attempts: int,
        elapsed_ms: float,
    ) -> ToolResult:
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_id=invocation.tool_id,
            status="timeout",
            summary=f"{spec.title} timed out after {spec.timeout_ms}ms",
            retryable=spec.idempotent,
            attempt_count=attempts,
            started_at=invocation.started_at,
            duration_ms=elapsed_ms,
            error_type="TimeoutError",
            error=f"Tool exceeded {spec.timeout_ms}ms execution deadline",
            next_actions=[f"Try fallback tool: {tool_id}" for tool_id in spec.fallback_tool_ids],
        )

    def _error_result(
        self,
        invocation: ToolInvocation,
        exc: Exception,
        spec: ToolSpec,
        started_monotonic: float,
    ) -> ToolResult:
        attempts = max(invocation.attempt, 1)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_id=invocation.tool_id,
            status="error",
            summary=f"{spec.title} failed: {type(exc).__name__}",
            retryable=spec.idempotent and self._is_retryable(exc, spec),
            attempt_count=attempts,
            started_at=invocation.started_at,
            duration_ms=(time.monotonic() - started_monotonic) * 1000,
            error_type=type(exc).__name__,
            error=str(exc),
            next_actions=[f"Try fallback tool: {tool_id}" for tool_id in spec.fallback_tool_ids],
        )

    def _cached_result(self, invocation: ToolInvocation, spec: ToolSpec) -> ToolResult | None:
        if invocation.metadata.get("cache_disabled") or not spec.cache_policy.enabled or not invocation.cache_key:
            return None
        return self.cache.get(invocation.cache_key)

    def _cache_result(self, invocation: ToolInvocation, spec: ToolSpec, result: ToolResult) -> None:
        if (
            not invocation.metadata.get("cache_disabled")
            and
            spec.cache_policy.enabled
            and invocation.cache_key
            and result.status in {"success", "warning"}
        ):
            self.cache.put(invocation.cache_key, result, spec.cache_policy.ttl_seconds)

    def _run_sync_attempts(
        self,
        request: ToolCallRequest,
        execute: SyncExecutor,
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> tuple[ToolResponse, int]:
        last_error: Exception | None = None
        token = _tool_stream_context.set(self._stream_context(request, invocation))
        try:
            for attempt in range(1, spec.retry_policy.max_attempts + 1):
                invocation.attempt = attempt
                self._stream_progress(
                    request,
                    invocation,
                    phase="attempt",
                    summary=f"Attempt {attempt} of {spec.retry_policy.max_attempts}",
                    metadata={"attempt": attempt},
                )
                try:
                    return execute(request), attempt
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt >= spec.retry_policy.max_attempts or not self._is_retryable(exc, spec):
                        raise
                    time.sleep(self._backoff_seconds(spec, attempt))
        finally:
            _tool_stream_context.reset(token)
        raise last_error or RuntimeError("tool execution failed without exception")

    async def _run_async_attempts(
        self,
        request: ToolCallRequest,
        execute: AsyncExecutor,
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> tuple[ToolResponse, int]:
        last_error: Exception | None = None
        token = _tool_stream_context.set(self._stream_context(request, invocation))
        try:
            for attempt in range(1, spec.retry_policy.max_attempts + 1):
                invocation.attempt = attempt
                self._stream_progress(
                    request,
                    invocation,
                    phase="attempt",
                    summary=f"Attempt {attempt} of {spec.retry_policy.max_attempts}",
                    metadata={"attempt": attempt},
                )
                try:
                    return await execute(request), attempt
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt >= spec.retry_policy.max_attempts or not self._is_retryable(exc, spec):
                        raise
                    await asyncio.sleep(self._backoff_seconds(spec, attempt))
        finally:
            _tool_stream_context.reset(token)
        raise last_error or RuntimeError("tool execution failed without exception")

    async def _stream_heartbeats(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        spec: ToolSpec,
        stop: asyncio.Event,
    ) -> None:
        interval_seconds = spec.progress_interval_ms / 1000
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                started_at = invocation.started_at or time.time()
                self._stream_progress(
                    request,
                    invocation,
                    phase="heartbeat",
                    summary=f"{spec.title} still running",
                    metadata={
                        "lane": spec.execution_lane,
                        "elapsed_ms": round((time.time() - started_at) * 1000, 3),
                    },
                )

    @staticmethod
    def _is_retryable(exc: Exception, spec: ToolSpec) -> bool:
        if not spec.idempotent or not spec.retry_policy.retryable_exceptions:
            return False
        names = {type(exc).__name__, f"{type(exc).__module__}.{type(exc).__name__}"}
        return bool(names & set(spec.retry_policy.retryable_exceptions))

    @staticmethod
    def _backoff_seconds(spec: ToolSpec, failed_attempt: int) -> float:
        return (
            spec.retry_policy.initial_backoff_ms
            * spec.retry_policy.backoff_multiplier ** max(failed_attempt - 1, 0)
            / 1000
        )

    def _emit_invocation(self, invocation: ToolInvocation) -> None:
        self._emit_event({
            "type": "tool_invocation",
            "invocation": invocation.model_dump(mode="json", exclude_none=True),
        })

    def _emit_event(self, event: dict[str, Any]) -> None:
        try:
            self.event_sink(event)
        except Exception:  # noqa: BLE001
            logger.exception("Tool execution event sink failed")

    def _stream_context(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
    ) -> _ToolStreamContext:
        runtime = getattr(request, "runtime", None)
        writer = getattr(runtime, "stream_writer", None)
        return _ToolStreamContext(
            invocation=invocation,
            writer=writer if callable(writer) else None,
            event_sink=self._emit_event,
        )

    def _stream_progress(
        self,
        request: ToolCallRequest,
        invocation: ToolInvocation,
        *,
        phase: str,
        summary: str,
        partial: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        _write_progress_event(
            self._stream_context(request, invocation),
            phase=phase,
            summary=summary,
            partial=partial,
            metadata=metadata,
        )

    def _sync_lock(self, key: str | None) -> threading.Lock | None:
        if not key:
            return None
        with self._sync_locks_guard:
            return self._sync_locks.setdefault(key, threading.Lock())

    def _async_lock(self, key: str | None) -> asyncio.Lock | None:
        if not key:
            return None
        loop_key = (id(asyncio.get_running_loop()), key)
        with self._async_locks_guard:
            return self._async_locks.setdefault(loop_key, asyncio.Lock())

    def _async_lane(self, lane: str) -> asyncio.Semaphore:
        loop_key = (id(asyncio.get_running_loop()), lane)
        with self._async_lanes_guard:
            return self._async_lanes.setdefault(
                loop_key,
                asyncio.Semaphore(self.lane_limits.capacity(lane)),
            )

    @staticmethod
    def _state_dict(state: Any) -> dict[str, Any]:
        if isinstance(state, dict):
            return state
        if hasattr(state, "model_dump"):
            return state.model_dump()
        return {}

    def _scope(self, state: Any) -> str:
        context = self._state_dict(state).get("tool_execution_context") or {}
        scope = context.get("scope") if isinstance(context, dict) else None
        return str(scope) if scope in {"chat", "research", "workflow"} else "chat"

    def _step_id(self, state: Any) -> str:
        state_dict = self._state_dict(state)
        context = state_dict.get("tool_execution_context") or {}
        if isinstance(context, dict) and context.get("step_id"):
            return str(context["step_id"])
        return "chat"

    @staticmethod
    def _args_preview(arguments: dict[str, Any]) -> str:
        return json.dumps(arguments, ensure_ascii=False, default=str)[:160]

    @staticmethod
    def _raw_arguments_hash(arguments: dict[str, Any]) -> str:
        from domain.tool_execution import canonical_arguments_hash  # noqa: PLC0415

        return canonical_arguments_hash(arguments)

    @staticmethod
    def _parse_content(content: Any) -> Any:
        if not isinstance(content, str):
            return copy.deepcopy(content)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    @staticmethod
    def _summary(parsed: Any, original: Any) -> str:
        if isinstance(parsed, dict):
            for key in ("summary", "message", "error"):
                if parsed.get(key):
                    return str(parsed[key])[:500]
            # Structured output is machine protocol. Never leak its serialized
            # representation into user-facing activity text.
            event_type = str(parsed.get("type") or "").replace("_", " ").strip()
            status = str(parsed.get("status") or "").replace("_", " ").strip()
            if event_type and status:
                return f"{event_type.capitalize()} · {status}"
            if event_type:
                return event_type.capitalize()
            if status:
                return status.capitalize()
            return "Tool completed"
        text = original if isinstance(original, str) else json.dumps(original, default=str)
        return text.strip().replace("\n", " ")[:500] or "Tool completed"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item is not None]


class ToolBatchExecutor:
    """Compiled one-node graph for concurrent, dependency-free tool batches."""

    def __init__(
        self,
        tools: Sequence[BaseTool | Callable[..., Any]],
        registry: ToolRegistry,
        *,
        middleware: ToolExecutionMiddleware | None = None,
    ) -> None:
        self.tools = tuple(tools)
        self.middleware = middleware or ToolExecutionMiddleware(registry)
        builder = StateGraph(_ToolBatchState)
        builder.add_node("tools", self.middleware.create_tool_node(self.tools))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        self.graph = builder.compile()

    @staticmethod
    def _input(
        tool_calls: Sequence[dict[str, Any]],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            **(context or {}),
            "messages": [AIMessage(content="", tool_calls=list(tool_calls))],
        }

    @staticmethod
    def _messages(output: dict[str, Any], call_ids: set[str]) -> list[ToolMessage]:
        return [
            message
            for message in output.get("messages") or []
            if isinstance(message, ToolMessage) and message.tool_call_id in call_ids
        ]

    def invoke(
        self,
        tool_calls: Sequence[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[ToolMessage]:
        if not tool_calls:
            return []
        call_ids = {str(call.get("id") or "") for call in tool_calls}
        output = self.graph.invoke(self._input(tool_calls, context))
        return self._messages(output, call_ids)

    async def ainvoke(
        self,
        tool_calls: Sequence[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[ToolMessage]:
        if not tool_calls:
            return []
        call_ids = {str(call.get("id") or "") for call in tool_calls}
        output = await self.graph.ainvoke(self._input(tool_calls, context))
        return self._messages(output, call_ids)


class CapabilityDispatcher:
    """Only execution entry point for registered capability implementations."""

    def __init__(
        self,
        tools: Sequence[BaseTool | Callable[..., Any]],
        registry: CapabilityRegistry,
        *,
        middleware: ToolExecutionMiddleware | None = None,
    ) -> None:
        self.registry = registry
        self.middleware = middleware or ToolExecutionMiddleware(registry)
        self._tools = {
            str(getattr(tool, "name", getattr(tool, "__name__", ""))): tool
            for tool in tools
        }
        self.registry.validate_tools(tuple(self._tools.values()))
        self._executor = ToolBatchExecutor(
            tuple(self._tools.values()),
            registry,
            middleware=self.middleware,
        )

    def invoke(
        self,
        tool_calls: Sequence[dict[str, Any]],
        *,
        surface: str,
        context: dict[str, Any] | None = None,
        allowed_capability_ids: set[str] | None = None,
        max_calls: int | None = None,
        tool_overrides: dict[str, BaseTool | Callable[..., Any]] | None = None,
    ) -> list[ToolMessage]:
        executable, rejected = self._prepare(
            tool_calls,
            surface=surface,
            allowed_capability_ids=allowed_capability_ids,
            max_calls=max_calls,
        )
        executor = self._executor_for(executable, tool_overrides)
        completed = executor.invoke(
            executable,
            context=self._context(surface, context, disable_cache=bool(tool_overrides)),
        ) if executable else []
        return self._ordered(tool_calls, [*rejected, *completed])

    async def ainvoke(
        self,
        tool_calls: Sequence[dict[str, Any]],
        *,
        surface: str,
        context: dict[str, Any] | None = None,
        allowed_capability_ids: set[str] | None = None,
        max_calls: int | None = None,
        tool_overrides: dict[str, BaseTool | Callable[..., Any]] | None = None,
    ) -> list[ToolMessage]:
        executable, rejected = self._prepare(
            tool_calls,
            surface=surface,
            allowed_capability_ids=allowed_capability_ids,
            max_calls=max_calls,
        )
        executor = self._executor_for(executable, tool_overrides)
        completed = await executor.ainvoke(
            executable,
            context=self._context(surface, context, disable_cache=bool(tool_overrides)),
        ) if executable else []
        return self._ordered(tool_calls, [*rejected, *completed])

    def _prepare(
        self,
        tool_calls: Sequence[dict[str, Any]],
        *,
        surface: str,
        allowed_capability_ids: set[str] | None,
        max_calls: int | None,
    ) -> tuple[list[dict[str, Any]], list[ToolMessage]]:
        exposed = {spec.capability_id for spec in self.registry.specs_for_surface(surface)}
        executable: list[dict[str, Any]] = []
        rejected: list[ToolMessage] = []
        for call in tool_calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("id") or uuid4().hex)
            reason = None
            if name not in self._tools:
                reason = f"Capability '{name}' has no registered implementation."
            elif name not in exposed:
                reason = f"Capability '{name}' is not exposed on {surface} surface."
            elif allowed_capability_ids is not None and name not in allowed_capability_ids:
                reason = f"Capability '{name}' blocked by execution policy."
            elif max_calls is not None and len(executable) >= max_calls:
                reason = "Capability call budget exhausted."
            if reason:
                rejected.append(ToolMessage(
                    content=json.dumps({"status": "error", "summary": reason, "capability_id": name}),
                    tool_call_id=call_id,
                    name=name or None,
                    status="error",
                ))
                continue
            executable.append({
                **call,
                "id": call_id,
                "name": name,
                "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                "type": "tool_call",
            })
        return executable, rejected

    def _executor_for(
        self,
        calls: Sequence[dict[str, Any]],
        overrides: dict[str, BaseTool | Callable[..., Any]] | None,
    ) -> ToolBatchExecutor:
        if not overrides:
            return self._executor
        selected = dict(self._tools)
        selected.update({
            name: self._adapt_override(name, implementation)
            for name, implementation in overrides.items()
        })
        implementations = list({
            str(call["name"]): selected[str(call["name"])]
            for call in calls
        }.values())
        return ToolBatchExecutor(implementations, self.registry, middleware=self.middleware)

    def _adapt_override(
        self,
        name: str,
        implementation: BaseTool | Callable[..., Any],
    ) -> BaseTool | Callable[..., Any]:
        if isinstance(implementation, BaseTool) or callable(implementation):
            return implementation
        invoke = getattr(implementation, "invoke", None)
        if not callable(invoke):
            raise TypeError(f"Capability override '{name}' must be callable or expose invoke()")
        canonical = self._tools[name]
        args_schema = getattr(canonical, "args_schema", None)

        def invoke_override(**kwargs: Any) -> Any:
            return invoke(kwargs)

        return StructuredTool.from_function(
            func=invoke_override,
            name=name,
            description=str(getattr(canonical, "description", name)),
            args_schema=args_schema,
        )

    @staticmethod
    def _context(
        surface: str,
        context: dict[str, Any] | None,
        *,
        disable_cache: bool = False,
    ) -> dict[str, Any]:
        payload = dict(context or {})
        execution_context = dict(payload.get("tool_execution_context") or {})
        execution_context.setdefault("scope", "workflow" if surface == "case" else surface)
        if disable_cache:
            execution_context["disable_cache"] = True
        payload["tool_execution_context"] = execution_context
        return payload

    @staticmethod
    def _ordered(
        calls: Sequence[dict[str, Any]],
        messages: Sequence[ToolMessage],
    ) -> list[ToolMessage]:
        by_id = {message.tool_call_id: message for message in messages}
        return [
            by_id[call_id]
            for call in calls
            if (call_id := str(call.get("id") or "")) in by_id
        ]


def unwrap_tool_result_message(message: ToolMessage) -> ToolMessage:
    """Convert normalized middleware envelope into tool's legacy observation."""
    try:
        result = ToolResult.model_validate_json(str(message.content))
    except (ValueError, TypeError):
        return message
    if result.status in {"error", "timeout"} and result.output is None:
        output: object = {
            "error": result.error or result.summary,
            "error_type": result.error_type,
            "tool_name": result.tool_id,
        }
    else:
        output = result.output
    content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return ToolMessage(
        content=content,
        tool_call_id=message.tool_call_id,
        name=message.name,
        status=message.status,
        artifact=message.artifact,
    )


def create_universal_tool_node(
    tools: Sequence[BaseTool | Callable[..., Any]],
    registry: ToolRegistry,
    *,
    cache: InMemoryToolCache | None = None,
    event_sink: EventSink | None = None,
    lane_limits: ToolLaneLimits | None = None,
    name: str = "tools",
    messages_key: str = "messages",
) -> ToolNode:
    middleware = ToolExecutionMiddleware(
        registry,
        cache=cache,
        event_sink=event_sink,
        lane_limits=lane_limits,
    )
    return middleware.create_tool_node(tools, name=name, messages_key=messages_key)

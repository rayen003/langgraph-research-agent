"""Typed contracts for tool registration, invocation, and results."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


ToolExecutionKind = Literal["read", "compute", "write", "workflow", "agent"]
ToolLatencyClass = Literal["instant", "interactive", "extended", "background"]
ToolExecutionLane = Literal["interactive", "background"]
CapabilitySurface = Literal["chat", "research", "case"]
ToolInvocationStatus = Literal[
    "queued",
    "running",
    "waiting",
    "completed",
    "partial",
    "timeout",
    "error",
    "cancelled",
]
ToolResultStatus = Literal["success", "warning", "partial", "timeout", "error", "waiting"]
ToolProgressPhase = Literal[
    "started",
    "attempt",
    "heartbeat",
    "partial",
    "completed",
    "timeout",
    "error",
]


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ToolCachePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ttl_seconds: int = Field(default=0, ge=0)
    stale_while_revalidate_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> "ToolCachePolicy":
        if self.enabled and self.ttl_seconds <= 0:
            raise ValueError("enabled cache policy requires ttl_seconds > 0")
        return self


class ToolRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=5)
    initial_backoff_ms: int = Field(default=0, ge=0, le=30_000)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    retryable_exceptions: list[str] = Field(default_factory=list)


class CapabilitySpec(BaseModel):
    """Canonical semantic and execution policy for one callable capability.

    ``tool_id`` remains accepted and readable during migration. Serialized
    catalog cards expose only canonical ``capability_id``.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("capability_id", "tool_id"),
    )
    version: str = Field(default="1", min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    execution_kind: ToolExecutionKind = "read"
    latency_class: ToolLatencyClass = "interactive"
    execution_lane: ToolExecutionLane = "interactive"
    first_event_slo_ms: int = Field(default=250, ge=0)
    timeout_ms: int = Field(default=10_000, gt=0)
    idempotent: bool = True
    supports_streaming: bool = False
    progress_interval_ms: int = Field(default=0, ge=0)
    requires_confirmation: bool = False
    cache_policy: ToolCachePolicy = Field(default_factory=ToolCachePolicy)
    retry_policy: ToolRetryPolicy = Field(default_factory=ToolRetryPolicy)
    fallback_tool_ids: list[str] = Field(default_factory=list)
    concurrency_key: str | None = None
    sensitive_arg_names: list[str] = Field(default_factory=list)
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    exposed_in: list[CapabilitySurface] = Field(default_factory=list)
    plannable: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    accepts: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    requires_all: list[str] = Field(default_factory=list)
    requires_any: list[str] = Field(default_factory=list)
    max_tool_calls: int = Field(default=0, ge=0)
    model_tier: str = "worker"
    supports_parallel: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def tool_id(self) -> str:
        return self.capability_id

    @model_validator(mode="after")
    def validate_execution_policy(self) -> "ToolSpec":
        if self.first_event_slo_ms > self.timeout_ms:
            raise ValueError("first_event_slo_ms cannot exceed timeout_ms")
        if self.progress_interval_ms and not self.supports_streaming:
            raise ValueError("progress_interval_ms requires supports_streaming=True")
        if self.progress_interval_ms > self.timeout_ms:
            raise ValueError("progress_interval_ms cannot exceed timeout_ms")
        if not self.idempotent and self.retry_policy.max_attempts > 1:
            raise ValueError("non-idempotent tools cannot retry automatically")
        if not self.idempotent and self.cache_policy.enabled:
            raise ValueError("non-idempotent tools cannot use execution cache")
        if self.tool_id in self.fallback_tool_ids:
            raise ValueError("tool cannot list itself as fallback")
        if self.execution_kind == "agent" and not self.allowed_tools:
            raise ValueError("agent capability requires allowed_tools")
        if self.execution_kind == "workflow" and self.supports_parallel and self.requires_confirmation:
            raise ValueError("HITL workflow cannot declare unrestricted parallel execution")
        return self


# Transitional import alias. One schema exists; callers migrate incrementally.
ToolSpec = CapabilitySpec


class ToolInvocation(BaseModel):
    """One immutable-identity execution attempt tracked across lifecycle events."""

    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(default_factory=lambda: f"inv_{uuid4().hex[:16]}")
    tool_id: str = Field(min_length=1)
    tool_version: str = "1"
    tool_call_id: str = ""
    trace_id: str = ""
    parent_span_id: str | None = None
    thread_id: str = ""
    session_id: str = ""
    user_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_hash: str = ""
    status: ToolInvocationStatus = "queued"
    attempt: int = Field(default=0, ge=0)
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    deadline_at: float | None = None
    ended_at: float | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    cache_key: str | None = None
    cached: bool = False
    result_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def populate_arguments_hash(self) -> "ToolInvocation":
        if not self.arguments_hash:
            self.arguments_hash = canonical_arguments_hash(self.arguments)
        return self


class ToolResult(BaseModel):
    """Normalized observation returned by every universally wrapped tool."""

    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(default_factory=lambda: f"result_{uuid4().hex[:16]}")
    invocation_id: str
    tool_id: str
    status: ToolResultStatus
    summary: str
    output: Any = None
    payload_ref: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    object_version_ids: list[str] = Field(default_factory=list)
    citation_refs: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    attempt_count: int = Field(default=1, ge=0)
    cached: bool = False
    started_at: float | None = None
    ended_at: float = Field(default_factory=time.time)
    duration_ms: float | None = Field(default=None, ge=0)
    error_type: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_tool_message_content(self) -> str:
        return self.model_dump_json(exclude_none=True)


class ToolProgress(BaseModel):
    """Custom-stream event emitted while one tool invocation is running."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: f"progress_{uuid4().hex[:16]}")
    invocation_id: str
    tool_id: str
    phase: ToolProgressPhase
    sequence: int = Field(ge=1)
    summary: str = ""
    partial: Any = None
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)

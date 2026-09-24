"""Typed collaboration contracts shared by API, storage, humans, and agents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ActorKind = Literal["human", "agent", "system"]
ChannelKind = Literal["channel", "direct", "case", "object"]
MentionKind = Literal["human", "agent", "channel", "object", "case", "citation"]
PermissionRole = Literal["owner", "admin", "editor", "reviewer", "commenter", "viewer"]


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str
    kind: ActorKind
    display_name: str
    handle: str
    avatar_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    status: Literal["available", "working", "waiting", "blocked", "offline"] = "available"


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str
    name: str
    created_by: str
    created_at: str


class Membership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str
    actor_id: str
    role: PermissionRole
    joined_at: str


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str
    workspace_id: str
    kind: ChannelKind = "channel"
    name: str
    topic: str = ""
    created_by: str
    object_id: str | None = None
    case_id: str | None = None
    created_at: str
    updated_at: str


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mention_id: str
    kind: MentionKind
    target_id: str
    label: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    requested_action: str | None = None
    context_refs: list[str] = Field(default_factory=list)


class CollaborationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    channel_id: str
    workspace_id: str
    actor_id: str
    body: str
    mentions: list[Mention] = Field(default_factory=list)
    object_version_ids: list[str] = Field(default_factory=list)
    parent_message_id: str | None = None
    created_at: str
    edited_at: str | None = None


class CommentThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment_id: str
    workspace_id: str
    object_id: str
    object_version_id: str
    block_id: str | None = None
    actor_id: str
    body: str
    status: Literal["open", "resolved"] = "open"
    created_at: str
    resolved_at: str | None = None


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestion_id: str
    workspace_id: str
    object_id: str
    base_version_id: str
    block_id: str | None = None
    actor_id: str
    patch: dict[str, Any]
    rationale: str = ""
    status: Literal["pending", "accepted", "rejected"] = "pending"
    created_at: str
    decided_by: str | None = None
    decided_at: str | None = None


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    workspace_id: str
    object_id: str
    object_version_id: str
    requested_by: str
    assigned_to: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    note: str = ""
    created_at: str
    decided_at: str | None = None


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notification_id: str
    workspace_id: str
    actor_id: str
    event_type: str
    title: str
    body: str = ""
    target_type: str
    target_id: str
    read: bool = False
    created_at: str


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: str
    workspace_id: str
    title: str
    description: str = ""
    assigned_by: str
    assigned_to: str
    case_id: str | None = None
    object_version_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "working", "blocked", "completed", "cancelled"] = "open"
    due_at: str | None = None
    created_at: str
    updated_at: str

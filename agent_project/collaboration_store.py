"""SQLite operations for durable human-human and human-agent collaboration."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from storage import _connect, _now

BUILTIN_AGENTS = (
    ("agent:research", "Research", "research", ["web_search", "documents", "company_research"]),
    ("agent:valuation", "Valuation", "valuation", ["dcf", "financial_modeling", "comparables"]),
    ("agent:writer", "Writer", "writer", ["memo", "deck", "synthesis"]),
)
ROLE_RANK = {"viewer": 0, "commenter": 1, "reviewer": 2, "editor": 3, "admin": 4, "owner": 5}


def ensure_workspace(workspace_id: str, *, name: str | None = None, actor_id: str = "human:local") -> dict[str, Any]:
    now = _now()
    with _connect() as conn:
        existing = conn.execute("SELECT * FROM collaboration_workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT OR IGNORE INTO collaboration_actors VALUES (?, 'human', ?, ?, NULL, '[]', 'available', ?, ?)",
                (actor_id, "Local analyst", actor_id.split(":", 1)[-1], now, now),
            )
            conn.execute(
                "INSERT INTO collaboration_workspaces VALUES (?, ?, ?, ?, ?)",
                (workspace_id, name or "Finance workspace", actor_id, now, now),
            )
            conn.execute(
                "INSERT INTO collaboration_memberships VALUES (?, ?, 'owner', ?)",
                (workspace_id, actor_id, now),
            )
        for agent_id, display_name, handle, capabilities in BUILTIN_AGENTS:
            conn.execute(
                "INSERT OR IGNORE INTO collaboration_actors VALUES (?, 'agent', ?, ?, NULL, ?, 'available', ?, ?)",
                (agent_id, display_name, handle, json.dumps(capabilities), now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO collaboration_memberships VALUES (?, ?, 'editor', ?)",
                (workspace_id, agent_id, now),
            )
        row = conn.execute("SELECT * FROM collaboration_workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
    return dict(row)


def upsert_actor(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    actor_id = str(payload.get("actor_id") or f"{payload.get('kind', 'human')}:{uuid4().hex[:12]}")
    handle = str(payload.get("handle") or actor_id.split(":", 1)[-1])
    with _connect() as conn:
        conn.execute("""
            INSERT INTO collaboration_actors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET display_name=excluded.display_name, handle=excluded.handle,
              avatar_url=excluded.avatar_url, capabilities=excluded.capabilities, status=excluded.status, updated_at=excluded.updated_at
        """, (actor_id, payload.get("kind", "human"), payload.get("display_name") or handle, handle,
              payload.get("avatar_url"), json.dumps(payload.get("capabilities") or []), payload.get("status", "available"), now, now))
    return get_actor(actor_id) or {}


def get_actor(actor_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM collaboration_actors WHERE actor_id=?", (actor_id,)).fetchone()
    if not row: return None
    result = dict(row); result["capabilities"] = json.loads(result["capabilities"]); return result


def list_actors(workspace_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("""SELECT a.*, m.role FROM collaboration_actors a
            JOIN collaboration_memberships m ON m.actor_id=a.actor_id WHERE m.workspace_id=? ORDER BY a.kind, a.display_name""", (workspace_id,)).fetchall()
    result=[]
    for row in rows:
        item=dict(row); item["capabilities"]=json.loads(item["capabilities"]); result.append(item)
    return result


def add_membership(workspace_id: str, actor_id: str, role: str = "viewer") -> dict[str, Any]:
    now = _now()
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM collaboration_actors WHERE actor_id=?", (actor_id,)).fetchone():
            raise ValueError(f"Actor '{actor_id}' not found")
        conn.execute(
            "INSERT INTO collaboration_memberships VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id, actor_id) DO UPDATE SET role=excluded.role",
            (workspace_id, actor_id, role, now),
        )
    return {"workspace_id": workspace_id, "actor_id": actor_id, "role": role, "joined_at": now}


def require_workspace_role(workspace_id: str, actor_id: str, minimum: str = "viewer") -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM collaboration_memberships WHERE workspace_id=? AND actor_id=?", (workspace_id, actor_id)).fetchone()
    if not row or ROLE_RANK.get(str(row["role"]), -1) < ROLE_RANK[minimum]:
        raise PermissionError(f"Actor '{actor_id}' requires {minimum} access to '{workspace_id}'")
    return dict(row)


def update_actor_status(actor_id: str, status: str) -> dict[str, Any] | None:
    now = _now()
    with _connect() as conn:
        conn.execute("UPDATE collaboration_actors SET status=?, updated_at=? WHERE actor_id=?", (status, now, actor_id))
    return get_actor(actor_id)


def create_channel(workspace_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    ensure_workspace(workspace_id, actor_id=actor_id)
    require_workspace_role(workspace_id, actor_id, "editor")
    now = _now(); channel_id = str(payload.get("channel_id") or f"channel:{uuid4().hex[:12]}")
    with _connect() as conn:
        conn.execute("INSERT INTO collaboration_channels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, workspace_id, payload.get("kind", "channel"), payload["name"], payload.get("topic", ""), actor_id,
             payload.get("object_id"), payload.get("case_id"), now, now))
    return {"channel_id": channel_id, "workspace_id": workspace_id, "created_by": actor_id, "created_at": now, "updated_at": now, **payload}


def list_channels(workspace_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM collaboration_channels WHERE workspace_id=? ORDER BY updated_at DESC", (workspace_id,)).fetchall()]


def create_message(channel_id: str, workspace_id: str, actor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    now = _now(); message_id = str(payload.get("message_id") or f"message:{uuid4().hex[:16]}")
    mentions = payload.get("mentions") or []; versions = payload.get("object_version_ids") or []
    with _connect() as conn:
        channel = conn.execute("SELECT workspace_id FROM collaboration_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if not channel or channel["workspace_id"] != workspace_id:
            raise ValueError(f"Channel '{channel_id}' does not belong to '{workspace_id}'")
        conn.execute("INSERT INTO collaboration_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (message_id, channel_id, workspace_id, actor_id, payload["body"], json.dumps(mentions), json.dumps(versions), payload.get("parent_message_id"), now))
        conn.execute("UPDATE collaboration_channels SET updated_at=? WHERE channel_id=?", (now, channel_id))
        for mention in mentions:
            target = str(mention.get("target_id") or "")
            if mention.get("kind") not in {"human", "agent"} or not target: continue
            conn.execute("INSERT INTO collaboration_notifications VALUES (?, ?, ?, 'mention', ?, ?, 'message', ?, 0, ?)",
                (f"notification:{uuid4().hex[:16]}", workspace_id, target, f"Mention from {actor_id}", payload["body"][:240], message_id, now))
    return {"message_id": message_id, "channel_id": channel_id, "workspace_id": workspace_id, "actor_id": actor_id,
            "body": payload["body"], "mentions": mentions, "object_version_ids": versions,
            "parent_message_id": payload.get("parent_message_id"), "created_at": now, "edited_at": None}


def list_messages(channel_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM collaboration_messages WHERE channel_id=? ORDER BY created_at ASC LIMIT ?", (channel_id, limit)).fetchall()
    result=[]
    for row in rows:
        item=dict(row); item["mentions"]=json.loads(item["mentions"]); item["object_version_ids"]=json.loads(item["object_version_ids"]); result.append(item)
    return result


def create_object_comment(workspace_id: str, object_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    now=_now(); comment_id=f"comment:{uuid4().hex[:16]}"
    with _connect() as conn:
        conn.execute("INSERT INTO collaboration_comments VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)",
            (comment_id, workspace_id, object_id, payload["object_version_id"], payload.get("block_id"), actor_id, payload["body"], now))
    return {"comment_id":comment_id,"workspace_id":workspace_id,"object_id":object_id,"actor_id":actor_id,"status":"open","created_at":now,**payload}


def list_object_comments(object_id: str) -> list[dict[str, Any]]:
    with _connect() as conn: return [dict(r) for r in conn.execute("SELECT * FROM collaboration_comments WHERE object_id=? ORDER BY created_at",(object_id,)).fetchall()]


def resolve_comment(comment_id: str, resolved: bool = True, actor_id: str = "human:local") -> dict[str, Any] | None:
    now = _now()
    with _connect() as conn:
        current = conn.execute("SELECT * FROM collaboration_comments WHERE comment_id=?", (comment_id,)).fetchone()
    if not current: return None
    require_workspace_role(current["workspace_id"], actor_id, "commenter")
    with _connect() as conn:
        conn.execute(
            "UPDATE collaboration_comments SET status=?, resolved_at=? WHERE comment_id=?",
            ("resolved" if resolved else "open", now if resolved else None, comment_id),
        )
        row = conn.execute("SELECT * FROM collaboration_comments WHERE comment_id=?", (comment_id,)).fetchone()
    return dict(row) if row else None


def create_suggestion(workspace_id: str, object_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    now = _now(); suggestion_id = f"suggestion:{uuid4().hex[:16]}"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO collaboration_suggestions VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (suggestion_id, workspace_id, object_id, payload["base_version_id"], payload.get("block_id"), actor_id,
             json.dumps(payload["patch"]), payload.get("rationale", ""), now),
        )
    return {"suggestion_id": suggestion_id, "workspace_id": workspace_id, "object_id": object_id,
            "actor_id": actor_id, "status": "pending", "created_at": now, **payload}


def list_object_suggestions(object_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM collaboration_suggestions WHERE object_id=? ORDER BY created_at", (object_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row); item["patch"] = json.loads(item["patch"]); result.append(item)
    return result


def decide_suggestion(suggestion_id: str, decision: str, actor_id: str) -> dict[str, Any] | None:
    if decision not in {"accepted", "rejected"}: raise ValueError("decision must be accepted or rejected")
    now = _now()
    with _connect() as conn:
        current = conn.execute("SELECT workspace_id FROM collaboration_suggestions WHERE suggestion_id=?", (suggestion_id,)).fetchone()
    if not current: return None
    require_workspace_role(current["workspace_id"], actor_id, "reviewer")
    with _connect() as conn:
        conn.execute("UPDATE collaboration_suggestions SET status=?, decided_by=?, decided_at=? WHERE suggestion_id=? AND status='pending'",
                     (decision, actor_id, now, suggestion_id))
        row = conn.execute("SELECT * FROM collaboration_suggestions WHERE suggestion_id=?", (suggestion_id,)).fetchone()
    if not row: return None
    result = dict(row); result["patch"] = json.loads(result["patch"]); return result


def create_approval(workspace_id: str, object_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    now=_now(); approval_id=f"approval:{uuid4().hex[:16]}"
    with _connect() as conn:
        conn.execute("INSERT INTO collaboration_approvals VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)",
            (approval_id,workspace_id,object_id,payload["object_version_id"],actor_id,payload["assigned_to"],payload.get("note", ""),now))
        conn.execute("INSERT INTO collaboration_notifications VALUES (?, ?, ?, 'approval_requested', ?, ?, 'approval', ?, 0, ?)",
            (f"notification:{uuid4().hex[:16]}", workspace_id, payload["assigned_to"], "Review requested", payload.get("note", ""), approval_id, now))
    return {"approval_id":approval_id,"workspace_id":workspace_id,"object_id":object_id,"requested_by":actor_id,"status":"pending","created_at":now,**payload}


def list_object_approvals(object_id: str) -> list[dict[str, Any]]:
    with _connect() as conn: return [dict(r) for r in conn.execute("SELECT * FROM collaboration_approvals WHERE object_id=? ORDER BY created_at", (object_id,)).fetchall()]


def decide_approval(approval_id: str, decision: str, actor_id: str, note: str = "") -> dict[str, Any] | None:
    if decision not in {"approved", "rejected"}: raise ValueError("decision must be approved or rejected")
    now = _now()
    with _connect() as conn:
        current = conn.execute("SELECT * FROM collaboration_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not current: return None
    require_workspace_role(current["workspace_id"], actor_id, "reviewer")
    if actor_id != current["assigned_to"]:
        require_workspace_role(current["workspace_id"], actor_id, "admin")
    with _connect() as conn:
        conn.execute("UPDATE collaboration_approvals SET status=?, note=?, decided_at=? WHERE approval_id=?",
                     (decision, note or current["note"], now, approval_id))
        conn.execute("INSERT INTO collaboration_notifications VALUES (?, ?, ?, 'approval_decided', ?, ?, 'approval', ?, 0, ?)",
            (f"notification:{uuid4().hex[:16]}", current["workspace_id"], current["requested_by"], f"Review {decision}", note, approval_id, now))
        row = conn.execute("SELECT * FROM collaboration_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    return dict(row) if row else None


def list_notifications(actor_id: str) -> list[dict[str, Any]]:
    with _connect() as conn: rows=conn.execute("SELECT * FROM collaboration_notifications WHERE actor_id=? ORDER BY created_at DESC LIMIT 100",(actor_id,)).fetchall()
    return [{**dict(r),"read":bool(r["read"])} for r in rows]


def mark_notification_read(notification_id: str, read: bool = True, actor_id: str = "human:local") -> dict[str, Any] | None:
    with _connect() as conn:
        conn.execute("UPDATE collaboration_notifications SET read=? WHERE notification_id=? AND actor_id=?", (int(read), notification_id, actor_id))
        row = conn.execute("SELECT * FROM collaboration_notifications WHERE notification_id=?", (notification_id,)).fetchone()
    if not row or row["actor_id"] != actor_id: return None
    return {**dict(row), "read": bool(row["read"])}


def create_assignment(workspace_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
    require_workspace_role(workspace_id, actor_id, "commenter")
    now = _now(); assignment_id = f"assignment:{uuid4().hex[:16]}"
    versions = payload.get("object_version_ids") or []
    with _connect() as conn:
        conn.execute("""
            INSERT INTO collaboration_assignments (
                assignment_id, workspace_id, title, description, assigned_by, assigned_to,
                case_id, object_version_ids, status, due_at, channel_id, source_message_id,
                thread_id, output_object_version_ids, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, '[]', NULL, ?, ?)
        """, (
            assignment_id, workspace_id, payload["title"], payload.get("description", ""), actor_id,
            payload["assigned_to"], payload.get("case_id"), json.dumps(versions), payload.get("due_at"),
            payload.get("channel_id"), payload.get("source_message_id"), payload.get("thread_id"), now, now,
        ))
        conn.execute("INSERT INTO collaboration_notifications VALUES (?, ?, ?, 'assignment', ?, ?, 'assignment', ?, 0, ?)",
            (f"notification:{uuid4().hex[:16]}", workspace_id, payload["assigned_to"], "New assignment", payload["title"], assignment_id, now))
    return {"assignment_id": assignment_id, "workspace_id": workspace_id, "assigned_by": actor_id,
            "status": "open", "created_at": now, "updated_at": now, **payload,
            "object_version_ids": versions, "output_object_version_ids": [], "error": None}


def list_assignments(workspace_id: str, actor_id: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM collaboration_assignments WHERE workspace_id=?"
    params: tuple[Any, ...] = (workspace_id,)
    if actor_id:
        query += " AND assigned_to=?"; params = (workspace_id, actor_id)
    query += " ORDER BY updated_at DESC"
    with _connect() as conn: rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["object_version_ids"] = json.loads(item["object_version_ids"])
        item["output_object_version_ids"] = json.loads(item.get("output_object_version_ids") or "[]")
        result.append(item)
    return result


def update_assignment(
    assignment_id: str,
    status: str,
    actor_id: str,
    *,
    thread_id: str | None = None,
    output_object_version_ids: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    with _connect() as conn:
        current = conn.execute("SELECT * FROM collaboration_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
    if not current: return None
    if actor_id != current["assigned_to"]:
        require_workspace_role(current["workspace_id"], actor_id, "editor")
    now = _now()
    with _connect() as conn:
        conn.execute("""
            UPDATE collaboration_assignments
            SET status=?, thread_id=COALESCE(?, thread_id),
                output_object_version_ids=COALESCE(?, output_object_version_ids),
                error=?, updated_at=?
            WHERE assignment_id=?
        """, (
            status,
            thread_id,
            json.dumps(output_object_version_ids) if output_object_version_ids is not None else None,
            error,
            now,
            assignment_id,
        ))
        row = conn.execute("SELECT * FROM collaboration_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
    result = dict(row)
    result["object_version_ids"] = json.loads(result["object_version_ids"])
    result["output_object_version_ids"] = json.loads(result.get("output_object_version_ids") or "[]")
    return result

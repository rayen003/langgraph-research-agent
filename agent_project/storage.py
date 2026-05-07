"""SQLite persistence for jobs, reports, and session memory."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = RUNS_DIR / "agent.db"
logger = logging.getLogger(__name__)


RUNNING_STATUSES = {
    "classifying",
    "planning",
    "workflow_running",
    "awaiting_assumptions",
    "executing",
    "synthesizing",
    "chat_responding",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                thread_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                mode TEXT NOT NULL,
                intent TEXT,
                status TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                report_path TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                thread_id TEXT PRIMARY KEY,
                session_id TEXT,
                objective TEXT NOT NULL,
                content TEXT NOT NULL,
                report_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES jobs(thread_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS session_memory (
                session_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                upload_path TEXT,
                created_at REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES jobs(thread_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_job_events_thread_id_event_id
            ON job_events(thread_id, event_id);

            CREATE TABLE IF NOT EXISTS job_steps (
                thread_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                tool_result_ids TEXT NOT NULL DEFAULT '[]',
                started_at TEXT,
                completed_at TEXT,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(thread_id, step_id),
                FOREIGN KEY(thread_id) REFERENCES jobs(thread_id) ON DELETE CASCADE
            );
            """
        )


def mark_stale_running_jobs() -> None:
    stale = ",".join("?" for _ in RUNNING_STATUSES)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE jobs
            SET status = 'interrupted',
                error = COALESCE(error, 'Server restarted before this job completed.'),
                updated_at = ?
            WHERE status IN ({stale})
            """,
            [_now(), *RUNNING_STATUSES],
        )


def upsert_job(
    *,
    thread_id: str,
    query: str,
    mode: str,
    status: str,
    session_id: str = "",
    intent: str | None = None,
) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (thread_id, query, mode, intent, status, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                query = excluded.query,
                mode = excluded.mode,
                status = excluded.status,
                session_id = excluded.session_id,
                intent = COALESCE(excluded.intent, jobs.intent),
                updated_at = excluded.updated_at
            """,
            (thread_id, query, mode, intent, status, session_id, now, now),
        )


def update_job(
    thread_id: str,
    *,
    status: str | None = None,
    intent: str | None = None,
    report_path: str | None = None,
    error: str | None = None,
) -> None:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if intent is not None:
        fields.append("intent = ?")
        values.append(intent)
    if report_path is not None:
        fields.append("report_path = ?")
        values.append(report_path)
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(thread_id)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE thread_id = ?", values)


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT thread_id, query, mode, intent, status, session_id, created_at, updated_at, report_path, error
            FROM jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_job(thread_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT thread_id, query, mode, intent, status, session_id, created_at, updated_at, report_path, error
            FROM jobs
            WHERE thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
    return dict(row) if row else None


def append_job_event(thread_id: str, event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "unknown")
    created_at = _now()
    payload = dict(event)
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_events (thread_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
        )
        event_id = int(cursor.lastrowid)
    payload["event_id"] = event_id
    payload["created_at"] = created_at
    return payload


def list_job_events(thread_id: str, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, payload, created_at
            FROM job_events
            WHERE thread_id = ? AND event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (thread_id, after_id, limit),
        ).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {"type": "error", "message": "Corrupt persisted event payload."}
        payload["event_id"] = row["event_id"]
        payload["created_at"] = row["created_at"]
        events.append(payload)
    return events


def sync_job_steps(thread_id: str, plan: dict[str, Any]) -> None:
    now = _now()
    try:
        with _connect() as conn:
            for step in plan.get("steps", []):
                conn.execute(
                    """
                    INSERT INTO job_steps (
                        thread_id, step_id, description, status, result,
                        tool_result_ids, started_at, completed_at, error, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    ON CONFLICT(thread_id, step_id) DO UPDATE SET
                        description = excluded.description,
                        status = excluded.status,
                        result = excluded.result,
                        tool_result_ids = excluded.tool_result_ids,
                        updated_at = excluded.updated_at
                    """,
                    (
                        thread_id,
                        step.get("id", ""),
                        step.get("description", ""),
                        step.get("status", "pending"),
                        step.get("result"),
                        json.dumps(step.get("tool_result_ids") or [], ensure_ascii=False),
                        now,
                    ),
                )
    except sqlite3.IntegrityError:
        # Studio/local graph runs can execute without a matching `jobs` row.
        logger.warning("Skipping sync_job_steps: missing jobs row for thread_id=%s", thread_id)


def update_job_step(
    thread_id: str,
    step_id: str,
    *,
    status: str | None = None,
    result: str | None = None,
    tool_result_ids: list[str] | None = None,
    error: str | None = None,
) -> None:
    fields: list[str] = []
    values: list[Any] = []
    now = _now()
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status == "running":
            fields.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in {"completed", "failed"}:
            fields.append("completed_at = ?")
            values.append(now)
    if result is not None:
        fields.append("result = ?")
        values.append(result)
    if tool_result_ids is not None:
        fields.append("tool_result_ids = ?")
        values.append(json.dumps(tool_result_ids, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(now)
    values.extend([thread_id, step_id])
    try:
        with _connect() as conn:
            conn.execute(
                f"UPDATE job_steps SET {', '.join(fields)} WHERE thread_id = ? AND step_id = ?",
                values,
            )
    except sqlite3.IntegrityError:
        logger.warning(
            "Skipping update_job_step due to FK constraints (thread_id=%s step_id=%s)",
            thread_id,
            step_id,
        )


def list_job_steps(thread_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT thread_id, step_id, description, status, result, tool_result_ids,
                   started_at, completed_at, error, updated_at
            FROM job_steps
            WHERE thread_id = ?
            ORDER BY step_id ASC
            """,
            (thread_id,),
        ).fetchall()
    steps: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["tool_result_ids"] = json.loads(item["tool_result_ids"])
        except (json.JSONDecodeError, TypeError):
            item["tool_result_ids"] = []
        steps.append(item)
    return steps


def store_report(
    *,
    thread_id: str,
    session_id: str,
    objective: str,
    content: str,
    report_path: str,
) -> None:
    now = _now()
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO reports (thread_id, session_id, objective, content, report_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    objective = excluded.objective,
                    content = excluded.content,
                    report_path = excluded.report_path,
                    created_at = excluded.created_at
                """,
                (thread_id, session_id, objective, content, report_path, now),
            )
        update_job(thread_id, report_path=report_path)
    except sqlite3.IntegrityError:
        # Studio/local LangGraph runs may not have a corresponding `jobs` row.
        # Keep graph execution alive; persistence to reports/jobs is best-effort.
        logger.warning("Skipping store_report due to FK constraints (thread_id=%s)", thread_id)


def get_report(thread_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT thread_id, session_id, objective, content, report_path, created_at FROM reports WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return dict(row) if row else None


def get_session_memory(session_id: str) -> str:
    if not session_id:
        return ""
    with _connect() as conn:
        row = conn.execute(
            "SELECT content FROM session_memory WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return str(row["content"]) if row else ""


def set_session_memory(session_id: str, content: str) -> None:
    if not session_id:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO session_memory (session_id, content, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                content = excluded.content,
                updated_at = excluded.updated_at
            """,
            (session_id, content, _now()),
        )


def upsert_document(doc: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO documents (
                doc_id, filename, session_id, status, chunk_count, page_count,
                error, upload_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                filename = excluded.filename,
                session_id = excluded.session_id,
                status = excluded.status,
                chunk_count = excluded.chunk_count,
                page_count = excluded.page_count,
                error = excluded.error,
                upload_path = excluded.upload_path,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                doc["doc_id"],
                doc["filename"],
                doc["session_id"],
                doc["status"],
                int(doc.get("chunk_count") or 0),
                int(doc.get("page_count") or 0),
                doc.get("error"),
                doc.get("upload_path"),
                float(doc["created_at"]),
                _now(),
            ),
        )


def update_document(doc_id: str, **fields: Any) -> None:
    allowed = {"filename", "session_id", "status", "chunk_count", "page_count", "error", "upload_path", "created_at"}
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.append(_now())
    values.append(doc_id)
    with _connect() as conn:
        conn.execute(f"UPDATE documents SET {', '.join(assignments)} WHERE doc_id = ?", values)


def list_documents(session_id: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if session_id is None:
            rows = conn.execute(
                """
                SELECT doc_id, filename, session_id, status, chunk_count, page_count,
                       error, upload_path, created_at
                FROM documents
                ORDER BY created_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT doc_id, filename, session_id, status, chunk_count, page_count,
                       error, upload_path, created_at
                FROM documents
                WHERE session_id = ?
                ORDER BY created_at DESC
                """,
                (session_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_document(doc_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT doc_id, filename, session_id, status, chunk_count, page_count,
                   error, upload_path, created_at
            FROM documents
            WHERE doc_id = ?
            """,
            (doc_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_document_metadata(doc_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))


init_db()

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
                company TEXT,
                ticker TEXT,
                doc_type TEXT,
                fiscal_period TEXT,
                subjects TEXT,
                stage TEXT,
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

            -- ── Knowledge Graph tables ─────────────────────────────────────
            -- Node ID schemes:
            --   Shared:     "{ticker}::{node_type}::{field}"
            --   Run-scoped: "{ticker}::{node_type}::{run_id}::{field}"
            CREATE TABLE IF NOT EXISTS kg_nodes (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                ticker TEXT NOT NULL,
                node_type TEXT NOT NULL,
                field TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.8,
                source TEXT NOT NULL,
                input_hash TEXT,
                run_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kg_nodes_session_ticker
            ON kg_nodes(session_id, ticker);

            CREATE INDEX IF NOT EXISTS idx_kg_nodes_ticker_type
            ON kg_nodes(ticker, node_type);

            CREATE TABLE IF NOT EXISTS kg_edges (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                src_id TEXT NOT NULL,
                tgt_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.8,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kg_edges_src
            ON kg_edges(src_id);

            CREATE INDEX IF NOT EXISTS idx_kg_edges_session
            ON kg_edges(session_id);

            CREATE TABLE IF NOT EXISTS kg_traversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT,
                age_s REAL,
                ts REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kg_traversals_run
            ON kg_traversals(run_id);

            CREATE TABLE IF NOT EXISTS session_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                collapsed INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_layout (
                session_id TEXT PRIMARY KEY,
                title_override TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                group_id TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (group_id) REFERENCES session_groups(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_session_layout_group
            ON session_layout(group_id);

            -- KG audit log: records findings from periodic quality checks
            CREATE TABLE IF NOT EXISTS kg_audit_log (
                audit_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                check_type TEXT NOT NULL,   -- cross_source, staleness, orphan, hallucination, entity_coherence
                ticker TEXT NOT NULL,
                node_type TEXT NOT NULL,
                field TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',  -- info, warning, error
                finding TEXT NOT NULL,     -- Human-readable description
                recommendation TEXT NOT NULL DEFAULT '', -- What to do about it
                source_tier TEXT,           -- Which source provided the data
                existing_value TEXT,        -- Value currently in KG
                conflicting_value TEXT,     -- Conflicting value (if applicable)
                auto_fixed INTEGER NOT NULL DEFAULT 0  -- Whether audit auto-corrected
            );

            CREATE INDEX IF NOT EXISTS idx_audit_ticker ON kg_audit_log(ticker);
            CREATE INDEX IF NOT EXISTS idx_audit_severity ON kg_audit_log(severity);
            CREATE INDEX IF NOT EXISTS idx_audit_type ON kg_audit_log(check_type);
            """
        )
        # ── Migrations: add entity metadata columns to older databases ──────
        _migrate_documents_entity_columns(conn)


def _migrate_documents_entity_columns(conn: sqlite3.Connection) -> None:
    """Add entity metadata columns if missing from older documents table."""
    new_cols = [
        ("company", "TEXT"),
        ("ticker", "TEXT"),
        ("doc_type", "TEXT"),
        ("fiscal_period", "TEXT"),
        ("subjects", "TEXT"),
        ("stage", "TEXT"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    for col_name, col_type in new_cols:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {col_type}")


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
                error, upload_path, company, ticker, doc_type, fiscal_period, subjects, stage, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                filename = excluded.filename,
                session_id = excluded.session_id,
                status = excluded.status,
                chunk_count = excluded.chunk_count,
                page_count = excluded.page_count,
                error = excluded.error,
                upload_path = excluded.upload_path,
                company = excluded.company,
                ticker = excluded.ticker,
                doc_type = excluded.doc_type,
                fiscal_period = excluded.fiscal_period,
                subjects = excluded.subjects,
                stage = excluded.stage,
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
                doc.get("company"),
                doc.get("ticker"),
                doc.get("doc_type"),
                doc.get("fiscal_period"),
                doc.get("subjects"),
                doc.get("stage"),
                float(doc["created_at"]),
                _now(),
            ),
        )


def update_document(doc_id: str, **fields: Any) -> None:
    allowed = {"filename", "session_id", "status", "chunk_count", "page_count", "error", "upload_path", "created_at",
               "company", "ticker", "doc_type", "fiscal_period", "subjects", "stage"}
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
                       error, upload_path, company, ticker, doc_type, fiscal_period,
                       subjects, stage, created_at
                FROM documents
                ORDER BY created_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT doc_id, filename, session_id, status, chunk_count, page_count,
                       error, upload_path, company, ticker, doc_type, fiscal_period,
                       subjects, stage, created_at
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


# ──────────────────────────────────────────────────────────────────────────────
# Knowledge Graph CRUD
# ──────────────────────────────────────────────────────────────────────────────


def _row_to_kg_node(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["value"] = json.loads(d["value"])
    except (TypeError, json.JSONDecodeError):
        pass
    return d


def upsert_kg_node(
    *,
    id: str,
    session_id: str | None,
    ticker: str,
    node_type: str,
    field: str,
    value: Any,
    confidence: float,
    source: str,
    input_hash: str | None = None,
    run_id: str | None = None,
    respect_user_lock: bool = True,
) -> None:
    """Insert or update a KG node.

    If ``respect_user_lock`` and an existing row has source='user_stated',
    a non-user-stated update is silently dropped (user beliefs are sticky).
    """
    import time as _time  # noqa: PLC0415
    now = _time.time()
    value_json = json.dumps(value, ensure_ascii=False, default=str)
    with _connect() as conn:
        if respect_user_lock:
            existing = conn.execute(
                "SELECT source FROM kg_nodes WHERE id = ?", (id,),
            ).fetchone()
            if existing and existing["source"] == "user_stated" and source != "user_stated":
                return
        conn.execute(
            """
            INSERT INTO kg_nodes(id, session_id, ticker, node_type, field, value,
                                 confidence, source, input_hash, run_id,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                value = excluded.value,
                confidence = excluded.confidence,
                source = excluded.source,
                input_hash = excluded.input_hash,
                updated_at = excluded.updated_at
            """,
            (id, session_id, ticker, node_type, field, value_json,
             confidence, source, input_hash, run_id, now, now),
        )


def get_kg_node(node_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM kg_nodes WHERE id = ?", (node_id,)).fetchone()
    return _row_to_kg_node(row) if row else None


def delete_kg_node(node_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM kg_nodes WHERE id = ?", (node_id,))
        conn.execute("DELETE FROM kg_edges WHERE src_id = ? OR tgt_id = ?", (node_id, node_id))


def list_kg_nodes(
    *,
    session_id: str | None = None,
    ticker: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if ticker is not None:
        clauses.append("ticker = ?")
        params.append(ticker)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM kg_nodes {where} ORDER BY updated_at DESC", params,
        ).fetchall()
    return [_row_to_kg_node(r) for r in rows]


def insert_kg_edge(
    *,
    id: str,
    session_id: str | None,
    src_id: str,
    tgt_id: str,
    relation: str,
    confidence: float = 0.8,
    source: str = "agent_inferred",
) -> None:
    import time as _time  # noqa: PLC0415
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO kg_edges
            (id, session_id, src_id, tgt_id, relation, confidence, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (id, session_id, src_id, tgt_id, relation, confidence, source, _time.time()),
        )


def delete_kg_edge(edge_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM kg_edges WHERE id = ?", (edge_id,))


def list_kg_edges(
    *,
    session_id: str | None = None,
    src_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if src_id is not None:
        clauses.append("src_id = ?")
        params.append(src_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM kg_edges {where}", params,
        ).fetchall()
    return [dict(r) for r in rows]


def insert_kg_traversal(
    *,
    run_id: str,
    node_id: str,
    status: str,
    action: str | None = None,
    age_s: float | None = None,
) -> None:
    import time as _time  # noqa: PLC0415
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_traversals(run_id, node_id, status, action, age_s, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, node_id, status, action, age_s, _time.time()),
        )


def list_kg_traversals(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kg_traversals WHERE run_id = ? ORDER BY ts ASC",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Session sidebar layout (groups, pin, order) ─────────────────────────────


def get_session_layout() -> dict[str, Any]:
    """Return persisted session groups and per-session layout metadata."""
    with _connect() as conn:
        groups = conn.execute(
            """
            SELECT id, name, color, collapsed, sort_order, created_at
            FROM session_groups
            ORDER BY sort_order ASC, created_at ASC
            """
        ).fetchall()
        sessions = conn.execute(
            """
            SELECT session_id, title_override, pinned, group_id, sort_order, updated_at
            FROM session_layout
            ORDER BY sort_order ASC, updated_at ASC
            """
        ).fetchall()
    return {
        "groups": [
            {
                "id": r["id"],
                "name": r["name"],
                "color": r["color"],
                "collapsed": bool(r["collapsed"]),
                "sort_order": int(r["sort_order"]),
                "created_at": r["created_at"],
            }
            for r in groups
        ],
        "sessions": [
            {
                "session_id": r["session_id"],
                "title_override": r["title_override"],
                "pinned": bool(r["pinned"]),
                "group_id": r["group_id"],
                "sort_order": int(r["sort_order"]),
                "updated_at": r["updated_at"],
            }
            for r in sessions
        ],
    }


def replace_session_layout(
    *,
    groups: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace all session layout rows (single-workspace sidebar state)."""
    now = _now()
    with _connect() as conn:
        conn.execute("DELETE FROM session_layout")
        conn.execute("DELETE FROM session_groups")
        for g in groups:
            conn.execute(
                """
                INSERT INTO session_groups (id, name, color, collapsed, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    g["id"],
                    g["name"],
                    g["color"],
                    1 if g.get("collapsed") else 0,
                    int(g.get("sort_order") or 0),
                    g.get("created_at") or now,
                ),
            )
        for s in sessions:
            conn.execute(
                """
                INSERT INTO session_layout
                (session_id, title_override, pinned, group_id, sort_order, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    s["session_id"],
                    s.get("title_override"),
                    1 if s.get("pinned") else 0,
                    s.get("group_id"),
                    int(s.get("sort_order") or 0),
                    s.get("updated_at") or now,
                ),
            )
    return get_session_layout()


init_db()

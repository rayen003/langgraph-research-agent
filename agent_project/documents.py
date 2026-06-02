"""
RAG pipeline: upload → parse → chunk → embed → store (ChromaDB).

Session-scoped: each session gets its own namespace via metadata filtering.
Hybrid retrieval: dense (ChromaDB cosine) + sparse (BM25) merged with RRF.
"""

import contextvars
import io
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Literal

# Must be set before chromadb import — silences broken PostHog telemetry errors
# (the bundled posthog raises "capture() takes 1 positional argument but 3 were
# given" on ClientStartEvent). The telemetry error is harmless/cosmetic; the
# env var disables capture without breaking client init.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction, DefaultEmbeddingFunction
from langchain_text_splitters import RecursiveCharacterTextSplitter
from storage import (
    delete_document_metadata,
    list_documents as list_document_metadata,
    update_document,
    upsert_document,
)

logger = logging.getLogger(__name__)

# ── Rich console logger for coloured terminal output ─────────────────────
# Uses the same Console instance as the rest of the project so styles
# compose with existing output (agent_log panels, etc.)
from utils import console as _console  # noqa: E402 — after logger is defined

def _rag_console(icon: str, label: str, detail: str = "", *, style: str = "") -> None:
    """Print a coloured RAG pipeline event to the terminal.

    Intended for developer visibility — complements the activity-stream
    events that drive the frontend."""
    if not style:
        style = "dim"
    _console.print(f"  {icon} [bold]{label}[/bold]", end="")
    if detail:
        _console.print(f"  [{style}]{detail}[/{style}]")
    else:
        _console.print()

# ---------------------------------------------------------------------------
# Context var — set by agent nodes before invoking tools
# ---------------------------------------------------------------------------

_session_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("rag_session_id", default="")

# ---------------------------------------------------------------------------
# ChromaDB setup
# ---------------------------------------------------------------------------

# Store root. The actual collection dir is provider-namespaced (see
# _chroma_dir) so OpenAI- and local-embedded stores never share a path —
# they have different embedding dims/EF configs and would otherwise conflict.
_CHROMA_ROOT = Path(__file__).parent / "runs"
_LEGACY_CHROMA_DIR = _CHROMA_ROOT / "chroma"  # pre-namespacing store (migration)


def _chroma_dir() -> Path:
    """Provider-namespaced Chroma store dir.

    openai embeddings → runs/chroma-openai ; local → runs/chroma-local.
    Switching providers switches dirs, so each persists independently and new
    documents append to the active provider's store instead of colliding.
    A legacy un-namespaced runs/chroma is migrated to the right namespace once.
    """
    provider = "openai" if _use_openai_embeddings() else "local"
    target = _CHROMA_ROOT / f"chroma-{provider}"
    # One-time migration: if only the legacy dir exists, adopt it for whichever
    # provider is active now (its EF config is validated on first open; a
    # mismatch triggers the normal reset path).
    if not target.exists() and _LEGACY_CHROMA_DIR.exists():
        try:
            _LEGACY_CHROMA_DIR.rename(target)
        except Exception:  # noqa: BLE001
            pass
    target.mkdir(parents=True, exist_ok=True)
    return target

UPLOADS_DIR = Path(__file__).parent / "runs" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def get_upload_path(doc_id: str, filename: str) -> Path:
    safe_name = Path(filename).name  # strip any path components
    folder = UPLOADS_DIR / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / safe_name

_chroma_client: chromadb.ClientAPI | None = None
_chroma_checked = False
_chroma_resetting = False


# ---------------------------------------------------------------------------
# RAG retrieval activity — emits substeps to the right-bar when a run/SSE
# context is bound. No-ops silently otherwise (e.g. DCF evidence assembly with
# no UI handler), so retrieval never depends on the caller's context.
# ---------------------------------------------------------------------------

_RAG_PARENT_ID = "rag_retrieval"


def _rag_emit(step: str, status: str, summary: str = "", **meta) -> None:
    """Best-effort RAG activity event. The ``retrieve`` step is the parent
    workflow span; all other steps nest under it as workflow_steps in the
    right-bar activity trace. Safe to call from any thread/context — no-ops
    when no UI handler is bound (e.g. background DCF evidence assembly)."""
    try:
        from utils import emit_activity  # noqa: PLC0415

        is_parent = step == "retrieve"
        payload = {"summary_line": summary, **meta} if summary or meta else None
        common = dict(
            status="started" if status == "start" else "completed",
            summary=summary or None,
            meta=dict(payload) if payload else None,
        )
        if is_parent:
            emit_activity(
                activity_id=_RAG_PARENT_ID,
                kind="workflow",
                name="workflow:rag",
                scope="workflow",
                display_label="Document retrieval",
                **common,
            )
        else:
            emit_activity(
                activity_id=f"{_RAG_PARENT_ID}_{step}",
                kind="workflow_step",
                name=f"workflow:rag:{step}",
                scope="workflow",
                step_id=_RAG_PARENT_ID,
                parent_activity_id=_RAG_PARENT_ID,
                **common,
            )
    except Exception:  # noqa: BLE001
        pass

_COLLECTION_NAME = "rag_documents"


def _use_openai_embeddings() -> bool:
    return os.getenv("USE_OPENAI_EMBEDDINGS", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))


def _expected_embedding_dim() -> int:
    return 1536 if _use_openai_embeddings() else 384


def _collection_metadata() -> dict[str, str]:
    return {
        "hnsw:space": "cosine",
        "embedding_provider": "openai" if _use_openai_embeddings() else "local",
        "embedding_dim": str(_expected_embedding_dim()),
    }


def _embedding_function():
    if _use_openai_embeddings():
        return OpenAIEmbeddingFunction(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model_name="text-embedding-3-small",
        )
    return DefaultEmbeddingFunction()


def _clear_chroma_system_cache() -> None:
    """Drop chromadb's per-path System cache.

    chromadb caches a ``System`` keyed by path. After we rename/recreate the
    store on disk, a fresh ``PersistentClient(same_path)`` would otherwise hand
    back the CACHED system — still holding the old collection config (e.g. the
    default-embedding-function), so the EF-conflict re-raises forever. Clearing
    the cache forces a clean re-open of the new on-disk store."""
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:  # noqa: BLE001
        pass


def _new_chroma_client() -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(_chroma_dir()))


def _get_chroma_client() -> chromadb.ClientAPI:
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = _new_chroma_client()
    return _chroma_client


def _open_collection() -> chromadb.Collection:
    try:
        return _get_chroma_client().get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=_embedding_function(),
            metadata=_collection_metadata(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_chroma_repairable(exc) and not _chroma_resetting:
            _reset_chroma_store(str(exc))
            return _get_chroma_client().get_or_create_collection(
                name=_COLLECTION_NAME,
                embedding_function=_embedding_function(),
                metadata=_collection_metadata(),
            )
        raise


def _is_embedding_mismatch(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        ("embedding dimension" in msg and "does not match" in msg)
        or "embedding function conflict" in msg
        or "embedding function already exists" in msg
    )


def _is_embedding_mismatch_text(msg: str) -> bool:
    return _is_embedding_mismatch(RuntimeError(msg))


def _is_chroma_repairable(exc: BaseException) -> bool:
    return _is_chroma_corrupt(exc) or _is_embedding_mismatch(exc)


def _collection_dim_mismatch(collection: chromadb.Collection) -> bool:
    meta = collection.metadata or {}
    stored = meta.get("embedding_dim")
    expected = _expected_embedding_dim()
    if stored is None:
        # Legacy local (384-dim) store while OpenAI embeddings are enabled now.
        return collection.count() > 0 and _use_openai_embeddings()
    try:
        return int(stored) != expected
    except (TypeError, ValueError):
        return True


def _is_chroma_corrupt(exc: BaseException) -> bool:
    msg = str(exc).lower()
    # KeyError('_type') / "trying to instantiate configuration" surface when the
    # on-disk collection-config JSON was written by an incompatible chromadb
    # version (the config schema changed and lacks the '_type' discriminator).
    # Treat as corrupt → reset wipes the stale store and re-indexes.
    if isinstance(exc, KeyError) and "_type" in str(exc):
        return True
    return (
        "metadata segment" in msg
        or "mismatched types" in msg
        or "not compatible with sql type" in msg
        or "'_type'" in msg
        or "trying to instantiate configuration" in msg
        or "unable to decode configuration" in msg
        or "invalidconfigurationerror" in msg
    )


def _reset_chroma_store(reason: str) -> None:
    """Backup and recreate Chroma when on-disk format is incompatible."""
    global _chroma_client, _chroma_checked, _chroma_resetting
    if _chroma_resetting:
        raise RuntimeError(f"ChromaDB store could not be repaired: {reason}")

    _chroma_resetting = True
    try:
        _chroma_client = None
        _chroma_checked = False
        # Drop chromadb's cached System for this path BEFORE moving the dir —
        # otherwise the recreated client reuses the stale (old-EF) config.
        _clear_chroma_system_cache()

        active_dir = _chroma_dir()
        backup: Path | None = None
        if active_dir.exists():
            backup = active_dir.parent / f"{active_dir.name}.bak.{int(time.time())}"
            if backup.exists():
                shutil.rmtree(backup)
            active_dir.rename(backup)

        active_dir.mkdir(parents=True, exist_ok=True)
        # Clear again AFTER the rename so the fresh client binds to the new dir.
        _clear_chroma_system_cache()
        _chroma_client = _new_chroma_client()
        collection = _get_chroma_client().get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=_embedding_function(),
            metadata=_collection_metadata(),
        )
        collection.count()

        logger.warning(
            "Reset ChromaDB store (%s). Backup at %s. Re-indexing uploaded documents…",
            reason,
            backup,
        )
        _reindex_ready_documents(collection)
        _chroma_checked = True
    finally:
        _chroma_resetting = False


def _has_ready_documents() -> bool:
    return any(d.get("status") == "ready" for d in list_document_metadata())


def _ensure_chroma_healthy() -> None:
    global _chroma_checked
    if _chroma_checked or _chroma_resetting:
        return
    try:
        collection = _open_collection()
        if _collection_dim_mismatch(collection):
            _reset_chroma_store(
                f"embedding config changed (expected {_expected_embedding_dim()}-dim "
                f"{_collection_metadata()['embedding_provider']})",
            )
            return
        if collection.count() == 0 and _has_ready_documents():
            logger.info("Chroma store empty but SQLite has ready documents — re-indexing…")
            _reindex_ready_documents(collection)
        _chroma_checked = True
    except Exception as exc:  # noqa: BLE001
        if _is_chroma_repairable(exc):
            _reset_chroma_store(str(exc))
            return
        raise


def _get_collection() -> chromadb.Collection:
    _ensure_chroma_healthy()
    return _open_collection()


# ---------------------------------------------------------------------------
# Document registry (hydrated from SQLite; kept in memory for fast status reads)
# ---------------------------------------------------------------------------

_doc_registry: dict[str, dict] = {
    d["doc_id"]: d
    for d in list_document_metadata()
}


def register_document(entry: dict) -> None:
    _doc_registry[entry["doc_id"]] = entry
    upsert_document(entry)


def _update_doc(doc_id: str, **fields) -> None:
    entry = _doc_registry.get(doc_id)
    if not entry:
        return
    entry.update(fields)
    update_document(doc_id, **fields)


def _backfill_document_metadata_from_chroma() -> None:
    if _doc_registry:
        return
    try:
        collection = _get_collection()
        total = collection.count()
        if total == 0:
            return
        raw = collection.get(include=["metadatas"], limit=total)
    except Exception:  # noqa: BLE001
        return

    grouped: dict[str, dict] = {}
    for meta in raw.get("metadatas", []) or []:
        if not isinstance(meta, dict):
            continue
        doc_id = meta.get("doc_id")
        if not doc_id:
            continue
        entry = grouped.setdefault(
            str(doc_id),
            {
                "doc_id": str(doc_id),
                "filename": meta.get("filename") or "uploaded document",
                "session_id": meta.get("session_id") or "",
                "status": "ready",
                "chunk_count": 0,
                "page_count": 0,
                "error": None,
                "created_at": time.time(),
                "company": meta.get("doc_company") or "",
                "ticker": meta.get("doc_ticker") or "",
                "doc_type": meta.get("doc_type") or "",
                "fiscal_period": meta.get("fiscal_period") or "",
                "subjects": meta.get("subjects") or "",
            },
        )
        entry["chunk_count"] += 1
        try:
            entry["page_count"] = max(int(entry["page_count"]), int(meta.get("page") or 0))
        except (TypeError, ValueError):
            pass
        # Fill entity metadata from first chunk that has it
        if not entry.get("company") and meta.get("doc_company"):
            entry["company"] = meta.get("doc_company")
            entry["ticker"] = meta.get("doc_ticker")
            entry["doc_type"] = meta.get("doc_type")
            entry["fiscal_period"] = meta.get("fiscal_period")
            entry["subjects"] = meta.get("subjects")

    for doc_id, entry in grouped.items():
        upload_dir = UPLOADS_DIR / doc_id
        if upload_dir.exists():
            files = [p for p in upload_dir.iterdir() if p.is_file()]
            if files:
                entry["upload_path"] = str(files[0])
                entry["filename"] = files[0].name
                entry["created_at"] = files[0].stat().st_mtime
        register_document(entry)


def get_doc_status(doc_id: str) -> dict | None:
    return _doc_registry.get(doc_id)


def list_docs(session_id: str) -> list[dict]:
    return [d for d in _doc_registry.values() if d["session_id"] == session_id]


_backfill_document_metadata_from_chroma()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_pdf(file_bytes: bytes) -> list[dict]:
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            for table in tables:
                if not table:
                    continue
                rows = [" | ".join(str(c or "").strip() for c in row) for row in table if row]
                if rows:
                    text += "\n[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]"
            if text.strip():
                pages.append({"text": text, "page": i + 1})
    return pages


def _parse_docx(file_bytes: bytes) -> list[dict]:
    from docx import Document  # type: ignore[import-untyped]
    doc = Document(io.BytesIO(file_bytes))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return [{"text": text, "page": 1}] if text.strip() else []


def _parse_csv(file_bytes: bytes, filename: str) -> list[dict]:
    import pandas as pd
    df = pd.read_csv(io.BytesIO(file_bytes))
    header = " | ".join(str(c) for c in df.columns)
    pages = []
    chunk_size = 25
    for i in range(0, len(df), chunk_size):
        rows = df.iloc[i : i + chunk_size].to_string(index=False)
        pages.append({
            "text": f"[TABLE: {filename}]\nColumns: {header}\n{rows}",
            "page": i // chunk_size + 1,
        })
    return pages


def _parse_xlsx(file_bytes: bytes, filename: str) -> list[dict]:
    import pandas as pd
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    pages = []
    chunk_size = 25
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        header = " | ".join(str(c) for c in df.columns)
        for i in range(0, len(df), chunk_size):
            rows = df.iloc[i : i + chunk_size].to_string(index=False)
            pages.append({
                "text": f"[SHEET: {sheet} — {filename}]\nColumns: {header}\n{rows}",
                "page": i // chunk_size + 1,
            })
    return pages


def _parse_file(file_bytes: bytes, filename: str) -> list[dict]:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(file_bytes)
    if ext in (".docx", ".doc"):
        return _parse_docx(file_bytes)
    if ext == ".csv":
        return _parse_csv(file_bytes, filename)
    if ext in (".xlsx", ".xls"):
        return _parse_xlsx(file_bytes, filename)
    # Plain text fallback
    text = file_bytes.decode("utf-8", errors="replace")
    return [{"text": text, "page": 1}] if text.strip() else []


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=64,
    separators=["\n\n", "\n", " ", ""],
)


def _chunk_pages(pages: list[dict]) -> list[dict]:
    chunks = []
    for page in pages:
        # Don't split table blocks — keep them intact
        if "[TABLE]" in page["text"] or "[SHEET:" in page["text"] or "[TABLE:" in page["text"]:
            chunks.append({**page, "chunk_index": 0})
            continue
        texts = _splitter.split_text(page["text"])
        for j, text in enumerate(texts):
            chunks.append({"text": text, "page": page["page"], "chunk_index": j})
    return chunks


def _upsert_chunks(
    collection: chromadb.Collection,
    *,
    doc_id: str,
    filename: str,
    session_id: str,
    chunks: list[dict],
    entity_meta: dict[str, Any] | None = None,
) -> None:
    ids, documents, metadatas = [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"{doc_id}_{i}")
        documents.append(chunk["text"])
        meta = {
            "doc_id": doc_id,
            "filename": filename,
            "page": chunk["page"],
            "session_id": session_id,
            "chunk_index": i,
        }
        # Attach document-level entity metadata to every chunk
        if entity_meta:
            meta["doc_company"] = entity_meta.get("company") or ""
            meta["doc_ticker"] = entity_meta.get("ticker") or ""
            meta["doc_type"] = entity_meta.get("doc_type") or ""
            meta["fiscal_period"] = entity_meta.get("fiscal_period") or ""
            subjects = entity_meta.get("subjects")
            if isinstance(subjects, list):
                meta["subjects"] = ",".join(str(s) for s in subjects)
        metadatas.append(meta)

    batch_size = 100
    for start in range(0, len(ids), batch_size):
        try:
            collection.upsert(
                ids=ids[start : start + batch_size],
                documents=documents[start : start + batch_size],
                metadatas=metadatas[start : start + batch_size],
            )
        except Exception as exc:  # noqa: BLE001
            if _is_chroma_repairable(exc) and not _chroma_resetting:
                _reset_chroma_store(str(exc))
                collection = _get_collection()
                collection.upsert(
                    ids=ids[start : start + batch_size],
                    documents=documents[start : start + batch_size],
                    metadatas=metadatas[start : start + batch_size],
                )
            else:
                raise


def _resolve_upload_path(entry: dict) -> Path | None:
    upload_path = entry.get("upload_path")
    if upload_path:
        path = Path(str(upload_path))
        if path.is_file():
            return path
    upload_dir = UPLOADS_DIR / str(entry.get("doc_id") or "")
    if upload_dir.is_dir():
        files = [p for p in upload_dir.iterdir() if p.is_file()]
        if files:
            return files[0]
    return None


def _index_file_into_chroma(
    file_bytes: bytes,
    filename: str,
    session_id: str,
    doc_id: str,
    *,
    collection: chromadb.Collection | None = None,
    entity_meta: dict[str, Any] | None = None,
) -> int:
    pages = _parse_file(file_bytes, filename)
    if not pages:
        return 0
    chunks = _chunk_pages(pages)
    col = collection or _get_collection()
    # Extract entity metadata from first few chunks if not already provided
    if not entity_meta:
        first_texts = [c["text"] for c in chunks[:8]]
        entity_meta = _extract_document_entities(filename, first_texts)
    _upsert_chunks(
        col,
        doc_id=doc_id,
        filename=filename,
        session_id=session_id,
        chunks=chunks,
        entity_meta=entity_meta,
    )
    return len(chunks)


def _reindex_ready_documents(collection: chromadb.Collection | None = None) -> None:
    col = collection or _open_collection()
    reindexed = 0
    for entry in list_document_metadata():
        status = entry.get("status")
        err = str(entry.get("error") or "")
        if status != "ready":
            if not (status == "error" and _is_embedding_mismatch_text(err)):
                continue
        path = _resolve_upload_path(entry)
        if path is None:
            continue
        try:
            chunk_count = _index_file_into_chroma(
                path.read_bytes(),
                str(entry.get("filename") or path.name),
                str(entry.get("session_id") or ""),
                str(entry["doc_id"]),
                collection=col,
            )
            if chunk_count:
                reindexed += 1
                _update_doc(
                    entry["doc_id"],
                    chunk_count=chunk_count,
                    status="ready",
                    error=None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to reindex document %s: %s", entry.get("doc_id"), exc)
            _update_doc(entry["doc_id"], status="error", error=f"Reindex failed: {exc}")
    logger.info("Chroma reindex complete — %d document(s) embedded", reindexed)


# ---------------------------------------------------------------------------
# Ingest (called in a background thread)
# ---------------------------------------------------------------------------


def ingest_document(file_bytes: bytes, filename: str, session_id: str, doc_id: str) -> None:
    """Parse → chunk → embed → index an uploaded document.

    Runs in a background thread (no run/SSE context), so progress is surfaced
    via the ``stage`` field on the doc registry — the upload card polls
    ``/documents/{id}/status`` and renders the live stage. Stages:
    ``uploading → parsing → chunking → embedding → ready`` (or ``error``).
    """
    try:
        # ── uploading: persist raw file (preview + reindex source) ──────────
        _rag_console("📤", "Upload", f"[cyan]{filename}[/cyan]", style="dim")
        _update_doc(doc_id, stage="uploading")
        upload_path = get_upload_path(doc_id, filename)
        upload_path.write_bytes(file_bytes)
        _update_doc(doc_id, upload_path=str(upload_path))

        # ── parsing: extract text pages ─────────────────────────────────────
        _update_doc(doc_id, stage="parsing")
        pages = _parse_file(file_bytes, filename)
        if not pages:
            _update_doc(
                doc_id, status="error", stage="error",
                error="No text could be extracted from the file.",
            )
            return

        # ── chunking ────────────────────────────────────────────────────────
        _update_doc(doc_id, stage="chunking")
        chunks = _chunk_pages(pages)
        if not chunks:
            _update_doc(
                doc_id, status="error", stage="error",
                error="No text could be extracted from the file.",
            )
            return

        # ── embedding + indexing (Chroma upsert runs the embedding fn) ──────
        _update_doc(doc_id, stage="embedding")
        col = _get_collection()
        # Extract entity metadata from first few chunks
        first_texts = [c["text"] for c in chunks[:8]]
        entity_meta = _extract_document_entities(filename, first_texts)
        _upsert_chunks(col, doc_id=doc_id, filename=filename, session_id=session_id, chunks=chunks, entity_meta=entity_meta)

        # Store entity metadata in doc registry so it survives in memory
        if entity_meta:
            _update_doc(
                doc_id,
                company=entity_meta.get("company"),
                ticker=entity_meta.get("ticker"),
                doc_type=entity_meta.get("doc_type"),
                fiscal_period=entity_meta.get("fiscal_period"),
                subjects=json.dumps(entity_meta.get("subjects", []), ensure_ascii=False),
            )

        # ── Fact extraction → KG ingestion ──────────────────────────────────
        # After chunks are indexed and entity metadata is saved, extract
        # structured financial facts from the document and ingest them into
        # the KG through the unified ingest pipeline.
        try:
            extract_and_ingest_facts(doc_id=doc_id, session_id=session_id)
        except Exception as fact_exc:  # noqa: BLE001
            logger.warning("Fact extraction → KG failed for doc %s: %s", doc_id, fact_exc)

        # ── Filing auto-ingestion ───────────────────────────────────────────
        # When the document is identified as an SEC filing (10-K, 10-Q, 8-K)
        # or annual report, register it as a verified filing node in the KG
        # so future analyses can reference the source material by ticker.
        doc_ticker = (entity_meta or {}).get("ticker", "")
        doc_type = (entity_meta or {}).get("doc_type", "")
        if doc_ticker and doc_type in ("sec_filing", "annual_report"):
            try:
                from kg import kg_write  # noqa: PLC0415
                ticker_up = doc_ticker.upper()
                period = (entity_meta or {}).get("fiscal_period", "") or "unknown"
                page_count = max(p["page"] for p in pages) if pages else 0

                # ONE filing node per document (keyed by type + period so a
                # 10-Q and a later 10-K coexist, but re-uploading the same
                # filing overwrites in place). The actual content stays in
                # ChromaDB; the node carries metadata + a short lead snippet +
                # source_doc_id so the UI can OPEN the original file via
                # GET /documents/{doc_id}/file. No per-page fragmentation.
                lead_text = "\n\n".join(c["text"] for c in chunks[:2]).strip()[:600]
                kg_write(
                    ticker=ticker_up,
                    node_type="filing",
                    field=f"{doc_type}::{period}",
                    value={
                        "filing_type": doc_type,
                        "fiscal_period": period,
                        "filename": filename,
                        "chunk_count": len(chunks),
                        "page_count": page_count,
                        "source_doc_id": doc_id,
                        "text": lead_text,
                    },
                    source="document_extraction",
                    confidence=0.95,
                    session_id=session_id,
                    source_doc_entity=entity_meta,
                )

                _rag_console(
                    "📄", "Filing → KG",
                    f"{ticker_up} · {doc_type} · {period} · {len(chunks)} chunks "
                    f"(openable via doc {doc_id})",
                    style="green",
                )
            except Exception as filing_exc:  # noqa: BLE001
                logger.warning("Filing auto-ingestion failed for doc %s: %s", doc_id, filing_exc)

        _update_doc(
            doc_id,
            status="ready",
            stage="ready",
            chunk_count=len(chunks),
            page_count=max(p["page"] for p in pages) if pages else 0,
            error=None,
        )
        company = (entity_meta or {}).get("company") or "?"
        ticker = (entity_meta or {}).get("ticker") or ""
        label = company + (f" ({ticker})" if ticker else "")
        _rag_console("✅", "Document ready", f"{len(chunks)} chunks · {label}", style="green")

    except Exception as exc:  # noqa: BLE001
        logger.exception("Document ingest failed for %s", doc_id)
        _update_doc(doc_id, status="error", stage="error", error=str(exc))


def delete_document(doc_id: str) -> None:
    info = _doc_registry.pop(doc_id, None)
    if not info:
        return
    delete_document_metadata(doc_id)
    try:
        collection = _get_collection()
        collection.delete(where={"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        pass
    # Remove uploaded file
    upload_dir = UPLOADS_DIR / doc_id
    if upload_dir.exists():
        import shutil
        try:
            shutil.rmtree(upload_dir)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Hybrid retrieval (dense + BM25, RRF merge)
# ---------------------------------------------------------------------------


def hybrid_search(query: str, session_id: str, n_results: int = 8) -> list[dict]:
    _rag_emit("retrieve", "start", f"“{query[:50]}”")
    collection = _get_collection()
    total = collection.count()
    if total == 0:
        session_ready = [d for d in list_docs(session_id) if d.get("status") == "ready"]
        if session_ready and _has_ready_documents():
            logger.info("Chroma empty — re-indexing before search (session=%s)", session_id)
            _reindex_ready_documents(collection)
            total = collection.count()
        if total == 0:
            _rag_emit("retrieve", "complete", "No documents to search")
            return []

    # ── Dense vector search (embeds query + ANN over chunk vectors) ─────────
    _rag_console("🔍", "Hybrid search", f"query=[cyan]{query[:80]}[/cyan]", style="blue")
    _rag_emit("embed_query", "start")
    k = min(20, total)
    try:
        dense = collection.query(
            query_texts=[query],
            n_results=k,
            where={"session_id": session_id},
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001
        if _is_chroma_repairable(exc) and not _chroma_resetting:
            _reset_chroma_store(str(exc))
            if _chroma_checked:
                return hybrid_search(query, session_id, n_results)
        logger.exception("Chroma query failed for session=%s query=%r", session_id, query)
        _rag_emit("retrieve", "complete", "Search failed")
        return []

    docs = dense["documents"][0]
    metas = dense["metadatas"][0]
    distances = dense["distances"][0]
    _rag_emit("embed_query", "complete", f"{len(docs)} candidate passages")
    _rag_console("  ⚡", "Dense candidates", f"{len(docs)} results", style="cyan")

    if not docs:
        _rag_emit("retrieve", "complete", "Nothing relevant found")
        return []

    # ── BM25 keyword scoring over the dense candidates ──────────────────────
    _rag_emit("keyword_rank", "start")
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        tokenized = [d.lower().split() for d in docs]
        bm25 = BM25Okapi(tokenized)
        bm25_scores = bm25.get_scores(query.lower().split())
    except Exception:  # noqa: BLE001
        # If BM25 fails, fall back to dense-only
        bm25_scores = [0.0] * len(docs)
    _rag_emit("keyword_rank", "complete")

    # Convert cosine distance → similarity (ChromaDB returns 0=identical, 2=opposite)
    dense_scores = [1.0 - d for d in distances]

    def rrf(rank: int, k: int = 60) -> float:
        return 1.0 / (k + rank + 1)

    dense_ranked = sorted(range(len(dense_scores)), key=lambda i: dense_scores[i], reverse=True)
    bm25_ranked = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)

    combined: dict[int, float] = {}
    for rank, idx in enumerate(dense_ranked):
        combined[idx] = combined.get(idx, 0.0) + rrf(rank)
    for rank, idx in enumerate(bm25_ranked):
        combined[idx] = combined.get(idx, 0.0) + rrf(rank)

    _rag_emit("fuse", "start")
    top = sorted(combined, key=lambda i: combined[i], reverse=True)[:n_results]
    out = [{"text": docs[i], "metadata": metas[i]} for i in top]
    _rag_emit("fuse", "complete", f"{len(out)} best passages")
    _rag_emit("retrieve", "complete", f"Found {len(out)} relevant passage" + ("s" if len(out)!=1 else ""))
    _rag_console("  🔀", "RRF fusion", f"{len(out)} passages selected", style="cyan")
    return out


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Document entity metadata extraction (runs once at upload time)
# ---------------------------------------------------------------------------

_ENTITY_PROMPT = (
    "Extract the primary subject entity from these document excerpts.\n"
    "Return JSON with these fields:\n"
    '  - "company": the full company name mentioned (e.g. "Meta Platforms, Inc.")\n'
    '  - "ticker": the stock ticker if identifiable (e.g. "META"), else null\n'
    '  - "doc_type": one of "earnings_call", "annual_report", "investor_presentation", "sec_filing", "research_report", "other"\n'
    '  - "fiscal_period": the quarter/year covered if identifiable (e.g. "Q1 2026"), else null\n'
    '  - "subjects": array of all companies/organizations mentioned in the excerpts (useful for multi-company docs)\n'
    "If the document covers MULTIPLE companies equally, list them all in subjects.\n"
    "If you cannot determine any of these fields with confidence, use null.\n"
    "Reply with ONLY the JSON object, no explanation."
)


def _extract_document_entities(filename: str, first_chunks: list[str]) -> dict[str, Any]:
    """Extract company name, ticker, doc_type, fiscal_period from document content.

    Uses a cheap LLM call on the first few chunks only. Runs once at upload time.
    Result cached in _doc_registry and persisted in ChromaDB chunk metadata."""
    import re  # noqa: PLC0415

    if not first_chunks:
        return {}

    # ── Deterministic filing detection from filename ──────────────────────
    # SEC filings have predictable naming patterns (10-K, 10-Q, 8-K, etc.).
    # Override the LLM's doc_type when the filename strongly indicates a filing.
    _fn_upper = filename.upper()
    _filename_filing = None
    if re.search(r'\b10[-_]?K\b', _fn_upper):
        _filename_filing = 'sec_filing'
    elif re.search(r'\b10[-_]?Q\b', _fn_upper):
        _filename_filing = 'sec_filing'
    elif re.search(r'\b8[-_]?K\b', _fn_upper):
        _filename_filing = 'sec_filing'
    elif re.search(r'\bS[-_]?1\b|\bFORM\s*S[-_]?1\b', _fn_upper):
        _filename_filing = 'sec_filing'
    elif re.search(r'\b20[-_]?F\b|\bFORM\s*20[-_]?F\b', _fn_upper):
        _filename_filing = 'sec_filing'

    sample = "\n---\n".join(first_chunks[:6])
    if len(sample) > 4000:
        sample = sample[:4000]

    try:
        import dotenv
        from langchain_openai import ChatOpenAI
        dotenv.load_dotenv()
        entity_llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY"), timeout=15)
        result = entity_llm.invoke([
            {"role": "system", "content": _ENTITY_PROMPT},
            {"role": "user", "content": f"Document filename: {filename}\n\nExcerpts:\n{sample}"},
        ])
        raw = (result.content or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        entities = json.loads(raw)
        # Apply filename-based filing override if it conflicts with LLM result
        if _filename_filing:
            entities["doc_type"] = _filename_filing
        company = entities.get("company") or "?"
        ticker = entities.get("ticker") or "?"
        doc_type = entities.get("doc_type") or "?"
        period = entities.get("fiscal_period") or ""
        _rag_console("📄", "Entity extraction",
                     f"{company} ({ticker}) · {doc_type}" + (f" · {period}" if period else ""),
                     style="green")
        return entities
    except Exception as exc:  # noqa: BLE001
        logger.warning("Document entity extraction failed for %s: %s", filename, exc)
        return {}


# ---------------------------------------------------------------------------
# Document fact extraction: structured facts from chunks → KG
# ---------------------------------------------------------------------------

_FACT_EXTRACTION_PROMPT = """You are a financial data extraction engine. Given document text and entity
metadata, extract structured financial facts.

For each fact found, output a JSON object with:
- "fact_type": one of "revenue", "net_income", "operating_income",
  "gross_profit", "eps", "shares_outstanding", "fcff_margin", "wacc_signal",
  "risk_factor", "competitive_moat", "guidance", "capital_allocation",
  "debt_metric", "growth_rate", "margin", "effective_tax_rate",
  "valuation_metric", "other"
- "field": the DCF-relevant field name (e.g. "base_revenue", "revenue_growth",
  "fcff_margin", "wacc", "capex_ratio", "terminal_growth", "shares_outstanding",
  "net_debt", "beta", "effective_tax_rate", "ebitda_margin", "fiscal_period"")
- "value": numeric value if extractable (e.g. 391.0 for $391B), null if not numeric
- "value_text": original text span (e.g. "revenue grew 8.2% year-over-year")
- "fiscal_period": e.g. "FY2024", "Q3 2024", "LTM", or ""
- "confidence": your confidence this fact is correct (0.0–1.0)

Rules:
- Extract ONLY facts explicitly stated in the text — never infer or calculate.
- Each fact should be atomic: one number, one metric, one claim.
- Use standard ticker for the company if known (e.g. "AAPL" not "Apple Inc").
- Fiscal period must match the document — "FY2024" not just "2024".
- If the text says "Apple reported revenue of $391 billion for FY2024",
  extract: {"fact_type": "revenue", "field": "base_revenue", "value": 391.0,
  "value_text": "revenue of $391 billion for FY2024", "fiscal_period": "FY2024", "confidence": 0.95}
- If a fact cannot be cleanly extracted, skip it. Quality over quantity.
- Return a JSON array of fact objects. Empty array if no facts found.

CRITICAL — match the fact_type to the EXACT line label, never a sibling metric:
- "revenue" / "net sales" / "total revenue" ONLY → fact_type "revenue".
- "Net income" → fact_type "net_income" (NOT revenue).
- "Operating income" / "income from operations" → "operating_income".
- "Gross profit" / "gross margin" (dollar) → "gross_profit".
- "Earnings per share" / "EPS" / "diluted EPS" → "eps".
- "Shares outstanding" / "diluted shares" → "shares_outstanding".
- "Total shareholders' equity" / "total equity" is EQUITY, not debt — use
  "other"; "debt_metric" is ONLY for borrowings / notes payable / total debt /
  net debt.
- "Effective tax rate" → "effective_tax_rate"; a dividend per share →
  "capital_allocation".
- Each income-statement line gets its OWN fact_type above. Never collapse
  net income / operating income / gross profit into "revenue".
- When the line item does not clearly map to one of the listed fact_types,
  set fact_type "other" and leave "field" equal to a short snake_case label
  of the actual line item. Do NOT mislabel — a wrong label corrupts the DCF.

Respond with ONLY the JSON array, no explanation."""


def _extract_document_facts(
    chunks: list[str],
    entity: dict[str, Any],
    doc_id: str,
    filename: str,
) -> list[dict[str, Any]]:
    """Extract structured financial facts from document chunks.

    Called once at ingestion time after entity extraction. Uses gpt-4o-mini
    to classify chunks into typed facts with numeric values, confidence
    scores, and fiscal periods.

    Returns a list of raw fact dicts (pre-DocumentFact) suitable for
    ``kg.ingest_facts()``.
    """
    if not chunks or not entity.get("ticker") or entity.get("ticker", "?") == "?":
        _rag_console("📄", "Fact extraction skipped",
                     f"no entity/ticker for {filename}", style="dim")
        return []

    ticker = entity["ticker"].upper()
    company = entity.get("company", "?")
    period = entity.get("fiscal_period", "")

    # Sample chunks spread ACROSS the document, not just the first few.
    # A real 10-Q/10-K opens with cover page + forward-looking boilerplate;
    # the financial statements and MD&A (where the numbers live) sit mid-to-
    # late document. Taking chunks[:6] yields 0 extractable facts. Even-sample
    # up to 12 chunks across the whole doc so the LLM sees real financials.
    usable = [c for c in chunks if (c or "").strip()]
    max_sample = 12
    if len(usable) <= max_sample:
        sample_chunks = usable
    else:
        step = len(usable) / max_sample
        sample_chunks = [usable[int(i * step)] for i in range(max_sample)]
    sample = "\n---\n".join(sample_chunks)
    if len(sample) > 12000:
        sample = sample[:12000]

    try:
        import dotenv
        from langchain_openai import ChatOpenAI
        dotenv.load_dotenv()
        fact_llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"), timeout=20)
        result = fact_llm.invoke([
            {"role": "system", "content": _FACT_EXTRACTION_PROMPT},
            {"role": "user", "content": (
                f"Company: {company} ({ticker})\n"
                f"Document: {filename}\n"
                f"Fiscal period: {period}\n\n"
                f"Text:\n{sample}"
            )},
        ])
        raw = (result.content or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        facts = json.loads(raw)
        if not isinstance(facts, list):
            facts = []

        # Enrich with entity metadata
        for fact in facts:
            fact.setdefault("ticker", ticker)
            fact.setdefault("source_doc_id", doc_id)
            fact.setdefault("source_filename", filename)
            fact.setdefault("source_tier", "document_extraction")
            fact.setdefault("node_type", "document_fact")
            # Default field mapping for common fact types
            ft = fact.get("fact_type", "")
            if not fact.get("field") and ft:
                field_map = {
                    "revenue": "base_revenue",
                    "net_income": "net_income",
                    "operating_income": "operating_income",
                    "gross_profit": "gross_profit",
                    "eps": "eps",
                    "shares_outstanding": "shares_outstanding",
                    "fcff_margin": "fcff_margin",
                    "wacc_signal": "wacc",
                    "risk_factor": "risk_factors",
                    "competitive_moat": "competitive_moat",
                    "guidance": "guidance",
                    "capital_allocation": "capex_ratio",
                    "debt_metric": "net_debt",
                    "growth_rate": "revenue_growth",
                    "margin": "ebitda_margin",
                    "valuation_metric": "valuation_multiple",
                }
                fact["field"] = field_map.get(ft, ft)

        _rag_console("📊", "Fact extraction",
                     f"{company} ({ticker}): {len(facts)} facts from {filename}",
                     style="green")
        return facts

    except Exception as exc:  # noqa: BLE001
        logger.warning("Document fact extraction failed for %s: %s", filename, exc)
        _rag_console("📊", "Fact extraction failed",
                     f"{filename}: {exc}", style="red")
        return []


def extract_and_ingest_facts(
    doc_id: str,
    session_id: str,
) -> list["IngestResult"]:
    """Extract facts from an already-ingested document and write to KG.

    Call this after ``ingest_document()`` completes. Reads the document's
    chunks from ChromaDB, extracts structured facts via LLM, and ingests
    them through the unified ``kg.ingest_facts()`` pipeline.

    Returns the list of IngestResult objects for auditing.
    """
    from kg import ingest_facts, DocumentFact  # noqa: PLC0415

    # Look up the document metadata
    entry = get_doc_status(doc_id) or {}
    filename = entry.get("filename", "?")
    entity = {
        "ticker": (entry.get("ticker") or ""),
        "company": (entry.get("company") or ""),
        "doc_type": (entry.get("doc_type") or ""),
        "fiscal_period": (entry.get("fiscal_period") or ""),
    }
    ticker = entity.get("ticker", "")
    if not ticker or ticker == "?":
        _rag_console("📊", "Fact → KG skipped", f"no ticker for {filename}", style="dim")
        return []

    # Retrieve chunks from ChromaDB — MUST filter by doc_id, otherwise we
    # sample the wrong document's chunks and extract facts for the wrong
    # company (e.g. AAPL's 10-Q yielding META's revenue). The previous
    # `hasattr(collection, "_metadata")` guard always evaluated False, so the
    # where-clause was dropped and get() returned the first N chunks of the
    # entire collection.
    try:
        collection = _get_collection()
        # Pull the FULL document (not just the first 12 chunks) so the
        # extractor can sample across it — financials live mid-document, not
        # on the cover page. _extract_document_facts even-samples this set.
        result = collection.get(
            where={"doc_id": doc_id},
            include=["documents", "metadatas"],
            limit=500,
        )
        docs = result.get("documents", []) or []
        metas = result.get("metadatas", []) or []
        # Order by chunk_index so the cross-document sampling is meaningful
        # (ChromaDB.get does not guarantee insertion order).
        paired = list(zip(docs, metas))
        paired.sort(key=lambda p: (p[1] or {}).get("chunk_index", 0))
        chunks = [d for d, _ in paired if d]
        if not chunks:
            # Fallback: scan and filter by doc_id (older chunks may predate
            # the doc_id metadata column).
            all_ids = collection.get(include=["documents", "metadatas"], limit=2000)
            scanned = []
            for i, meta in enumerate(all_ids.get("metadatas", []) or []):
                if meta and meta.get("doc_id") == doc_id:
                    doc_text = (all_ids.get("documents") or [])[i]
                    if doc_text:
                        scanned.append((meta.get("chunk_index", i), doc_text))
            scanned.sort(key=lambda p: p[0])
            chunks = [t for _, t in scanned]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to retrieve chunks for fact extraction doc_id=%s: %s", doc_id, exc)
        return []

    if not chunks:
        _rag_console("📊", "Fact → KG skipped", f"no chunks for {filename}", style="dim")
        return []

    # Extract facts
    raw_facts = _extract_document_facts(chunks, entity, doc_id, filename)
    if not raw_facts:
        return []

    # fact_type values that map to dedicated KG Layer 2 node types.
    # All others stay as "document_fact".
    _FACT_TYPE_TO_NODE_TYPE: dict[str, str] = {
        "guidance": "guidance",
        "risk_factor": "risk_factor",
        "competitive_moat": "competitive_moat",
        "capital_allocation": "capital_allocation",
    }

    # Convert raw dicts → DocumentFact objects
    document_facts = []
    for raw in raw_facts:
        try:
            fact_type = raw.get("fact_type", "other")
            node_type = _FACT_TYPE_TO_NODE_TYPE.get(fact_type, "document_fact")
            fact = DocumentFact(
                ticker=raw.get("ticker", ticker),
                fact_type=fact_type,
                field=raw.get("field", raw.get("fact_type", "other")),
                value=raw.get("value"),
                value_text=raw.get("value_text", ""),
                fiscal_period=raw.get("fiscal_period", ""),
                confidence=float(raw.get("confidence", 0.6)),
                source_doc_id=doc_id,
                source_filename=filename,
                source_page=raw.get("source_page"),
                source_tier="document_extraction",
                node_type=node_type,
                metadata={"fact_type": fact_type},
            )
            document_facts.append(fact)
        except (TypeError, ValueError) as exc:
            logger.debug("Skipping invalid fact: %s", exc)
            continue

    # Ingest through the unified pipeline
    results = ingest_facts(document_facts, session_id=session_id)
    _rag_console(
        "📊", "Facts → KG",
        f"{len(results)} facts: "
        f"{sum(1 for r in results if r.status.value == 'accepted')} accepted, "
        f"{sum(1 for r in results if r.status.value == 'duplicate')} dupes, "
        f"{sum(1 for r in results if r.status.value == 'contradiction')} contradicts",
        style="green",
    )
    return results


# ---------------------------------------------------------------------------
# Gate model: classifies RAG results against the user query
# ---------------------------------------------------------------------------

_GATE_PROMPT = """You are a relevance gate. Given a user query and retrieved document chunks
(with metadata about what company/document they come from), classify whether
the documents are sufficient to answer the query.

Return JSON with:
  - "status": one of "relevant", "partial", "mismatch"
  - "covered": array of topics/entities the documents DO cover
  - "missing": array of topics/entities the documents do NOT cover (null if status=relevant)
  - "reasoning": one-sentence explanation
  - "chunk_ids": array of the chunk indices (0-based) that are actually
    relevant to the query — keep this TIGHT (only the chunks that answer it,
    not every retrieved chunk). Their full text is returned to the agent.

Classification rules:
- "relevant": the chunks contain what's needed. Documents are about the right
  company/topic. ALSO use "relevant" for GENERIC requests that name no specific
  entity — e.g. "analyse these", "summarize the document", "what does this say",
  "give me the highlights". A generic ask + a coherent document set = relevant.
  The user clearly wants the uploaded docs analyzed.
- "partial": chunks cover SOME of what's asked, but key pieces are missing.
  Example: user asks about Apple AND Meta, docs only cover Meta.
  Or user asks for revenue AND margins, docs only have revenue.
  List what is covered AND what is missing.
- "mismatch": ONLY when the query names a SPECIFIC entity that is DIFFERENT from
  the documents. Example: user explicitly asks about "Apple earnings" but all
  chunks are about Meta. There is a NAMED entity conflict.
  Do NOT return mismatch for vague/entity-less queries — those are "relevant".
  Do NOT return mismatch just because the query is short or generic.

Be precise about company names and tickers. A mismatch requires the user to
NAME a different company than the docs — not merely the absence of a match.

Reply with ONLY the JSON object, no explanation."""


def _classify_rag_results(
    query: str,
    results: list[dict],
    ready_docs: list[dict],
) -> dict[str, Any]:
    """Classify RAG results vs query using a small LLM.

    Returns {status, covered, missing, reasoning, chunk_ids}."""
    # Build a compact summary of results with entity metadata for the gate model
    chunks_summary: list[dict] = []
    for i, r in enumerate(results):
        meta = r.get("metadata", {})
        chunks_summary.append({
            "chunk_id": i,
            "source": meta.get("filename", "unknown"),
            "company": meta.get("doc_company"),
            "ticker": meta.get("doc_ticker"),
            "doc_type": meta.get("doc_type"),
            "fiscal_period": meta.get("fiscal_period"),
            "page": meta.get("page"),
            "text_preview": (r.get("text", "") or "")[:300],
        })

    # Also include doc-level summary
    doc_summaries = []
    for d in ready_docs:
        doc_summaries.append({
            "filename": d.get("filename", "unknown"),
            "company": d.get("company"),
            "ticker": d.get("ticker"),
            "doc_type": d.get("doc_type"),
            "fiscal_period": d.get("fiscal_period"),
            "subjects": d.get("subjects"),
        })

    gate_input = json.dumps({
        "query": query,
        "documents": doc_summaries,
        "retrieved_chunks": chunks_summary,
    }, ensure_ascii=False, default=str)

    try:
        import dotenv
        from langchain_openai import ChatOpenAI
        dotenv.load_dotenv()
        gate_llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY"), timeout=15)
        result = gate_llm.invoke([
            {"role": "system", "content": _GATE_PROMPT},
            {"role": "user", "content": gate_input},
        ])
        raw = (result.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        verdict = json.loads(raw)
        # ── Colour verdict display ──────────────────────────────────────
        status = verdict.get("status", "?")
        style_map = {"relevant": "green", "partial": "yellow", "mismatch": "red"}
        v_style = style_map.get(status, "dim")
        covered = verdict.get("covered", [])
        missing = verdict.get("missing") or []
        covered_str = ", ".join(covered[:4]) if covered else "—"
        _rag_console("🚦", "Gate verdict",
                     f"[{v_style}]{status.upper()}[/{v_style}]  covered=[{covered_str}]"
                     + (f"  missing=[{', '.join(missing[:4])}]" if missing else ""),
                     style=v_style)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gate model classification failed: %s", exc)
        # Fallback: return all results as relevant with note
        verdict = {
            "status": "relevant",
            "covered": ["(gate model failed — treating as relevant)"],
            "missing": None,
            "reasoning": "Gate model classification failed; returning all chunks for agent review.",
            "chunk_ids": list(range(len(results))),
        }

    # Ensure chunk_ids are present
    if "chunk_ids" not in verdict:
        verdict["chunk_ids"] = list(range(len(results)))

    # ── Inline FULL chunk text for the chunks the gate selected ─────────────
    # The agent answers directly from these — there is no separate
    # retrieve_tool_result round-trip (chunks are not persisted as tool
    # results). Cap each chunk so the verdict stays a reasonable size; the
    # selected set is small (gate picks the relevant few).
    selected = verdict.get("chunk_ids") or list(range(len(results)))
    chunks_out: list[dict] = []
    for cid in selected:
        if not isinstance(cid, int) or cid < 0 or cid >= len(results):
            continue
        r = results[cid]
        meta = r.get("metadata", {}) or {}
        text = str(r.get("text") or "")
        if len(text) > 2000:
            text = text[:2000] + "…"
        chunks_out.append({
            "chunk_id": cid,
            "source": meta.get("filename", "unknown"),
            "page": meta.get("page"),
            "company": meta.get("doc_company") or "",
            "ticker": meta.get("doc_ticker") or "",
            "text": text,
        })
    verdict["chunks"] = chunks_out

    return verdict


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def _make_search_documents_tool():
    from langchain_core.tools import tool  # local import avoids circular

    @tool
    def search_documents(query: str, skip_gate: bool = False) -> str:
        """Search documents uploaded by the user in this session.
        ALWAYS call this BEFORE search_web for every factual query.

        Args:
            query: what to search for in uploaded documents
            skip_gate: set True to skip the relevance-classification gate model
                (1-2s LLM call). Use when you already know what's in the docs
                from prior turns and don't need the gate's mismatch detection.
                When True, returns raw chunk_ids that you can fetch with
                retrieve_tool_result.

        Returns a relevance verdict. The relevant passages are INLINE in the
        `chunks` array (each has full `text` + source/page/ticker) — read them
        directly, there is no separate fetch step.
        - status 'relevant': all needed content found → answer from `chunks`
        - status 'partial': docs cover some topics; `missing` lists the rest → use `chunks` + search_web for the gaps
        - status 'mismatch': docs are about different entities than the query → tell the user about the discrepancy and ask for clarification
        - status 'gate_skipped': no gate ran (skip_gate=True) → evaluate the `chunks` array yourself from metadata + previews
        - status 'none': no docs uploaded or no matches at all"""
        session_id = _session_ctx.get()
        if not session_id:
            return json.dumps({"status": "none", "message": "No active session — no documents available."})

        # Check if session has any docs
        session_docs = list_docs(session_id)
        ready_docs = [d for d in session_docs if d["status"] == "ready"]
        processing = [d for d in session_docs if d["status"] == "processing"]
        if not ready_docs:
            if processing:
                return json.dumps({
                    "status": "none",
                    "message": "Documents are still being indexed — try again in a few seconds.",
                })
            return json.dumps({"status": "none", "message": "No documents have been uploaded in this session."})

        results = hybrid_search(query, session_id)
        if not results:
            errored = [d for d in session_docs if d["status"] == "error"]
            hint = ""
            if errored:
                hint = f" ({len(errored)} document(s) failed indexing — re-upload them.)"
            return json.dumps({
                "status": "none",
                "message": f"No relevant content found for: {query}{hint}",
            })

        # ── Gate model (skippable): classify relevance of RAG results vs query ──
        if skip_gate:
            # Fast path: return chunk IDs + document metadata so agent can evaluate relevance
            _rag_console("⚡", "Gate skipped", f"{len(results)} chunks — agent evaluates", style="yellow")
            chunk_summaries = []
            for i, r in enumerate(results):
                meta = r.get("metadata", {})
                text = str(r.get("text") or "")
                if len(text) > 2000:
                    text = text[:2000] + "…"
                chunk_summaries.append({
                    "chunk_id": i,
                    "source": meta.get("filename", "unknown"),
                    "company": meta.get("doc_company") or "",
                    "ticker": meta.get("doc_ticker") or "",
                    "doc_type": meta.get("doc_type") or "",
                    "fiscal_period": meta.get("fiscal_period") or "",
                    "page": meta.get("page"),
                    "text": text,
                })
            doc_summaries = []
            for d in ready_docs:
                doc_summaries.append({
                    "filename": d.get("filename", "unknown"),
                    "company": d.get("company") or "",
                    "ticker": d.get("ticker") or "",
                    "doc_type": d.get("doc_type") or "",
                    "fiscal_period": d.get("fiscal_period") or "",
                })
            return json.dumps({
                "status": "gate_skipped",
                "documents": doc_summaries,
                "chunks": chunk_summaries,
                "chunk_ids": list(range(len(results))),
            }, ensure_ascii=False)

        verdict = _classify_rag_results(query, results, ready_docs)

        return json.dumps(verdict, ensure_ascii=False)

    return search_documents


search_documents = _make_search_documents_tool()

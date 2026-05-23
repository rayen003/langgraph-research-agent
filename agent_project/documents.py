"""
RAG pipeline: upload → parse → chunk → embed → store (ChromaDB).

Session-scoped: each session gets its own namespace via metadata filtering.
Hybrid retrieval: dense (ChromaDB cosine) + sparse (BM25) merged with RRF.
"""

import contextvars
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Literal

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction, DefaultEmbeddingFunction
from langchain_text_splitters import RecursiveCharacterTextSplitter
from storage import (
    delete_document_metadata,
    list_documents as list_document_metadata,
    update_document,
    upsert_document,
)

# ---------------------------------------------------------------------------
# Context var — set by agent nodes before invoking tools
# ---------------------------------------------------------------------------

_session_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("rag_session_id", default="")

# ---------------------------------------------------------------------------
# ChromaDB setup
# ---------------------------------------------------------------------------

CHROMA_DIR = Path(__file__).parent / "runs" / "chroma"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = Path(__file__).parent / "runs" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def get_upload_path(doc_id: str, filename: str) -> Path:
    safe_name = Path(filename).name  # strip any path components
    folder = UPLOADS_DIR / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / safe_name

_chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))

_COLLECTION_NAME = "rag_documents"


def _get_collection() -> chromadb.Collection:
    # Use local sentence-transformers embeddings (free, no API required).
    # Falls back to OpenAI if OPENAI_API_KEY is set and USE_OPENAI_EMBEDDINGS=1.
    use_openai = os.getenv("USE_OPENAI_EMBEDDINGS", "0") == "1" and os.getenv("OPENAI_API_KEY")
    if use_openai:
        embedding_fn = OpenAIEmbeddingFunction(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model_name="text-embedding-3-small",
        )
    else:
        embedding_fn = DefaultEmbeddingFunction()
    return _chroma_client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )


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
            },
        )
        entry["chunk_count"] += 1
        try:
            entry["page_count"] = max(int(entry["page_count"]), int(meta.get("page") or 0))
        except (TypeError, ValueError):
            pass

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


# ---------------------------------------------------------------------------
# Ingest (called in a background thread)
# ---------------------------------------------------------------------------


def ingest_document(file_bytes: bytes, filename: str, session_id: str, doc_id: str) -> None:
    try:
        # Persist raw file so the frontend can preview it later
        upload_path = get_upload_path(doc_id, filename)
        upload_path.write_bytes(file_bytes)
        _update_doc(doc_id, upload_path=str(upload_path))

        pages = _parse_file(file_bytes, filename)
        if not pages:
            _update_doc(
                doc_id,
                status="error",
                error="No text could be extracted from the file.",
            )
            return

        chunks = _chunk_pages(pages)
        collection = _get_collection()

        ids, documents, metadatas = [], [], []
        for i, chunk in enumerate(chunks):
            ids.append(f"{doc_id}_{i}")
            documents.append(chunk["text"])
            metadatas.append({
                "doc_id": doc_id,
                "filename": filename,
                "page": chunk["page"],
                "session_id": session_id,
                "chunk_index": i,
            })

        batch_size = 100
        for start in range(0, len(ids), batch_size):
            collection.upsert(
                ids=ids[start : start + batch_size],
                documents=documents[start : start + batch_size],
                metadatas=metadatas[start : start + batch_size],
            )

        _update_doc(
            doc_id,
            status="ready",
            chunk_count=len(chunks),
            page_count=max(c["page"] for c in chunks),
            error=None,
        )

    except Exception as exc:  # noqa: BLE001
        _update_doc(doc_id, status="error", error=str(exc))


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
    collection = _get_collection()
    total = collection.count()
    if total == 0:
        return []

    k = min(20, total)
    try:
        dense = collection.query(
            query_texts=[query],
            n_results=k,
            where={"session_id": session_id},
            include=["documents", "metadatas", "distances"],
        )
    except Exception:  # noqa: BLE001
        return []

    docs = dense["documents"][0]
    metas = dense["metadatas"][0]
    distances = dense["distances"][0]

    if not docs:
        return []

    # BM25 over the dense candidates
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        tokenized = [d.lower().split() for d in docs]
        bm25 = BM25Okapi(tokenized)
        bm25_scores = bm25.get_scores(query.lower().split())
    except Exception:  # noqa: BLE001
        # If BM25 fails, fall back to dense-only
        bm25_scores = [0.0] * len(docs)

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

    top = sorted(combined, key=lambda i: combined[i], reverse=True)[:n_results]
    return [{"text": docs[i], "metadata": metas[i]} for i in top]


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def _make_search_documents_tool():
    from langchain_core.tools import tool  # local import avoids circular

    @tool
    def search_documents(query: str) -> str:
        """Search documents uploaded by the user in this session.
        Use when the user asks about content from their uploaded files (PDFs, spreadsheets, etc.).
        Returns the most relevant passages with source attribution."""
        session_id = _session_ctx.get()
        if not session_id:
            return json.dumps({"error": "No active session — no documents available."})

        # Check if session has any docs
        session_docs = list_docs(session_id)
        ready_docs = [d for d in session_docs if d["status"] == "ready"]
        if not ready_docs:
            return json.dumps({"results": [], "message": "No documents have been uploaded in this session."})

        results = hybrid_search(query, session_id)
        if not results:
            return json.dumps({"results": [], "message": f"No relevant content found for: {query}"})

        formatted = []
        for r in results:
            meta = r["metadata"]
            formatted.append({
                "source": meta.get("filename", "unknown"),
                "page": meta.get("page", "?"),
                "text": r["text"],
            })

        summary = f"Found {len(formatted)} relevant passage(s) from uploaded documents."
        return json.dumps({"results": formatted, "summary": summary}, ensure_ascii=False)

    return search_documents


search_documents = _make_search_documents_tool()

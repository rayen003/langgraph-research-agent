"""RAG store repair helpers — embedding dimension mismatch detection."""

from __future__ import annotations

import pytest


def test_embedding_mismatch_detection():
    from documents import _is_embedding_mismatch, _is_embedding_mismatch_text

    exc = RuntimeError("Embedding dimension 1536 does not match collection dimensionality 384")
    assert _is_embedding_mismatch(exc) is True
    assert _is_embedding_mismatch_text(str(exc)) is True
    assert _is_embedding_mismatch(RuntimeError("network timeout")) is False


def test_collection_dim_mismatch_legacy_local_store(monkeypatch):
    from documents import _collection_dim_mismatch, _expected_embedding_dim

    class FakeCollection:
        metadata = {"hnsw:space": "cosine"}
        count = lambda self: 42

    monkeypatch.setenv("USE_OPENAI_EMBEDDINGS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _expected_embedding_dim() == 1536
    assert _collection_dim_mismatch(FakeCollection()) is True


def test_collection_dim_mismatch_matches_metadata(monkeypatch):
    from documents import _collection_dim_mismatch

    class FakeCollection:
        metadata = {"embedding_dim": "1536"}
        count = lambda self: 10

    monkeypatch.setenv("USE_OPENAI_EMBEDDINGS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _collection_dim_mismatch(FakeCollection()) is False

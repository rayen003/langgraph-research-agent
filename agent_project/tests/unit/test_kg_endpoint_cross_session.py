"""Regression tests for cross-session KG endpoint.

Before the fix, GET /kg/{session_id} filtered nodes to `session_id` only.
A DCF rerun that targeted a *new* session wrote its nodes there, and when
the KG panel called /kg/old_session it saw an empty graph — "KG deleted".

After the fix both endpoints return ALL nodes regardless of session_id.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-placeholder")
os.environ.setdefault("EXA_API_KEY", "sk-test-placeholder")

import pytest
from fastapi.testclient import TestClient

# The server imports `storage` (plain name), not `agent_project.storage`, so
# we must patch the same module object the server resolves at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import storage  # noqa: E402  (must come after sys.path tweak)
from server import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=True)


def _node(node_id: str, session_id: str, ticker: str, node_type: str) -> dict:
    return {
        "id": node_id,
        "session_id": session_id,
        "ticker": ticker,
        "node_type": node_type,
        "field": "test",
        "value": 1.0,
        "confidence": 0.9,
        "source": "agent",
        "input_hash": None,
        "run_id": None,
        "created_at": 0.0,
        "updated_at": 0.0,
    }


def _edge(edge_id: str, session_id: str, src: str, tgt: str) -> dict:
    return {
        "id": edge_id,
        "session_id": session_id,
        "src_id": src,
        "tgt_id": tgt,
        "relation": "HAS_RUN",
        "confidence": 0.9,
        "source": "agent",
        "created_at": 0.0,
    }


# Nodes from two different sessions
SESSION_A_NODES = [
    _node("aapl-co", "session-a", "AAPL", "company"),
    _node("aapl-run1", "session-a", "AAPL", "dcf_run"),
    _node("aapl-wacc", "session-a", "AAPL", "run_assumption"),
]
SESSION_B_NODES = [
    # Rerun: new session, same ticker. These must NOT be invisible from session-a view.
    _node("aapl-run2", "session-b", "AAPL", "dcf_run"),
    _node("aapl-wacc2", "session-b", "AAPL", "run_assumption"),
]
ALL_NODES = SESSION_A_NODES + SESSION_B_NODES

SESSION_A_EDGES = [_edge("e1", "session-a", "aapl-co", "aapl-run1")]
SESSION_B_EDGES = [_edge("e2", "session-b", "aapl-run2", "aapl-wacc2")]
ALL_EDGES = SESSION_A_EDGES + SESSION_B_EDGES


def test_kg_full_endpoint_is_cross_session(monkeypatch):
    """GET /kg/{session_id} must return nodes from ALL sessions, not just session_id."""
    monkeypatch.setattr(storage, "list_kg_nodes", lambda **_kw: ALL_NODES)
    monkeypatch.setattr(storage, "list_kg_edges", lambda **_kw: ALL_EDGES)

    # Request with session-a in the URL — must still return session-b nodes.
    resp = client.get("/kg/session-a")
    assert resp.status_code == 200
    data = resp.json()

    returned_ids = {n["id"] for n in data["nodes"]}
    assert "aapl-run2" in returned_ids, "session-b rerun node must be visible from session-a view"
    assert "aapl-wacc2" in returned_ids
    # Also keeps session-a nodes
    assert "aapl-co" in returned_ids
    assert len(data["edges"]) == 2


def test_kg_full_endpoint_does_not_filter_by_session_id(monkeypatch):
    """Regardless of which session_id is passed in the URL, result is the same."""
    monkeypatch.setattr(storage, "list_kg_nodes", lambda **_kw: ALL_NODES)
    monkeypatch.setattr(storage, "list_kg_edges", lambda **_kw: ALL_EDGES)

    resp_a = client.get("/kg/session-a")
    resp_b = client.get("/kg/session-b")
    resp_new = client.get("/kg/brand-new-session")

    # All three URLs return identical data.
    ids_a = {n["id"] for n in resp_a.json()["nodes"]}
    ids_b = {n["id"] for n in resp_b.json()["nodes"]}
    ids_new = {n["id"] for n in resp_new.json()["nodes"]}
    assert ids_a == ids_b == ids_new == {n["id"] for n in ALL_NODES}


def test_kg_subgraph_endpoint_is_cross_session(monkeypatch):
    """GET /kg/{session_id}/subgraph/{ticker} must span all sessions for the ticker."""
    aapl_nodes = ALL_NODES  # all are AAPL
    aapl_edges = ALL_EDGES

    monkeypatch.setattr(storage, "list_kg_nodes", lambda **_kw: aapl_nodes)
    monkeypatch.setattr(storage, "list_kg_edges", lambda **_kw: aapl_edges)

    resp = client.get("/kg/session-a/subgraph/AAPL")
    assert resp.status_code == 200
    data = resp.json()
    returned_ids = {n["id"] for n in data["nodes"]}
    # Session-b rerun node must appear in the subgraph.
    assert "aapl-run2" in returned_ids
    assert "aapl-wacc2" in returned_ids

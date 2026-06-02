"""Tests for session sidebar layout persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "agent.db"
        monkeypatch.setattr(storage, "DB_PATH", db_path)
        monkeypatch.setattr(storage, "RUNS_DIR", Path(tmp))
        storage.init_db()
        yield storage


def test_session_layout_roundtrip(isolated_db):
    storage = isolated_db
    payload = {
        "groups": [
            {
                "id": "g_1",
                "name": "AAPL work",
                "color": "blue",
                "collapsed": False,
                "sort_order": 0,
                "created_at": "2026-05-31T12:00:00+00:00",
            }
        ],
        "sessions": [
            {
                "session_id": "s_1",
                "title_override": "Apple DCF",
                "pinned": True,
                "group_id": "g_1",
                "sort_order": 0,
                "updated_at": "2026-05-31T12:00:00+00:00",
            }
        ],
    }
    storage.replace_session_layout(**payload)
    out = storage.get_session_layout()
    assert len(out["groups"]) == 1
    assert out["groups"][0]["name"] == "AAPL work"
    assert len(out["sessions"]) == 1
    assert out["sessions"][0]["pinned"] is True
    assert out["sessions"][0]["group_id"] == "g_1"

    storage.replace_session_layout(groups=[], sessions=[])
    assert storage.get_session_layout() == {"groups": [], "sessions": []}

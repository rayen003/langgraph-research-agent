"""Durable LangGraph checkpoint factory supporting sync and async callers."""

from __future__ import annotations

import asyncio
import atexit
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.sqlite import SqliteSaver


class DurableSqliteSaver(SqliteSaver):
    """SqliteSaver with async methods delegated to worker threads."""

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        rows = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for row in rows:
            yield row

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


_LOCK = threading.Lock()
_SAVERS: dict[tuple[str, str], DurableSqliteSaver] = {}
_CONNECTIONS: list[sqlite3.Connection] = []


def checkpoint_directory() -> Path:
    configured = os.getenv("AGENT_CHECKPOINT_DIR")
    path = Path(configured).expanduser() if configured else Path(__file__).resolve().parent / "runtime" / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def durable_checkpointer(graph_id: str) -> DurableSqliteSaver:
    """Return process-wide durable saver dedicated to one compiled graph."""
    directory = checkpoint_directory()
    key = (str(directory.resolve()), graph_id)
    with _LOCK:
        existing = _SAVERS.get(key)
        if existing is not None:
            return existing
        connection = sqlite3.connect(directory / f"{graph_id}.sqlite3", check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        saver = DurableSqliteSaver(connection)
        _CONNECTIONS.append(connection)
        _SAVERS[key] = saver
        return saver


def close_checkpointers() -> None:
    with _LOCK:
        for connection in _CONNECTIONS:
            connection.close()
        _CONNECTIONS.clear()
        _SAVERS.clear()


atexit.register(close_checkpointers)


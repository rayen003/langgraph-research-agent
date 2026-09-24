import asyncio
import sqlite3
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph

from checkpointing import DurableSqliteSaver


class _State(TypedDict):
    value: int


def _graph(saver: DurableSqliteSaver):
    builder = StateGraph(_State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=saver)


def _saver(path) -> tuple[sqlite3.Connection, DurableSqliteSaver]:
    connection = sqlite3.connect(path, check_same_thread=False)
    return connection, DurableSqliteSaver(connection)


def test_sqlite_checkpointer_restores_state_after_connection_restart(tmp_path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    config = {"configurable": {"thread_id": "durable-thread"}}
    first_connection, first_saver = _saver(path)
    assert _graph(first_saver).invoke({"value": 1}, config=config)["value"] == 2
    first_connection.close()

    second_connection, second_saver = _saver(path)
    snapshot = _graph(second_saver).get_state(config)
    second_connection.close()

    assert snapshot.values["value"] == 2


def test_sqlite_checkpointer_supports_async_graph_invocation(tmp_path) -> None:
    connection, saver = _saver(tmp_path / "async-checkpoints.sqlite3")
    config = {"configurable": {"thread_id": "async-thread"}}

    async def run() -> dict:
        return await _graph(saver).ainvoke({"value": 4}, config=config)

    result = asyncio.run(run())
    connection.close()

    assert result["value"] == 5


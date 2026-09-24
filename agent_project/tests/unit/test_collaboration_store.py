from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def collaboration_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(storage, "DB_PATH", Path(tmp) / "agent.db")
        storage.init_db()
        yield


def test_channel_message_mentions_create_notifications(collaboration_db) -> None:
    import collaboration_store as store

    workspace = store.ensure_workspace("workspace:finance")
    store.upsert_actor({
        "actor_id": "agent:valuation",
        "kind": "agent",
        "display_name": "Valuation Agent",
        "handle": "valuation",
        "capabilities": ["run_dcf_workflow"],
    })
    store.add_membership(workspace["workspace_id"], "agent:valuation", "editor")
    channel = store.create_channel("workspace:finance", {"name": "apple-research"}, "human:local")
    message = store.create_message(channel["channel_id"], "workspace:finance", "human:local", {
        "body": "@valuation review #memo:aapl@v3",
        "mentions": [{
            "mention_id": "mention:1",
            "kind": "agent",
            "target_id": "agent:valuation",
            "label": "@valuation",
            "start": 0,
            "end": 10,
            "requested_action": "review",
            "context_refs": ["memo:aapl:v3"],
        }],
        "object_version_ids": ["memo:aapl:v3"],
    })

    assert store.list_messages(channel["channel_id"])[0]["message_id"] == message["message_id"]
    notification = store.list_notifications("agent:valuation")[0]
    assert notification["event_type"] == "mention"
    assert notification["target_id"] == message["message_id"]


def test_object_comment_and_approval_keep_exact_version(collaboration_db) -> None:
    import collaboration_store as store

    store.ensure_workspace("workspace:finance")
    comment = store.create_object_comment("workspace:finance", "memo:aapl", {
        "object_version_id": "memo:aapl:v3",
        "block_id": "recommendation",
        "body": "Show downside sensitivity.",
    }, "human:local")
    approval = store.create_approval("workspace:finance", "memo:aapl", {
        "object_version_id": "memo:aapl:v3",
        "assigned_to": "human:pm",
        "note": "IC sign-off",
    }, "human:local")

    assert store.list_object_comments("memo:aapl")[0]["object_version_id"] == "memo:aapl:v3"
    assert comment["block_id"] == "recommendation"
    assert approval["object_version_id"] == "memo:aapl:v3"
    assert approval["status"] == "pending"


def test_review_lifecycle_and_notification_state(collaboration_db) -> None:
    import collaboration_store as store

    store.ensure_workspace("workspace:finance")
    store.upsert_actor({"actor_id": "human:pm", "kind": "human", "display_name": "Portfolio Manager", "handle": "pm"})
    store.add_membership("workspace:finance", "human:pm", "reviewer")

    suggestion = store.create_suggestion("workspace:finance", "memo:aapl", {
        "base_version_id": "memo:aapl:v3", "block_id": "thesis",
        "patch": {"replacement": "Tighter thesis"}, "rationale": "Remove repetition",
    }, "human:pm")
    assert store.decide_suggestion(suggestion["suggestion_id"], "accepted", "human:local")["status"] == "accepted"

    comment = store.create_object_comment("workspace:finance", "memo:aapl", {
        "object_version_id": "memo:aapl:v3", "body": "Check source.",
    }, "human:pm")
    assert store.resolve_comment(comment["comment_id"])["status"] == "resolved"

    approval = store.create_approval("workspace:finance", "memo:aapl", {
        "object_version_id": "memo:aapl:v3", "assigned_to": "human:pm", "note": "IC review",
    }, "human:local")
    notification = store.list_notifications("human:pm")[0]
    assert notification["event_type"] == "approval_requested"
    assert store.mark_notification_read(notification["notification_id"], actor_id="human:pm")["read"] is True
    assert store.decide_approval(approval["approval_id"], "approved", "human:pm")["status"] == "approved"


def test_existing_workspace_bootstrap_cannot_grant_owner(collaboration_db) -> None:
    import collaboration_store as store

    store.ensure_workspace("workspace:finance", actor_id="human:owner")
    store.ensure_workspace("workspace:finance", actor_id="human:intruder")
    with pytest.raises(PermissionError):
        store.require_workspace_role("workspace:finance", "human:intruder", "viewer")


def test_agent_assignment_response_creates_versioned_result(collaboration_db) -> None:
    import asyncio
    import server
    import storage
    import collaboration_store as store

    store.ensure_workspace("workspace:finance")
    channel = store.create_channel("workspace:finance", {"name": "research"}, "human:local")
    loop = asyncio.new_event_loop()
    try:
        run = server.RunState("thread_assignment", loop, "Research Apple", "auto", "workspace:finance")
        run.collaboration_channel_id = channel["channel_id"]
        run.collaboration_workspace_id = "workspace:finance"
        run.collaboration_actor_id = "agent:research"
        run.collaboration_object_version_ids = ["document:aapl:v1"]
        assignment = store.create_assignment("workspace:finance", {
            "title": "Research Apple",
            "description": "Research Apple",
            "assigned_to": "agent:research",
            "object_version_ids": ["document:aapl:v1"],
            "channel_id": channel["channel_id"],
            "source_message_id": "message:source",
            "thread_id": run.thread_id,
        }, "human:local")
        run.collaboration_assignment_id = assignment["assignment_id"]
        storage.upsert_job(thread_id=run.thread_id, query=run.query, mode=run.mode, status=run.status, session_id=run.session_id)

        run.memo_hitl_payload = {"draft": {"title": "Draft"}}
        server._make_event_bridge(run)({"type": "chat_complete", "content": "Draft awaits review."})
        assert store.list_assignments("workspace:finance")[0]["status"] == "open"
        assert not store.list_messages(channel["channel_id"])
        run.memo_hitl_payload = None
        server._make_event_bridge(run)({"type": "chat_complete", "content": "Cited research result."})

        messages = store.list_messages(channel["channel_id"])
        assert messages[-1]["actor_id"] == "agent:research"
        assert len(messages[-1]["object_version_ids"]) == 2
        result = storage.get_workspace_object("task_result:thread_assignment")
        assert result is not None
        assert result["source_version_ids"] == ["document:aapl:v1"]
        stored_assignment = store.list_assignments("workspace:finance")[0]
        assert stored_assignment["status"] == "completed"
        assert stored_assignment["thread_id"] == "thread_assignment"
        assert stored_assignment["channel_id"] == channel["channel_id"]
        assert stored_assignment["source_message_id"] == "message:source"
        assert stored_assignment["output_object_version_ids"] == [result["version_id"]]
    finally:
        loop.close()


def test_assignment_execution_links_are_optional_and_appendable(collaboration_db) -> None:
    import collaboration_store as store

    store.ensure_workspace("workspace:finance")
    task = store.create_assignment("workspace:finance", {
        "title": "Review Apple memo",
        "assigned_to": "human:local",
        "object_version_ids": ["memo:aapl:v000001"],
    }, "human:local")

    assert task.get("thread_id") is None
    assert task["output_object_version_ids"] == []

    updated = store.update_assignment(
        task["assignment_id"],
        "completed",
        "human:local",
        thread_id="thread_review",
        output_object_version_ids=["memo:aapl:v000002"],
    )
    assert updated is not None
    assert updated["thread_id"] == "thread_review"
    assert updated["output_object_version_ids"] == ["memo:aapl:v000002"]


def test_manual_task_starts_and_completes_without_channel(collaboration_db, monkeypatch) -> None:
    import asyncio
    import collaboration_store as store
    import server

    store.ensure_workspace("workspace:manual")
    async def execute(thread_id, *_args):
        run = server._run_registry[thread_id]
        server._make_event_bridge(run)({"type": "chat_complete", "content": "Completed research."})

    monkeypatch.setattr(server, "_run_agent_task", execute)
    async def scenario():
        result = await server.create_collaboration_assignment("workspace:manual", server.AssignmentCreateRequest(
            title="Research request", description="Compare companies", assigned_to="agent:research", start_now=True,
        ))
        await asyncio.sleep(0)
        return result

    result = asyncio.run(scenario())
    server._run_registry.pop(result["thread_id"], None)
    task = store.list_assignments("workspace:manual")[0]
    assert task["status"] == "completed"
    assert task["output_object_version_ids"]
    assert task["channel_id"] is None


def test_agent_channel_mention_creates_tracked_task_context(
    collaboration_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import server
    import collaboration_store as store

    async def _noop_run(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_run_agent_task", _noop_run)
    store.ensure_workspace("workspace:finance")
    channel = store.create_channel("workspace:finance", {"name": "apple"}, "human:local")
    request = server.CollaborationMessageRequest(
        body="@research prepare an Apple earnings memo",
        mentions=[{
            "mention_id": "mention:task",
            "kind": "agent",
            "target_id": "agent:research",
            "label": "@research",
            "start": 0,
            "end": 9,
            "requested_action": "execute",
            "context_refs": ["filing:aapl:v000001"],
        }],
        object_version_ids=["filing:aapl:v000001"],
    )

    response = asyncio.run(server.create_collaboration_message(
        "workspace:finance", channel["channel_id"], request,
    ))
    task = response["assignment"]
    run = server._run_registry.pop(task["thread_id"])

    assert task["status"] == "working"
    assert task["channel_id"] == channel["channel_id"]
    assert task["source_message_id"] == response["message_id"]
    assert run.query == request.body
    assert run.collaboration_assignment_id == task["assignment_id"]
    assert run.collaboration_task_context["goal"] == request.body
    assert run.collaboration_task_context["input_object_version_ids"] == ["filing:aapl:v000001"]

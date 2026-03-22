"""Compatibility re-exports — all consumers keep their existing imports unchanged."""

from utils.events import emit_ui_event, set_ui_event_handler
from utils.formatting import (
    format_message,
    format_message_content,
    format_messages,
    format_plan,
    format_tool_call,
    format_tool_error,
    format_tool_result,
    show_prompt,
    stream_agent,
)
from utils.persistence import (
    BASE_DIR,
    RUNS_DIR,
    console,
    get_artifacts_dir,
    get_next_pending_step,
    get_run_dir,
    has_pending_steps,
    list_artifact_paths,
    mark_step,
    persist_context_item,
    persist_tool_result,
    save_artifact_file,
    save_final_report,
    save_plan,
    set_thread_id,
)

__all__ = [
    # events
    "emit_ui_event",
    "set_ui_event_handler",
    # persistence
    "BASE_DIR",
    "RUNS_DIR",
    "console",
    "get_artifacts_dir",
    "get_next_pending_step",
    "get_run_dir",
    "has_pending_steps",
    "list_artifact_paths",
    "mark_step",
    "persist_context_item",
    "persist_tool_result",
    "save_artifact_file",
    "save_final_report",
    "save_plan",
    "set_thread_id",
    # formatting
    "format_message",
    "format_message_content",
    "format_messages",
    "format_plan",
    "format_tool_call",
    "format_tool_error",
    "format_tool_result",
    "show_prompt",
    "stream_agent",
]

"""Resolve and collect existing tool-result and workspace-object references."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from langchain_core.messages import ToolMessage

from domain.tool_execution import ToolResult
from utils import get_run_dir


_OBJECT_VERSION_RE = re.compile(r"^(?P<object_id>.+):v(?P<version>\d{6})$")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
_TEXT_OUTPUTS = {"answer", "report", "table"}


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values:
        value = str(raw or "")
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve_ref(ref_id: str) -> dict[str, Any]:
    """Resolve existing reference without introducing another storage model."""
    ref_id = str(ref_id or "").strip()
    version_match = _OBJECT_VERSION_RE.match(ref_id)
    if version_match:
        from storage import get_workspace_object_version  # noqa: PLC0415

        object_id = version_match.group("object_id")
        version = int(version_match.group("version"))
        payload = get_workspace_object_version(object_id, version)
        if payload is None:
            raise KeyError(f"Unknown object version reference: {ref_id}")
        return {"ref_id": ref_id, "kind": "object_version", "payload": payload}

    tool_path = get_run_dir() / "tool_results" / f"{ref_id}.json"
    if tool_path.exists():
        try:
            payload = json.loads(tool_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt tool result reference: {ref_id}") from exc
        return {
            "ref_id": ref_id,
            "kind": "tool_result",
            "payload": payload,
            "payload_ref": str(tool_path),
        }

    from storage import get_workspace_object  # noqa: PLC0415

    payload = get_workspace_object(ref_id)
    if payload is not None:
        return {"ref_id": ref_id, "kind": "workspace_object", "payload": payload}
    raise KeyError(f"Unknown reference: {ref_id}")


def _normalized_result(message: ToolMessage) -> ToolResult | None:
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    raw = artifact.get("tool_result") if isinstance(artifact, dict) else None
    if isinstance(raw, dict):
        try:
            return ToolResult.model_validate(raw)
        except ValueError:
            pass
    try:
        return ToolResult.model_validate_json(str(message.content))
    except (ValueError, TypeError):
        return None


def collect_refs(messages: Iterable[ToolMessage]) -> dict[str, list[str]]:
    """Collect payload and artifact references from normalized or legacy messages."""
    result_refs: list[str] = []
    artifact_refs: list[str] = []
    artifact_paths: list[str] = []
    for message in messages:
        normalized = _normalized_result(message)
        payload = _json_dict(message.content) or {}
        if normalized is not None:
            output = _json_dict(normalized.output) or {}
            result_refs.extend([
                str(output.get("tool_result_id") or ""),
                str(payload.get("tool_result_id") or ""),
            ])
            artifact_refs.extend(normalized.object_version_ids)
            artifact_paths.extend(normalized.artifact_paths)
            payload = {**output, **payload}
        result_refs.append(str(payload.get("tool_result_id") or ""))
        artifact_refs.extend(str(value) for value in (payload.get("object_version_ids") or []))
        artifact_paths.extend(str(value) for value in (payload.get("artifact_paths") or []))
    return {
        "result_refs": _unique(result_refs),
        "artifact_refs": _unique(artifact_refs),
        "artifact_paths": _unique(artifact_paths),
    }


def merge_refs(current: dict[str, Any], observed: dict[str, list[str]]) -> dict[str, Any]:
    """Append references to current-turn ledger while preserving first-seen order."""
    return {
        **current,
        "result_refs": _unique([*(current.get("result_refs") or []), *observed.get("result_refs", [])]),
        "artifact_refs": _unique([*(current.get("artifact_refs") or []), *observed.get("artifact_refs", [])]),
        "artifact_paths": _unique([*(current.get("artifact_paths") or []), *observed.get("artifact_paths", [])]),
    }


def completion_gaps(
    required_outputs: Iterable[str],
    *,
    final_text: str,
    artifact_paths: Iterable[str],
    artifact_refs: Iterable[str],
) -> list[str]:
    """Return generic requested outputs still missing from current turn."""
    required = _unique(str(value) for value in required_outputs)
    paths = [Path(path) for path in artifact_paths]
    gaps: list[str] = []
    for output in required:
        if output in _TEXT_OUTPUTS and not final_text.strip():
            gaps.append(output)
        elif output == "chart" and not (
            any(path.suffix.lower() in _IMAGE_SUFFIXES for path in paths)
            or any(str(ref).startswith("chart:") for ref in artifact_refs)
        ):
            gaps.append(output)
        elif output == "pdf" and not any(path.suffix.lower() == ".pdf" for path in paths):
            gaps.append(output)
        elif output not in {*_TEXT_OUTPUTS, "chart", "pdf"} and not artifact_refs:
            gaps.append(output)
    return gaps

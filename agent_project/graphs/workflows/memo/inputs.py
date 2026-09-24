"""Resolve memo tool arguments and approved workflow context into typed sources."""

from __future__ import annotations

import json
from typing import Any

from storage import get_workspace_object

from .state import MemoBrief, MemoSource


def resolve_memo_inputs(
    sources: list[dict[str, Any]] | None,
    brief: dict[str, Any] | str,
    *,
    workflow_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = workflow_context or {}
    if isinstance(brief, str):
        brief = {"title": brief}
    brief = dict(brief or {})
    brief.setdefault("title", f"{context.get('company_name') or 'Investment'} memo")
    brief.setdefault("company_name", context.get("company_name"))
    brief.setdefault("objective", context.get("user_intent") or "Produce decision-ready investment memo.")
    brief.setdefault("focus_areas", context.get("focus_areas") or [])
    output_requirements = context.get("output_requirements") or {}
    if output_requirements.get("audience") == "investment_committee":
        brief.setdefault("audience", "investment_committee")

    resolved = list(sources or [])
    existing_ids = {source.get("object_id") for source in resolved}
    for object_id in context.get("selected_artifact_ids") or []:
        if object_id in existing_ids:
            continue
        obj = get_workspace_object(str(object_id))
        if not obj:
            continue
        resolved.append(MemoSource(
            type="workspace_object",
            title=str(obj.get("title") or object_id),
            summary=str(obj.get("summary") or ""),
            content=json.dumps(obj.get("payload") or {}, ensure_ascii=False, default=str),
            object_id=str(object_id),
            version_id=obj.get("version_id"),
            source_refs=obj.get("source_refs") or [],
        ).model_dump())
    for artifact in context.get("available_artifacts") or []:
        object_id = artifact.get("object_id")
        if object_id and object_id not in existing_ids and object_id in set(context.get("selected_artifact_ids") or []):
            resolved.append(MemoSource(
                type="workspace_object",
                title=str(artifact.get("title") or object_id),
                summary=str(artifact.get("summary") or ""),
                object_id=str(object_id),
                version_id=artifact.get("version_id"),
            ).model_dump())
    for doc_id in context.get("selected_doc_ids") or []:
        resolved.append(MemoSource(type="document", title=str(doc_id), doc_id=str(doc_id)).model_dump())
    return [MemoSource.model_validate(source).model_dump() for source in resolved], MemoBrief.model_validate(brief).model_dump()


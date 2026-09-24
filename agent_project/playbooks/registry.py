"""Local markdown playbook registry."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


class PlaybookNotFoundError(KeyError):
    """Raised when a requested playbook id is not registered."""


class PlaybookRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).parent

    def list_cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/*.md")):
            if path.name.upper() == "README.MD":
                continue
            try:
                meta, _body = self._read_playbook(path)
            except ValueError:
                continue
            cards.append({
                "id": meta.get("id"),
                "kind": meta.get("kind"),
                "title": meta.get("title"),
                "summary": meta.get("summary"),
                "route_levels": meta.get("route_levels") or [],
            })
        return [card for card in cards if card.get("id")]

    def load_packet(self, playbook_id: str) -> dict[str, Any]:
        path = self._path_for(playbook_id)
        meta, body = self._read_playbook(path)
        return {
            "id": meta.get("id") or playbook_id,
            "kind": meta.get("kind") or "micro",
            "version": str(meta.get("version") or "0.1"),
            "title": meta.get("title") or playbook_id,
            "summary": meta.get("summary") or "",
            "route_levels": meta.get("route_levels") or [],
            "allowed_tools": meta.get("allowed_tools") or [],
            "allowed_workflows": meta.get("allowed_workflows") or [],
            "tool_budget": meta.get("tool_budget") or {},
            "object_policy": meta.get("object_policy") or {},
            "required_outputs": meta.get("required_outputs") or {},
            "validators": meta.get("validators") or [],
            "stop_conditions": meta.get("stop_conditions") or [],
            "instructions": self._compact_body(body),
        }

    def _path_for(self, playbook_id: str) -> Path:
        for path in sorted(self.root.glob("*/*.md")):
            try:
                meta, _body = self._read_playbook(path)
            except ValueError:
                continue
            if meta.get("id") == playbook_id:
                return path
        raise PlaybookNotFoundError(playbook_id)

    @staticmethod
    @lru_cache(maxsize=128)
    def _read_playbook(path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"missing frontmatter: {path}")
        try:
            _start, raw_meta, body = text.split("---", 2)
        except ValueError as exc:
            raise ValueError(f"invalid frontmatter: {path}") from exc
        meta = yaml.safe_load(raw_meta) or {}
        if not isinstance(meta, dict):
            raise ValueError(f"frontmatter must be mapping: {path}")
        return meta, body.strip()

    @staticmethod
    def _compact_body(body: str, *, max_chars: int = 2400) -> str:
        compact = "\n".join(line.rstrip() for line in body.splitlines() if line.strip())
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars].rstrip() + "\n..."

"""Process-wide capability execution boundary."""

from __future__ import annotations

from functools import lru_cache

from tool_catalog import build_current_capability_registry
from tool_runtime import CapabilityDispatcher


@lru_cache(maxsize=1)
def get_capability_dispatcher() -> CapabilityDispatcher:
    from tools import ALL_TOOLS  # noqa: PLC0415

    return CapabilityDispatcher(ALL_TOOLS, build_current_capability_registry())


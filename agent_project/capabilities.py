"""Compatibility exports for canonical capability catalog.

New code imports registry builders from ``tool_catalog``. This module remains
while callers migrate from pre-consolidation capability imports.
"""

from domain.tool_execution import CapabilitySpec
from tool_catalog import build_current_capability_registry
from tool_runtime import CapabilityRegistry


Capability = CapabilitySpec


def current_registry() -> CapabilityRegistry:
    return build_current_capability_registry()


__all__ = ["Capability", "CapabilityRegistry", "CapabilitySpec", "current_registry"]

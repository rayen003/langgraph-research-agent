"""LangGraph version compatibility — single import point for Command.

Command was introduced in langgraph 0.2.57.  Older installs (e.g. 0.2.45
that ships with base Anaconda) raise ImportError.  We detect this once at
import time and give a clear message rather than a cryptic mid-request crash.
"""

from __future__ import annotations

try:
    from langgraph.types import Command  # noqa: F401
except ImportError as _e:
    import importlib.metadata as _meta

    try:
        _ver = _meta.version("langgraph")
    except Exception:
        _ver = "unknown"

    raise RuntimeError(
        f"langgraph {_ver} is too old — Command requires >=0.2.57.\n"
        "Fix: run the server via  ./start.sh  (uses uv env) rather than system Python.\n"
        "Or:  pip install 'langgraph>=0.3.0'  in the active environment."
    ) from _e

__all__ = ["Command"]

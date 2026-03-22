"""UI event hooks consumed by Chainlit and other frontends."""

from typing import Any

_ui_event_handler: Any = None


def set_ui_event_handler(handler: Any) -> None:
    """Register a callback that receives fine-grained execution events."""
    global _ui_event_handler  # noqa: PLW0603
    _ui_event_handler = handler


def emit_ui_event(event: dict) -> None:
    """Fire an event to the registered UI handler, if any."""
    handler = _ui_event_handler
    if handler is not None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001
            pass

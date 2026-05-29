"""Deck theme resolution — Phase B1.

Maps a ``DeckBrief`` (audience + optional explicit tokens) to a concrete
``DeckTheme`` of colors, font sizes, and spacing that ``assemble.py`` threads
through every layout renderer. This replaces the module-level design-token
constants so visual style can be tuned per run without code changes.

Resolution order (later wins):
    1. Base defaults.
    2. Audience preset (board / ic / internal / client / generic).
    3. Explicit brief tokens (``density``, ``accent``, ``font_scale``).

Determinism: same brief → same theme. No I/O, no randomness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

# ---------------------------------------------------------------------------
# Base palette (the original assemble.py constants)
# ---------------------------------------------------------------------------

_DEFAULT_DARK = RGBColor(0x1A, 0x23, 0x32)    # slate
_DEFAULT_ACCENT = RGBColor(0x2D, 0xD4, 0xBF)  # teal
_DEFAULT_ACCENT_ALT = RGBColor(0xE3, 0x5D, 0x6A)  # coral (bear column)
_DEFAULT_BODY = RGBColor(0x33, 0x33, 0x33)    # near-black
_DEFAULT_MUTED = RGBColor(0x80, 0x80, 0x80)   # grey
_DEFAULT_PANEL_FILL = RGBColor(0xF6, 0xF7, 0xF9)
_DEFAULT_PANEL_LINE = RGBColor(0xE0, 0xE3, 0xE9)

# Density → (margin inches, vertical-spacing multiplier).
_DENSITY: dict[str, tuple[float, float]] = {
    "compact": (0.5, 0.72),
    "standard": (0.6, 1.0),
    "spacious": (0.8, 1.3),
}

# Audience → (density, font_scale, accent hex). accent=None keeps the teal default.
_AUDIENCE_PRESET: dict[str, tuple[str, float, str | None]] = {
    "board": ("spacious", 1.1, "#1f3a5f"),   # roomy, conservative navy accent
    "ic": ("standard", 1.0, None),           # the default house style (teal)
    "internal": ("compact", 0.95, None),     # dense working deck
    "client": ("standard", 1.05, None),      # slightly larger, brandable
    "generic": ("standard", 1.0, None),
}

_FONT_SCALE_MIN = 0.8
_FONT_SCALE_MAX = 1.4


@dataclass(frozen=True)
class DeckTheme:
    """Resolved visual tokens for one deck render."""

    dark: RGBColor
    accent: RGBColor
    accent_alt: RGBColor
    body: RGBColor
    muted: RGBColor
    panel_fill: RGBColor
    panel_line: RGBColor
    font_scale: float
    density: str
    margin_x: Any  # EMU (Inches)
    margin_y: Any  # EMU (Inches)
    _space_mult: float

    # ── Scaled helpers ───────────────────────────────────────────────────
    def size(self, base_pt: float) -> Pt:
        """Font size in points, scaled by ``font_scale``."""
        return Pt(round(base_pt * self.font_scale))

    def space(self, base_pt: float) -> Pt:
        """Paragraph ``space_after`` in points, scaled by density."""
        return Pt(round(base_pt * self._space_mult))


def _parse_hex(value: Any) -> RGBColor | None:
    """Parse '#rrggbb' / 'rrggbb' (also 3-digit shorthand) → RGBColor, else None."""
    if not isinstance(value, str):
        return None
    s = value.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)  # #abc → aabbcc
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return None
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def resolve_theme(brief: dict[str, Any] | None) -> DeckTheme:
    """Build a ``DeckTheme`` from a (possibly partial) brief dict."""
    brief = brief or {}
    audience = str(brief.get("audience") or "generic")
    preset_density, preset_scale, preset_accent_hex = _AUDIENCE_PRESET.get(
        audience, _AUDIENCE_PRESET["generic"]
    )

    # Density: explicit brief token wins over audience preset.
    density = brief.get("density") or preset_density
    if density not in _DENSITY:
        density = "standard"
    margin_in, space_mult = _DENSITY[density]

    # Accent: explicit valid hex wins, else audience preset, else teal default.
    accent = (
        _parse_hex(brief.get("accent"))
        or _parse_hex(preset_accent_hex)
        or _DEFAULT_ACCENT
    )

    # Font scale: explicit token wins; clamp to bounds.
    raw_scale = brief.get("font_scale")
    scale = preset_scale if raw_scale is None else float(raw_scale)
    scale = max(_FONT_SCALE_MIN, min(_FONT_SCALE_MAX, scale))

    return DeckTheme(
        dark=_DEFAULT_DARK,
        accent=accent,
        accent_alt=_DEFAULT_ACCENT_ALT,
        body=_DEFAULT_BODY,
        muted=_DEFAULT_MUTED,
        panel_fill=_DEFAULT_PANEL_FILL,
        panel_line=_DEFAULT_PANEL_LINE,
        font_scale=scale,
        density=density,
        margin_x=Inches(margin_in),
        margin_y=Inches(margin_in),
        _space_mult=space_mult,
    )


__all__ = ["DeckTheme", "resolve_theme"]

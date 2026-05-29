"""Adapter protocol + shared helpers for deck workflow source normalization.

Each ``DeckSource`` subclass has a paired ``SourceAdapter`` that converts it
into ``NormalizedBlock`` items.  Adapters are the ONLY place that know their
source type's schema — downstream nodes operate on blocks generically.

Adding a new input type:
1. Add Pydantic subclass to ``deck/state.py`` (extend the DeckSource union).
2. Create ``deck/adapters/<type>.py`` implementing ``SourceAdapter``.
3. Register in ``deck/adapters/__init__.py`` ADAPTERS dict.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from ..state import DeckSource, NormalizedBlock


@runtime_checkable
class SourceAdapter(Protocol):
    """Contract for source-to-block normalization.

    Implementations must be DETERMINISTIC — same input → same blocks (incl. IDs).
    Required for KG ``deck_run`` snapshot stability across re-runs.
    """

    source_type: str

    def normalize(self, source: DeckSource, *, session_id: str = "") -> list[NormalizedBlock]:
        """Convert a typed source into a list of normalized blocks.

        ``session_id`` is the active run/session — adapters that query
        session-scoped stores (e.g. ChromaDB session collection) must use it.
        Adapters that don't need it ignore the kwarg.

        Adapters should:
          - Use ``make_block_id(source_type, source_ref, idx, content)`` for IDs.
          - Populate ``evidence_refs`` so the citation drawer resolves correctly.
          - Set ``suggested_slide_layouts`` as hints (outline gen may override).
          - Return an empty list (NOT raise) when the source is empty / unfetchable;
            log a warning instead.  The graph tolerates empty adapter output.
        """
        ...

    # Optional hook (duck-typed, NOT required by the Protocol): adapters whose
    # source carries an evidence corpus may expose it keyed by ``evidence_id`` so
    # the references slide can resolve raw IDs → human-readable citations.
    #     def collect_evidence(self, source, *, session_id="") -> dict[str, dict]: ...
    # normalize_all_node calls it via getattr; adapters without it contribute none.


# ---------------------------------------------------------------------------
# Deterministic ID generation
# ---------------------------------------------------------------------------


def _hash_short(value: str, length: int = 8) -> str:
    """Stable short hash for block IDs.  SHA-256 truncated."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def make_block_id(
    *,
    source_type: str,
    source_ref: str,
    idx: int,
    content_signature: str | None = None,
) -> str:
    """Build a deterministic, KG-friendly block ID.

    Format: ``{source_type}_{source_ref_hash}_{idx}[_{content_hash}]``

    The optional ``content_signature`` is hashed in when block ordering may
    change but stable identity by content is needed (e.g. retrieval results).
    """
    ref_hash = _hash_short(source_ref or "anon", 8)
    parts = [source_type, ref_hash, str(idx)]
    if content_signature:
        parts.append(_hash_short(content_signature, 6))
    return "_".join(parts)


__all__ = ["SourceAdapter", "make_block_id"]

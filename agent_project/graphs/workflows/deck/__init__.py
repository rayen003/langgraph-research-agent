"""Deck workflow — generate slide decks from polymorphic typed sources.

Public API:
    from agent_project.graphs.workflows.deck import (
        run_deck_workflow_sync,
        deck_workflow_app,
        DeckBrief,
        DcfOutputSource,
        DocumentSource,
        WebSource,
        ManualTextSource,
        KgSubgraphSource,
        ChartArtifactSource,
    )

DCF integration: ``DcfOutputSource`` is one of several supported source
types.  This module has no DCF dependency outside ``adapters/dcf_output.py``.
"""

from .graph import deck_workflow_app, run_deck_workflow_sync
from .state import (
    ChartArtifactSource,
    DcfOutputSource,
    DeckBrief,
    DeckSource,
    DocumentSource,
    KgSubgraphSource,
    ManualTextSource,
    WebSource,
)

__all__ = [
    "deck_workflow_app",
    "run_deck_workflow_sync",
    "DeckBrief",
    "DeckSource",
    "DcfOutputSource",
    "DocumentSource",
    "WebSource",
    "ManualTextSource",
    "KgSubgraphSource",
    "ChartArtifactSource",
]

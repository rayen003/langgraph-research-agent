"""Tests for all router functions — pure deterministic, no LLM/network."""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from agent_project.tests.helpers import build_test_state


# ---------------------------------------------------------------------------
# route_after_assumptions (review.py)
# ---------------------------------------------------------------------------

from langgraph.graph import END
from agent_project.graphs.workflows.dcf.review import route_after_assumptions


def test_route_after_assumptions_approved_goes_to_scenario_runner():
    state = build_test_state(assumptions_approved=True)
    assert route_after_assumptions(state) == "scenario_runner"


def test_route_after_assumptions_not_approved_goes_to_end():
    state = build_test_state(assumptions_approved=False)
    assert route_after_assumptions(state) == END


# ---------------------------------------------------------------------------
# route_after_analysis (refinement.py)
# ---------------------------------------------------------------------------

from agent_project.graphs.workflows.dcf.refinement import route_after_analysis


def test_route_after_analysis_should_refine():
    state = build_test_state(critique={"should_refine": True})
    assert route_after_analysis(state) == "refine_assumptions"


def test_route_after_analysis_no_refine():
    state = build_test_state(critique={"should_refine": False})
    assert route_after_analysis(state) == "finalize"


def test_route_after_analysis_no_critique():
    state = build_test_state(critique=None)
    assert route_after_analysis(state) == "finalize"


# ---------------------------------------------------------------------------
# route_after_review / route_after_review_val (review_loop.py)
# ---------------------------------------------------------------------------

from agent_project.graphs.workflows.dcf.review_loop import (
    route_after_review,
    route_after_review_val,
)


def test_route_after_review_should_refine():
    state = build_test_state(critique={"should_refine": True})
    assert route_after_review(state) == "coherence_gate"


def test_route_after_review_no_refine():
    """When no refinement, route to divergences (analysis runs every loop)."""
    state = build_test_state(critique={"should_refine": False})
    assert route_after_review(state) == "detect_divergences"


def test_route_after_review_val_should_refine():
    state = build_test_state(critique={"should_refine": True})
    assert route_after_review_val(state) == "coherence_gate"


def test_route_after_review_val_no_refine():
    state = build_test_state(critique={"should_refine": False})
    assert route_after_review_val(state) == "detect_divergences"


# ---------------------------------------------------------------------------
# route_after_cache_check (lifecycle.py)
# ---------------------------------------------------------------------------

from agent_project.graphs.workflows.dcf.lifecycle import route_after_cache_check


def test_route_after_cache_check_skip_flag_goes_to_formulate():
    """skip_semantic_synthesis=True → fast path, skip evidence assembly."""
    state = build_test_state(
        kg_cache_flags={"skip_semantic_synthesis": True}
    )
    assert route_after_cache_check(state) == "formulate_thesis"


def test_route_after_cache_check_no_skip_goes_to_assemble():
    state = build_test_state(
        kg_cache_flags={"skip_semantic_synthesis": False}
    )
    assert route_after_cache_check(state) == "assemble_evidence"


def test_route_after_cache_check_empty_flags_goes_to_assemble():
    state = build_test_state(kg_cache_flags={})
    assert route_after_cache_check(state) == "assemble_evidence"

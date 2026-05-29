"""Guard: SlideContent must bind via function_calling, NOT strict json_schema.

SlideContent carries bare-dict fields (`columns`, `flow_steps`) which serialize
to JSON Schema with `additionalProperties: true`. OpenAI's default strict
json_schema structured-output mode rejects that at request time with a 400
("'additionalProperties' is required to be supplied and to be false"), silently
forcing every slide to deterministic fallback content. slides.py therefore pins
`method="function_calling"`. These tests fail loudly if that ever regresses.
"""

from __future__ import annotations

from graphs.workflows.deck.state import SlideContent


def test_slidecontent_schema_has_open_dict_field():
    # Documents WHY strict mode breaks: at least one nested object permits
    # additional properties. If this ever stops being true, the function_calling
    # pin may no longer be strictly necessary — revisit the call sites.
    schema = SlideContent.model_json_schema()
    blob = repr(schema)
    assert "additionalProperties" in blob
    # columns/flow_steps are the offending bare-dict fields.
    assert "columns" in schema["properties"]
    assert "flow_steps" in schema["properties"]


def test_slides_pin_function_calling_method():
    # Cheap source-level guard: both with_structured_output(SlideContent, ...)
    # call sites must pass method="function_calling".
    import inspect

    from graphs.workflows.deck import slides

    src = inspect.getsource(slides)
    calls = src.count("with_structured_output(SlideContent")
    assert calls >= 2, "expected >=2 SlideContent structured-output call sites"
    pinned = src.count('with_structured_output(SlideContent, method="function_calling")')
    assert pinned == calls, "every SlideContent call site must pin function_calling"

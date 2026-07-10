"""Unit tests for agent.page_state.PageState's prompt rendering."""

from __future__ import annotations

from agent.page_state import PageState


def test_to_prompt_text_includes_coordinates_when_present() -> None:
    state = PageState(
        url="https://internal.example.local/",
        title="Home",
        visible_text="hi",
        elements=[{"tag": "button", "selector": "#go", "text": "Go", "x": 120, "y": 340}],
    )

    text = state.to_prompt_text()

    assert 'selector="#go"' in text
    assert "x=120 y=340" in text


def test_to_prompt_text_omits_coordinates_when_absent() -> None:
    state = PageState(
        url="https://internal.example.local/",
        title="Home",
        visible_text="hi",
        elements=[{"tag": "button", "selector": "#go", "text": "Go"}],
    )

    text = state.to_prompt_text()

    assert "x=" not in text

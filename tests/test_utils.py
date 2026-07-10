"""Unit tests for agent.utils: ActionResult, Timer, and JSON extraction."""

from __future__ import annotations

import time

import pytest

from agent.utils import ActionResult, Timer, extract_json


def test_action_result_defaults() -> None:
    result = ActionResult(success=True, message="ok")
    assert result.data is None
    assert result.error is None


def test_timer_measures_elapsed_time() -> None:
    with Timer() as timer:
        time.sleep(0.01)
    assert timer.elapsed >= 0.01


def test_extract_json_plain_object() -> None:
    assert extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_with_surrounding_text() -> None:
    text = 'Sure, here is the JSON:\n```json\n{"action": "click", "selector": "#go"}\n```'
    assert extract_json(text) == {"action": "click", "selector": "#go"}


def test_extract_json_raises_on_no_json() -> None:
    with pytest.raises(ValueError):
        extract_json("no json here at all")

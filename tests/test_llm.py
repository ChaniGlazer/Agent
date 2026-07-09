"""Unit tests for agent.llm: response parsing/validation shared by all providers."""

from __future__ import annotations

import pytest

from agent.llm import LLMProvider, LLMResponseError


class _StubProvider(LLMProvider):
    """A provider whose raw response is fixed, to test parsing in isolation."""

    def __init__(self, raw_response: str) -> None:
        super().__init__(model_name="stub", api_key=None)
        self._raw_response = raw_response

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        return self._raw_response


def test_get_next_action_parses_valid_json() -> None:
    provider = _StubProvider('{"reason": "clicking go", "action": "click", "selector": "#go", "finished": false}')
    decision = provider.get_next_action("system", "user")
    assert decision["action"] == "click"
    assert decision["selector"] == "#go"
    assert decision["text"] == ""
    assert decision["finished"] is False


def test_get_next_action_defaults_missing_optional_keys() -> None:
    provider = _StubProvider('{"reason": "done", "action": "stop", "finished": true}')
    decision = provider.get_next_action("system", "user")
    assert decision["selector"] is None
    assert decision["text"] == ""


def test_get_next_action_raises_on_invalid_json() -> None:
    provider = _StubProvider("not json at all")
    with pytest.raises(LLMResponseError):
        provider.get_next_action("system", "user")


def test_get_next_action_raises_on_missing_required_keys() -> None:
    provider = _StubProvider('{"reason": "missing action field", "finished": false}')
    with pytest.raises(LLMResponseError):
        provider.get_next_action("system", "user")

"""Unit tests for agent.llm: response parsing/validation shared by all providers."""

from __future__ import annotations

import pytest

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.llm import (
    DeepSeekProvider,
    LLMProvider,
    LLMResponseError,
    OpenAICompatibleProvider,
    create_llm_provider,
)


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


def test_deepseek_provider_targets_deepseek_base_url() -> None:
    provider = DeepSeekProvider(model_name="deepseek-chat", api_key="sk-test")
    assert str(provider._client.base_url).rstrip("/") == "https://api.deepseek.com"


def test_create_llm_provider_returns_deepseek_provider_for_deepseek_config(tmp_path) -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        approval_mode=ApprovalMode.NONE,
        llm_provider=LLMProviderName.DEEPSEEK,
        model_name="deepseek-chat",
        llm_api_key="sk-test",
    )
    provider = create_llm_provider(config)
    assert isinstance(provider, DeepSeekProvider)


@pytest.mark.parametrize(
    ("provider_name", "expected_base_url"),
    [
        (LLMProviderName.GOOGLE, "https://generativelanguage.googleapis.com/v1beta/openai/"),
        (LLMProviderName.GROQ, "https://api.groq.com/openai/v1"),
        (LLMProviderName.TOGETHER, "https://api.together.ai/v1"),
        (LLMProviderName.OPENROUTER, "https://openrouter.ai/api/v1"),
        (LLMProviderName.MISTRAL, "https://api.mistral.ai/v1"),
        (LLMProviderName.NVIDIA, "https://integrate.api.nvidia.com/v1"),
        (LLMProviderName.COHERE, "https://api.cohere.ai/compatibility/v1"),
        (LLMProviderName.HUGGINGFACE, "https://router.huggingface.co/v1"),
    ],
)
def test_create_llm_provider_uses_each_free_tier_providers_default_base_url(
    tmp_path, provider_name: LLMProviderName, expected_base_url: str
) -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        llm_provider=provider_name,
        model_name="some-model",
        llm_api_key="key",
        llm_base_url=expected_base_url,  # mirrors what AgentConfig.from_yaml would have resolved
    )
    provider = create_llm_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert str(provider._client.base_url).rstrip("/") == expected_base_url.rstrip("/")


def test_openai_compatible_provider_requires_openai_package_message_mentions_pip() -> None:
    provider = OpenAICompatibleProvider("some-model", "key", base_url="https://example.com/v1")
    assert provider.base_url == "https://example.com/v1"


def test_construct_provider_raises_clear_error_when_base_url_missing() -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        llm_provider=LLMProviderName.CLOUDFLARE,
        model_name="some-model",
        llm_api_key="key",
        llm_base_url=None,
    )
    with pytest.raises(ValueError, match="base_url"):
        create_llm_provider(config)

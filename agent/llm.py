"""LLM provider abstraction: turns (system prompt, user prompt) into a
validated action-decision dict.

Two concrete providers are included (OpenAI, Anthropic); both are imported
lazily so the unused one never needs to be installed. Providers only need to
implement ``_complete`` - JSON extraction/validation is shared in the base
class so every provider returns the same guaranteed shape.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from agent.config import AgentConfig, LLMProviderName
from agent.utils import extract_json

logger = logging.getLogger(__name__)

#: Keys the agent's action schema must always contain.
REQUIRED_KEYS = {"reason", "action", "finished"}


class LLMResponseError(ValueError):
    """Raised when the LLM output cannot be parsed into a valid action JSON object."""


class LLMProvider(ABC):
    """Base interface for chat-completion providers used to pick the next browser action.

    Args:
        model_name: Model identifier to request from the provider.
        api_key: API key for the provider (may be None if picked up from the
            provider SDK's own environment-variable conventions).
    """

    def __init__(self, model_name: str, api_key: str | None) -> None:
        self.model_name = model_name
        self.api_key = api_key

    @abstractmethod
    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        """Send the prompts to the underlying model and return its raw text response."""

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Call the model and parse its response into the agent's action schema.

        Returns:
            A dict guaranteed to contain at least ``reason``, ``action``, and
            ``finished``, with ``selector``/``text`` defaulted if absent.

        Raises:
            LLMResponseError: If the response is not valid JSON or is missing
                required keys.
        """
        raw = self._complete(system_prompt, user_prompt)
        logger.debug("Raw LLM response: %s", raw)
        try:
            data = extract_json(raw)
        except ValueError as exc:
            raise LLMResponseError(f"LLM did not return valid JSON: {exc}") from exc

        missing = REQUIRED_KEYS - data.keys()
        if missing:
            raise LLMResponseError(f"LLM response missing required keys: {sorted(missing)}")

        data.setdefault("selector", None)
        data.setdefault("text", "")
        return data


class OpenAIProvider(LLMProvider):
    """LLM provider backed by the OpenAI Chat Completions API."""

    def __init__(self, model_name: str, api_key: str | None) -> None:
        super().__init__(model_name, api_key)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for llm_provider: openai. "
                "Install it with: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=api_key)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return response.choices[0].message.content or ""


class AnthropicProvider(LLMProvider):
    """LLM provider backed by the Anthropic Messages API."""

    def __init__(self, model_name: str, api_key: str | None) -> None:
        super().__init__(model_name, api_key)
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ImportError(
                "The 'anthropic' package is required for llm_provider: anthropic. "
                "Install it with: pip install anthropic"
            ) from exc
        self._client = Anthropic(api_key=api_key)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self.model_name,
            max_tokens=1024,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class DeepSeekProvider(LLMProvider):
    """LLM provider backed by the DeepSeek API.

    DeepSeek exposes an OpenAI-compatible Chat Completions endpoint, so this
    reuses the ``openai`` SDK pointed at DeepSeek's base URL instead of
    depending on a separate package.
    """

    _BASE_URL = "https://api.deepseek.com"

    def __init__(self, model_name: str, api_key: str | None) -> None:
        super().__init__(model_name, api_key)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for llm_provider: deepseek "
                "(DeepSeek's API is OpenAI-compatible). Install it with: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=api_key, base_url=self._BASE_URL)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return response.choices[0].message.content or ""


def _build_single_provider(provider: LLMProviderName, model_name: str, api_key: str | None) -> LLMProvider:
    """Construct one concrete provider instance."""
    if provider == LLMProviderName.OPENAI:
        return OpenAIProvider(model_name, api_key)
    if provider == LLMProviderName.ANTHROPIC:
        return AnthropicProvider(model_name, api_key)
    if provider == LLMProviderName.DEEPSEEK:
        return DeepSeekProvider(model_name, api_key)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def create_llm_provider(config: AgentConfig):
    """Instantiate the decision-maker selected in ``config``.

    Returns a plain :class:`LLMProvider` in single-model mode (no ``models``
    list, or one model with no usage budgets), or an
    :class:`agent.router.ModelRouter` when multiple models - or usage
    budgets - are configured. Both expose the same
    ``get_next_action(system, user)`` interface.

    Raises:
        ValueError: If a configured provider is not supported.
    """
    specs = config.models
    if not specs:
        return _build_single_provider(config.llm_provider, config.model_name, config.llm_api_key)

    single_unbudgeted = (
        len(specs) == 1
        and specs[0].max_requests_per_minute is None
        and specs[0].max_requests_per_day is None
    )
    if single_unbudgeted:
        spec = specs[0]
        return _build_single_provider(spec.provider, spec.model_name, spec.api_key)

    # Imported here (not at module top) because agent.router imports this module.
    from agent.router import ModelRouter

    return ModelRouter(specs)

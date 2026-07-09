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


def create_llm_provider(config: AgentConfig) -> LLMProvider:
    """Instantiate the LLM provider selected in ``config``.

    Raises:
        ValueError: If ``config.llm_provider`` is not a supported provider.
    """
    if config.llm_provider == LLMProviderName.OPENAI:
        return OpenAIProvider(config.model_name, config.llm_api_key)
    if config.llm_provider == LLMProviderName.ANTHROPIC:
        return AnthropicProvider(config.model_name, config.llm_api_key)
    raise ValueError(f"Unsupported LLM provider: {config.llm_provider}")

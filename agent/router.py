"""Multi-model routing: pick the right LLM for each step, respect per-model
usage budgets, and fall back automatically when a provider rate-limits.

:class:`ModelRouter` exposes the same ``get_next_action(system, user)``
interface as a single :class:`agent.llm.LLMProvider`, so the controller and
server use it transparently. Internally it:

1. **Classifies the step** ("simple" vs "complex") from the prompt itself -
   the first step of a task and any step following a recent failure are
   "complex" (they need planning/recovery reasoning); routine mid-task steps
   are "simple". Each configured model declares which kinds it serves.
2. **Tracks local usage budgets** per model (requests/minute via a sliding
   60-second window, requests/day reset at UTC midnight) and skips models
   whose budget is spent.
3. **Falls back on provider errors**: an HTTP 429 (rate limit) or 529
   (overloaded) response benches the model for its configured cooldown and
   moves on to the next candidate.

Selection order: models serving the step's kind, by ascending ``priority``
(configuration order breaks ties). If no kind-matching model is available,
any available model is used - answering with a mismatched model beats
failing the task.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from agent.config import LLMProviderName, ModelSpec
from agent.llm import (
    AnthropicProvider,
    DeepSeekProvider,
    LLMProvider,
    OpenAIProvider,
)

logger = logging.getLogger(__name__)


class NoModelAvailableError(RuntimeError):
    """Raised when every configured model is benched (cooldown) or out of budget."""


def classify_task(user_prompt: str) -> str:
    """Classify one step as "simple" or "complex" from the built user prompt.

    Heuristic (deliberately cheap - no extra LLM call):
        * First step of a task (no action history yet) -> "complex", since the
          model must understand the goal and plan from scratch.
        * Any of the last three recorded actions failed -> "complex", since
          the model must reason its way out of the failure.
        * Otherwise -> "simple" (routine continuation).

    The prompt format is our own (see :func:`agent.prompts.build_user_prompt`),
    which is what makes this string inspection reliable.
    """
    if "(no actions performed yet)" in user_prompt:
        return "complex"
    history_section = user_prompt.split("ACTION HISTORY", 1)[-1]
    recent_lines = [line for line in history_section.splitlines() if line.startswith("- [")][-3:]
    if any(line.startswith("- [FAILED]") for line in recent_lines):
        return "complex"
    return "simple"


def _is_capacity_error(exc: BaseException) -> bool:
    """True for provider errors that mean "back off", not "your request is wrong".

    Detected by HTTP status (429 rate limit, 529 Anthropic overloaded) or by
    exception class name (openai.RateLimitError / anthropic.RateLimitError and
    friends), so no SDK import is needed here.
    """
    status = getattr(exc, "status_code", None)
    if status in (429, 529):
        return True
    name = type(exc).__name__
    return "RateLimit" in name or "Overloaded" in name


def _build_provider(spec: ModelSpec) -> LLMProvider:
    """Construct the concrete provider for one model spec."""
    if spec.provider == LLMProviderName.OPENAI:
        return OpenAIProvider(spec.model_name, spec.api_key)
    if spec.provider == LLMProviderName.ANTHROPIC:
        return AnthropicProvider(spec.model_name, spec.api_key)
    if spec.provider == LLMProviderName.DEEPSEEK:
        return DeepSeekProvider(spec.model_name, spec.api_key)
    raise ValueError(f"Unsupported LLM provider: {spec.provider}")


class ManagedModel:
    """One model in the pool: its spec, lazily-built provider, and usage state.

    The provider client is only constructed on first use, so listing a model
    whose SDK isn't installed costs nothing until that model is actually picked.
    """

    def __init__(self, spec: ModelSpec, provider_factory: Callable[[ModelSpec], LLMProvider] = _build_provider) -> None:
        self.spec = spec
        self._provider_factory = provider_factory
        self._provider: LLMProvider | None = None
        self._recent_requests: deque[float] = deque()
        self._daily_count = 0
        self._daily_date: str = ""
        self._cooldown_until: float = 0.0

    @property
    def label(self) -> str:
        """Human-readable identifier used in logs."""
        return f"{self.spec.provider.value}/{self.spec.model_name}"

    def provider(self) -> LLMProvider:
        """Return (building on first use) the concrete provider client."""
        if self._provider is None:
            self._provider = self._provider_factory(self.spec)
        return self._provider

    def is_available(self, now: float) -> bool:
        """Whether this model may be used right now (cooldown + budgets)."""
        if now < self._cooldown_until:
            return False
        if self.spec.max_requests_per_minute is not None:
            self._trim_window(now)
            if len(self._recent_requests) >= self.spec.max_requests_per_minute:
                return False
        if self.spec.max_requests_per_day is not None:
            self._roll_day()
            if self._daily_count >= self.spec.max_requests_per_day:
                return False
        return True

    def record_request(self, now: float) -> None:
        """Count one outbound request against this model's budgets."""
        self._trim_window(now)
        self._recent_requests.append(now)
        self._roll_day()
        self._daily_count += 1

    def start_cooldown(self, now: float) -> None:
        """Bench this model after a rate-limit/overloaded error."""
        self._cooldown_until = now + self.spec.cooldown_seconds
        logger.warning("Model %s benched for %.0fs after a capacity error.", self.label, self.spec.cooldown_seconds)

    def _trim_window(self, now: float) -> None:
        while self._recent_requests and now - self._recent_requests[0] > 60.0:
            self._recent_requests.popleft()

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0


class ModelRouter:
    """Routes each ``get_next_action`` call to the best available model.

    Implements the same duck-typed interface as :class:`agent.llm.LLMProvider`
    (``get_next_action(system_prompt, user_prompt) -> dict``), so it can be
    dropped in anywhere a single provider is expected.
    """

    def __init__(
        self,
        specs: list[ModelSpec],
        provider_factory: Callable[[ModelSpec], LLMProvider] = _build_provider,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not specs:
            raise ValueError("ModelRouter requires at least one model spec.")
        self._models = [ManagedModel(spec, provider_factory) for spec in specs]
        self._clock = clock
        self._lock = threading.Lock()

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Classify the step, pick candidates, and try them in order.

        Raises:
            NoModelAvailableError: If every model is benched or out of budget,
                or every candidate failed with a capacity error.
            LLMResponseError: Propagated unchanged from the chosen provider if
                its response is malformed (this is not a routing problem).
        """
        kind = classify_task(user_prompt)
        candidates = self._candidates_for(kind)
        if not candidates:
            raise NoModelAvailableError(
                f"No model is currently available for a '{kind}' step: all configured "
                "models are cooling down after rate limits or have exhausted their usage budgets."
            )

        last_capacity_error: BaseException | None = None
        for model in candidates:
            with self._lock:
                model.record_request(self._clock())
            try:
                logger.info("Routing '%s' step to %s", kind, model.label)
                return model.provider().get_next_action(system_prompt, user_prompt)
            except Exception as exc:  # noqa: BLE001 - classified right below
                if _is_capacity_error(exc):
                    last_capacity_error = exc
                    with self._lock:
                        model.start_cooldown(self._clock())
                    continue
                raise

        raise NoModelAvailableError(
            "Every candidate model hit a rate limit for this step."
        ) from last_capacity_error

    def _candidates_for(self, kind: str) -> list[ManagedModel]:
        """Available models serving ``kind`` (by priority), then any available model."""
        now = self._clock()
        with self._lock:
            available = [m for m in self._models if m.is_available(now)]
        matching = [m for m in available if kind in m.spec.tasks]
        fallback = [m for m in available if kind not in m.spec.tasks]

        def order(models: list[ManagedModel]) -> list[ManagedModel]:
            return sorted(models, key=lambda m: (m.spec.priority, self._models.index(m)))

        return order(matching) + order(fallback)

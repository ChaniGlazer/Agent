"""Unit tests for agent.router: step classification, task-fit routing,
usage budgets (per-minute and per-day), and rate-limit fallback/cooldown.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import AgentConfig, LLMProviderName, ModelSpec
from agent.llm import create_llm_provider
from agent.prompts import build_user_prompt
from agent.memory import Memory
from agent.page_state import PageState
from agent.router import ModelRouter, NoModelAvailableError, classify_task


class FakeProvider:
    """Stands in for a concrete LLMProvider; scriptable to fail with rate limits."""

    def __init__(self, name: str, failures: int = 0) -> None:
        self.name = name
        self.calls = 0
        self._failures = failures

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls += 1
        if self._failures > 0:
            self._failures -= 1
            raise FakeRateLimitError("429 too many requests")
        return {"reason": "ok", "action": "click", "selector": "#go", "text": "", "finished": False, "served_by": self.name}


class FakeRateLimitError(Exception):
    status_code = 429


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _spec(name: str, **overrides: Any) -> ModelSpec:
    defaults: dict[str, Any] = dict(provider=LLMProviderName.OPENAI, model_name=name)
    defaults.update(overrides)
    return ModelSpec(**defaults)


def _router(specs: list[ModelSpec], providers: dict[str, FakeProvider], clock: FakeClock | None = None) -> ModelRouter:
    return ModelRouter(
        specs,
        provider_factory=lambda spec: providers[spec.model_name],
        clock=clock or FakeClock(),
    )


_SIMPLE_PROMPT = (
    "GOAL:\ndo it\n\nPAGE STATE:\nURL: x\n\nACTION HISTORY (most recent last):\n"
    "- [success] click selector='#a' -> Clicked element '#a'.\n\nRespond with the single next action as a JSON object."
)
_FIRST_STEP_PROMPT = (
    "GOAL:\ndo it\n\nPAGE STATE:\nURL: x\n\nACTION HISTORY (most recent last):\n"
    "(no actions performed yet)\n\nRespond with the single next action as a JSON object."
)
_FAILED_PROMPT = (
    "GOAL:\ndo it\n\nPAGE STATE:\nURL: x\n\nACTION HISTORY (most recent last):\n"
    "- [success] click selector='#a' -> ok\n- [FAILED] click selector='#b' -> Timed out.\n\n"
    "Respond with the single next action as a JSON object."
)


# --------------------------------------------------------------------- #
# classify_task
# --------------------------------------------------------------------- #

def test_first_step_is_complex() -> None:
    assert classify_task(_FIRST_STEP_PROMPT) == "complex"


def test_recent_failure_is_complex() -> None:
    assert classify_task(_FAILED_PROMPT) == "complex"


def test_routine_continuation_is_simple() -> None:
    assert classify_task(_SIMPLE_PROMPT) == "simple"


def test_classification_matches_real_prompt_builder() -> None:
    memory = Memory(goal="g")
    state = PageState(url="https://x", title="t", visible_text="v")
    assert classify_task(build_user_prompt("g", state, memory)) == "complex"

    memory.record(action="click", selector="#a", text=None, success=True, message="ok", error=None, duration_seconds=0.1)
    assert classify_task(build_user_prompt("g", state, memory)) == "simple"

    memory.record(action="click", selector="#b", text=None, success=False, message="fail", error="timeout", duration_seconds=0.1)
    assert classify_task(build_user_prompt("g", state, memory)) == "complex"


# --------------------------------------------------------------------- #
# Task-fit routing
# --------------------------------------------------------------------- #

def test_complex_step_prefers_complex_capable_model() -> None:
    providers = {"cheap": FakeProvider("cheap"), "smart": FakeProvider("smart")}
    router = _router(
        [
            _spec("cheap", tasks=("simple",), priority=1),
            _spec("smart", tasks=("complex",), priority=1),
        ],
        providers,
    )

    decision = router.get_next_action("sys", _FIRST_STEP_PROMPT)

    assert decision["served_by"] == "smart"
    assert providers["cheap"].calls == 0


def test_simple_step_prefers_simple_capable_model() -> None:
    providers = {"cheap": FakeProvider("cheap"), "smart": FakeProvider("smart")}
    router = _router(
        [
            _spec("cheap", tasks=("simple",), priority=1),
            _spec("smart", tasks=("complex",), priority=1),
        ],
        providers,
    )

    decision = router.get_next_action("sys", _SIMPLE_PROMPT)

    assert decision["served_by"] == "cheap"


def test_priority_breaks_ties_within_a_task_kind() -> None:
    providers = {"a": FakeProvider("a"), "b": FakeProvider("b")}
    router = _router(
        [
            _spec("a", tasks=("simple",), priority=2),
            _spec("b", tasks=("simple",), priority=1),
        ],
        providers,
    )

    decision = router.get_next_action("sys", _SIMPLE_PROMPT)

    assert decision["served_by"] == "b"


def test_mismatched_model_is_used_rather_than_failing() -> None:
    providers = {"smart": FakeProvider("smart")}
    router = _router([_spec("smart", tasks=("complex",))], providers)

    decision = router.get_next_action("sys", _SIMPLE_PROMPT)

    assert decision["served_by"] == "smart"


# --------------------------------------------------------------------- #
# Usage budgets
# --------------------------------------------------------------------- #

def test_rpm_budget_moves_traffic_to_the_next_model() -> None:
    clock = FakeClock()
    providers = {"limited": FakeProvider("limited"), "backup": FakeProvider("backup")}
    router = _router(
        [
            _spec("limited", priority=1, max_requests_per_minute=2),
            _spec("backup", priority=2),
        ],
        providers,
        clock,
    )

    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "limited"
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "limited"
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "backup"

    clock.now += 61  # the sliding minute window frees up
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "limited"


def test_daily_budget_is_respected() -> None:
    providers = {"limited": FakeProvider("limited"), "backup": FakeProvider("backup")}
    router = _router(
        [
            _spec("limited", priority=1, max_requests_per_day=1),
            _spec("backup", priority=2),
        ],
        providers,
    )

    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "limited"
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "backup"


# --------------------------------------------------------------------- #
# Rate-limit fallback + cooldown
# --------------------------------------------------------------------- #

def test_rate_limited_model_falls_back_and_cools_down() -> None:
    clock = FakeClock()
    providers = {"flaky": FakeProvider("flaky", failures=1), "backup": FakeProvider("backup")}
    router = _router(
        [
            _spec("flaky", priority=1, cooldown_seconds=60),
            _spec("backup", priority=2),
        ],
        providers,
        clock,
    )

    # First call: flaky 429s, backup answers.
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "backup"
    assert providers["flaky"].calls == 1

    # While cooling down, flaky isn't even tried.
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "backup"
    assert providers["flaky"].calls == 1

    # After the cooldown expires, flaky is preferred again.
    clock.now += 61
    assert router.get_next_action("sys", _SIMPLE_PROMPT)["served_by"] == "flaky"


def test_non_capacity_errors_propagate_without_fallback() -> None:
    class BrokenProvider:
        def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
            raise ValueError("malformed request")

    providers = {"broken": BrokenProvider(), "backup": FakeProvider("backup")}  # type: ignore[dict-item]
    router = _router([_spec("broken", priority=1), _spec("backup", priority=2)], providers)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        router.get_next_action("sys", _SIMPLE_PROMPT)
    assert providers["backup"].calls == 0  # type: ignore[union-attr]


def test_all_models_exhausted_raises_no_model_available() -> None:
    providers = {"only": FakeProvider("only")}
    router = _router([_spec("only", max_requests_per_day=1)], providers)

    router.get_next_action("sys", _SIMPLE_PROMPT)
    with pytest.raises(NoModelAvailableError):
        router.get_next_action("sys", _SIMPLE_PROMPT)


def test_all_models_rate_limited_raises_no_model_available() -> None:
    providers = {"a": FakeProvider("a", failures=5), "b": FakeProvider("b", failures=5)}
    router = _router([_spec("a"), _spec("b")], providers)

    with pytest.raises(NoModelAvailableError):
        router.get_next_action("sys", _SIMPLE_PROMPT)


# --------------------------------------------------------------------- #
# Factory wiring
# --------------------------------------------------------------------- #

def test_factory_returns_router_for_multiple_models(tmp_path) -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        models=[
            _spec("gpt-4o-mini", api_key="k1"),
            _spec("deepseek-chat", provider=LLMProviderName.DEEPSEEK, api_key="k2"),
        ],
    )
    provider = create_llm_provider(config)
    assert isinstance(provider, ModelRouter)


def test_factory_returns_router_for_single_budgeted_model(tmp_path) -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        models=[_spec("gpt-4o-mini", api_key="k1", max_requests_per_minute=10)],
    )
    provider = create_llm_provider(config)
    assert isinstance(provider, ModelRouter)


def test_model_spec_rejects_unknown_task_kind() -> None:
    with pytest.raises(ValueError):
        ModelSpec(provider=LLMProviderName.OPENAI, model_name="x", tasks=("weird",))

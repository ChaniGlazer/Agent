"""Unit tests for agent.tools.ToolExecutor: dry-run, approval gating, and the
retry -> refresh -> screenshot error-recovery flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.memory import Memory
from agent.tools import ToolExecutor


def _make_config(tmp_path: Path, **overrides: object) -> AgentConfig:
    defaults = dict(
        target_url="https://internal.example.local",
        remote_debug_port=9222,
        timeout_ms=100,
        retry_count=2,
        retry_delay_seconds=0.0,
        max_steps=5,
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        dry_run=False,
        approval_mode=ApprovalMode.NONE,
        headless=False,
        llm_provider=LLMProviderName.OPENAI,
        model_name="gpt-4o-mini",
        llm_api_key="unused",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_page() -> tuple[MagicMock, MagicMock]:
    locator = MagicMock()
    page = MagicMock()
    page.locator.return_value.first = locator
    return page, locator


def test_dry_run_skips_mutating_action_without_touching_the_page(tmp_path: Path) -> None:
    page, locator = _make_page()
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))

    result = tools.execute("click", selector="#submit")

    assert result.success is True
    assert "[DRY RUN]" in result.message
    locator.click.assert_not_called()


def test_read_action_still_runs_in_dry_run(tmp_path: Path) -> None:
    page, locator = _make_page()
    locator.evaluate.return_value = "div"
    locator.inner_text.return_value = "hello"
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))

    result = tools.execute("read", selector="#msg")

    assert result.success is True
    assert result.data == "hello"


def test_approval_each_action_denied_blocks_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page, locator = _make_page()
    config = _make_config(tmp_path, approval_mode=ApprovalMode.EACH_ACTION)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    result = tools.execute("click", selector="#submit")

    assert result.success is False
    assert result.error == "approval_denied"
    locator.click.assert_not_called()


def test_approval_final_only_does_not_prompt_for_non_final_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page, locator = _make_page()
    config = _make_config(tmp_path, approval_mode=ApprovalMode.FINAL_ONLY)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))

    def _fail_if_called(_prompt: str) -> str:
        raise AssertionError("input() should not be called for non-final actions")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    result = tools.execute("click", selector="#submit", is_final=False)
    assert result.success is True


def test_approval_final_only_prompts_for_final_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page, locator = _make_page()
    config = _make_config(tmp_path, approval_mode=ApprovalMode.FINAL_ONLY)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    result = tools.execute("click", selector="#submit", is_final=True)

    assert result.success is True
    locator.click.assert_called_once()


def test_execute_retries_before_succeeding(tmp_path: Path) -> None:
    page, locator = _make_page()
    locator.click.side_effect = [PlaywrightTimeoutError("try again"), None]
    config = _make_config(tmp_path, retry_count=2)
    memory = Memory(goal="g")
    tools = ToolExecutor(page=page, config=config, memory=memory)

    result = tools.execute("click", selector="#submit")

    assert result.success is True
    assert locator.click.call_count == 2
    assert memory.history[-1].success is True


def test_execute_falls_back_to_refresh_then_screenshots_on_persistent_failure(tmp_path: Path) -> None:
    page, locator = _make_page()
    locator.click.side_effect = PlaywrightTimeoutError("always fails")
    config = _make_config(tmp_path, retry_count=2)
    memory = Memory(goal="g")
    tools = ToolExecutor(page=page, config=config, memory=memory)

    result = tools.execute("click", selector="#submit")

    assert result.success is False
    assert result.error is not None
    page.reload.assert_called_once()
    page.screenshot.assert_called_once()
    assert (tmp_path / "screenshots").exists()
    assert memory.errors, "the failure should have been recorded in memory"


def test_unknown_action_is_reported_without_touching_the_page(tmp_path: Path) -> None:
    page, locator = _make_page()
    config = _make_config(tmp_path)
    tools = ToolExecutor(page=page, config=config, memory=Memory(goal="g"))

    result = tools.execute("teleport", selector="#anything")

    assert result.success is False
    assert result.error == "unknown_action"
    locator.click.assert_not_called()

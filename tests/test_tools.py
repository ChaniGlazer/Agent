"""Unit tests for agent.tools.ToolExecutor: dry-run, approval gating, and the
retry -> refresh -> screenshot error-recovery flow - driven through a fake
RemoteBrowser so no real extension/WebSocket is needed.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.memory import Memory
from agent.page_state import PageState
from agent.tools import ToolExecutor
from agent.utils import ActionResult


class FakeRemoteBrowser:
    """Stands in for agent.connection.RemoteBrowser in tests."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, str | None, str | None, bool]] = []
        self.action_results: dict[str, list[ActionResult]] = {}
        self.approval_queue: list[bool] = []
        self.logs: list[tuple[str, str]] = []

    async def get_page_state(self) -> PageState:
        return PageState(url="https://internal.example.local/", title="Home", visible_text="hi", elements=[])

    async def execute_action(self, action: str, selector: str | None = None, text: str | None = None, is_final: bool = False) -> ActionResult:
        self.execute_calls.append((action, selector, text, is_final))
        queue = self.action_results.get(action)
        if queue:
            return queue.pop(0)
        return ActionResult(True, f"did {action}")

    async def request_approval(self, action: str, selector: str | None, text: str | None) -> bool:
        return self.approval_queue.pop(0)

    async def send_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def _make_config(tmp_path: Path, **overrides: object) -> AgentConfig:
    defaults = dict(
        target_url="https://internal.example.local",
        auth_token="secret",
        request_timeout_seconds=1.0,
        retry_count=2,
        retry_delay_seconds=0.0,
        max_steps=5,
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        dry_run=False,
        approval_mode=ApprovalMode.NONE,
        llm_provider=LLMProviderName.OPENAI,
        model_name="gpt-4o-mini",
        llm_api_key="unused",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


async def test_dry_run_skips_mutating_action_without_touching_the_browser(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("click", selector="#submit")

    assert result.success is True
    assert "[DRY RUN]" in result.message
    assert browser.execute_calls == []


async def test_read_action_still_runs_in_dry_run(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.action_results["read"] = [ActionResult(True, "read ok", data="hello")]
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("read", selector="#msg")

    assert result.success is True
    assert result.data == "hello"
    assert browser.execute_calls == [("read", "#msg", None, False)]


async def test_approval_each_action_denied_blocks_execution(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.approval_queue = [False]
    config = _make_config(tmp_path, approval_mode=ApprovalMode.EACH_ACTION)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("click", selector="#submit")

    assert result.success is False
    assert result.error == "approval_denied"
    assert browser.execute_calls == []


async def test_approval_final_only_does_not_ask_for_non_final_actions(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.approval_queue = []  # popping would raise IndexError if approval were requested
    config = _make_config(tmp_path, approval_mode=ApprovalMode.FINAL_ONLY)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("click", selector="#submit", is_final=False)

    assert result.success is True


async def test_approval_final_only_asks_for_final_action(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.approval_queue = [True]
    config = _make_config(tmp_path, approval_mode=ApprovalMode.FINAL_ONLY)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("click", selector="#submit", is_final=True)

    assert result.success is True
    assert browser.execute_calls == [("click", "#submit", None, True)]


async def test_execute_retries_before_succeeding(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.action_results["click"] = [ActionResult(False, "nope", error="timeout"), ActionResult(True, "ok")]
    config = _make_config(tmp_path, retry_count=2)
    memory = Memory(goal="g")
    tools = ToolExecutor(browser=browser, config=config, memory=memory)

    result = await tools.execute("click", selector="#submit")

    assert result.success is True
    assert len(browser.execute_calls) == 2
    assert memory.history[-1].success is True


async def test_execute_falls_back_to_refresh_then_screenshots_on_persistent_failure(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.action_results["click"] = [
        ActionResult(False, "nope", error="timeout"),
        ActionResult(False, "nope", error="timeout"),
        ActionResult(False, "still nope", error="timeout"),
    ]
    browser.action_results["refresh"] = [ActionResult(True, "refreshed")]
    fake_png = base64.b64encode(b"not-a-real-png").decode()
    browser.action_results["screenshot"] = [ActionResult(True, "captured", data=fake_png)]
    config = _make_config(tmp_path, retry_count=2)
    memory = Memory(goal="g")
    tools = ToolExecutor(browser=browser, config=config, memory=memory)

    result = await tools.execute("click", selector="#submit")

    assert result.success is False
    assert result.error is not None
    assert any(call[0] == "refresh" for call in browser.execute_calls)
    assert any(call[0] == "screenshot" for call in browser.execute_calls)
    screenshots = list((tmp_path / "screenshots").glob("*.png"))
    assert len(screenshots) == 1
    assert screenshots[0].read_bytes() == b"not-a-real-png"
    assert memory.errors, "the failure should have been recorded in memory"


async def test_unknown_action_is_reported_without_touching_the_browser(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("teleport", selector="#anything")

    assert result.success is False
    assert result.error == "unknown_action"
    assert browser.execute_calls == []


async def test_open_tab_and_close_tab_are_skipped_in_dry_run(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    open_result = await tools.execute("open_tab", selector="#row-1")
    close_result = await tools.execute("close_tab")

    assert open_result.success is True and "[DRY RUN]" in open_result.message
    assert close_result.success is True and "[DRY RUN]" in close_result.message
    assert browser.execute_calls == []


async def test_check_links_runs_even_in_dry_run_and_reports_broken_links(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    broken = [{"href": "https://internal.example.local/dead", "text": "dead link", "reason": "HTTP 404"}]
    browser.action_results["check_links"] = [
        ActionResult(True, "Checked 3 link(s): 1 broken - https://internal.example.local/dead (HTTP 404)", data={"total": 3, "ok_count": 2, "broken": broken})
    ]
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("check_links", selector="#inquiry-body")

    assert result.success is True
    assert result.data["broken"] == broken
    assert browser.execute_calls == [("check_links", "#inquiry-body", None, False)]


async def test_open_tab_failure_without_a_resolvable_link_is_reported(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.action_results["open_tab"] = [ActionResult(False, "no link", error="no_href")] * 2
    config = _make_config(tmp_path, retry_count=1)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("open_tab", selector="#not-a-link")

    assert result.success is False
    assert result.error == "no_href"


async def test_tap_and_type_are_skipped_in_dry_run(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    tap_result = await tools.execute("tap", text="120,340")
    type_result = await tools.execute("type", text="hello")

    assert tap_result.success is True and "[DRY RUN]" in tap_result.message
    assert type_result.success is True and "[DRY RUN]" in type_result.message
    assert browser.execute_calls == []


async def test_scroll_is_skipped_in_dry_run(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path, dry_run=True)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("scroll", text="down")

    assert result.success is True and "[DRY RUN]" in result.message
    assert browser.execute_calls == []


async def test_tap_forwards_text_to_the_browser(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    browser.action_results["tap"] = [ActionResult(True, "Tapped the element at (120, 340).")]
    config = _make_config(tmp_path)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    result = await tools.execute("tap", text="120,340")

    assert result.success is True
    assert browser.execute_calls == [("tap", None, "120,340", False)]


async def test_read_page_state_delegates_to_browser(tmp_path: Path) -> None:
    browser = FakeRemoteBrowser()
    config = _make_config(tmp_path)
    tools = ToolExecutor(browser=browser, config=config, memory=Memory(goal="g"))

    state = await tools.read_page_state()

    assert state.url == "https://internal.example.local/"

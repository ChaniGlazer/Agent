"""Unit tests for the low-level action primitives in agent.actions.*.

These use a mocked Playwright Page/Locator so they run without a real
browser. Real end-to-end behavior is covered by tests/integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.actions.click import click
from agent.actions.fill import fill
from agent.actions.navigate import navigate
from agent.actions.read import describe_page_state, read_text
from agent.actions.wait import wait_for_selector


def _mock_page_with_locator() -> tuple[MagicMock, MagicMock]:
    locator = MagicMock()
    page = MagicMock()
    page.locator.return_value.first = locator
    return page, locator


def test_click_success() -> None:
    page, locator = _mock_page_with_locator()
    result = click(page, "#submit", timeout_ms=1000)
    assert result.success is True
    locator.wait_for.assert_called_once_with(state="visible", timeout=1000)
    locator.click.assert_called_once_with(timeout=1000)


def test_click_timeout_is_reported_as_failure() -> None:
    page, locator = _mock_page_with_locator()
    locator.click.side_effect = PlaywrightTimeoutError("timed out")
    result = click(page, "#missing", timeout_ms=1000)
    assert result.success is False
    assert result.error is not None


def test_fill_success() -> None:
    page, locator = _mock_page_with_locator()
    result = fill(page, "#name", "Alice", timeout_ms=1000)
    assert result.success is True
    locator.fill.assert_called_once_with("Alice", timeout=1000)


def test_wait_for_selector_success() -> None:
    page, locator = _mock_page_with_locator()
    result = wait_for_selector(page, "#panel", timeout_ms=500, state="visible")
    assert result.success is True
    locator.wait_for.assert_called_once_with(state="visible", timeout=500)


def test_wait_for_selector_failure() -> None:
    page, locator = _mock_page_with_locator()
    locator.wait_for.side_effect = PlaywrightTimeoutError("nope")
    result = wait_for_selector(page, "#panel", timeout_ms=500)
    assert result.success is False


def test_navigate_success() -> None:
    page = MagicMock()
    page.url = "https://internal.example.local/home"
    result = navigate(page, "https://internal.example.local/home", timeout_ms=1000)
    assert result.success is True
    assert result.data == page.url
    page.goto.assert_called_once_with(
        "https://internal.example.local/home", timeout=1000, wait_until="domcontentloaded"
    )


def test_navigate_failure() -> None:
    page = MagicMock()
    page.goto.side_effect = RuntimeError("dns failure")
    result = navigate(page, "https://bad.example", timeout_ms=1000)
    assert result.success is False
    assert "dns failure" in (result.error or "")


def test_read_text_from_input_uses_input_value() -> None:
    page, locator = _mock_page_with_locator()
    locator.evaluate.return_value = "input"
    locator.input_value.return_value = "current value"
    result = read_text(page, "#field", timeout_ms=1000)
    assert result.success is True
    assert result.data == "current value"
    locator.input_value.assert_called_once()


def test_read_text_from_div_uses_inner_text() -> None:
    page, locator = _mock_page_with_locator()
    locator.evaluate.return_value = "div"
    locator.inner_text.return_value = "Hello world"
    result = read_text(page, "#message", timeout_ms=1000)
    assert result.success is True
    assert result.data == "Hello world"
    locator.inner_text.assert_called_once()


def test_describe_page_state_handles_evaluate_and_body_text() -> None:
    page = MagicMock()
    page.url = "https://internal.example.local/"
    page.title.return_value = "Home"
    page.locator.return_value.inner_text.return_value = "Welcome to the app"
    page.evaluate.return_value = [
        {"tag": "button", "type": "", "selector": "#go", "text": "Go", "disabled": False}
    ]

    state = describe_page_state(page)

    assert state.url == page.url
    assert state.title == "Home"
    assert "Welcome" in state.visible_text
    assert state.elements[0]["selector"] == "#go"
    assert "#go" in state.to_prompt_text()


def test_describe_page_state_tolerates_evaluate_failure() -> None:
    page = MagicMock()
    page.url = "https://internal.example.local/"
    page.title.return_value = "Home"
    page.locator.return_value.inner_text.return_value = "text"
    page.evaluate.side_effect = RuntimeError("js failed")

    state = describe_page_state(page)
    assert state.elements == []

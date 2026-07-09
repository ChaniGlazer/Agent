"""Integration test: drives the full read-state -> decide -> act loop
(AgentController + ToolExecutor + real Playwright actions) against a real,
locally rendered page in headless Chromium.

The LLM is replaced with a small scripted fake that returns a fixed
sequence of decisions, so this test exercises real DOM interaction without
requiring network access or an API key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import sync_playwright

#: Pre-installed Chromium binary used by this sandbox when it doesn't match
#: the pip-installed Playwright package's expected browser revision.
_FALLBACK_CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.controller import AgentController
from agent.memory import Memory
from agent.tools import ToolExecutor

_FIXTURE_HTML = """
<!DOCTYPE html>
<html>
<body>
    <input id="name" name="name" placeholder="Your name" />
    <button id="submit" onclick="
        document.getElementById('result').innerText = 'Hello, ' + document.getElementById('name').value + '!';
    ">Submit</button>
    <div id="result"></div>
</body>
</html>
"""


class ScriptedLLM:
    """A stand-in LLM provider that returns a pre-programmed sequence of decisions."""

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self._decisions = list(decisions)

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        assert self._decisions, "ScriptedLLM ran out of scripted decisions"
        decision = self._decisions.pop(0)
        decision.setdefault("selector", None)
        decision.setdefault("text", "")
        return decision


@pytest.fixture
def headless_page():
    launch_kwargs: dict[str, Any] = {"headless": True}
    if os.path.exists(_FALLBACK_CHROMIUM_PATH):
        launch_kwargs["executable_path"] = _FALLBACK_CHROMIUM_PATH
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_content(_FIXTURE_HTML)
        yield page
        browser.close()


def test_fill_and_click_flow_completes_the_goal(headless_page, tmp_path: Path) -> None:
    config = AgentConfig(
        target_url="about:blank",
        timeout_ms=5_000,
        retry_count=2,
        retry_delay_seconds=0.0,
        max_steps=5,
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        dry_run=False,
        approval_mode=ApprovalMode.NONE,
        llm_provider=LLMProviderName.OPENAI,
        model_name="unused",
        llm_api_key="unused",
    )
    memory = Memory(goal="Fill in the name field with 'Ada' and submit the form.")
    tools = ToolExecutor(page=headless_page, config=config, memory=memory)

    llm = ScriptedLLM(
        [
            {"reason": "enter the name", "action": "fill", "selector": "#name", "text": "Ada", "finished": False},
            {"reason": "submit the form", "action": "click", "selector": "#submit", "finished": True},
        ]
    )

    controller = AgentController(config=config, llm=llm, tools=tools, memory=memory)
    result = controller.run(memory.goal)

    assert result.completed is True
    assert result.stop_reason == "finished"
    assert headless_page.locator("#result").inner_text() == "Hello, Ada!"
    assert len(memory.history) == 2
    assert all(record.success for record in memory.history)


def test_dry_run_never_mutates_the_page(headless_page, tmp_path: Path) -> None:
    config = AgentConfig(
        target_url="about:blank",
        timeout_ms=5_000,
        retry_count=1,
        retry_delay_seconds=0.0,
        max_steps=5,
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        dry_run=True,
        approval_mode=ApprovalMode.NONE,
        llm_provider=LLMProviderName.OPENAI,
        model_name="unused",
        llm_api_key="unused",
    )
    memory = Memory(goal="Fill in the name field with 'Ada' and submit the form.")
    tools = ToolExecutor(page=headless_page, config=config, memory=memory)

    llm = ScriptedLLM(
        [
            {"reason": "enter the name", "action": "fill", "selector": "#name", "text": "Ada", "finished": True},
        ]
    )

    controller = AgentController(config=config, llm=llm, tools=tools, memory=memory)
    result = controller.run(memory.goal)

    assert result.completed is True
    assert headless_page.locator("#name").input_value() == ""
    assert headless_page.locator("#result").inner_text() == ""

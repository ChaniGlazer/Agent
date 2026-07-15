"""Unit tests for agent.controller.AgentController's read-decide-act loop,
driven by a scripted fake LLM and a fake RemoteBrowser (no real connection
or LLM API calls).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.controller import AgentController
from agent.llm import LLMResponseError
from agent.memory import Memory
from agent.page_state import PageState
from agent.tools import ToolExecutor
from agent.utils import ActionResult


class ScriptedLLM:
    """Returns a fixed sequence of decisions (or raises a fixed sequence of exceptions)."""

    def __init__(self, decisions: list[dict[str, Any] | Exception]) -> None:
        self._decisions = list(decisions)

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        item = self._decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        item.setdefault("selector", None)
        item.setdefault("text", "")
        return item


class FakeRemoteBrowser:
    async def get_page_state(self) -> PageState:
        return PageState(url="https://internal.example.local/", title="Home", visible_text="hi", elements=[])

    async def execute_action(self, action: str, selector: str | None = None, text: str | None = None, is_final: bool = False) -> ActionResult:
        return ActionResult(True, f"did {action}")

    async def request_approval(self, action: str, selector: str | None, text: str | None) -> bool:
        return True

    async def send_log(self, level: str, message: str) -> None:
        pass


def _make_config(tmp_path: Path, **overrides: object) -> AgentConfig:
    defaults = dict(
        target_url="https://internal.example.local",
        auth_token="secret",
        retry_count=1,
        retry_delay_seconds=0.0,
        max_steps=3,
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


def _make_controller(tmp_path: Path, decisions: list[dict[str, Any] | Exception], **config_overrides: object):
    config = _make_config(tmp_path, **config_overrides)
    memory = Memory(goal="do the thing")
    tools = ToolExecutor(browser=FakeRemoteBrowser(), config=config, memory=memory)
    llm = ScriptedLLM(decisions)
    controller = AgentController(config=config, llm=llm, tools=tools, memory=memory)
    return controller, memory


async def test_run_completes_when_an_action_reports_finished(tmp_path: Path) -> None:
    controller, memory = _make_controller(
        tmp_path,
        [
            {"reason": "fill the name", "action": "fill", "selector": "#name", "text": "Ada", "finished": False},
            {"reason": "submit", "action": "click", "selector": "#submit", "finished": True},
        ],
    )

    result = await controller.run(memory.goal)

    assert result.completed is True
    assert result.stop_reason == "finished"
    assert result.steps_taken == 2
    assert len(memory.history) == 2


async def test_run_stops_immediately_on_stop_action(tmp_path: Path) -> None:
    controller, memory = _make_controller(
        tmp_path, [{"reason": "already done", "action": "stop", "finished": True}]
    )

    result = await controller.run(memory.goal)

    assert result.completed is True
    assert result.stop_reason == "finished"
    assert memory.history == []  # "stop" never dispatches a tool


async def test_run_stops_and_reports_when_agent_asks_for_help(tmp_path: Path) -> None:
    controller, memory = _make_controller(
        tmp_path, [{"reason": "missing info", "action": "ask", "finished": False}]
    )

    result = await controller.run(memory.goal)

    assert result.completed is False
    assert result.stop_reason.startswith("needs_human_input:")


async def test_run_exhausts_max_steps_if_never_finished(tmp_path: Path) -> None:
    decisions = [
        {"reason": "click again", "action": "click", "selector": "#go", "finished": False} for _ in range(3)
    ]
    controller, memory = _make_controller(tmp_path, decisions, max_steps=3)

    result = await controller.run(memory.goal)

    assert result.completed is False
    assert result.stop_reason == "max_steps_exhausted"
    assert result.steps_taken == 3


async def test_run_recovers_from_a_single_invalid_llm_response(tmp_path: Path) -> None:
    controller, memory = _make_controller(
        tmp_path,
        [
            LLMResponseError("bad json"),
            {"reason": "done", "action": "stop", "finished": True},
        ],
        max_steps=3,
    )

    result = await controller.run(memory.goal)

    assert result.completed is True
    assert memory.errors  # the invalid-response error was recorded


async def test_request_stop_halts_the_loop_before_the_next_step(tmp_path: Path) -> None:
    controller, memory = _make_controller(
        tmp_path,
        [{"reason": "step", "action": "click", "selector": "#go", "finished": False} for _ in range(5)],
        max_steps=5,
    )
    controller.request_stop()

    result = await controller.run(memory.goal)

    assert result.completed is False
    assert result.stop_reason == "stopped_by_operator"
    assert result.steps_taken == 0

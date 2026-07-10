"""The agent's main control loop: read page state -> ask the LLM -> execute
the chosen action -> record the outcome -> repeat until the task is finished,
the agent asks for human input, the operator stops it, or the step budget is
exhausted.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from agent.config import AgentConfig
from agent.llm import LLMProvider, LLMResponseError
from agent.memory import Memory
from agent.prompts import SYSTEM_PROMPT, build_user_prompt
from agent.router import NoModelAvailableError
from agent.tools import ToolExecutor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TaskResult:
    """Outcome of a controller run.

    Attributes:
        completed: Whether the agent reported the goal as accomplished.
        steps_taken: Number of LLM/action loop iterations performed.
        stop_reason: Short machine-readable reason the loop ended, one of
            "finished", "needs_human_input: <reason>", "stopped_by_operator",
            "no_model_available: <reason>", or "max_steps_exhausted".
    """

    completed: bool
    steps_taken: int
    stop_reason: str


class AgentController:
    """Drives the read-state -> decide -> act -> verify loop for one task.

    Args:
        config: Active agent configuration (step budget, etc.).
        llm: LLM provider used to decide the next action at each step.
        tools: Tool executor bound to the extension-controlled tab.
        memory: Memory instance the loop reads from and writes to.
    """

    def __init__(self, config: AgentConfig, llm: LLMProvider, tools: ToolExecutor, memory: Memory) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._stop_requested = False

    def request_stop(self) -> None:
        """Ask the loop to stop before its next step (called from outside, e.g.
        when the extension sends a "stop_task" message)."""
        self._stop_requested = True

    async def run(self, goal: str) -> TaskResult:
        """Execute the agent loop for ``goal`` and return the final outcome."""
        logger.info("Starting task: %s", goal)

        for step in range(1, self._config.max_steps + 1):
            if self._stop_requested:
                logger.info("Stop requested by operator; ending task.")
                return TaskResult(completed=False, steps_taken=step - 1, stop_reason="stopped_by_operator")

            logger.info("--- Step %d/%d ---", step, self._config.max_steps)

            page_state = await self._tools.read_page_state()
            user_prompt = build_user_prompt(goal, page_state, self._memory)

            try:
                decision = await asyncio.to_thread(self._llm.get_next_action, SYSTEM_PROMPT, user_prompt)
            except LLMResponseError as exc:
                logger.error("LLM produced an invalid response: %s", exc)
                self._memory.errors.append(str(exc))
                continue
            except NoModelAvailableError as exc:
                logger.error("No LLM is currently available: %s", exc)
                self._memory.errors.append(str(exc))
                return TaskResult(completed=False, steps_taken=step, stop_reason=f"no_model_available: {exc}")

            action = decision["action"]
            reason = decision.get("reason", "")
            logger.info("LLM decision: action=%s reason=%s finished=%s", action, reason, decision.get("finished"))

            if action == "ask":
                logger.warning("Agent requested human input and is stopping: %s", reason)
                return TaskResult(completed=False, steps_taken=step, stop_reason=f"needs_human_input: {reason}")

            if action == "stop":
                logger.info("Agent reports the task is already complete: %s", reason)
                return TaskResult(completed=True, steps_taken=step, stop_reason="finished")

            result = await self._tools.execute(
                action=action,
                selector=decision.get("selector"),
                text=decision.get("text"),
                is_final=bool(decision.get("finished")),
            )

            if not result.success:
                logger.warning("Action failed: %s", result.message)

            if decision.get("finished"):
                logger.info("Agent reports the task is finished after this action: %s", reason)
                return TaskResult(completed=True, steps_taken=step, stop_reason="finished")

        logger.warning("Reached max_steps (%d) without the task reporting completion.", self._config.max_steps)
        return TaskResult(completed=False, steps_taken=self._config.max_steps, stop_reason="max_steps_exhausted")

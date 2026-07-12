"""The agent's tool surface: click, fill, read, wait, screenshot, navigate,
scroll, press, tap, type, upload, download, refresh, open_tab, close_tab,
check_links.

:class:`ToolExecutor` is the single entry point the controller uses to run
any tool. Actual DOM work happens inside the browser extension; every tool
call here is a request sent to the extension over :class:`agent.connection.RemoteBrowser`
and awaited. On top of that RPC, this module layers the same cross-cutting
concerns the original local version had:

1. **Safety gates** - dry-run short-circuiting of mutating actions, and
   human-approval prompts (per action, or only before a final/submitting
   action), surfaced through the extension's popup.
2. **Error recovery** - retry N times, then ask the extension to refresh the
   page and retry once more, then capture a screenshot and return a
   structured error.
3. **Observability** - every attempt is timed, logged, and recorded in
   :class:`agent.memory.Memory`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

from agent.config import AgentConfig, ApprovalMode
from agent.connection import RemoteBrowser
from agent.logger import log_action
from agent.memory import Memory
from agent.page_state import PageState
from agent.utils import ActionResult, Timer

logger = logging.getLogger(__name__)

#: Actions that change page state and must therefore be skipped in dry-run mode.
MUTATING_ACTIONS = {
    "click", "fill", "press", "upload", "navigate", "refresh", "scroll", "download",
    "open_tab", "close_tab", "tap", "type",
}

#: All tool names the executor knows how to dispatch.
KNOWN_ACTIONS = {
    "click", "fill", "read", "wait", "screenshot", "navigate",
    "scroll", "press", "upload", "download", "refresh",
    "open_tab", "close_tab", "check_links", "tap", "type",
}


class ToolExecutor:
    """Executes the agent's fixed set of browser tools via a :class:`RemoteBrowser`.

    Args:
        browser: The remote-browser connection (backed by the extension).
        config: The active agent configuration (timeouts, retries, safety modes).
        memory: The task's memory, updated with the outcome of every action.
    """

    def __init__(self, browser: RemoteBrowser, config: AgentConfig, memory: Memory) -> None:
        self._browser = browser
        self._config = config
        self._memory = memory

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def execute(
        self,
        action: str,
        selector: str | None = None,
        text: str | None = None,
        is_final: bool = False,
    ) -> ActionResult:
        """Run one named tool, applying approval checks, dry-run handling,
        timing, retries/recovery, and memory + log recording.

        Args:
            action: One of :data:`KNOWN_ACTIONS`.
            selector: Target element selector, where applicable.
            text: Free-text payload: text to type (``fill``), a key name
                (``press``), a destination URL (``navigate``), or a file path
                (``upload``).
            is_final: Whether this action is the task's concluding/submitting
                step; used to gate ``ApprovalMode.FINAL_ONLY``.

        Returns:
            The :class:`ActionResult` of the (possibly retried) action.
        """
        if action not in KNOWN_ACTIONS:
            result = ActionResult(success=False, message=f"Unknown action '{action}'.", error="unknown_action")
            self._record(action, selector, text, result, 0.0)
            return result

        if not await self._is_approved(action, selector, text, is_final):
            result = ActionResult(
                success=False, message="Action was not approved by the operator.", error="approval_denied"
            )
            self._record(action, selector, text, result, 0.0)
            return result

        if self._config.dry_run and action in MUTATING_ACTIONS:
            message = f"[DRY RUN] Would perform '{action}' selector={selector!r} text={text!r}"
            logger.info(message)
            await self._browser.send_log("info", message)
            result = ActionResult(success=True, message=message)
            self._record(action, selector, text, result, 0.0)
            return result

        with Timer() as timer:
            result = await self._execute_with_recovery(action, selector, text, is_final)
        self._record(action, selector, text, result, timer.elapsed)
        return result

    async def read_page_state(self) -> PageState:
        """Return a fresh snapshot of the current page for the LLM prompt."""
        return await self._browser.get_page_state()

    # ------------------------------------------------------------------ #
    # Approval / safety gates
    # ------------------------------------------------------------------ #

    async def _is_approved(self, action: str, selector: str | None, text: str | None, is_final: bool) -> bool:
        mode = self._config.approval_mode
        if mode == ApprovalMode.NONE:
            return True
        if mode == ApprovalMode.FINAL_ONLY and not is_final:
            return True

        approved = await self._browser.request_approval(action, selector, text)
        logger.info("Approval request for '%s' -> %s", action, "granted" if approved else "denied")
        return approved

    # ------------------------------------------------------------------ #
    # Retry / error-recovery flow
    # ------------------------------------------------------------------ #

    async def _execute_with_recovery(
        self, action: str, selector: str | None, text: str | None, is_final: bool
    ) -> ActionResult:
        attempts = max(1, self._config.retry_count)
        result = ActionResult(success=False, message="Action was not attempted.")

        for attempt in range(1, attempts + 1):
            result = await self._dispatch_once(action, selector, text, is_final)
            if result.success:
                return result
            logger.warning(
                "Attempt %d/%d failed for action '%s': %s", attempt, attempts, action, result.error or result.message
            )
            if attempt < attempts:
                await asyncio.sleep(self._config.retry_delay_seconds)

        logger.info("Retries exhausted for '%s'; refreshing the page and retrying once more.", action)
        refresh_result = await self._browser.execute_action("refresh")
        if refresh_result.success:
            result = await self._dispatch_once(action, selector, text, is_final)
            if result.success:
                return result

        screenshot_path = await self._screenshot(name=f"error_{action}")
        logger.error("Action '%s' failed after retries and a refresh. Screenshot: %s", action, screenshot_path)
        return ActionResult(
            success=False,
            message=f"Action '{action}' failed after {attempts} attempt(s) and a page refresh.",
            error=result.error or result.message,
            data={"screenshot": str(screenshot_path) if screenshot_path else None},
        )

    async def _dispatch_once(self, action: str, selector: str | None, text: str | None, is_final: bool) -> ActionResult:
        if action == "screenshot":
            return await self._screenshot_result(selector)
        return await self._browser.execute_action(action, selector=selector, text=text, is_final=is_final)

    def _record(self, action: str, selector: str | None, text: str | None, result: ActionResult, duration: float) -> None:
        self._memory.record(
            action=action, selector=selector, text=text, success=result.success,
            message=result.message, error=result.error, duration_seconds=duration,
        )
        log_action(logger, action, selector, result.success, duration, result.error)

    # ------------------------------------------------------------------ #
    # Screenshot handling (image bytes travel over the WebSocket as base64)
    # ------------------------------------------------------------------ #

    async def _screenshot_result(self, selector: str | None) -> ActionResult:
        path = await self._screenshot(selector=selector)
        if path is None:
            return ActionResult(False, "Screenshot failed.", error="screenshot_failed")
        return ActionResult(True, f"Screenshot saved to {path}", data=str(path))

    async def _screenshot(self, selector: str | None = None, name: str = "screenshot") -> Path | None:
        """Ask the extension to capture the visible tab (or one element),
        then decode and persist the returned base64 PNG.

        Note:
            Extensions can only capture the *visible viewport*
            (``chrome.tabs.captureVisibleTab``), not a stitched full-page
            image the way a local Playwright session could.
        """
        try:
            result = await self._browser.execute_action("screenshot", selector=selector)
            if not result.success or not result.data:
                logger.warning("Extension could not produce a screenshot: %s", result.message)
                return None
            image_bytes = base64.b64decode(result.data)
            folder = Path(self._config.screenshot_folder)
            folder.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            path = folder / f"{name}_{timestamp}.png"
            path.write_bytes(image_bytes)
            logger.info("Screenshot saved: %s", path)
            return path
        except Exception:
            logger.exception("Failed to capture/save screenshot")
            return None

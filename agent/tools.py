"""The agent's tool surface: click, fill, read, wait, screenshot, navigate,
scroll, press, upload, download, refresh.

:class:`ToolExecutor` is the single entry point the controller uses to run
any tool. It layers three cross-cutting concerns on top of the raw
Playwright actions in ``agent.actions``:

1. **Safety gates** - dry-run short-circuiting of mutating actions, and
   human-approval prompts (per action, or only before a final/submitting
   action).
2. **Error recovery** - retry N times, then refresh the page and retry once
   more, then capture a screenshot and return a structured error.
3. **Observability** - every attempt is timed, logged, and recorded in
   :class:`agent.memory.Memory`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page

from agent.actions.click import click as _click
from agent.actions.fill import fill as _fill
from agent.actions.navigate import navigate as _navigate
from agent.actions.read import PageState, describe_page_state
from agent.actions.read import read_text as _read_text
from agent.actions.wait import wait_for_selector as _wait_for_selector
from agent.config import AgentConfig, ApprovalMode
from agent.logger import log_action
from agent.memory import Memory
from agent.utils import ActionResult, Timer

logger = logging.getLogger(__name__)

#: Actions that change browser/page state and must therefore be skipped in dry-run mode.
MUTATING_ACTIONS = {"click", "fill", "press", "upload", "navigate", "refresh", "scroll", "download"}

#: All tool names the executor knows how to dispatch.
KNOWN_ACTIONS = {
    "click", "fill", "read", "wait", "screenshot", "navigate",
    "scroll", "press", "upload", "download", "refresh",
}


class ToolExecutor:
    """Executes the agent's fixed set of browser tools against a single Page.

    Args:
        page: The Playwright page the agent is allowed to operate on.
        config: The active agent configuration (timeouts, retries, safety modes).
        memory: The task's memory, updated with the outcome of every action.
    """

    def __init__(self, page: Page, config: AgentConfig, memory: Memory) -> None:
        self._page = page
        self._config = config
        self._memory = memory

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def execute(
        self,
        action: str,
        selector: str | None = None,
        text: str | None = None,
        key: str | None = None,
        file_path: str | None = None,
        url: str | None = None,
        is_final: bool = False,
    ) -> ActionResult:
        """Run one named tool, applying approval checks, dry-run handling,
        timing, retries/recovery, and memory + log recording.

        Args:
            action: One of :data:`KNOWN_ACTIONS`.
            selector: Target element selector, where applicable.
            text: Text to type (``fill``), for actions that need free text.
            key: Keyboard key to press (``press``).
            file_path: Local file path to upload (``upload``).
            url: Destination URL (``navigate``).
            is_final: Whether this action is the task's concluding/submitting
                step; used to gate ``ApprovalMode.FINAL_ONLY``.

        Returns:
            The :class:`ActionResult` of the (possibly retried) action.
        """
        if action not in KNOWN_ACTIONS:
            result = ActionResult(success=False, message=f"Unknown action '{action}'.", error="unknown_action")
            self._record(action, selector, text, result, 0.0)
            return result

        if not self._is_approved(action, selector, text, url, is_final):
            result = ActionResult(
                success=False, message="Action was not approved by the operator.", error="approval_denied"
            )
            self._record(action, selector, text, result, 0.0)
            return result

        if self._config.dry_run and action in MUTATING_ACTIONS:
            message = f"[DRY RUN] Would perform '{action}' selector={selector!r} text={text!r} url={url!r}"
            logger.info(message)
            result = ActionResult(success=True, message=message)
            self._record(action, selector, text, result, 0.0)
            return result

        with Timer() as timer:
            result = self._execute_with_recovery(action, selector, text, key, file_path, url)
        self._record(action, selector, text, result, timer.elapsed)
        return result

    def read_page_state(self) -> PageState:
        """Return a fresh snapshot of the current page for the LLM prompt."""
        return describe_page_state(self._page)

    # ------------------------------------------------------------------ #
    # Approval / safety gates
    # ------------------------------------------------------------------ #

    def _is_approved(
        self, action: str, selector: str | None, text: str | None, url: str | None, is_final: bool
    ) -> bool:
        mode = self._config.approval_mode
        if mode == ApprovalMode.NONE:
            return True
        if mode == ApprovalMode.FINAL_ONLY and not is_final:
            return True

        target = selector or url or ""
        prompt = f"Approve action '{action}' on '{target}'" + (f" with text '{text}'" if text else "") + "? [y/N]: "
        answer = input(prompt).strip().lower()
        approved = answer in ("y", "yes")
        logger.info("Approval request for '%s' -> %s", action, "granted" if approved else "denied")
        return approved

    # ------------------------------------------------------------------ #
    # Retry / error-recovery flow
    # ------------------------------------------------------------------ #

    def _execute_with_recovery(
        self, action: str, selector: str | None, text: str | None, key: str | None,
        file_path: str | None, url: str | None,
    ) -> ActionResult:
        attempts = max(1, self._config.retry_count)
        result = ActionResult(success=False, message="Action was not attempted.")

        for attempt in range(1, attempts + 1):
            result = self._dispatch_once(action, selector, text, key, file_path, url)
            if result.success:
                return result
            logger.warning(
                "Attempt %d/%d failed for action '%s': %s", attempt, attempts, action, result.error or result.message
            )
            if attempt < attempts:
                time.sleep(self._config.retry_delay_seconds)

        logger.info("Retries exhausted for '%s'; refreshing the page and retrying once more.", action)
        refresh_result = self._refresh()
        if refresh_result.success:
            result = self._dispatch_once(action, selector, text, key, file_path, url)
            if result.success:
                return result

        screenshot_path = self._screenshot(name=f"error_{action}")
        logger.error("Action '%s' failed after retries and a refresh. Screenshot: %s", action, screenshot_path)
        return ActionResult(
            success=False,
            message=f"Action '{action}' failed after {attempts} attempt(s) and a page refresh.",
            error=result.error or result.message,
            data={"screenshot": str(screenshot_path) if screenshot_path else None},
        )

    def _dispatch_once(
        self, action: str, selector: str | None, text: str | None, key: str | None,
        file_path: str | None, url: str | None,
    ) -> ActionResult:
        handler = self._DISPATCH[action]
        return handler(self, selector=selector, text=text, key=key, file_path=file_path, url=url)

    def _record(self, action: str, selector: str | None, text: str | None, result: ActionResult, duration: float) -> None:
        self._memory.record(
            action=action, selector=selector, text=text, success=result.success,
            message=result.message, error=result.error, duration_seconds=duration,
        )
        log_action(logger, action, selector, result.success, duration, result.error)

    # ------------------------------------------------------------------ #
    # Individual tool implementations
    # ------------------------------------------------------------------ #

    def _t_click(self, selector: str | None = None, **_: object) -> ActionResult:
        if not selector:
            return ActionResult(False, "click requires a selector.", error="missing_selector")
        return _click(self._page, selector, self._config.timeout_ms)

    def _t_fill(self, selector: str | None = None, text: str | None = None, **_: object) -> ActionResult:
        if not selector:
            return ActionResult(False, "fill requires a selector.", error="missing_selector")
        return _fill(self._page, selector, text or "", self._config.timeout_ms)

    def _t_read(self, selector: str | None = None, **_: object) -> ActionResult:
        if not selector:
            return ActionResult(False, "read requires a selector.", error="missing_selector")
        return _read_text(self._page, selector, self._config.timeout_ms)

    def _t_wait(self, selector: str | None = None, **_: object) -> ActionResult:
        if not selector:
            return ActionResult(False, "wait requires a selector.", error="missing_selector")
        return _wait_for_selector(self._page, selector, self._config.timeout_ms)

    def _t_navigate(self, url: str | None = None, **_: object) -> ActionResult:
        if not url:
            return ActionResult(False, "navigate requires a url.", error="missing_url")
        return _navigate(self._page, url, self._config.timeout_ms)

    def _t_screenshot(self, selector: str | None = None, **_: object) -> ActionResult:
        path = self._screenshot(selector=selector)
        if path is None:
            return ActionResult(False, "Screenshot failed.", error="screenshot_failed")
        return ActionResult(True, f"Screenshot saved to {path}", data=str(path))

    def _t_scroll(self, selector: str | None = None, **_: object) -> ActionResult:
        try:
            if selector:
                self._page.locator(selector).first.scroll_into_view_if_needed(timeout=self._config.timeout_ms)
                return ActionResult(True, f"Scrolled '{selector}' into view.")
            self._page.mouse.wheel(0, 1000)
            return ActionResult(True, "Scrolled the page down.")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "Scroll failed.", error=str(exc))

    def _t_press(self, selector: str | None = None, key: str | None = None, **_: object) -> ActionResult:
        if not key:
            return ActionResult(False, "press requires a key.", error="missing_key")
        try:
            if selector:
                self._page.locator(selector).first.press(key, timeout=self._config.timeout_ms)
            else:
                self._page.keyboard.press(key)
            return ActionResult(True, f"Pressed key '{key}'.")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, f"Failed to press '{key}'.", error=str(exc))

    def _t_upload(self, selector: str | None = None, file_path: str | None = None, **_: object) -> ActionResult:
        if not selector or not file_path:
            return ActionResult(False, "upload requires a selector and a file_path.", error="missing_arguments")
        try:
            self._page.locator(selector).first.set_input_files(file_path, timeout=self._config.timeout_ms)
            return ActionResult(True, f"Uploaded '{file_path}' via '{selector}'.")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "Upload failed.", error=str(exc))

    def _t_download(self, selector: str | None = None, **_: object) -> ActionResult:
        if not selector:
            return ActionResult(False, "download requires a selector that triggers the download.", error="missing_selector")
        try:
            with self._page.expect_download(timeout=self._config.timeout_ms) as download_info:
                self._page.locator(selector).first.click(timeout=self._config.timeout_ms)
            download = download_info.value
            dest = Path(self._config.data_folder) / download.suggested_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(dest))
            return ActionResult(True, f"Downloaded file to '{dest}'.", data=str(dest))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "Download failed.", error=str(exc))

    def _t_refresh(self, **_: object) -> ActionResult:
        return self._refresh()

    def _refresh(self) -> ActionResult:
        try:
            self._page.reload(timeout=self._config.timeout_ms, wait_until="domcontentloaded")
            return ActionResult(True, "Page refreshed.")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "Refresh failed.", error=str(exc))

    def _screenshot(self, selector: str | None = None, name: str = "screenshot") -> Path | None:
        """Capture a full-page or single-element screenshot into the configured folder."""
        try:
            folder = Path(self._config.screenshot_folder)
            folder.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            path = folder / f"{name}_{timestamp}.png"
            if selector:
                self._page.locator(selector).first.screenshot(path=str(path))
            else:
                self._page.screenshot(path=str(path), full_page=True)
            logger.info("Screenshot saved: %s", path)
            return path
        except Exception:
            logger.exception("Failed to capture screenshot")
            return None

    _DISPATCH: dict[str, Callable[..., ActionResult]]


ToolExecutor._DISPATCH = {
    "click": ToolExecutor._t_click,
    "fill": ToolExecutor._t_fill,
    "read": ToolExecutor._t_read,
    "wait": ToolExecutor._t_wait,
    "navigate": ToolExecutor._t_navigate,
    "screenshot": ToolExecutor._t_screenshot,
    "scroll": ToolExecutor._t_scroll,
    "press": ToolExecutor._t_press,
    "upload": ToolExecutor._t_upload,
    "download": ToolExecutor._t_download,
    "refresh": ToolExecutor._t_refresh,
}

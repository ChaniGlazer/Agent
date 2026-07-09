"""Wait action: block until an element reaches a desired state."""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.utils import ActionResult

logger = logging.getLogger(__name__)


def wait_for_selector(
    page: Page, selector: str, timeout_ms: int = 30_000, state: str = "visible"
) -> ActionResult:
    """Wait for the first element matched by ``selector`` to reach ``state``.

    Args:
        page: The Playwright page to act on.
        selector: A CSS (or Playwright) selector identifying the target element.
        timeout_ms: Maximum time to wait.
        state: One of Playwright's locator states: "attached", "detached",
            "visible", or "hidden".

    Returns:
        ActionResult describing whether the wait condition was met.
    """
    logger.debug("wait_for_selector(selector=%r, state=%r)", selector, state)
    try:
        page.locator(selector).first.wait_for(state=state, timeout=timeout_ms)  # type: ignore[arg-type]
        return ActionResult(success=True, message=f"Element '{selector}' reached state '{state}'.")
    except PlaywrightTimeoutError as exc:
        return ActionResult(
            success=False, message=f"Timed out waiting for '{selector}' to be '{state}'.", error=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        logger.exception("Unexpected error waiting for '%s'", selector)
        return ActionResult(success=False, message=f"Failed waiting for '{selector}'.", error=str(exc))

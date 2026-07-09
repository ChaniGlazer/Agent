"""Click action: click an element identified by a CSS/text selector.

Deliberately avoids raw mouse-coordinate clicks; Playwright locators are
resolved to elements and clicked through the DOM, benefiting from Playwright's
built-in actionability checks (visible, enabled, stable, receives events).
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.utils import ActionResult

logger = logging.getLogger(__name__)


def click(page: Page, selector: str, timeout_ms: int = 30_000) -> ActionResult:
    """Click the first element matched by ``selector``.

    Args:
        page: The Playwright page to act on.
        selector: A CSS (or Playwright) selector identifying the target element.
        timeout_ms: Maximum time to wait for the element to become clickable.

    Returns:
        ActionResult describing whether the click succeeded.
    """
    logger.debug("click(selector=%r)", selector)
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.click(timeout=timeout_ms)
        return ActionResult(success=True, message=f"Clicked element '{selector}'.")
    except PlaywrightTimeoutError as exc:
        return ActionResult(success=False, message=f"Timed out clicking '{selector}'.", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        logger.exception("Unexpected error clicking '%s'", selector)
        return ActionResult(success=False, message=f"Failed to click '{selector}'.", error=str(exc))

"""Fill action: type text into an input/textarea identified by a selector."""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.utils import ActionResult

logger = logging.getLogger(__name__)


def fill(page: Page, selector: str, text: str, timeout_ms: int = 30_000) -> ActionResult:
    """Clear and type ``text`` into the first element matched by ``selector``.

    Args:
        page: The Playwright page to act on.
        selector: A CSS (or Playwright) selector identifying the target field.
        text: The text to type into the field.
        timeout_ms: Maximum time to wait for the element to become editable.

    Returns:
        ActionResult describing whether the fill succeeded.
    """
    logger.debug("fill(selector=%r, text_length=%d)", selector, len(text))
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.fill(text, timeout=timeout_ms)
        return ActionResult(success=True, message=f"Filled '{selector}' with the given text.")
    except PlaywrightTimeoutError as exc:
        return ActionResult(success=False, message=f"Timed out filling '{selector}'.", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        logger.exception("Unexpected error filling '%s'", selector)
        return ActionResult(success=False, message=f"Failed to fill '{selector}'.", error=str(exc))

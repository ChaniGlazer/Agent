"""Navigate action: load a URL in the current page/tab."""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.utils import ActionResult

logger = logging.getLogger(__name__)


def navigate(page: Page, url: str, timeout_ms: int = 30_000) -> ActionResult:
    """Navigate ``page`` to ``url``.

    Args:
        page: The Playwright page to act on.
        url: The absolute URL to navigate to.
        timeout_ms: Maximum time to wait for navigation to complete.

    Returns:
        ActionResult describing whether navigation succeeded, with the
        resulting page URL as ``data``.
    """
    logger.debug("navigate(url=%r)", url)
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return ActionResult(success=True, message=f"Navigated to '{url}'.", data=page.url)
    except PlaywrightTimeoutError as exc:
        return ActionResult(success=False, message=f"Timed out navigating to '{url}'.", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        logger.exception("Unexpected error navigating to '%s'", url)
        return ActionResult(success=False, message=f"Failed to navigate to '{url}'.", error=str(exc))

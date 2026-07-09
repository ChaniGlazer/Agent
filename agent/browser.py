"""Connection to an already-running Chrome instance via the Chrome DevTools
Protocol (CDP) / remote debugging.

The agent is scoped to a single internal website and must never launch its
own browser window - it attaches to the user's existing, already
authenticated Chrome session and selects the correct tab by URL.
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

logger = logging.getLogger(__name__)


class BrowserConnectionError(RuntimeError):
    """Raised when the agent cannot attach to an existing Chrome instance."""


class BrowserManager:
    """Attaches to an existing Chrome instance over CDP and exposes its tabs.

    This class never starts a new browser process. Chrome must already be
    running with remote debugging enabled, e.g.::

        chrome --remote-debugging-port=9222

    Usage:
        with BrowserManager(remote_debug_port=9222) as manager:
            page = manager.get_page(url_contains="internal.example.local")
            ...
    """

    def __init__(self, remote_debug_port: int, timeout_ms: int = 30_000) -> None:
        self._port = remote_debug_port
        self._timeout_ms = timeout_ms
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    def __enter__(self) -> "BrowserManager":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> Browser:
        """Attach to Chrome via CDP at ``http://localhost:<port>``.

        Raises:
            BrowserConnectionError: If no Chrome instance is listening on the
                configured remote debugging port.
        """
        cdp_url = f"http://localhost:{self._port}"
        logger.info("Connecting to existing Chrome instance at %s", cdp_url)
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url, timeout=self._timeout_ms)
        except Exception as exc:
            self._playwright.stop()
            self._playwright = None
            raise BrowserConnectionError(
                f"Could not connect to Chrome on port {self._port}. Make sure Chrome is "
                f"already running with remote debugging enabled, e.g.: "
                f"'chrome --remote-debugging-port={self._port}'."
            ) from exc
        logger.info("Connected. Existing browser contexts: %d", len(self._browser.contexts))
        return self._browser

    def get_page(self, url_contains: Optional[str] = None) -> Page:
        """Return the tab (page) the agent should operate on.

        If ``url_contains`` is given, every open tab across every context is
        searched for one whose current URL contains that substring. If none
        matches, a new tab is opened within the first existing browser
        context (never a new browser window/process).

        Args:
            url_contains: Substring the target tab's URL must contain, e.g.
                the configured ``target_url``.

        Raises:
            BrowserConnectionError: If not connected, or the connected Chrome
                instance exposes no browser context at all.
        """
        if not self._browser:
            raise BrowserConnectionError("Not connected to a browser. Call connect() first.")

        for context in self._browser.contexts:
            for page in context.pages:
                if url_contains is None or url_contains in page.url:
                    logger.info("Using existing tab: %s", page.url)
                    page.set_default_timeout(self._timeout_ms)
                    return page

        if not self._browser.contexts:
            raise BrowserConnectionError("The connected Chrome instance has no open browser context/tab.")

        context = self._browser.contexts[0]
        page = context.new_page()
        page.set_default_timeout(self._timeout_ms)
        logger.info("No matching tab found for %r; opened a new tab in the existing browser context.", url_contains)
        return page

    def close(self) -> None:
        """Disconnect the CDP session. The user's actual Chrome window stays open."""
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self._browser = None
        logger.info("Disconnected from browser (Chrome itself remains open).")

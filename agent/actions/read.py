"""Read actions: extract text/values from a single element, and summarize
the whole page into an LLM-friendly :class:`PageState` snapshot.

Reading always goes through the DOM (``innerText`` / ``value`` / attribute
inspection) - OCR and screenshots are never used to understand page content,
only to record evidence for humans and error diagnostics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.utils import ActionResult

logger = logging.getLogger(__name__)

# Collects every commonly-interactive element that is currently visible on
# screen and derives a stable, ready-to-use selector for each one, preferring
# semantic attributes (id, data-testid, name, aria-label) over positional
# fallbacks.
_DESCRIBE_INTERACTIVE_ELEMENTS_JS = r"""
() => {
    const MAX = 60;
    const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [contenteditable="true"]';
    const candidates = Array.from(document.querySelectorAll(selectors)).filter((el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    }).slice(0, MAX);

    const cssEscape = (value) => (window.CSS && CSS.escape) ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, '_');

    const buildSelector = (el) => {
        if (el.id) return '#' + cssEscape(el.id);
        const testId = el.getAttribute('data-testid');
        if (testId) return `[data-testid="${testId}"]`;
        const name = el.getAttribute('name');
        if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return `${el.tagName.toLowerCase()}[aria-label="${ariaLabel}"]`;
        // Fallback: a positional nth-of-type path from the document body.
        const path = [];
        let node = el;
        while (node && node.nodeType === 1 && node !== document.body) {
            let index = 1;
            let sibling = node;
            while ((sibling = sibling.previousElementSibling)) {
                if (sibling.tagName === node.tagName) index += 1;
            }
            path.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${index})`);
            node = node.parentElement;
        }
        return path.length ? path.join(' > ') : el.tagName.toLowerCase();
    };

    return candidates.map((el) => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        selector: buildSelector(el),
        text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 80),
        placeholder: el.getAttribute('placeholder') || '',
        name: el.getAttribute('name') || '',
        role: el.getAttribute('role') || '',
        disabled: !!el.disabled,
    }));
}
"""


@dataclass(slots=True)
class PageState:
    """A compact, LLM-friendly snapshot of the current page.

    Attributes:
        url: The page's current URL.
        title: The page's title.
        visible_text: Truncated visible body text, for general context.
        elements: List of interactive elements, each with a ready-to-use selector.
    """

    url: str
    title: str
    visible_text: str
    elements: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        """Render this page state as plain text suitable for an LLM prompt."""
        lines = [
            f"URL: {self.url}",
            f"Title: {self.title}",
            "",
            "Visible text (truncated):",
            self.visible_text[:1500] if self.visible_text else "(empty)",
            "",
            "Interactive elements:",
        ]
        if not self.elements:
            lines.append("(none detected)")
        for el in self.elements:
            descriptor = f"- <{el['tag']}"
            if el.get("type"):
                descriptor += f" type={el['type']}"
            descriptor += f'> selector="{el["selector"]}" text="{el["text"]}"'
            if el.get("disabled"):
                descriptor += " [disabled]"
            lines.append(descriptor)
        return "\n".join(lines)


def read_text(page: Page, selector: str, timeout_ms: int = 30_000) -> ActionResult:
    """Read the visible text (or current value, for form fields) of one element.

    Args:
        page: The Playwright page to act on.
        selector: A CSS (or Playwright) selector identifying the target element.
        timeout_ms: Maximum time to wait for the element to be present.

    Returns:
        ActionResult whose ``data`` holds the extracted string on success.
    """
    logger.debug("read_text(selector=%r)", selector)
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="attached", timeout=timeout_ms)
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag in ("input", "textarea", "select"):
            value = locator.input_value(timeout=timeout_ms)
        else:
            value = locator.inner_text(timeout=timeout_ms)
        return ActionResult(success=True, message=f"Read content of '{selector}'.", data=value)
    except PlaywrightTimeoutError as exc:
        return ActionResult(success=False, message=f"Timed out reading '{selector}'.", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        logger.exception("Unexpected error reading '%s'", selector)
        return ActionResult(success=False, message=f"Failed to read '{selector}'.", error=str(exc))


def describe_page_state(page: Page, max_text_chars: int = 2000) -> PageState:
    """Build a :class:`PageState` snapshot of ``page`` for the LLM prompt.

    Reads the DOM directly (via ``innerText`` and attribute inspection) - no
    screenshots or OCR are involved in understanding page content.

    Args:
        page: The Playwright page to summarize.
        max_text_chars: Maximum number of visible-text characters to keep.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except Exception:
        logger.warning("Could not read body text; page may still be loading.")
        body_text = ""

    try:
        elements = page.evaluate(_DESCRIBE_INTERACTIVE_ELEMENTS_JS)
    except Exception:
        logger.exception("Failed to enumerate interactive elements")
        elements = []

    return PageState(
        url=page.url,
        title=page.title(),
        visible_text=body_text[:max_text_chars],
        elements=elements,
    )

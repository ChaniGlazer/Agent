"""The LLM-facing snapshot of the current page.

DOM extraction itself happens inside the browser extension's content script
(the server has no direct access to the user's browser) - this module only
defines the shared data shape and how it is rendered into the LLM prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PageState:
    """A compact, LLM-friendly snapshot of the current page.

    Attributes:
        url: The page's current URL.
        title: The page's title.
        visible_text: Truncated visible body text, for general context.
        elements: List of interactive elements, each with a ready-to-use selector,
            as extracted by the extension's content script.
    """

    url: str
    title: str
    visible_text: str
    elements: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageState":
        """Build a PageState from the JSON payload sent by the extension."""
        return cls(
            url=data.get("url", ""),
            title=data.get("title", ""),
            visible_text=data.get("visible_text", ""),
            elements=data.get("elements", []),
        )

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
            descriptor = f"- <{el.get('tag', '?')}"
            if el.get("type"):
                descriptor += f" type={el['type']}"
            descriptor += f'> selector="{el.get("selector", "")}" text="{el.get("text", "")}"'
            if el.get("x") is not None and el.get("y") is not None:
                descriptor += f' x={el["x"]} y={el["y"]}'
            if el.get("disabled"):
                descriptor += " [disabled]"
            lines.append(descriptor)
        return "\n".join(lines)

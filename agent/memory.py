"""In-memory (and persistable) record of a single agent task run.

Tracks the goal, the full action history, intermediate results the agent
chooses to remember between steps, and any errors encountered - this is the
context that gets summarized back into every LLM prompt so the agent knows
what it already tried.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActionRecord:
    """A single completed action and its outcome."""

    timestamp: str
    action: str
    selector: str | None
    text: str | None
    success: bool
    message: str
    error: str | None
    duration_seconds: float


class Memory:
    """Tracks everything the agent has done and learned during one task run.

    Attributes:
        goal: The natural-language objective for this run.
        history: Ordered list of every action taken and its result.
        intermediate_results: Free-form key/value store for data the agent
            wants to carry between steps (e.g. a value read from the page).
        errors: Flat list of error messages encountered, for quick inspection.
    """

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.history: list[ActionRecord] = []
        self.intermediate_results: dict[str, Any] = {}
        self.errors: list[str] = []

    @property
    def last_action(self) -> ActionRecord | None:
        """The most recently recorded action, or None if nothing happened yet."""
        return self.history[-1] if self.history else None

    def record(
        self,
        action: str,
        selector: str | None,
        text: str | None,
        success: bool,
        message: str,
        error: str | None,
        duration_seconds: float,
    ) -> ActionRecord:
        """Append a new action outcome to the history."""
        entry = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            selector=selector,
            text=text,
            success=success,
            message=message,
            error=error,
            duration_seconds=duration_seconds,
        )
        self.history.append(entry)
        if not success and error:
            self.errors.append(error)
        logger.debug("Memory updated: %s", entry)
        return entry

    def set_result(self, key: str, value: Any) -> None:
        """Store an intermediate result under ``key`` for later reference."""
        self.intermediate_results[key] = value

    def history_summary(self, limit: int = 10) -> str:
        """Render the last ``limit`` actions as compact text for LLM prompts."""
        recent = self.history[-limit:]
        if not recent:
            return "(no actions performed yet)"
        lines = []
        for rec in recent:
            status = "success" if rec.success else "FAILED"
            details = f" selector={rec.selector!r}" if rec.selector else ""
            text_part = f" text={rec.text!r}" if rec.text else ""
            lines.append(f"- [{status}] {rec.action}{details}{text_part} -> {rec.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full memory state to a plain dict."""
        return {
            "goal": self.goal,
            "history": [asdict(r) for r in self.history],
            "intermediate_results": self.intermediate_results,
            "errors": self.errors,
        }

    def save(self, data_folder: str | Path, filename: str = "memory.json") -> Path:
        """Persist the current memory state as JSON under ``data_folder``."""
        data_folder = Path(data_folder)
        data_folder.mkdir(parents=True, exist_ok=True)
        path = data_folder / filename
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Memory saved to %s", path)
        return path

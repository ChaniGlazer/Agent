"""Small, dependency-free helpers shared across the agent: a generic result
type, a timing context manager, and a tolerant JSON extractor for parsing
LLM output.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ActionResult:
    """The outcome of a single tool/action invocation.

    Attributes:
        success: Whether the action completed successfully.
        message: Human-readable summary, always present.
        data: Optional payload (e.g. text read from the page, a screenshot path).
        error: Optional machine-readable error string, set only on failure.
    """

    success: bool
    message: str
    data: Any = None
    error: str | None = None


class Timer:
    """Context manager that measures wall-clock elapsed time in seconds.

    Example:
        with Timer() as t:
            do_something()
        print(t.elapsed)
    """

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.elapsed: float = 0.0
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.elapsed = time.perf_counter() - self._start


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object found in ``text``.

    LLM responses are expected to be pure JSON, but this tolerates minor
    wrapping (e.g. accidental markdown code fences) by falling back to a
    regex search for the outermost ``{...}`` block.

    Raises:
        ValueError: If no valid JSON object can be found/parsed.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError(f"No JSON object found in text: {text!r}")
    return json.loads(match.group(0))

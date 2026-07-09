"""Small, dependency-free helpers shared across the agent: a generic result
type, a timing context manager, a retry decorator, and a tolerant JSON
extractor for parsing LLM output.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(slots=True)
class ActionResult:
    """The outcome of a single tool/action invocation.

    Attributes:
        success: Whether the action completed successfully.
        message: Human-readable summary, always present.
        data: Optional payload (e.g. text read from the page, a file path).
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


def retry(
    times: int,
    delay_seconds: float = 1.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Decorator that retries a callable up to ``times`` attempts with a fixed
    delay between attempts, re-raising the last exception if all attempts fail.

    Args:
        times: Total number of attempts (must be >= 1).
        delay_seconds: Seconds to sleep between failed attempts.
        exceptions: Exception types that trigger a retry.
    """

    def decorator(func: Callable[_P, _T]) -> Callable[_P, _T]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            last_exc: BaseException | None = None
            for attempt in range(1, max(1, times) + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001 - intentional broad catch for retry
                    last_exc = exc
                    logger.warning(
                        "Attempt %d/%d failed for %s: %s", attempt, times, func.__name__, exc
                    )
                    if attempt < times:
                        time.sleep(delay_seconds)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


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

"""Low-level Playwright action primitives (click, fill, read, navigate, wait).

Each module exposes a small, single-purpose function that operates on a
Playwright ``Page`` and returns an :class:`agent.utils.ActionResult`. Higher
level orchestration (retries, dry-run, approval, logging) lives in
``agent.tools.ToolExecutor``.
"""

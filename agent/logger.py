"""Central logging setup for the agent.

Every tool action is logged with a consistent structured format including
timing and error information, in addition to normal free-text log messages
emitted throughout the codebase.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logger(
    log_folder: str | Path,
    level: int = logging.INFO,
    name: str = "agent",
    filename: str = "agent.log",
) -> logging.Logger:
    """Configure and return the agent's root logger.

    Attaches a console handler (for interactive feedback) and a rotating file
    handler (for durable logs) under ``log_folder``. Safe to call multiple
    times; existing handlers of the same type are not duplicated.

    Args:
        log_folder: Directory the log file is written into (created if missing).
        level: Logging level for both handlers.
        name: Name of the logger to configure.
        filename: Log file name within ``log_folder``.
    """
    log_folder = Path(log_folder)
    log_folder.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in logger.handlers)
    if not has_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    has_file = any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    if not has_file:
        file_handler = RotatingFileHandler(
            log_folder / filename, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def log_action(
    logger: logging.Logger,
    action: str,
    selector: str | None,
    success: bool,
    duration_seconds: float,
    error: str | None = None,
) -> None:
    """Emit one structured log line describing a completed tool action.

    Includes the action name, target selector, success/failure status,
    duration, and error message (if any), as required for auditing the
    agent's behavior.
    """
    status = "OK" if success else "FAIL"
    if error:
        logger.error(
            "ACTION=%s SELECTOR=%s STATUS=%s DURATION=%.3fs ERROR=%s",
            action, selector, status, duration_seconds, error,
        )
    else:
        logger.info(
            "ACTION=%s SELECTOR=%s STATUS=%s DURATION=%.3fs",
            action, selector, status, duration_seconds,
        )

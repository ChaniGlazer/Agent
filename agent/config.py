"""Loading and validating the agent's configuration.

Configuration is read from a YAML file (see ``config.yaml.example``) into an
:class:`AgentConfig` dataclass. Secrets (LLM API keys) may be provided either
directly in the YAML file or, preferably, via the ``OPENAI_API_KEY`` /
``ANTHROPIC_API_KEY`` / ``DEEPSEEK_API_KEY`` environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ApprovalMode(str, Enum):
    """Controls when the agent must pause for human approval before acting."""

    NONE = "none"
    EACH_ACTION = "each_action"
    FINAL_ONLY = "final_only"


class LLMProviderName(str, Enum):
    """Supported LLM backends."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"


_ENV_API_KEY_BY_PROVIDER = {
    LLMProviderName.OPENAI: "OPENAI_API_KEY",
    LLMProviderName.ANTHROPIC: "ANTHROPIC_API_KEY",
    LLMProviderName.DEEPSEEK: "DEEPSEEK_API_KEY",
}


@dataclass(slots=True)
class AgentConfig:
    """All runtime configuration for a single agent run.

    Attributes:
        target_url: The single internal website the agent is allowed to operate on
            (used both to locate the correct browser tab and as a safety anchor).
        remote_debug_port: Port of the already-running Chrome's remote debugging
            interface (``chrome --remote-debugging-port=<port>``).
        timeout_ms: Default timeout for Playwright operations, in milliseconds.
        retry_count: Number of attempts for a failing tool action before the
            error-recovery flow (refresh + screenshot) kicks in.
        retry_delay_seconds: Delay between retry attempts.
        max_steps: Hard cap on LLM/action loop iterations per task, to guarantee
            termination even if the model never reports completion.
        screenshot_folder: Directory where screenshots are saved.
        log_folder: Directory where log files are written.
        data_folder: Directory where memory/state snapshots and downloads are saved.
        dry_run: If True, mutating actions are only reported, never performed.
        approval_mode: When human approval is required before acting.
        headless: Informational flag describing the target Chrome instance;
            the agent never launches its own browser, so this does not control
            browser startup.
        llm_provider: Which LLM backend to use.
        model_name: Model identifier passed to the LLM provider.
        llm_api_key: API key for the LLM provider (falls back to environment).
    """

    target_url: str
    remote_debug_port: int = 9222
    timeout_ms: int = 30_000
    retry_count: int = 3
    retry_delay_seconds: float = 2.0
    max_steps: int = 25
    screenshot_folder: Path = Path("agent/screenshots")
    log_folder: Path = Path("agent/logs")
    data_folder: Path = Path("agent/data")
    dry_run: bool = False
    approval_mode: ApprovalMode = ApprovalMode.NONE
    headless: bool = False
    llm_provider: LLMProviderName = LLMProviderName.OPENAI
    model_name: str = "gpt-4o-mini"
    llm_api_key: str | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        """Load configuration from a YAML file.

        Missing optional keys fall back to the dataclass defaults. The LLM API
        key, if not set in the file, is resolved from the provider-specific
        environment variable.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}. Copy config.yaml.example to config.yaml "
                "and fill in your settings."
            )
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        if "target_url" not in raw or not raw["target_url"]:
            raise ValueError("config.yaml must define 'target_url'.")

        llm_provider = LLMProviderName(raw.get("llm_provider", LLMProviderName.OPENAI.value))
        approval_mode = ApprovalMode(raw.get("approval_mode", ApprovalMode.NONE.value))

        api_key = raw.get("llm_api_key") or os.environ.get(_ENV_API_KEY_BY_PROVIDER[llm_provider])

        config = cls(
            target_url=raw["target_url"],
            remote_debug_port=int(raw.get("remote_debug_port", 9222)),
            timeout_ms=int(raw.get("timeout_ms", 30_000)),
            retry_count=int(raw.get("retry_count", 3)),
            retry_delay_seconds=float(raw.get("retry_delay_seconds", 2.0)),
            max_steps=int(raw.get("max_steps", 25)),
            screenshot_folder=Path(raw.get("screenshot_folder", "agent/screenshots")),
            log_folder=Path(raw.get("log_folder", "agent/logs")),
            data_folder=Path(raw.get("data_folder", "agent/data")),
            dry_run=bool(raw.get("dry_run", False)),
            approval_mode=approval_mode,
            headless=bool(raw.get("headless", False)),
            llm_provider=llm_provider,
            model_name=raw.get("model_name", "gpt-4o-mini"),
            llm_api_key=api_key,
        )
        logger.debug("Loaded configuration: %s", config)
        return config

    def ensure_directories(self) -> None:
        """Create the screenshot/log/data folders if they do not exist yet."""
        for folder in (self.screenshot_folder, self.log_folder, self.data_folder):
            Path(folder).mkdir(parents=True, exist_ok=True)

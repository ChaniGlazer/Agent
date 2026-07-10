"""Loading and validating the agent's configuration.

Configuration is read from a YAML file (see ``config.yaml.example``) into an
:class:`AgentConfig` dataclass. Secrets (the LLM API key and the extension
auth token) may be provided either directly in the YAML file or, preferably,
via environment variables (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` /
``DEEPSEEK_API_KEY`` / ``AGENT_AUTH_TOKEN``) - the latter is how secrets are
normally supplied on Render.
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
    """All runtime configuration for the agent server.

    Attributes:
        target_url: The single internal website the agent is allowed to operate
            on. Sent to the extension on connect so it can refuse to act on any
            other site, and included as context for the LLM.
        auth_token: Shared secret the extension must present (as a `token`
            query parameter) to open a WebSocket session. Required so the
            publicly reachable Render URL can't be driven by strangers.
        host: Local bind address for `uvicorn` (only relevant when running
            the server locally; Render provides its own routing).
        port: Local bind port for `uvicorn`. On Render this is overridden by
            the `PORT` environment variable (see `agent/main.py`).
        request_timeout_seconds: Maximum time to wait for the extension to
            reply to a single request (get_page_state / execute_action / approval).
        retry_count: Number of attempts for a failing tool action before the
            error-recovery flow (refresh + screenshot) kicks in.
        retry_delay_seconds: Delay between retry attempts.
        max_steps: Hard cap on LLM/action loop iterations per task, to guarantee
            termination even if the model never reports completion.
        screenshot_folder: Directory where screenshots are saved.
        log_folder: Directory where log files are written.
        data_folder: Directory where memory/state snapshots are saved.
        dry_run: If True, mutating actions are only reported, never performed.
        approval_mode: When human approval is required before acting.
        llm_provider: Which LLM backend to use.
        model_name: Model identifier passed to the LLM provider.
        llm_api_key: API key for the LLM provider (falls back to environment).
    """

    target_url: str
    auth_token: str | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    request_timeout_seconds: float = 30.0
    retry_count: int = 3
    retry_delay_seconds: float = 2.0
    max_steps: int = 25
    screenshot_folder: Path = Path("agent/screenshots")
    log_folder: Path = Path("agent/logs")
    data_folder: Path = Path("agent/data")
    dry_run: bool = False
    approval_mode: ApprovalMode = ApprovalMode.NONE
    llm_provider: LLMProviderName = LLMProviderName.OPENAI
    model_name: str = "gpt-4o-mini"
    llm_api_key: str | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        """Load configuration from a YAML file.

        Missing optional keys fall back to the dataclass defaults. `target_url`,
        `llm_provider`, `model_name`, the LLM API key, and the extension auth
        token can all be overridden by environment variables
        (`TARGET_URL` / `LLM_PROVIDER` / `MODEL_NAME` / `<PROVIDER>_API_KEY` /
        `AGENT_AUTH_TOKEN`) without editing the file - this is how Render
        deployments are normally configured, since `config.yaml` itself is
        git-ignored and won't exist in a deployed checkout unless the build
        step copies `config.yaml.example` into place.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}. Copy config.yaml.example to config.yaml "
                "and fill in your settings."
            )
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        target_url = os.environ.get("TARGET_URL") or raw.get("target_url")
        if not target_url:
            raise ValueError("target_url must be set, via config.yaml or the TARGET_URL environment variable.")

        llm_provider = LLMProviderName(
            os.environ.get("LLM_PROVIDER") or raw.get("llm_provider", LLMProviderName.OPENAI.value)
        )
        approval_mode = ApprovalMode(raw.get("approval_mode", ApprovalMode.NONE.value))

        api_key = raw.get("llm_api_key") or os.environ.get(_ENV_API_KEY_BY_PROVIDER[llm_provider])
        auth_token = raw.get("auth_token") or os.environ.get("AGENT_AUTH_TOKEN")

        config = cls(
            target_url=target_url,
            auth_token=auth_token,
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", 8000)),
            request_timeout_seconds=float(raw.get("request_timeout_seconds", 30.0)),
            retry_count=int(raw.get("retry_count", 3)),
            retry_delay_seconds=float(raw.get("retry_delay_seconds", 2.0)),
            max_steps=int(raw.get("max_steps", 25)),
            screenshot_folder=Path(raw.get("screenshot_folder", "agent/screenshots")),
            log_folder=Path(raw.get("log_folder", "agent/logs")),
            data_folder=Path(raw.get("data_folder", "agent/data")),
            dry_run=bool(raw.get("dry_run", False)),
            approval_mode=approval_mode,
            llm_provider=llm_provider,
            model_name=os.environ.get("MODEL_NAME") or raw.get("model_name", "gpt-4o-mini"),
            llm_api_key=api_key,
        )
        if not config.auth_token:
            logger.warning(
                "No auth_token configured - the WebSocket endpoint will reject every "
                "connection. Set 'auth_token' in config.yaml or the AGENT_AUTH_TOKEN "
                "environment variable."
            )
        logger.debug("Loaded configuration: %s", config)
        return config

    def ensure_directories(self) -> None:
        """Create the screenshot/log/data folders if they do not exist yet."""
        for folder in (self.screenshot_folder, self.log_folder, self.data_folder):
            Path(folder).mkdir(parents=True, exist_ok=True)

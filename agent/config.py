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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Task kinds a model can be assigned to. "complex" steps are planning-heavy
#: (first step of a task, or recovery after failures); "simple" steps are
#: routine mid-task actions. See agent/router.py's classify_task.
VALID_TASK_KINDS = ("simple", "complex")


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
class ModelSpec:
    """One model in the multi-model routing pool.

    Attributes:
        provider: Which LLM backend serves this model.
        model_name: Model identifier passed to the provider.
        api_key: API key for this model's provider (falls back to the
            provider-specific environment variable when omitted).
        tasks: Which step kinds this model should serve - any subset of
            ``("simple", "complex")``. A model listed only under "complex"
            is reserved for planning/recovery steps; one listed only under
            "simple" handles routine mid-task actions.
        priority: Lower value = preferred. Models with equal priority keep
            their configuration order.
        max_requests_per_minute: Local budget; once this many requests were
            sent within the last 60 seconds, the router moves on to the next
            model until the window frees up. None = unlimited.
        max_requests_per_day: Local daily budget (resets at UTC midnight).
            None = unlimited.
        cooldown_seconds: How long to bench this model after its provider
            returns a rate-limit/overloaded error (HTTP 429/529).
    """

    provider: LLMProviderName
    model_name: str
    api_key: str | None = None
    tasks: tuple[str, ...] = VALID_TASK_KINDS
    priority: int = 100
    max_requests_per_minute: int | None = None
    max_requests_per_day: int | None = None
    cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        for kind in self.tasks:
            if kind not in VALID_TASK_KINDS:
                raise ValueError(
                    f"Unknown task kind {kind!r} for model {self.model_name!r}; "
                    f"valid kinds: {list(VALID_TASK_KINDS)}"
                )
        if not self.tasks:
            raise ValueError(f"Model {self.model_name!r} must list at least one task kind.")


def _parse_model_spec(raw: dict[str, Any]) -> ModelSpec:
    """Build a ModelSpec from one entry of the YAML ``models:`` list."""
    if "provider" not in raw or "model_name" not in raw:
        raise ValueError(f"Each entry under 'models' needs 'provider' and 'model_name': {raw!r}")
    provider = LLMProviderName(raw["provider"])
    api_key = raw.get("api_key") or os.environ.get(_ENV_API_KEY_BY_PROVIDER[provider])
    return ModelSpec(
        provider=provider,
        model_name=raw["model_name"],
        api_key=api_key,
        tasks=tuple(raw.get("tasks", VALID_TASK_KINDS)),
        priority=int(raw.get("priority", 100)),
        max_requests_per_minute=(
            int(raw["max_requests_per_minute"]) if raw.get("max_requests_per_minute") is not None else None
        ),
        max_requests_per_day=(
            int(raw["max_requests_per_day"]) if raw.get("max_requests_per_day") is not None else None
        ),
        cooldown_seconds=float(raw.get("cooldown_seconds", 60.0)),
    )


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
        llm_provider: Which LLM backend to use (single-model mode; ignored
            when ``models`` is non-empty except as a fallback description).
        model_name: Model identifier passed to the LLM provider (single-model mode).
        llm_api_key: API key for the LLM provider (falls back to environment).
        models: Optional multi-model routing pool. When non-empty, each task
            step is routed to the best-fitting available model (by task kind,
            priority, and local usage budgets), with automatic fallback when
            a model hits a provider rate limit. When empty, the single
            ``llm_provider``/``model_name`` pair is used directly.
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
    models: list[ModelSpec] = field(default_factory=list)

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
        models = [_parse_model_spec(entry) for entry in raw.get("models") or []]

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
            models=models,
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

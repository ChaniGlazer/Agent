"""Unit tests for agent.config.AgentConfig.from_yaml, in particular the
environment-variable overrides used for Render deployments (config.yaml
itself is git-ignored and won't exist in a deployed checkout unless seeded
from config.yaml.example, so TARGET_URL/AGENT_AUTH_TOKEN/etc. must be able
to fully configure the server on their own).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import AgentConfig, ApprovalMode, LLMProviderName, ModelSpec


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_from_yaml_applies_defaults(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://internal.example.local"\n')

    config = AgentConfig.from_yaml(path)

    assert config.target_url == "https://internal.example.local"
    assert config.retry_count == 3
    assert config.max_steps == 25
    assert config.approval_mode == ApprovalMode.NONE
    assert config.llm_provider == LLMProviderName.OPENAI


def test_from_yaml_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        AgentConfig.from_yaml("/nonexistent/config.yaml")


def test_from_yaml_requires_target_url_from_somewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_yaml(tmp_path, "retry_count: 5\n")
    monkeypatch.delenv("TARGET_URL", raising=False)

    with pytest.raises(ValueError):
        AgentConfig.from_yaml(path)


def test_target_url_env_var_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://from-yaml.example"\n')
    monkeypatch.setenv("TARGET_URL", "https://from-env.example")

    config = AgentConfig.from_yaml(path)

    assert config.target_url == "https://from-env.example"


def test_target_url_env_var_alone_is_sufficient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_yaml(tmp_path, "retry_count: 5\n")
    monkeypatch.setenv("TARGET_URL", "https://from-env.example")

    config = AgentConfig.from_yaml(path)

    assert config.target_url == "https://from-env.example"
    assert config.retry_count == 5


def test_auth_token_resolved_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://internal.example.local"\n')
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "s3cr3t")

    config = AgentConfig.from_yaml(path)

    assert config.auth_token == "s3cr3t"


def test_llm_api_key_resolved_from_provider_specific_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://internal.example.local"\nllm_provider: "deepseek"\n')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-123")

    config = AgentConfig.from_yaml(path)

    assert config.llm_provider == LLMProviderName.DEEPSEEK
    assert config.llm_api_key == "dk-123"


def test_llm_provider_and_model_name_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://internal.example.local"\n')
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_NAME", "claude-sonnet-5")

    config = AgentConfig.from_yaml(path)

    assert config.llm_provider == LLMProviderName.ANTHROPIC
    assert config.model_name == "claude-sonnet-5"


def test_yaml_values_used_when_no_env_override_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TARGET_URL", "LLM_PROVIDER", "MODEL_NAME", "AGENT_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    path = _write_yaml(
        tmp_path,
        'target_url: "https://internal.example.local"\n'
        'auth_token: "yaml-token"\n'
        'llm_provider: "anthropic"\n'
        'model_name: "claude-sonnet-5"\n',
    )

    config = AgentConfig.from_yaml(path)

    assert config.auth_token == "yaml-token"
    assert config.llm_provider == LLMProviderName.ANTHROPIC
    assert config.model_name == "claude-sonnet-5"


def test_models_list_parsed_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-env")
    path = _write_yaml(
        tmp_path,
        """
target_url: "https://internal.example.local"
models:
  - provider: "deepseek"
    model_name: "deepseek-chat"
    tasks: ["simple"]
    priority: 1
    max_requests_per_minute: 30
    max_requests_per_day: 500
  - provider: "anthropic"
    model_name: "claude-sonnet-5"
    api_key: "inline-key"
    tasks: ["complex"]
    priority: 2
""",
    )

    config = AgentConfig.from_yaml(path)

    assert len(config.models) == 2
    first, second = config.models
    assert first.provider == LLMProviderName.DEEPSEEK
    assert first.api_key == "dk-env"  # resolved from the provider's env var
    assert first.tasks == ("simple",)
    assert first.max_requests_per_minute == 30
    assert first.max_requests_per_day == 500
    assert second.api_key == "inline-key"
    assert second.tasks == ("complex",)


def test_models_entry_missing_fields_raises(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        'target_url: "https://x"\nmodels:\n  - provider: "openai"\n',
    )
    with pytest.raises(ValueError):
        AgentConfig.from_yaml(path)


def test_no_models_key_leaves_single_model_mode(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, 'target_url: "https://internal.example.local"\n')

    config = AgentConfig.from_yaml(path)

    assert config.models == []


def test_ensure_directories_creates_missing_folders(tmp_path: Path) -> None:
    config = AgentConfig(
        target_url="https://internal.example.local",
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
    )

    config.ensure_directories()

    assert (tmp_path / "screenshots").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "data").is_dir()

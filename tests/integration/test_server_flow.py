"""Integration test: drives the full HTTP long-polling protocol between the
server (agent.server.create_app) and a simulated extension, using FastAPI's
TestClient. No real browser or LLM API is involved - DOM responses and LLM
decisions are both scripted, so this exercises exactly the wiring that
matters: auth, session handling, the request/response correlation, and the
controller loop running concurrently with polling.

Every test uses TestClient as a context manager (`with TestClient(app) as
client:`). That isn't cosmetic: only inside that context does TestClient
keep one persistent event loop alive across separate .get()/.post() calls,
matching how a real, long-running uvicorn process behaves. Without it, each
call gets its own throwaway event loop, which silently kills the background
asyncio task the controller runs in between calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent.config import AgentConfig, ApprovalMode, LLMProviderName
from agent.server import create_app


class ScriptedLLM:
    """A stand-in LLM provider returning a pre-programmed sequence of decisions."""

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self._decisions = list(decisions)

    def get_next_action(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        decision = self._decisions.pop(0)
        decision.setdefault("selector", None)
        decision.setdefault("text", "")
        return decision


def _make_config(tmp_path: Path, **overrides: object) -> AgentConfig:
    defaults = dict(
        target_url="https://internal.example.local",
        auth_token="test-token",
        request_timeout_seconds=5.0,
        retry_count=1,
        retry_delay_seconds=0.0,
        max_steps=5,
        screenshot_folder=tmp_path / "screenshots",
        log_folder=tmp_path / "logs",
        data_folder=tmp_path / "data",
        dry_run=False,
        approval_mode=ApprovalMode.NONE,
        llm_provider=LLMProviderName.OPENAI,
        model_name="gpt-4o-mini",
        llm_api_key="unused",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


_PAGE_STATE_PAYLOAD = {
    "url": "https://internal.example.local/form",
    "title": "Form",
    "visible_text": "Name: ___",
    "elements": [{"tag": "input", "selector": "#name", "text": ""}, {"tag": "button", "selector": "#submit", "text": "Submit"}],
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _connect(client: TestClient, config: AgentConfig) -> str:
    response = client.post("/agent/connect", headers=_auth(config.auth_token))
    assert response.status_code == 200
    body = response.json()
    assert body["target_url"] == config.target_url
    return body["session_id"]


def _poll(client: TestClient, session_id: str, config: AgentConfig) -> dict[str, Any]:
    response = client.get(f"/agent/poll?session_id={session_id}", headers=_auth(config.auth_token))
    assert response.status_code == 200
    return response.json()


def _send(client: TestClient, session_id: str, config: AgentConfig, message: dict[str, Any]) -> None:
    response = client.post(f"/agent/message?session_id={session_id}", headers=_auth(config.auth_token), json=message)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_healthz_and_root(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}

        root_response = client.get("/")
        assert root_response.status_code == 200
        assert root_response.headers["content-type"].startswith("text/html")
        assert "CodeBloom" in root_response.text
        # The landing page must never leak what this backend actually does.
        assert config.target_url not in root_response.text
        assert "target_url" not in root_response.text


def test_connect_rejects_missing_or_invalid_token(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        assert client.post("/agent/connect").status_code == 401
        assert client.post("/agent/connect", headers=_auth("wrong-token")).status_code == 401


def test_connect_accepts_valid_token(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        session_id = _connect(client, config)
        assert session_id


def test_poll_and_message_reject_unknown_session(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        assert client.get("/agent/poll?session_id=nonexistent", headers=_auth(config.auth_token)).status_code == 404
        assert (
            client.post(
                "/agent/message?session_id=nonexistent", headers=_auth(config.auth_token), json={"type": "hello"}
            ).status_code
            == 404
        )


def test_poll_and_message_reject_missing_or_invalid_token(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        session_id = _connect(client, config)

        assert client.get(f"/agent/poll?session_id={session_id}").status_code == 401
        assert client.get(f"/agent/poll?session_id={session_id}", headers=_auth("wrong-token")).status_code == 401
        assert client.post(f"/agent/message?session_id={session_id}", json={"type": "hello"}).status_code == 401


def test_poll_returns_idle_after_timeout_with_nothing_queued(tmp_path: Path, monkeypatch) -> None:
    import agent.server as server_module

    monkeypatch.setattr(server_module, "POLL_TIMEOUT_SECONDS", 0.05)
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        session_id = _connect(client, config)
        assert _poll(client, session_id, config) == {"type": "idle"}


def test_stop_task_with_no_active_task_reports_an_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    with TestClient(app) as client:
        session_id = _connect(client, config)

        _send(client, session_id, config, {"type": "stop_task"})
        message = _poll(client, session_id, config)
        assert message["type"] == "error"


def test_start_task_reports_error_when_llm_factory_fails(tmp_path: Path) -> None:
    """A broken LLM config (e.g. a missing API key) must not fail silently.

    Before this was fixed, an exception raised while building the LLM
    provider (synchronously, during "start_task" handling) propagated out of
    the FastAPI route as an uncaught 500 - a response the extension's
    fetch()-based sendToServer() never inspects for success, so the task
    appeared to start (the popup was already showing "running") and then
    nothing ever happened: no logs, no requests, no error, indefinitely.
    """

    def _broken_llm_factory(cfg: AgentConfig) -> Any:
        raise RuntimeError("no API key configured for provider 'openai'")

    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=_broken_llm_factory)
    with TestClient(app) as client:
        session_id = _connect(client, config)

        _send(client, session_id, config, {"type": "start_task", "goal": "Do something."})

        error_msg = _poll(client, session_id, config)
        assert error_msg["type"] == "error"
        assert "no API key configured" in error_msg["message"]

        finished = _poll(client, session_id, config)
        assert finished == {"type": "task_finished", "completed": False, "stop_reason": "server_error"}

        # A subsequent start_task must be accepted - the failed attempt must
        # not have left a phantom "active_task" blocking new ones.
        _send(client, session_id, config, {"type": "start_task", "goal": "Do something."})
        error_msg = _poll(client, session_id, config)
        assert error_msg["type"] == "error"


def test_full_task_flow_over_http_polling(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    decisions = [
        {"reason": "enter the name", "action": "fill", "selector": "#name", "text": "Ada", "finished": False},
        {"reason": "submit the form", "action": "click", "selector": "#submit", "finished": True},
    ]
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM(decisions))
    with TestClient(app) as client:
        session_id = _connect(client, config)

        _send(client, session_id, config, {"type": "start_task", "goal": "Fill in the name and submit."})

        log_msg = _poll(client, session_id, config)
        assert log_msg["type"] == "log"
        assert "Task started" in log_msg["message"]

        # Step 1: get_page_state -> fill
        req = _poll(client, session_id, config)
        assert req["type"] == "request" and req["action"] == "get_page_state"
        _send(client, session_id, config, {"type": "response", "request_id": req["request_id"], "payload": _PAGE_STATE_PAYLOAD})

        req = _poll(client, session_id, config)
        assert req["action"] == "execute_action"
        assert req["params"] == {"action": "fill", "selector": "#name", "text": "Ada", "is_final": False}
        _send(
            client,
            session_id,
            config,
            {"type": "response", "request_id": req["request_id"], "payload": {"success": True, "message": "filled"}},
        )

        # Step 2: get_page_state -> click (final)
        req = _poll(client, session_id, config)
        assert req["action"] == "get_page_state"
        _send(client, session_id, config, {"type": "response", "request_id": req["request_id"], "payload": _PAGE_STATE_PAYLOAD})

        req = _poll(client, session_id, config)
        assert req["action"] == "execute_action"
        assert req["params"] == {"action": "click", "selector": "#submit", "text": "", "is_final": True}
        _send(
            client,
            session_id,
            config,
            {"type": "response", "request_id": req["request_id"], "payload": {"success": True, "message": "clicked"}},
        )

        finished = _poll(client, session_id, config)
        assert finished == {"type": "task_finished", "completed": True, "stop_reason": "finished"}

    memory_file = config.data_folder / "memory.json"
    assert memory_file.exists()

"""Integration test: drives the full WebSocket protocol between the server
(agent.server.create_app) and a simulated extension, using FastAPI's
TestClient. No real browser or LLM API is involved - DOM responses and LLM
decisions are both scripted, so this exercises exactly the wiring that
matters: auth, the request/response correlation, and the controller loop
running concurrently with the message-receive loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


def test_healthz_and_root(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}

    root_response = client.get("/")
    assert root_response.status_code == 200
    assert root_response.headers["content-type"].startswith("text/html")
    assert "CodeBloom" in root_response.text
    # The landing page must never leak what this backend actually does.
    assert config.target_url not in root_response.text
    assert "target_url" not in root_response.text


def test_websocket_rejects_invalid_token(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong-token"):
            pass


def test_stop_task_with_no_active_task_reports_an_error(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM([]))
    client = TestClient(app)

    with client.websocket_connect(f"/ws?token={config.auth_token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello_ack"

        ws.send_json({"type": "stop_task"})
        response = ws.receive_json()
        assert response["type"] == "error"


def test_full_task_flow_over_websocket(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    decisions = [
        {"reason": "enter the name", "action": "fill", "selector": "#name", "text": "Ada", "finished": False},
        {"reason": "submit the form", "action": "click", "selector": "#submit", "finished": True},
    ]
    app = create_app(config, llm_factory=lambda cfg: ScriptedLLM(decisions))
    client = TestClient(app)

    with client.websocket_connect(f"/ws?token={config.auth_token}") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello_ack", "target_url": config.target_url}

        ws.send_json({"type": "start_task", "goal": "Fill in the name and submit."})

        log_msg = ws.receive_json()
        assert log_msg["type"] == "log"
        assert "Task started" in log_msg["message"]

        # Step 1: get_page_state -> fill
        req = ws.receive_json()
        assert req["type"] == "request" and req["action"] == "get_page_state"
        ws.send_json({"type": "response", "request_id": req["request_id"], "payload": _PAGE_STATE_PAYLOAD})

        req = ws.receive_json()
        assert req["action"] == "execute_action"
        assert req["params"] == {"action": "fill", "selector": "#name", "text": "Ada", "is_final": False}
        ws.send_json(
            {"type": "response", "request_id": req["request_id"], "payload": {"success": True, "message": "filled"}}
        )

        # Step 2: get_page_state -> click (final)
        req = ws.receive_json()
        assert req["action"] == "get_page_state"
        ws.send_json({"type": "response", "request_id": req["request_id"], "payload": _PAGE_STATE_PAYLOAD})

        req = ws.receive_json()
        assert req["action"] == "execute_action"
        assert req["params"] == {"action": "click", "selector": "#submit", "text": "", "is_final": True}
        ws.send_json(
            {"type": "response", "request_id": req["request_id"], "payload": {"success": True, "message": "clicked"}}
        )

        finished = ws.receive_json()
        assert finished == {"type": "task_finished", "completed": True, "stop_reason": "finished"}

    memory_file = config.data_folder / "memory.json"
    assert memory_file.exists()

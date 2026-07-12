"""The agent's server: a FastAPI app exposing a single WebSocket endpoint the
browser extension connects to.

This is the piece meant to run on Render. It never touches a browser itself;
it holds the "brain" (LLM + control loop) and delegates every DOM action to
whichever extension is currently connected, via the request/response protocol
implemented in :mod:`agent.connection`.

Wire protocol (JSON text frames over `/ws?token=<auth_token>`):

Extension -> server:
    {"type": "hello"}
    {"type": "start_task", "goal": str, "dry_run": bool | null, "approval_mode": str | null}
    {"type": "stop_task"}
    {"type": "response", "request_id": str, "payload": {...}}

Server -> extension:
    {"type": "hello_ack", "target_url": str}
    {"type": "request", "request_id": str, "action": str, "params": {...}}
    {"type": "log", "level": str, "message": str}
    {"type": "task_finished", "completed": bool, "stop_reason": str}
    {"type": "error", "message": str}
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from agent.config import AgentConfig, ApprovalMode
from agent.connection import ExtensionConnection, RemoteBrowser
from agent.controller import AgentController
from agent.llm import LLMProvider, create_llm_provider
from agent.memory import Memory
from agent.tools import ToolExecutor

logger = logging.getLogger(__name__)

#: How to build the LLM provider for a task; overridable in tests so no real API is called.
LLMFactory = Callable[[AgentConfig], LLMProvider]

#: The public landing page served at "/" - deliberately generic company
#: branding with no mention of what this backend actually does.
_LANDING_PAGE_PATH = Path(__file__).parent / "static" / "index.html"


def create_app(config: AgentConfig, llm_factory: LLMFactory = create_llm_provider) -> FastAPI:
    """Build the FastAPI application bound to ``config``.

    A factory (rather than a module-level singleton) so tests can construct
    an app against an isolated, temp-directory :class:`AgentConfig` and swap
    in a scripted LLM provider instead of calling a real API.
    """
    app = FastAPI(title="Web Agent", description="Playwright-free browser agent server")
    app.state.config = config
    landing_page_html = _LANDING_PAGE_PATH.read_text(encoding="utf-8")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return landing_page_html

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await _handle_session(websocket, config, llm_factory)

    return app


async def _handle_session(websocket: WebSocket, config: AgentConfig, llm_factory: LLMFactory) -> None:
    """Own one extension's WebSocket connection for its full lifetime."""
    token = websocket.query_params.get("token")
    if not config.auth_token or token != config.auth_token:
        logger.warning("Rejected WebSocket connection with invalid token.")
        await websocket.close(code=4401, reason="invalid or missing token")
        return

    await websocket.accept()
    logger.info("Extension connected.")

    connection = ExtensionConnection(websocket, request_timeout=config.request_timeout_seconds)
    browser = RemoteBrowser(connection)
    await connection.send_hello_ack(config.target_url)

    active_controller: AgentController | None = None
    active_task: asyncio.Task | None = None

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")

            if msg_type == "response":
                request_id = message.get("request_id", "")
                connection.resolve_response(request_id, message.get("payload", {}))

            elif msg_type == "start_task":
                if active_task is not None and not active_task.done():
                    await connection.send_error("A task is already running; send stop_task first.")
                    continue
                goal = message.get("goal", "").strip()
                if not goal:
                    await connection.send_error("start_task requires a non-empty 'goal'.")
                    continue

                task_config = _effective_config(config, message)
                memory = Memory(goal=goal)
                tools = ToolExecutor(browser=browser, config=task_config, memory=memory)
                llm = llm_factory(task_config)
                active_controller = AgentController(config=task_config, llm=llm, tools=tools, memory=memory)
                active_task = _spawn_task(active_controller, memory, connection, task_config, goal)

            elif msg_type == "stop_task":
                if active_controller is not None:
                    active_controller.request_stop()
                else:
                    await connection.send_error("No task is currently running.")

            elif msg_type == "hello":
                logger.debug("Received hello from extension.")

            else:
                logger.warning("Unknown message type from extension: %r", msg_type)

    except WebSocketDisconnect:
        logger.info("Extension disconnected.")
    finally:
        if active_controller is not None:
            active_controller.request_stop()
        connection.cancel_all("connection closed")


def _effective_config(config: AgentConfig, start_message: dict) -> AgentConfig:
    """Apply a task's optional dry_run/approval_mode overrides on top of the base config."""
    overrides: dict = {}
    if start_message.get("dry_run") is not None:
        overrides["dry_run"] = bool(start_message["dry_run"])
    if start_message.get("approval_mode"):
        overrides["approval_mode"] = ApprovalMode(start_message["approval_mode"])
    return dataclasses.replace(config, **overrides) if overrides else config


def _spawn_task(
    controller: AgentController,
    memory: Memory,
    connection: ExtensionConnection,
    config: AgentConfig,
    goal: str,
) -> asyncio.Task:
    """Run the controller loop as a background task and report its outcome."""

    async def _run() -> None:
        try:
            await connection.send_log("info", f"Task started: {goal}")
            result = await controller.run(goal)
            memory.save(config.data_folder)
            await connection.send_task_finished(result.completed, result.stop_reason)
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the extension
            logger.exception("Task crashed")
            memory.save(config.data_folder)
            await connection.send_error(f"Task crashed: {exc}")
            await connection.send_task_finished(False, "server_error")

    return asyncio.create_task(_run())

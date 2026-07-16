"""The agent's server: a FastAPI app exposing a small HTTP API the browser
extension talks to via long-polling.

This is the piece meant to run on Render. It never touches a browser itself;
it holds the "brain" (LLM + control loop) and delegates every DOM action to
whichever extension is currently connected, via the request/response protocol
implemented in :mod:`agent.connection`.

Transport: plain HTTPS long-polling, not a WebSocket. A WebSocket's upgrade
handshake is a distinct HTTP mechanism that some corporate/content filters
either can't inspect or simply block outright, breaking the connection with
no way for client-side code to work around it. Long-polling looks exactly
like any other REST call - the extension always initiates, and the "server
pushes a message" half of the protocol is simulated by the extension holding
a `GET /agent/poll` request open until one is available.

Auth: every endpoint requires `Authorization: Bearer <auth_token>` - unlike a
WebSocket, `fetch()` can set arbitrary headers, so there is no need for the
`Sec-WebSocket-Protocol` trick this module used before.

Wire protocol:

    POST /agent/connect
        -> {"session_id": str, "target_url": str}
        Starts a fresh session; call this once, then loop on /agent/poll and
        /agent/message using the returned session_id.

    GET /agent/poll?session_id=<id>
        Long-polls (up to ~25s) for the next message the server wants to
        deliver, one of:
            {"type": "request", "request_id": str, "action": str, "params": {...}}
            {"type": "log", "level": str, "message": str}
            {"type": "task_finished", "completed": bool, "stop_reason": str}
            {"type": "error", "message": str}
        or, if nothing arrived within the timeout: {"type": "idle"}. Either
        way, poll again immediately - this is what stands in for the
        WebSocket's server -> extension direction.

    POST /agent/message?session_id=<id>
        Body is one JSON message, extension -> server:
            {"type": "hello"}
            {"type": "start_task", "goal": str, "dry_run": bool | null, "approval_mode": str | null}
            {"type": "stop_task"}
            {"type": "response", "request_id": str, "payload": {...}}
        Returns {"ok": true} immediately; results/errors are delivered
        asynchronously via /agent/poll, same as the "response" direction on
        a WebSocket would have been.

A session with no poll/message activity for a while is treated as
abandoned and cleaned up (any active task stopped) the next time
/agent/connect runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
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

#: How long a single GET /agent/poll may hang before returning {"type": "idle"}.
POLL_TIMEOUT_SECONDS = 25.0

#: A session untouched (no poll, no message) for this long is considered
#: abandoned and gets cleaned up on the next /agent/connect.
SESSION_IDLE_TIMEOUT_SECONDS = 300.0

#: The public landing page served at "/" - deliberately generic company
#: branding with no mention of what this backend actually does.
_LANDING_PAGE_PATH = Path(__file__).parent / "static" / "index.html"


class _QueueTransport:
    """Adapts an :class:`asyncio.Queue` to the ``SendJSON`` protocol
    :class:`~agent.connection.ExtensionConnection` expects, so it can stay
    completely unaware of whether it's backed by a WebSocket or a poll queue.
    """

    def __init__(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        self._queue = queue

    async def send_json(self, data: dict[str, Any]) -> None:
        await self._queue.put(data)


@dataclasses.dataclass
class _Session:
    """Everything the server needs to remember for one connected extension."""

    session_id: str
    outbound: "asyncio.Queue[dict[str, Any]]"
    connection: ExtensionConnection
    browser: RemoteBrowser
    last_seen: float
    active_controller: AgentController | None = None
    active_task: asyncio.Task | None = None


def create_app(config: AgentConfig, llm_factory: LLMFactory = create_llm_provider) -> FastAPI:
    """Build the FastAPI application bound to ``config``.

    A factory (rather than a module-level singleton) so tests can construct
    an app against an isolated, temp-directory :class:`AgentConfig` and swap
    in a scripted LLM provider instead of calling a real API.
    """
    app = FastAPI(title="Web Agent", description="Playwright-free browser agent server")
    app.state.config = config
    landing_page_html = _LANDING_PAGE_PATH.read_text(encoding="utf-8")

    sessions: dict[str, _Session] = {}

    def _check_auth(authorization: str | None) -> None:
        expected = f"Bearer {config.auth_token}"
        if not config.auth_token or authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing token")

    def _get_session(session_id: str) -> _Session:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session; call /agent/connect again")
        session.last_seen = time.monotonic()
        return session

    def _reap_stale_sessions() -> None:
        now = time.monotonic()
        stale_ids = [sid for sid, s in sessions.items() if now - s.last_seen > SESSION_IDLE_TIMEOUT_SECONDS]
        for sid in stale_ids:
            session = sessions.pop(sid)
            _teardown_session(session, "session timed out")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return landing_page_html

    @app.post("/agent/connect")
    async def agent_connect(authorization: str | None = Header(None)) -> dict[str, Any]:
        _check_auth(authorization)
        _reap_stale_sessions()

        session_id = uuid.uuid4().hex
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        connection = ExtensionConnection(_QueueTransport(queue), request_timeout=config.request_timeout_seconds)
        browser = RemoteBrowser(connection)
        sessions[session_id] = _Session(
            session_id=session_id, outbound=queue, connection=connection, browser=browser, last_seen=time.monotonic()
        )
        logger.info("Extension connected (session=%s).", session_id)
        return {"session_id": session_id, "target_url": config.target_url}

    @app.get("/agent/poll")
    async def agent_poll(session_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        _check_auth(authorization)
        session = _get_session(session_id)
        try:
            return await asyncio.wait_for(session.outbound.get(), timeout=POLL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"type": "idle"}

    @app.post("/agent/message")
    async def agent_message(
        session_id: str, message: dict[str, Any], authorization: str | None = Header(None)
    ) -> dict[str, bool]:
        _check_auth(authorization)
        session = _get_session(session_id)
        await _handle_message(session, message, config, llm_factory)
        return {"ok": True}

    return app


async def _handle_message(session: _Session, message: dict[str, Any], config: AgentConfig, llm_factory: LLMFactory) -> None:
    """Apply one extension -> server message to ``session``."""
    msg_type = message.get("type")

    if msg_type == "response":
        request_id = message.get("request_id", "")
        session.connection.resolve_response(request_id, message.get("payload", {}))

    elif msg_type == "start_task":
        if session.active_task is not None and not session.active_task.done():
            await session.connection.send_error("A task is already running; send stop_task first.")
            return
        goal = message.get("goal", "").strip()
        if not goal:
            await session.connection.send_error("start_task requires a non-empty 'goal'.")
            return

        try:
            task_config = _effective_config(config, message)
            memory = Memory(goal=goal)
            tools = ToolExecutor(browser=session.browser, config=task_config, memory=memory)
            llm = llm_factory(task_config)
        except Exception as exc:  # noqa: BLE001 - e.g. a missing API key or bad model config
            logger.exception("Failed to start task")
            await session.connection.send_error(f"Could not start task: {exc}")
            await session.connection.send_task_finished(False, "server_error")
            return

        session.active_controller = AgentController(config=task_config, llm=llm, tools=tools, memory=memory)
        session.active_task = _spawn_task(session.active_controller, memory, session.connection, task_config, goal)

    elif msg_type == "stop_task":
        if session.active_controller is not None:
            session.active_controller.request_stop()
        else:
            await session.connection.send_error("No task is currently running.")

    elif msg_type == "hello":
        logger.debug("Received hello from extension (session=%s).", session.session_id)

    else:
        logger.warning("Unknown message type from extension: %r", msg_type)


def _teardown_session(session: _Session, reason: str) -> None:
    if session.active_controller is not None:
        session.active_controller.request_stop()
    if session.active_task is not None and not session.active_task.done():
        session.active_task.cancel()
    session.connection.cancel_all(reason)


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

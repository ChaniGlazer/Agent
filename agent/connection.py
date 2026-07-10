"""Request/response protocol between the server and the browser extension.

The server never talks to a browser directly - it sends JSON "request"
messages over a WebSocket to the extension, which executes the actual DOM
work (reading page state, clicking, filling, ...) and replies with a
correlated "response" message. :class:`ExtensionConnection` implements that
correlation; :class:`RemoteBrowser` exposes it as the small, typed API the
rest of the agent (``ToolExecutor``, ``AgentController``) depends on.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol

from agent.page_state import PageState
from agent.utils import ActionResult

logger = logging.getLogger(__name__)

#: Default time to wait for the extension to answer a single request.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class ConnectionTimeoutError(RuntimeError):
    """Raised when the extension does not respond to a request in time."""


class ConnectionClosedError(RuntimeError):
    """Raised when a request cannot be sent because the connection is gone."""


class SendJSON(Protocol):
    """The minimal transport capability :class:`ExtensionConnection` needs.

    Satisfied by ``starlette.websockets.WebSocket`` and by simple fakes in tests.
    """

    async def send_json(self, data: dict[str, Any]) -> None: ...


class ExtensionConnection:
    """Correlates outbound requests to a single extension with their replies.

    One instance is created per WebSocket connection. Incoming "response"
    messages are fed in by the server's receive loop via
    :meth:`resolve_response`; this class does not read from the socket itself,
    which keeps it trivially testable without a real WebSocket.
    """

    def __init__(self, transport: SendJSON, request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> None:
        self._transport = transport
        self._request_timeout = request_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def send_request(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request to the extension and wait for its correlated reply.

        Args:
            action: The request's action name (e.g. "get_page_state").
            params: Action-specific parameters.

        Raises:
            ConnectionTimeoutError: If no reply arrives within the timeout.
            ConnectionClosedError: If the underlying transport rejects the send.
        """
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._transport.send_json(
                {"type": "request", "request_id": request_id, "action": action, "params": params or {}}
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(request_id, None)
            raise ConnectionClosedError(f"Could not send '{action}' request to the extension.") from exc

        try:
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionTimeoutError(
                f"Extension did not respond to '{action}' within {self._request_timeout}s."
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    def resolve_response(self, request_id: str, payload: dict[str, Any]) -> None:
        """Fulfil the pending request matching ``request_id``, if any is still waiting."""
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(payload)
        else:
            logger.debug("Received response for unknown/expired request_id=%s", request_id)

    def cancel_all(self, reason: str) -> None:
        """Fail every pending request, e.g. because the connection dropped."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionClosedError(reason))
        self._pending.clear()

    async def send_log(self, level: str, message: str) -> None:
        """Push a log line to the extension for display in its popup."""
        await self._transport.send_json({"type": "log", "level": level, "message": message})

    async def send_task_finished(self, completed: bool, stop_reason: str) -> None:
        """Notify the extension that the current task has ended."""
        await self._transport.send_json(
            {"type": "task_finished", "completed": completed, "stop_reason": stop_reason}
        )

    async def send_hello_ack(self, target_url: str) -> None:
        """Acknowledge the handshake and tell the extension which site it may act on."""
        await self._transport.send_json({"type": "hello_ack", "target_url": target_url})

    async def send_error(self, message: str) -> None:
        """Report a server-side error to the extension."""
        await self._transport.send_json({"type": "error", "message": message})


class RemoteBrowser:
    """The agent's view of "the browser": a thin, typed wrapper around
    :class:`ExtensionConnection`'s request/response protocol.
    """

    def __init__(self, connection: ExtensionConnection) -> None:
        self._connection = connection

    async def get_page_state(self) -> PageState:
        """Ask the extension for a fresh snapshot of the active tab."""
        payload = await self._connection.send_request("get_page_state")
        return PageState.from_dict(payload)

    async def execute_action(
        self, action: str, selector: str | None = None, text: str | None = None, is_final: bool = False
    ) -> ActionResult:
        """Ask the extension to perform one DOM action and report the outcome."""
        payload = await self._connection.send_request(
            "execute_action",
            {"action": action, "selector": selector, "text": text, "is_final": is_final},
        )
        return ActionResult(
            success=bool(payload.get("success")),
            message=payload.get("message", ""),
            data=payload.get("data"),
            error=payload.get("error"),
        )

    async def request_approval(self, action: str, selector: str | None, text: str | None) -> bool:
        """Ask the human, via the extension's popup, to approve a pending action."""
        payload = await self._connection.send_request(
            "approval", {"action": action, "selector": selector, "text": text}
        )
        return bool(payload.get("approved"))

    async def send_log(self, level: str, message: str) -> None:
        """Forward a log line to the extension (surfaced in its popup)."""
        await self._connection.send_log(level, message)

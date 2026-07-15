"""Unit tests for agent.connection: request/response correlation
(ExtensionConnection) and the typed RemoteBrowser wrapper around it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.connection import (
    ConnectionClosedError,
    ConnectionTimeoutError,
    ExtensionConnection,
    RemoteBrowser,
)


class FakeTransport:
    """Records every JSON message that would have been sent to the extension."""

    def __init__(self, should_fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._should_fail = should_fail

    async def send_json(self, data: dict[str, Any]) -> None:
        if self._should_fail:
            raise RuntimeError("socket is closed")
        self.sent.append(data)


async def _respond_after(transport: FakeTransport, connection: ExtensionConnection, payload: dict, delay: float = 0.01) -> None:
    await asyncio.sleep(delay)
    request_id = transport.sent[-1]["request_id"]
    connection.resolve_response(request_id, payload)


async def test_send_request_resolves_with_matching_response() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=1.0)

    responder = asyncio.create_task(_respond_after(transport, connection, {"success": True}))
    payload = await connection.send_request("get_page_state")
    await responder

    assert payload == {"success": True}
    assert transport.sent[0]["type"] == "request"
    assert transport.sent[0]["action"] == "get_page_state"


async def test_send_request_times_out_when_no_response_arrives() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=0.05)

    with pytest.raises(ConnectionTimeoutError):
        await connection.send_request("get_page_state")


async def test_send_request_raises_when_transport_fails() -> None:
    transport = FakeTransport(should_fail=True)
    connection = ExtensionConnection(transport, request_timeout=1.0)

    with pytest.raises(ConnectionClosedError):
        await connection.send_request("get_page_state")


async def test_cancel_all_fails_every_pending_request() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=5.0)

    task = asyncio.create_task(connection.send_request("get_page_state"))
    await asyncio.sleep(0.01)
    connection.cancel_all("connection closed")

    with pytest.raises(ConnectionClosedError):
        await task


async def test_resolve_response_for_unknown_request_id_is_a_no_op() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport)
    connection.resolve_response("does-not-exist", {"ok": True})  # must not raise


async def test_remote_browser_get_page_state_parses_payload() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=1.0)
    browser = RemoteBrowser(connection)

    responder = asyncio.create_task(
        _respond_after(
            transport, connection,
            {"url": "https://internal.example.local/", "title": "Home", "visible_text": "hi", "elements": [{"selector": "#go"}]},
        )
    )
    state = await browser.get_page_state()
    await responder

    assert transport.sent[-1]["action"] == "get_page_state"
    assert state.url == "https://internal.example.local/"
    assert state.title == "Home"
    assert state.elements[0]["selector"] == "#go"


async def test_remote_browser_execute_action_builds_request_and_parses_result() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=1.0)
    browser = RemoteBrowser(connection)

    responder = asyncio.create_task(
        _respond_after(transport, connection, {"success": True, "message": "clicked", "data": None, "error": None})
    )
    result = await browser.execute_action("click", selector="#go", text=None, is_final=True)
    await responder

    sent = transport.sent[-1]
    assert sent["action"] == "execute_action"
    assert sent["params"] == {"action": "click", "selector": "#go", "text": None, "is_final": True}
    assert result.success is True
    assert result.message == "clicked"


async def test_remote_browser_request_approval() -> None:
    transport = FakeTransport()
    connection = ExtensionConnection(transport, request_timeout=1.0)
    browser = RemoteBrowser(connection)

    responder = asyncio.create_task(_respond_after(transport, connection, {"approved": False}))
    approved = await browser.request_approval("click", "#danger", None)
    await responder

    assert transport.sent[-1]["action"] == "approval"
    assert approved is False

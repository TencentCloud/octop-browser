"""Behavior tests for target-scoped CDP messages."""

from __future__ import annotations

import json
from typing import Any

import pytest

from octop_browser.cdp.client import CDPClient, CDPSessionError


class FakeSocket:
    def __init__(
        self, client: CDPClient, events: list[dict[str, Any]] | None = None
    ) -> None:
        self.client = client
        self.events = events or []
        self.messages: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.messages.append(message)
        response: dict[str, Any] = {"id": message["id"], "result": {"ok": True}}
        if "sessionId" in message:
            response["sessionId"] = message["sessionId"]
        self.client._pending[message["id"]].set_result(response)

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield json.dumps(event)

        return iterate()


async def test_scoped_client_pins_commands_and_detach_to_one_session() -> None:
    client = CDPClient()
    socket = FakeSocket(client)
    client._ws = socket  # noqa: SLF001

    await client.send("Runtime.enable")
    scoped = client.with_session("frame-session")
    await scoped.send("DOM.getDocument", {"depth": -1})
    await scoped.enable_domain("Page")
    await scoped.close()

    assert "sessionId" not in socket.messages[0]
    assert [message.get("sessionId") for message in socket.messages[1:-1]] == [
        "frame-session",
        "frame-session",
    ]
    assert socket.messages[-1] == {
        "id": 4,
        "method": "Target.detachFromTarget",
        "params": {"sessionId": "frame-session"},
    }
    assert scoped.with_session("frame-session") is scoped
    with pytest.raises(CDPSessionError):
        scoped.with_session("other-session")


async def test_event_payload_preserves_top_level_session_id() -> None:
    client = CDPClient()
    client._ws = FakeSocket(  # noqa: SLF001
        client,
        events=[
            {
                "method": "Runtime.executionContextCreated",
                "sessionId": "frame-session",
                "params": {"context": {"id": 7, "auxData": {"isDefault": True}}},
            }
        ],
    )
    events: list[dict[str, Any]] = []
    client.on("Runtime.executionContextCreated", events.append)

    await client._recv_loop()  # noqa: SLF001

    assert events == [
        {
            "context": {"id": 7, "auxData": {"isDefault": True}},
            "sessionId": "frame-session",
            "__client": client,
        }
    ]

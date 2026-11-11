from __future__ import annotations

import asyncio

import pytest

from octop_browser.actions.navigate import navigate, validate_navigation_url


class FakeClient:
    def __init__(self, response: dict):
        self.response = response
        self.listeners: dict[str, object] = {}
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        return self.response

    def on(self, event: str, callback):
        self.listeners[event] = callback
        if event == "Page.loadEventFired":
            callback({})

    def off(self, event: str, callback):
        self.listeners.pop(event, None)


class FakeRefCache:
    def __init__(self):
        self.invalidated = False

    def invalidate(self) -> None:
        self.invalidated = True


class LoadDuringNavigateClient:
    def __init__(self):
        self.listeners: dict[str, object] = {}
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method == "Page.navigate":
            callback = self.listeners.get("Page.loadEventFired")
            assert callback is not None, (
                "load listener must be registered before Page.navigate"
            )
            callback({})
            return {"frameId": "F"}
        raise AssertionError(f"unexpected CDP method {method}")

    def on(self, event: str, callback):
        self.listeners[event] = callback

    def off(self, event: str, callback):
        if self.listeners.get(event) is callback:
            self.listeners.pop(event, None)


@pytest.mark.asyncio
async def test_navigate_reports_page_navigate_error_text() -> None:
    client = FakeClient({"frameId": "F", "errorText": "net::ERR_NAME_NOT_RESOLVED"})
    ref_cache = FakeRefCache()

    result = await navigate(client, ref_cache, "https://bad.invalid")

    assert result.success is False
    assert (
        result.error
        == "Navigation failed for https://bad.invalid: net::ERR_NAME_NOT_RESOLVED"
    )
    assert result.content == ""
    assert ref_cache.invalidated is True
    assert client.sent == [("Page.navigate", {"url": "https://bad.invalid"})]


@pytest.mark.asyncio
async def test_navigate_registers_load_listener_before_page_navigate() -> None:
    client = LoadDuringNavigateClient()
    ref_cache = FakeRefCache()

    result = await navigate(client, ref_cache, "https://example.com/fast")

    assert result.success is True
    assert result.content == "Navigated to https://example.com/fast"
    assert ref_cache.invalidated is True
    assert client.sent == [("Page.navigate", {"url": "https://example.com/fast"})]
    assert client.listeners == {}


@pytest.mark.asyncio
async def test_navigate_does_not_wait_thirty_seconds_when_load_event_is_missing(
    monkeypatch,
) -> None:
    client = FakeClient({"frameId": "F"})
    ref_cache = FakeRefCache()
    observed_timeout: float | None = None

    async def fake_wait_for(future, timeout):
        nonlocal observed_timeout
        observed_timeout = timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    result = await navigate(client, ref_cache, "https://example.com/slow")

    assert result.success is True
    assert observed_timeout is not None
    assert observed_timeout <= 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_url",
    [
        "file:///etc/passwd",
        "file:///",
        "file:/etc/passwd",
        "FILE:///C:/Windows/win.ini",
        "  file:///etc/passwd  ",
        "view-source:file:///etc/passwd",
        "view-source:https://example.com",
        "javascript:alert(1)",
        "chrome://settings",
        "chrome-extension://abcdef/popup.html",
        "devtools://devtools/bundled/inspector.html",
        "filesystem:https://example.com/temporary/a.txt",
        "about:config",
        "https://",
        "data:",
        "/etc/passwd",
        "",
        "   ",
        "https://example.com/\n",
    ],
)
async def test_navigate_blocks_unsafe_url_schemes_without_cdp(blocked_url: str) -> None:
    client = FakeClient({"frameId": "F"})
    ref_cache = FakeRefCache()

    result = await navigate(client, ref_cache, blocked_url)

    assert result.success is False
    assert result.content == ""
    assert result.error is not None
    assert "Blocked" in result.error
    assert client.sent == []
    assert client.listeners == {}
    assert ref_cache.invalidated is False


@pytest.mark.parametrize(
    "allowed_url",
    [
        "https://example.com",
        "http://127.0.0.1:8080/demo.html",
        "data:text/html,<h1>Hello</h1>",
        "about:blank",
    ],
)
def test_validate_navigation_url_allows_safe_schemes(allowed_url: str) -> None:
    assert validate_navigation_url(allowed_url) is None

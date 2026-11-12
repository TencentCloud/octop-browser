"""Navigation actions: navigate, go_back, go_forward, reload."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

from octop_browser.cdp.client import CDPClient
from octop_browser.dom.refs import RefCache
from octop_browser.models import ActionMetrics, ToolResult

_LOAD_EVENT_TIMEOUT_SECONDS = 5.0


def validate_navigation_url(url: str) -> str | None:
    """Validate that a navigation target uses a safe URL scheme.

    Returns a descriptive error message when blocked, or ``None`` when allowed.
    """
    stripped = url.strip()
    if not stripped:
        return "Blocked navigation URL: URL must not be empty."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return f"Blocked navigation URL {url!r}: control characters are not allowed."
    try:
        parsed = urlsplit(stripped)
    except ValueError as exc:
        return f"Blocked invalid navigation URL {url!r}: {exc}"

    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        if not parsed.netloc:
            return f"Blocked navigation URL {url!r}: missing host."
        return None
    if scheme == "data":
        if not parsed.path:
            return f"Blocked navigation URL {url!r}: empty data URL."
        return None
    if scheme == "about" and stripped.lower() == "about:blank":
        return None
    return (
        f"Blocked unsafe URL scheme {scheme or '<none>'!r} for navigation target: "
        "only http, https, data, and about:blank URLs are allowed."
    )


def _metrics(action: str, start: float) -> ActionMetrics:
    return ActionMetrics(
        action=action,
        duration_ms=int((time.monotonic() - start) * 1000),
        dom_nodes_scanned=0,
        estimated_tokens=20,
    )


async def navigate(client: CDPClient, ref_cache: RefCache, url: str) -> ToolResult:
    """Navigate the page to a URL and wait for load."""
    start = time.monotonic()
    if url_error := validate_navigation_url(url):
        return ToolResult(
            success=False,
            content="",
            error=url_error,
            metrics=_metrics("navigate", start),
        )
    ref_cache.invalidate()
    load_event: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    def on_load(_params: dict[str, Any]) -> None:
        if not load_event.done():
            load_event.set_result(None)

    client.on("Page.loadEventFired", on_load)
    try:
        result = await client.send("Page.navigate", {"url": url})
        error_text = result.get("errorText")
        if error_text:
            return ToolResult(
                success=False,
                content="",
                error=f"Navigation failed for {url}: {error_text}",
                metrics=_metrics("navigate", start),
            )
        try:
            await asyncio.wait_for(load_event, timeout=_LOAD_EVENT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass
    finally:
        client.off("Page.loadEventFired", on_load)

    _ = result.get("frameId", url)
    content = f"Navigated to {url}"
    return ToolResult(
        success=True, content=content, metrics=_metrics("navigate", start)
    )


async def go_back(client: CDPClient, ref_cache: RefCache) -> ToolResult:
    """Navigate back in browser history."""
    start = time.monotonic()
    ref_cache.invalidate()
    await client.send("Runtime.evaluate", {"expression": "history.back()"})
    return ToolResult(
        success=True, content="Navigated back", metrics=_metrics("go_back", start)
    )


async def go_forward(client: CDPClient, ref_cache: RefCache) -> ToolResult:
    """Navigate forward in browser history."""
    start = time.monotonic()
    ref_cache.invalidate()
    await client.send("Runtime.evaluate", {"expression": "history.forward()"})
    return ToolResult(
        success=True, content="Navigated forward", metrics=_metrics("go_forward", start)
    )


async def reload(client: CDPClient, ref_cache: RefCache) -> ToolResult:
    """Reload the current page."""
    start = time.monotonic()
    ref_cache.invalidate()
    await client.send("Page.reload", {"ignoreCache": False})
    return ToolResult(
        success=True, content="Page reloaded", metrics=_metrics("reload", start)
    )

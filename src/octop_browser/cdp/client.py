"""Pure asyncio Chrome DevTools Protocol WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from octop_browser.settings import settings as _settings

logger = logging.getLogger(__name__)


class CDPSessionError(Exception):
    """Raised when a CDP command fails or the session is invalid."""


class ActivityTracker(Protocol):
    # pylint: disable=multiple-statements  # ruff format collapses stub bodies
    def activity_started(self) -> None: ...

    def activity_finished(self) -> None: ...


def _is_page_level_url(ws_url: str) -> bool:
    """Classify a CDP WebSocket URL.

    Chrome's page-level debug URLs contain ``/devtools/page/<id>`` and bind the
    socket to exactly one page — page-domain commands go out as-is. Everything
    else (Chrome's ``/devtools/browser/<id>`` and single-endpoint servers such
    as Lightpanda's ``ws://host:port/``) is a browser-level endpoint where
    page-domain commands must carry a session id obtained from
    ``Target.attachToTarget``.
    """
    return "/devtools/page/" in ws_url


class CDPClient:
    """
    Manages a single CDP WebSocket connection to one browser page.

    On a page-level endpoint the socket talks to that page directly. On a
    browser-level endpoint :meth:`connect` attaches to a page target (reusing
    an existing one when possible, creating one otherwise) and stamps every
    subsequent command with the returned session id, so callers can use the
    same page-domain API either way.

    Usage::

        client = CDPClient()
        await client.connect("ws://localhost:9222/devtools/page/ABC")
        result = await client.send("Page.navigate", {"url": "https://example.com"})
        await client.close()
    """

    def __init__(
        self,
        timeout: float | None = None,
        *,
        activity_tracker: ActivityTracker | None = None,
    ) -> None:
        self._timeout = timeout if timeout is not None else _settings.cdp_timeout
        self._activity_tracker = activity_tracker
        self._ws: ClientConnection | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._id = 0
        self._session_id: str | None = None
        self._current_target_id: str | None = None
        self._current_target_url: str | None = None
        self._browser_level = False

    @property
    def is_browser_level(self) -> bool:
        """True when connected to a browser-level endpoint (Target domain available)."""
        return self._browser_level

    @property
    def session_id(self) -> str | None:
        """Session id stamped on outgoing commands, if any."""
        return self._session_id

    @property
    def current_target_id(self) -> str | None:
        """Target id of the attached page session, if any."""
        return self._current_target_id

    @property
    def current_target_url(self) -> str | None:
        """URL of the attached page target as last reported by the browser."""
        return self._current_target_url

    async def connect(self, ws_url: str, target_id: str | None = None) -> None:
        """Connect to a CDP WebSocket endpoint.

        ``websockets`` defaults to a 1 MiB receive frame cap; CDP responses
        for ``Page.captureScreenshot`` routinely exceed that on real-world
        pages (a base64 PNG of a 1440×900 viewport easily lands at 1.3-2 MiB).
        Hitting the cap closes the socket with code 1009 and the agent sees
        a confusing ``message too big`` error mid-action. Honor
        ``settings.cdp_max_message_size`` so screenshots up to that size
        come through cleanly.

        Browser-level endpoints are probed and attached here: page-domain
        commands sent without a session id fail on standard CDP servers
        (Chrome rejects them; Lightpanda answers
        ``BrowserContextNotLoaded``), which made ``BROWSER_USE_CDP_WS_URL``
        unusable against such servers.

        Args:
            ws_url: Endpoint to dial.
            target_id: Browser-level endpoints only — attach to this page
                target instead of picking/creating one. When the preferred
                target no longer exists, connect falls back to automatic
                selection instead of failing. Callers that manage several
                pages over one socket (the recorder) pass it explicitly.
        """
        ws = await websockets.connect(
            ws_url,
            max_size=_settings.cdp_max_message_size,
        )
        self._ws = ws
        self._recv_task = asyncio.create_task(self._recv_loop())
        if _is_page_level_url(ws_url):
            self._browser_level = False
            self._session_id = None
            logger.debug("CDP connected to page-level endpoint %s", ws_url)
            return
        self._browser_level = True
        try:
            try:
                await self.attach_page_session(target_id)
            except CDPSessionError:
                if target_id is None:
                    raise
                # The remembered target died between sessions; attach to
                # whatever page the browser still has instead of failing.
                logger.debug(
                    "Preferred target %s is gone; attaching to an available page",
                    target_id,
                )
                await self.attach_page_session()
        except CDPSessionError as exc:
            await self.close()
            raise CDPSessionError(
                f"Browser-level endpoint {ws_url} could not attach to a page "
                f"target: {exc}"
            ) from exc
        logger.debug("CDP connected to browser-level endpoint %s", ws_url)

    async def attach_page_session(self, target_id: str | None = None) -> str:
        """Attach to a page target; later commands carry its session id.

        Browser-level connections only. Resolves *target_id* — or picks an
        existing page target (creating one when none exists) when omitted —
        and issues ``Target.attachToTarget(flatten=True)``. Returns the new
        session id. Switching targets re-stamps the same socket instead of
        reconnecting.
        """
        if self._ws is None:
            raise CDPSessionError("Not connected")
        url: str | None = None
        if target_id is None:
            target_id, url = await self._pick_page_target()
        else:
            pages = await self._page_targets()
            match = next((t for t in pages if t.get("targetId") == target_id), None)
            if match is not None:
                url = match.get("url") or None
        result = await self._send_command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            session_id=None,
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise CDPSessionError(
                f"Target.attachToTarget returned no session id for target {target_id}"
            )
        self._session_id = session_id
        self._current_target_id = target_id
        self._current_target_url = url
        return session_id

    async def _page_targets(self) -> list[dict[str, Any]]:
        """Return the browser's current page target infos."""
        result = await self._send_command("Target.getTargets", session_id=None)
        return [t for t in result.get("targetInfos", []) if t.get("type") == "page"]

    async def _pick_page_target(self) -> tuple[str, str | None]:
        """Return ``(target_id, url)`` of a usable page target.

        Preference order mirrors ``launcher.get_page_ws_url``: a non-blank
        page first, then any page, then a freshly created ``about:blank``.
        """
        pages = await self._page_targets()
        non_blank = [t for t in pages if t.get("url") and t.get("url") != "about:blank"]
        for candidate in [*non_blank, *pages]:
            target_id = candidate.get("targetId")
            if isinstance(target_id, str) and target_id:
                return target_id, (candidate.get("url") or None)
        created = await self._send_command(
            "Target.createTarget", {"url": "about:blank"}, session_id=None
        )
        target_id = created.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise CDPSessionError(
                "No page target available and Target.createTarget returned none"
            )
        return target_id, "about:blank"

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        track_activity: bool = True,
    ) -> dict[str, Any]:
        """Send a CDP command and await its response."""
        return await self._send_command(
            method, params, self._session_id, track_activity=track_activity
        )

    async def _send_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        track_activity: bool = True,
    ) -> dict[str, Any]:
        """Send one command, stamped with *session_id* when given.

        Browser-domain commands (``Target.*``) pass ``session_id=None``;
        page-domain commands go through :meth:`send`, which stamps the
        session id from the last ``Target.attachToTarget``.
        """
        if self._ws is None:
            raise CDPSessionError("Not connected")
        tracker = self._activity_tracker if track_activity else None
        if tracker is not None:
            tracker.activity_started()
        try:
            self._id += 1
            msg_id = self._id
            payload_dict: dict[str, Any] = {
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
            if session_id:
                payload_dict["sessionId"] = session_id
            payload = json.dumps(payload_dict)
            loop = asyncio.get_event_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[msg_id] = future
            try:
                await self._ws.send(payload)
                result = await asyncio.wait_for(future, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                self._pending.pop(msg_id, None)
                raise CDPSessionError(
                    f"Timeout waiting for response to {method}"
                ) from exc
            except CDPSessionError:
                raise
            except Exception as exc:
                self._pending.pop(msg_id, None)
                raise CDPSessionError(f"CDP send failed for {method}: {exc}") from exc
        finally:
            if tracker is not None:
                tracker.activity_finished()
        if "error" in result:
            raise CDPSessionError(f"CDP error for {method}: {result['error']}")
        inner: dict[str, Any] = result.get("result", {})
        return inner

    async def send_passive(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a health/status probe without extending the idle deadline."""
        return await self.send(method, params, track_activity=False)

    async def enable_domain(self, domain: str) -> None:
        """Enable a CDP domain (e.g. 'DOM', 'Page', 'Input')."""
        await self.send(f"{domain}.enable")

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a listener for a CDP event."""
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        """Unregister a listener."""
        listeners = self._listeners.get(event, [])
        if callback in listeners:
            listeners.remove(callback)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        ws = self._ws
        if ws:
            self._ws = None
            await ws.close()
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._session_id = None
        self._current_target_id = None
        self._current_target_url = None
        self._browser_level = False
        logger.debug("CDP connection closed")

    async def _recv_loop(self) -> None:
        """Background task: read messages and dispatch to pending futures or
        listeners."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg: dict[str, Any] = json.loads(raw)
                if "id" in msg:
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif "method" in msg:
                    method: str = msg["method"]
                    params: dict[str, Any] = msg.get("params", {})
                    callback_params = dict(params)
                    callback_params["__client"] = self
                    for cb in self._listeners.get(method, []):
                        result = cb(callback_params)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            logger.debug("CDP recv loop ended: %s", exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPSessionError(f"Connection lost: {exc}"))
            self._pending.clear()

"""Direct CDP mode (BROWSER_USE_CDP_WS_URL) against a mock CDP server.

Covers the direct-connect defects found while driving Lightpanda
(single-endpoint CDP server, no HTTP /json):

* browser-level endpoints attach a page session automatically — page-domain
  commands without a session id used to fail with BrowserContextNotLoaded
* page-level endpoints (Chrome's /devtools/page/<id>) keep the no-session
  behaviour, so the local-Chrome path does not regress
* tab switching re-attaches over the same socket via Target.attachToTarget
  instead of raising or relying on Chrome-only HTTP /json
* viewport failures warn instead of failing silently
* close(kill=True) leaves a remote browser running and says so
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import Server, serve

from octop_browser.cdp.client import CDPClient
from octop_browser.profile import ProfileManager
from octop_browser.session import BrowserSession
from octop_browser.settings import OctopSettings


class FakeCDPServer:
    """Minimal flatten-mode CDP server speaking real WebSocket.

    ``require_session`` mimics a standard browser-level endpoint: page-domain
    commands without a session id are rejected (Lightpanda answers
    BrowserContextNotLoaded, Chrome drops the command). Page-level endpoint
    tests turn it off.
    """

    def __init__(self) -> None:
        self.targets: list[dict[str, Any]] = [
            {
                "targetId": "TAB-1",
                "type": "page",
                "url": "https://example.com",
                "title": "Example",
            },
            {
                "targetId": "TAB-9",
                "type": "page",
                "url": "about:blank",
                "title": "",
            },
        ]
        self.attached: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.viewport: dict[str, Any] | None = None
        self.fail_viewport = False
        self.require_session = True
        self.create_target_already_loaded = False
        self._server: Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def ws_url(self, path: str = "/") -> str:
        return f"ws://127.0.0.1:{self.port}{path}"

    def methods(self) -> list[str]:
        return [str(m["method"]) for m in self.received]

    async def _handler(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            await self._dispatch(ws, msg)

    async def _dispatch(self, ws: Any, msg: dict[str, Any]) -> None:
        method = msg["method"]
        msg_id = msg["id"]
        params = msg.get("params", {})
        if method == "Target.getTargets":
            await self._reply(ws, msg_id, {"targetInfos": self.targets})
        elif method == "Target.createTarget":
            if self.create_target_already_loaded:
                await self._error(ws, msg_id, "TargetAlreadyLoaded")
                return
            target_id = f"NEW-{len(self.targets) + 1}"
            self.targets.append(
                {
                    "targetId": target_id,
                    "type": "page",
                    "url": str(params.get("url") or "about:blank"),
                    "title": "",
                }
            )
            await self._reply(ws, msg_id, {"targetId": target_id})
        elif method == "Target.attachToTarget":
            requested = params.get("targetId")
            if not any(t.get("targetId") == requested for t in self.targets):
                await self._error(ws, msg_id, "No target with given id")
                return
            self.attached.append(
                {
                    "targetId": params.get("targetId"),
                    "flatten": params.get("flatten"),
                    "sessionId": msg.get("sessionId"),
                }
            )
            await self._reply(ws, msg_id, {"sessionId": f"SID-{len(self.attached)}"})
        elif method == "Target.activateTarget":
            await self._reply(ws, msg_id, {})
        elif method == "Target.closeTarget":
            closed = params.get("targetId")
            self.targets = [t for t in self.targets if t.get("targetId") != closed]
            await self._reply(ws, msg_id, {"success": True})
        elif method in ("Page.enable", "DOM.enable", "Runtime.enable"):
            if self.require_session and not msg.get("sessionId"):
                await self._error(ws, msg_id, "missing sessionId on page command")
                return
            await self._reply(ws, msg_id, {})
        elif method == "Emulation.setDeviceMetricsOverride":
            if self.fail_viewport:
                await self._error(ws, msg_id, "Emulation domain unavailable")
                return
            self.viewport = dict(params)
            await self._reply(ws, msg_id, {})
        elif method == "Page.navigate":
            await self._reply(ws, msg_id, {"frameId": "FRAME-1"})
            await ws.send(json.dumps({"method": "Page.loadEventFired", "params": {}}))
        elif method == "Runtime.evaluate":
            await self._reply(
                ws,
                msg_id,
                {"result": {"type": "string", "value": params.get("expression", "")}},
            )
        else:
            await self._reply(ws, msg_id, {})

    async def _reply(self, ws: Any, msg_id: int, result: dict[str, Any]) -> None:
        await ws.send(json.dumps({"id": msg_id, "result": result}))

    async def _error(self, ws: Any, msg_id: int, message: str) -> None:
        await ws.send(
            json.dumps({"id": msg_id, "error": {"code": -32000, "message": message}})
        )


@pytest.fixture()
async def cdp_server() -> Any:
    server = FakeCDPServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def make_session(ws_url: str, tmp_path: Any, **overrides: Any) -> BrowserSession:
    pm = ProfileManager(base_dir=tmp_path / "profiles")
    cfg = OctopSettings(cdp_ws_url=ws_url, **overrides)
    return await BrowserSession.create(
        profile="direct", profile_manager=pm, settings=cfg
    )


_PAGE_ENABLES = ("Page.enable", "DOM.enable", "Runtime.enable")


async def test_browser_level_endpoint_attaches_page_session(cdp_server, tmp_path):
    """A browser-level URL gets an automatic Target.attachToTarget session and
    every page-domain command carries the returned session id."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        # Reuses the non-blank TAB-1, same preference as launcher.get_page_ws_url.
        assert cdp_server.attached == [
            {"targetId": "TAB-1", "flatten": True, "sessionId": None}
        ]
        assert sess._internal.client.session_id == "SID-1"
        enables = [m for m in cdp_server.received if m["method"] in _PAGE_ENABLES]
        assert len(enables) == 3
        assert all(m.get("sessionId") == "SID-1" for m in enables)

        result = await sess.eval_js("1 + 1")
        assert result.success is True
        eval_msg = [
            m for m in cdp_server.received if m["method"] == "Runtime.evaluate"
        ][-1]
        assert eval_msg.get("sessionId") == "SID-1"
    finally:
        await sess.close()


async def test_browser_level_endpoint_creates_target_when_none_exists(
    cdp_server, tmp_path
):
    """Zero page targets: connect creates an about:blank one and attaches."""
    cdp_server.targets = []
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        assert cdp_server.attached[0]["targetId"] == "NEW-1"
        assert cdp_server.targets[0]["url"] == "about:blank"
    finally:
        await sess.close()


async def test_page_level_endpoint_skips_session_layer(cdp_server, tmp_path):
    """Chrome's page-level URLs keep the original no-session behaviour."""
    cdp_server.require_session = False
    sess = await make_session(cdp_server.ws_url("/devtools/page/ABC"), tmp_path)
    try:
        assert cdp_server.attached == []
        assert sess._internal.client.session_id is None
        assert sess._internal.client.is_browser_level is False
        enables = [m for m in cdp_server.received if m["method"] in _PAGE_ENABLES]
        assert len(enables) == 3
        assert all(m.get("sessionId") is None for m in enables)
        result = await sess.eval_js("location.href")
        assert result.success is True
    finally:
        await sess.close()


async def test_switch_tab_reattaches_over_same_socket(cdp_server, tmp_path):
    """Tab switching re-stamps the session id on the live socket — no
    reconnect, no HTTP /json lookup."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        before = len(cdp_server.received)
        result = await sess.switch_tab("TAB-9")
        assert result.success is True
        assert cdp_server.attached[-1] == {
            "targetId": "TAB-9",
            "flatten": True,
            "sessionId": None,
        }
        assert sess._internal.client.session_id == "SID-2"
        # Domains are per-session: re-enabled on the new session id.
        new_enables = [
            m for m in cdp_server.received[before:] if m["method"] in _PAGE_ENABLES
        ]
        assert len(new_enables) == 3
        assert all(m.get("sessionId") == "SID-2" for m in new_enables)
        # The socket survived: no second connection was dialed.
        assert cdp_server.methods().count("Target.attachToTarget") == 2
    finally:
        await sess.close()


async def test_viewport_applied_with_session_id(cdp_server, tmp_path):
    """Emulation.setDeviceMetricsOverride reaches the page with the session id
    and the configured dimensions."""
    sess = await make_session(
        cdp_server.ws_url(), tmp_path, viewport_width=1280, viewport_height=800
    )
    try:
        assert cdp_server.viewport == {
            "width": 1280,
            "height": 800,
            "deviceScaleFactor": 1,
            "mobile": False,
        }
        viewport_msg = [
            m
            for m in cdp_server.received
            if m["method"] == "Emulation.setDeviceMetricsOverride"
        ][-1]
        assert viewport_msg.get("sessionId") == "SID-1"
    finally:
        await sess.close()


async def test_viewport_failure_warns_and_session_survives(
    caplog, cdp_server, tmp_path
):
    """A rejected viewport override is logged, not swallowed, and does not
    take the session down."""
    cdp_server.fail_viewport = True
    with caplog.at_level(logging.WARNING, logger="octop_browser.session"):
        sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        assert sess._internal.client.session_id == "SID-1"
        assert any(
            "setDeviceMetricsOverride" in record.message for record in caplog.records
        )
        result = await sess.eval_js("1")
        assert result.success is True
    finally:
        await sess.close()


async def test_close_kill_leaves_remote_browser_running(
    caplog, cdp_server, tmp_path, monkeypatch
):
    """kill=True in direct mode must not terminate the remote browser; it
    says so instead of acting silently."""
    terminated: list[Any] = []

    async def fake_terminate(*args: Any, **kwargs: Any) -> bool:
        terminated.append(True)
        return True

    monkeypatch.setattr("octop_browser.cdp.launcher.terminate_browser", fake_terminate)
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    with caplog.at_level(logging.INFO, logger="octop_browser.session"):
        await sess.close(kill=True)
    assert terminated == []
    assert any("direct CDP mode" in record.message for record in caplog.records)


async def test_list_tabs_uses_cdp_targets_in_direct_mode(cdp_server, tmp_path):
    """list_tabs reads Target.getTargets instead of Chrome's HTTP /json."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.list_tabs()
        assert result.success is True
        tabs = result.metadata["tabs"]  # type: ignore[index]
        assert [t["tab_id"] for t in tabs] == ["TAB-1", "TAB-9"]
        assert tabs[0]["active"] is True
        assert tabs[1]["active"] is False
    finally:
        await sess.close()


async def test_new_tab_reuses_blank_target_via_cdp(cdp_server, tmp_path):
    """A background about:blank target is found through CDP and reused — no
    new target is created."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.new_tab("https://example.org/x")
        assert result.success is True
        assert result.content == "TAB-9"
        assert result.metadata == {"target_id": "TAB-9"}
        assert "Target.createTarget" not in cdp_server.methods()
        assert cdp_server.attached[-1]["targetId"] == "TAB-9"
        nav_msg = [m for m in cdp_server.received if m["method"] == "Page.navigate"][-1]
        assert nav_msg["params"]["url"] == "https://example.org/x"
        assert nav_msg.get("sessionId") == "SID-2"
    finally:
        await sess.close()


async def test_new_tab_force_new_creates_and_attaches(cdp_server, tmp_path):
    """force_new creates a target and attaches the session to it."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.new_tab("https://example.org/new", force_new=True)
        assert result.success is True
        assert result.metadata == {"target_id": "NEW-3"}
        create_msg = [
            m for m in cdp_server.received if m["method"] == "Target.createTarget"
        ][-1]
        assert create_msg["params"]["url"] == "https://example.org/new"
        assert cdp_server.attached[-1]["targetId"] == "NEW-3"
        assert sess._internal.client.session_id == "SID-2"
    finally:
        await sess.close()


async def test_close_tab_removes_target(cdp_server, tmp_path):
    """close_tab issues Target.closeTarget over the browser-level session."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.close_tab("TAB-9")
        assert result.success is True
        close_msg = [
            m for m in cdp_server.received if m["method"] == "Target.closeTarget"
        ][-1]
        assert close_msg["params"]["targetId"] == "TAB-9"
        assert [t["targetId"] for t in cdp_server.targets] == ["TAB-1"]
    finally:
        await sess.close()


# ----------------------------------------------------------------------
# Second batch: target persistence, explicit-target attach, recorder
# ----------------------------------------------------------------------


async def test_connect_persists_target_for_cross_process_reuse(cdp_server, tmp_path):
    """Direct mode used to skip profile.save_target, leaving
    _current_target_id() (and list_tabs' active marker) empty."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        target_file = sess._internal._profile.data_dir / ".octop-browser-target.json"
        assert target_file.is_file()
        assert json.loads(target_file.read_text())["target_id"] == "TAB-1"
        assert sess._current_target_id() == "TAB-1"
    finally:
        await sess.close()


async def test_list_tabs_marks_persisted_target_active(cdp_server, tmp_path):
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.list_tabs()
        tabs = result.metadata["tabs"]  # type: ignore[index]
        assert tabs[0]["active"] is True
        assert tabs[0]["tab_id"] == "TAB-1"
    finally:
        await sess.close()


async def test_connect_with_explicit_target_id(cdp_server):
    """Recorder-style usage: attach to a named target, not the preferred one."""
    client = CDPClient()
    await client.connect(cdp_server.ws_url(), target_id="TAB-9")
    try:
        assert cdp_server.attached[-1]["targetId"] == "TAB-9"
        assert client.current_target_id == "TAB-9"
        assert client.session_id == "SID-1"
        await client.enable_domain("Page")
        assert cdp_server.received[-1].get("sessionId") == "SID-1"
    finally:
        await client.close()


async def test_recorder_direct_mode_discovers_new_tab(cdp_server, tmp_path):
    """A tab opened after recording starts is discovered over CDP
    (Target.getTargets) and gets its own client plus a page_opened event.
    Before the fix, direct mode relied on Chrome-only HTTP /json and never
    saw new tabs."""
    from datetime import datetime, timezone

    from octop_browser.record.models import (
        PrivacySettings,
        RecordingManifest,
        RecordingTarget,
    )
    from octop_browser.record.recorder import RecordSession
    from octop_browser.record.settings import RecordReplaySettings
    from octop_browser.record.store import RecordingStore

    store = RecordingStore(RecordReplaySettings(base_dir=tmp_path / "rec"))
    manifest = RecordingManifest(
        recording_id="rec_test",
        created_at=datetime.now(timezone.utc).isoformat(),
        profile="direct",
        target=RecordingTarget(start_url="https://example.com"),
        privacy=PrivacySettings(),
    )
    store.create(manifest)
    client = CDPClient()
    await client.connect(cdp_server.ws_url())
    session = RecordSession(
        client,
        store,
        manifest,
        cdp_ws_url=cdp_server.ws_url(),
        initial_target_id=client.current_target_id,
    )
    try:
        await session.start()
        assert "TAB-1" in session._clients
        cdp_server.targets.append(
            {
                "targetId": "TAB-2",
                "type": "page",
                "url": "https://new.example",
                "title": "New",
            }
        )
        await session._attach_known_targets()
        assert "TAB-2" in session._clients
        assert session._client_target_ids[id(session._clients["TAB-2"])] == "TAB-2"
        assert session._clients["TAB-2"].current_target_id == "TAB-2"
        events = store.read_events("rec_test")
        opened = [
            e
            for e in events
            if e.get("kind") == "page_opened" and e.get("targetId") == "TAB-2"
        ]
        assert opened and opened[0]["url"] == "https://new.example"
        # The poll loop keeps running in direct mode now (it used to be
        # Chrome-only) and must not drop the initial client.
        await asyncio.sleep(0.1)
        assert "TAB-1" in session._clients
    finally:
        await session.stop(generate_steps=False)
        await client.close()


async def test_close_session_kill_direct_mode_logs(caplog, monkeypatch, tmp_path):
    """browser_tool close_session kill=True in direct mode: no terminate
    attempt, and an info log instead of silence."""
    from octop_browser import tool_interface as ti
    from octop_browser.settings import settings as live_settings

    terminated: list[Any] = []

    async def fake_terminate(*args: Any, **kwargs: Any) -> bool:
        terminated.append(True)
        return True

    monkeypatch.setattr("octop_browser.cdp.launcher.terminate_browser", fake_terminate)
    monkeypatch.setattr(live_settings, "cdp_ws_url", "ws://127.0.0.1:9222/")
    monkeypatch.setattr(live_settings, "profiles_dir", tmp_path / "profiles")
    with caplog.at_level(logging.INFO, logger="octop_browser.tool_interface"):
        result = await ti.browser_tool(
            action="close_session", profile="never-created", kill=True
        )
    assert result.success is True
    assert terminated == []
    assert any("direct CDP mode" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# Third batch: single-tab browsers (Lightpanda rejects the second
# Target.createTarget with TargetAlreadyLoaded)
# ----------------------------------------------------------------------


async def test_new_tab_reuses_target_when_already_loaded(cdp_server, tmp_path):
    """Lightpanda-style servers reject the second createTarget; new_tab
    falls back to the existing target showing the same URL."""
    cdp_server.create_target_already_loaded = True
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.new_tab("https://example.com", force_new=True)
        assert result.success is True
        assert result.content == "TAB-1"
        assert result.metadata == {"target_id": "TAB-1"}
        create_msgs = [
            m for m in cdp_server.received if m["method"] == "Target.createTarget"
        ]
        assert len(create_msgs) == 1
        assert cdp_server.attached[-1]["targetId"] == "TAB-1"
        assert sess._internal.client.current_target_id == "TAB-1"
    finally:
        await sess.close()


async def test_new_tab_already_loaded_without_match_still_fails(cdp_server, tmp_path):
    """TargetAlreadyLoaded with no same-URL target is a real failure."""
    cdp_server.create_target_already_loaded = True
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.new_tab("https://nowhere.example", force_new=True)
        assert result.success is False
        assert "TargetAlreadyLoaded" in str(result.error)
    finally:
        await sess.close()


async def test_new_tab_already_loaded_tolerates_trailing_slash(cdp_server, tmp_path):
    """Chrome normalizes URLs with a trailing slash in Target.getTargets;
    the reuse lookup must not miss that match."""
    cdp_server.create_target_already_loaded = True
    cdp_server.targets[0]["url"] = "https://example.com/"
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        result = await sess.new_tab("https://example.com", force_new=True)
        assert result.success is True
        assert result.content == "TAB-1"
    finally:
        await sess.close()


# ----------------------------------------------------------------------
# Fourth batch: CLI entry point, sticky target, lost-page warning
# ----------------------------------------------------------------------


async def test_connect_prefers_remembered_target(cdp_server, tmp_path):
    """A second session on the same profile re-attaches to the target the
    first one left behind, even when another non-blank page exists."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    await sess.close()
    # A second page appeared while we were disconnected.
    cdp_server.targets.append(
        {
            "targetId": "TAB-2",
            "type": "page",
            "url": "https://other.example",
            "title": "Other",
        }
    )
    sess2 = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        assert cdp_server.attached[-1]["targetId"] == "TAB-1"
        assert sess2._internal.client.current_target_id == "TAB-1"
    finally:
        await sess2.close()


async def test_connect_warns_when_remote_page_lost(caplog, cdp_server, tmp_path):
    """Lightpanda recycles target ids, so an unchanged id does not prove the
    page survived — a URL mismatch must warn (once)."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    await sess.navigate("https://example.com")
    await sess.close()
    # Same id, fresh page — the browser dropped the old target.
    cdp_server.targets[0]["url"] = "about:blank"
    with caplog.at_level(logging.WARNING, logger="octop_browser.session"):
        sess2 = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        assert any("lost the previous page" in r.message for r in caplog.records)
        assert any("https://example.com" in r.message for r in caplog.records)
    finally:
        await sess2.close()


async def test_connect_falls_back_when_remembered_target_gone(cdp_server, tmp_path):
    """The remembered target died; connect attaches to an available page
    instead of failing."""
    sess = await make_session(cdp_server.ws_url(), tmp_path)
    await sess.close()
    cdp_server.targets = [
        {
            "targetId": "TAB-7",
            "type": "page",
            "url": "https://fresh.example",
            "title": "Fresh",
        }
    ]
    sess2 = await make_session(cdp_server.ws_url(), tmp_path)
    try:
        assert cdp_server.attached[-1]["targetId"] == "TAB-7"
        assert sess2._internal.client.current_target_id == "TAB-7"
    finally:
        await sess2.close()


def test_python_dash_m_entry_point_exists():
    """``python -m octop_browser`` is documented in the README and wired into
    ``make run``; the package must expose __main__."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "octop_browser", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(root),
    )
    assert result.returncode == 0
    assert "octop-browser CLI" in result.stdout

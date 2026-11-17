"""Unit tests for the screenshot path-resolution logic.

These cover the *path computation* in ``actions.capture._resolve_path``
without spinning up Chrome — the actual CDP round-trip is covered by the
finnie live test suite.
"""

from __future__ import annotations

from pathlib import Path

from octop_browser.actions.capture import _resolve_path
from octop_browser.settings import OctopSettings


def _cfg(tmp_path: Path) -> OctopSettings:
    return OctopSettings(screenshots_dir=tmp_path)


def test_default_filename_is_timestamped_png(tmp_path: Path):
    p = _resolve_path(None, _cfg(tmp_path))
    assert p.parent == tmp_path
    assert p.name.startswith("octop-")
    assert p.suffix == ".png"
    # Created the directory if it didn't exist.
    assert tmp_path.exists()


def test_absolute_path_is_used_verbatim(tmp_path: Path):
    target = tmp_path / "snap" / "shot.png"
    p = _resolve_path(target, _cfg(tmp_path / "elsewhere"))
    assert p == target
    assert target.parent.exists()  # parent dir auto-created


def test_relative_path_resolves_under_screenshots_dir(tmp_path: Path):
    p = _resolve_path("daily/shot.png", _cfg(tmp_path))
    assert p == tmp_path / "daily" / "shot.png"
    assert (tmp_path / "daily").exists()


def test_user_home_is_expanded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = _resolve_path("~/myshot.png", _cfg(tmp_path / "ignored"))
    # ~ is expanded *before* the relative-path branch; an absolute path
    # then bypasses screenshots_dir entirely.
    assert p == tmp_path / "myshot.png"


def test_apply_viewport_sends_setdevicemetricsoverride(tmp_path: Path):
    """``_InternalCDPSession._apply_viewport`` should emit a
    ``Emulation.setDeviceMetricsOverride`` CDP call with the configured
    width/height — that's what makes default screenshots come out at a
    sensible size instead of Chromium's tiny ~800×600 default.
    """
    import asyncio

    from octop_browser.profile import Profile
    from octop_browser.session import _InternalCDPSession
    from octop_browser.settings import OctopSettings

    sent: list[tuple[str, dict]] = []

    class _FakeClient:
        async def send(self, method: str, params: dict | None = None) -> dict:
            sent.append((method, params or {}))
            return {}

    cfg = OctopSettings(viewport_width=1280, viewport_height=720)
    profile = Profile(name="t", data_dir=tmp_path, cdp_port=9999)
    sess = _InternalCDPSession(profile, cfg)
    sess._client = _FakeClient()  # type: ignore[assignment]

    asyncio.run(sess._apply_viewport())

    assert sent == [
        (
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 720,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
    ]


async def test_switch_target_preserves_command_gate(tmp_path: Path, monkeypatch):
    from contextlib import asynccontextmanager

    import octop_browser.session as session_module
    from octop_browser.profile import Profile
    from octop_browser.session import _InternalCDPSession
    from octop_browser.settings import OctopSettings

    events: list[str] = []

    @asynccontextmanager
    async def gate(method: str, params: dict):
        events.append(f"enter:{method}")
        yield
        events.append(f"exit:{method}")

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.command_gate = kwargs.get("command_gate")

        async def send(self, *args, **kwargs) -> dict:
            return {}

        async def close(self) -> None:
            events.append("close")

        async def connect(self, ws_url: str) -> None:
            events.append("connect")

        async def enable_domain(self, domain: str) -> None:
            return

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return

        async def json(self, content_type=None) -> list[dict]:
            events.append("http")
            return [{"type": "page", "id": "next", "webSocketDebuggerUrl": "ws://next"}]

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return

        def get(self, url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(session_module, "CDPClient", FakeClient)
    monkeypatch.setattr(session_module.aiohttp, "ClientSession", FakeHttp)
    profile = Profile(name="test", data_dir=tmp_path, cdp_port=9222)
    internal = _InternalCDPSession(profile, OctopSettings(), command_gate=gate)
    original = internal.client
    await internal.switch_to_target("next", activate=False)
    assert original.command_gate is gate
    assert internal.client.command_gate is gate
    assert events == [
        "enter:BrowserSession.switch_to_target",
        "http",
        "close",
        "connect",
        "exit:BrowserSession.switch_to_target",
    ]


async def test_connect_gate_covers_launch_and_websocket_without_nesting(
    tmp_path: Path, monkeypatch
):
    from contextlib import asynccontextmanager

    import octop_browser.session as session_module
    from octop_browser.profile import Profile
    from octop_browser.session import _InternalCDPSession
    from octop_browser.settings import OctopSettings

    events: list[str] = []

    @asynccontextmanager
    async def gate(method: str, params: dict):
        events.append(f"enter:{method}")
        yield
        events.append(f"exit:{method}")

    class FakeClient:
        async def connect(self, ws_url: str) -> None:
            events.append("websocket")

        async def enable_domain(self, domain: str) -> None:
            events.append(f"enable:{domain}")

    async def launch(profile, headless: bool) -> None:
        events.append("launch")

    async def page_ws_url(port: int, profile=None) -> str:
        events.append("resolve-page")
        return "ws://test"

    async def no_viewport() -> None:
        return

    monkeypatch.setattr(session_module, "launch_or_attach", launch)
    monkeypatch.setattr(session_module, "get_page_ws_url", page_ws_url)
    profile = Profile(name="test", data_dir=tmp_path, cdp_port=9222)
    internal = _InternalCDPSession(profile, OctopSettings(), command_gate=gate)
    internal._client = FakeClient()
    monkeypatch.setattr(internal, "_apply_viewport", no_viewport)

    await internal.connect()

    assert events == [
        "enter:BrowserSession.connect",
        "launch",
        "resolve-page",
        "websocket",
        "exit:BrowserSession.connect",
        "enable:Page",
        "enable:DOM",
        "enable:Runtime",
    ]

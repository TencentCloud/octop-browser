"""Tests for the record daemon client helpers."""

from __future__ import annotations

from octop_browser.record import daemon


async def test_send_request_reports_missing_daemon(monkeypatch, tmp_path):
    """A missing socket must yield an actionable message, not a raw errno."""
    monkeypatch.setattr(daemon, "socket_path", lambda: tmp_path / "absent.sock")

    result = await daemon.send_request({"command": "status"})

    assert result["ok"] is False
    assert "daemon-start" in result["error"]
    assert "Errno" not in result["error"]


async def test_is_running_false_when_socket_absent(monkeypatch, tmp_path):
    """``is_running`` still resolves to False now that send_request returns a dict."""
    monkeypatch.setattr(daemon, "socket_path", lambda: tmp_path / "absent.sock")

    assert await daemon.is_running() is False


async def test_daemon_uses_the_shared_profile_lease(monkeypatch, tmp_path):
    from octop_browser.lease import lease_path
    from octop_browser.settings import settings

    monkeypatch.setattr(settings, "profiles_dir", tmp_path)
    instance = daemon.RecordDaemon()
    gate = instance.controller._gate("user-1")
    assert gate is not None
    async with gate("Runtime.evaluate", {}):
        assert lease_path("user-1", profiles_dir=tmp_path).is_file()


async def test_daemon_rejects_recording_start_during_user_control(
    monkeypatch, tmp_path
):
    from octop_browser.lease import write_control_owner
    from octop_browser.settings import settings

    monkeypatch.setattr(settings, "profiles_dir", tmp_path)
    write_control_owner("user-1", "user")
    instance = daemon.RecordDaemon()

    result = await instance.dispatch({"command": "start", "profile": "user-1"})

    assert result["ok"] is False
    assert result["code"] == "WRITE_BLOCKED"
    assert instance.active is None


async def test_recording_target_clients_use_the_same_gate(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from octop_browser.lease import profile_lease
    from octop_browser.record.models import RecordingManifest
    from octop_browser.record.recorder import RecordSession
    from octop_browser.record.settings import RecordReplaySettings
    from octop_browser.record.store import RecordingStore

    created = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.command_gate = kwargs.get("command_gate")
            created.append(self)

        async def connect(self, _ws_url: str) -> None:
            return

        async def close(self) -> None:
            return

    @asynccontextmanager
    async def gate(_method: str, _params: dict):
        async with profile_lease("user-1", profiles_dir=tmp_path):
            yield

    from octop_browser.record import recorder

    monkeypatch.setattr(recorder, "CDPClient", FakeClient)
    store = RecordingStore(RecordReplaySettings(base_dir=tmp_path / "records"))
    session = RecordSession(
        object(),
        store,
        RecordingManifest(recordingId="rec_gate", createdAt="now"),
        command_gate=gate,
    )
    session._target_infos = AsyncMock(
        return_value=[
            {
                "id": "target-2",
                "type": "page",
                "url": "https://example.test",
                "webSocketDebuggerUrl": "ws://target-2",
            }
        ]
    )
    session._install_on_client = AsyncMock()
    store.create(session.manifest)

    await session._attach_known_targets()

    assert len(created) == 1
    assert created[0].command_gate is gate
    session._install_on_client.assert_awaited_once_with(created[0], "target-2")

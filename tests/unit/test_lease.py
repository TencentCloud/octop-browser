"""Behavior tests for profile-level cross-process leases."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from octop_browser.lease import (
    BrowserLeaseError,
    lease_path,
    profile_command_gate,
    profile_lease,
)


async def test_gate_uses_the_shared_profile_path(tmp_path: Path) -> None:
    async with profile_command_gate("user-1", profiles_dir=tmp_path)(
        "Input.dispatchMouseEvent", {}
    ):
        path = lease_path("user-1", profiles_dir=tmp_path)
        assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "user-1"


async def test_nested_lease_in_the_same_task_locks_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop_browser import lease as lease_module

    lock_count = 0
    original_lock = lease_module._lock

    def counted_lock(handle: object) -> None:
        nonlocal lock_count
        lock_count += 1
        original_lock(handle)

    monkeypatch.setattr(lease_module, "_lock", counted_lock)
    async with profile_lease("user-1", profiles_dir=tmp_path):
        async with profile_lease("user-1", profiles_dir=tmp_path):
            assert lock_count == 1
    assert lock_count == 1


async def test_reentrancy_does_not_leak_to_an_independent_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextvars import Context

    from octop_browser import lease as lease_module

    now = [0.0]
    monkeypatch.setattr(lease_module.time, "monotonic", lambda: now[0])

    async def advance(_seconds: float) -> None:
        now[0] += 11.0

    async def compete() -> None:
        async with profile_lease("user-1", profiles_dir=tmp_path):
            pass

    monkeypatch.setattr(lease_module.asyncio, "sleep", advance)
    async with profile_lease("user-1", profiles_dir=tmp_path):
        with pytest.raises(BrowserLeaseError) as error:
            await Context().run(asyncio.create_task, compete())
    assert error.value.code == "WRITE_LOCKED"


async def test_two_writers_cannot_overlap(tmp_path: Path) -> None:
    active = False

    async def write() -> None:
        nonlocal active
        async with profile_lease("user-1", profiles_dir=tmp_path):
            assert active is False
            active = True
            await asyncio.sleep(0)
            active = False

    await asyncio.gather(write(), write())


async def test_timeout_has_a_stable_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop_browser import lease as lease_module

    monkeypatch.setattr(
        lease_module, "_lock", lambda _handle: (_ for _ in ()).throw(OSError("locked"))
    )
    now = [0.0]
    monkeypatch.setattr(lease_module.time, "monotonic", lambda: now[0])

    async def advance(_seconds: float) -> None:
        now[0] += 11.0

    monkeypatch.setattr(lease_module.asyncio, "sleep", advance)
    with pytest.raises(BrowserLeaseError) as error:
        async with profile_lease("user-1", profiles_dir=tmp_path):
            pass
    assert error.value.code == "WRITE_LOCKED"

"""Cross-process coordination for writers sharing a browser profile."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from octop_browser.cdp.client import CommandGate
from octop_browser.settings import settings as browser_settings


class BrowserLeaseError(RuntimeError):
    """Raised when another process keeps the profile write lease."""

    code = "WRITE_LOCKED"


def lease_path(profile: str, *, profiles_dir: Path | None = None) -> Path:
    root = (
        profiles_dir
        if profiles_dir is not None
        else Path(browser_settings.profiles_dir)
    )
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    return root / ".leases" / f"{digest}.json"


def _lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(Any, importlib.import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(Any, importlib.import_module("msvcrt"))
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_HELD_LEASES: ContextVar[frozenset[Path]] = ContextVar(
    "octop_browser_held_leases", default=frozenset()
)


@asynccontextmanager
async def profile_lease(
    profile: str, *, profiles_dir: Path | None = None
) -> AsyncIterator[None]:
    path = lease_path(profile, profiles_dir=profiles_dir)
    if path in _HELD_LEASES.get():
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                _lock(handle)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise BrowserLeaseError(
                        "Another writer holds the browser lease"
                    ) from error
                await asyncio.sleep(0.05)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "profile": profile,
                        "acquiredAt": time.time(),
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            handle.flush()
            token = _HELD_LEASES.set(_HELD_LEASES.get() | {path})
            try:
                yield
            finally:
                _HELD_LEASES.reset(token)
        finally:
            _unlock(handle)


def profile_command_gate(
    profile: str, *, profiles_dir: Path | None = None
) -> CommandGate:
    @asynccontextmanager
    async def gate(_method: str, _params: dict[str, Any]) -> AsyncIterator[None]:
        async with profile_lease(profile, profiles_dir=profiles_dir):
            yield

    return gate

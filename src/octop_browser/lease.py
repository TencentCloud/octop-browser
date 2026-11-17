"""Cross-process coordination for writers sharing a browser profile."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from octop_browser.cdp.client import CommandGate
from octop_browser.settings import settings as browser_settings


class BrowserLeaseError(RuntimeError):
    """Raised when profile write coordination rejects an operation."""

    def __init__(self, message: str, *, code: str = "WRITE_LOCKED") -> None:
        super().__init__(message)
        self.code = code


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


def control_owner_path(profile: str, *, profiles_dir: Path | None = None) -> Path:
    return lease_path(profile, profiles_dir=profiles_dir).with_suffix(".owner.json")


def read_control_owner(profile: str, *, profiles_dir: Path | None = None) -> str:
    path = control_owner_path(profile, profiles_dir=profiles_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        owner = data.get("owner") if isinstance(data, dict) else None
        if owner not in {"agent", "user"}:
            raise ValueError("Unknown control owner")
        return str(owner)
    except FileNotFoundError:
        return "agent"
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise BrowserLeaseError(
            "The browser control state is invalid or unreadable", code="WRITE_BLOCKED"
        ) from error


def write_control_owner(
    profile: str, owner: str, *, profiles_dir: Path | None = None
) -> None:
    if owner not in {"agent", "user"}:
        raise ValueError("The browser control owner must be 'agent' or 'user'")
    path = control_owner_path(profile, profiles_dir=profiles_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"owner": owner}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_control_owner(profile: str, *, profiles_dir: Path | None = None) -> None:
    control_owner_path(profile, profiles_dir=profiles_dir).unlink(missing_ok=True)


def profile_control_command_gate(
    profile: str, *, profiles_dir: Path | None = None
) -> CommandGate:
    lease_gate = profile_command_gate(profile, profiles_dir=profiles_dir)

    @asynccontextmanager
    async def gate(method: str, params: dict[str, Any]) -> AsyncIterator[None]:
        owner = await asyncio.to_thread(
            read_control_owner, profile, profiles_dir=profiles_dir
        )
        if owner == "user":
            raise BrowserLeaseError(
                "User control is active; daemon writes are blocked",
                code="WRITE_BLOCKED",
            )
        async with lease_gate(method, params):
            yield

    return gate

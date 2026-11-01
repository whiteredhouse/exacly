"""Checked local invocation fence; native deadlines use the existing real Linux watchdog."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from exacly import execution_store as store
from exacly import executor_runtime

try:
    import fcntl
except ImportError:  # pragma: no cover - supported effects require POSIX ownership.
    fcntl = None  # type: ignore[assignment]

Doc = dict[str, Any]


def _identity(metadata: Doc) -> tuple[Path, bytes]:
    path = Path(metadata["path"]).parent / (".delivery-invocation-" + metadata["id"] + ".lock")
    content = store.canonical(
        {
            key: metadata[key]
            for key in (
                "schema_version",
                "profile",
                "id",
                "path",
                "objective",
                "device",
                "inode",
                "limits",
                "native_limits",
            )
        }
    ).encode()
    return path, content


def _cleanup_path(metadata: Doc, call_id: str) -> Path:
    store.require(isinstance(call_id, str) and store.DIGEST.fullmatch(call_id), "invalid delivery cleanup call")
    return Path(metadata["path"]).parent / (".delivery-cleanup-" + str(metadata["id"]) + "-" + call_id[7:] + ".json")


def retain_cleanup(metadata: Doc, call_id: str, cleaned: bool) -> None:
    """A deadline-killed watchdog never writes SQLite; retain one small atomic kernel fact."""
    path = _cleanup_path(metadata, call_id)
    value = {
        "schema_version": "0.1.0",
        "kind": "delivery-runtime-cleanup",
        "journal_id": metadata["id"],
        "call_id": call_id,
        "owner": executor_runtime.identity(os.getpid()),
        "cleaned": cleaned,
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".delivery-cleanup-", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(store.canonical(value).encode())
            output.flush()
            os.fsync(output.fileno())
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # A cleanup fact is create-only. Never replace an earlier owner/outcome if a
            # repeated callback or competing process races this publication.
            os.link(temporary, path, follow_symlinks=False)
            os.fsync(parent)
            with suppress(OSError):
                temporary.unlink()
            temporary = None
        finally:
            os.close(parent)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def read_cleanup(metadata: Doc, call_id: str) -> Doc | None:
    """Read bounded local proof; the journal must separately compare its exact retained owner."""
    path = _cleanup_path(metadata, call_id)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        store.file_info(descriptor)
        content = os.read(descriptor, 16385)
        store.require(len(content) <= 16384, "delivery cleanup receipt exceeds its bound")
        value = json.loads(content, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(
            isinstance(value, dict)
            and set(value) == {"schema_version", "kind", "journal_id", "call_id", "owner", "cleaned"}
            and value["schema_version"] == "0.1.0"
            and value["kind"] == "delivery-runtime-cleanup"
            and value["journal_id"] == metadata["id"]
            and value["call_id"] == call_id
            and type(value["cleaned"]) is bool,
            "invalid delivery cleanup receipt",
        )
        executor_runtime.validate_identity(value["owner"])
        store.require(store.canonical(value).encode() == content, "noncanonical delivery cleanup receipt")
        return dict(value)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise store.StoreError("invalid delivery cleanup receipt") from error
    finally:
        os.close(descriptor)


def initialize(metadata: Doc, parent_fd: int) -> None:
    """Called only inside fresh journal initialization; never recreate a missing historical fence."""
    path, content = _identity(metadata)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        store.file_info(descriptor)
        store.require(os.write(descriptor, content) == len(content), "delivery fence write was incomplete")
        os.fsync(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def validate_identity(metadata: Doc) -> None:
    """Read the original scope anchor without taking invocation authority or repairing it."""
    path, expected = _identity(metadata)
    store.location(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = store.file_info(descriptor)
        store.require(os.pread(descriptor, len(expected) + 1, 0) == expected, "delivery invocation identity changed")
        current = path.lstat()
        store.require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "delivery fence was replaced")
    finally:
        os.close(descriptor)


@contextmanager
def invocation(metadata: Doc, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Serialize effects and effective revocation, surviving parent death in the native watchdog.

    This cooperating-user mutex does not fence remote writers. A forked watchdog inherits the
    descriptor, so another local operation cannot cross it before that watchdog terminates.
    """
    store.require(os.name == "posix" and fcntl is not None, "delivery effects require POSIX invocation ownership")
    path, expected = _identity(metadata)
    store.location(path)
    deadline = store.Deadline(lock_timeout)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = store.file_info(descriptor)
        store.require(os.pread(descriptor, len(expected) + 1, 0) == expected, "delivery invocation identity changed")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, InterruptedError):
                deadline.check()
                time.sleep(min(store.LOCK_RETRY_INTERVAL, deadline.remaining()))
        current = path.lstat()
        store.require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "delivery fence was replaced")
        store.require(os.pread(descriptor, len(expected) + 1, 0) == expected, "delivery fence bytes changed")
        yield
        current = path.lstat()
        store.require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "delivery fence was replaced")
    finally:
        os.close(descriptor)

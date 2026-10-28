"""Conditional, atomic publication of local state by cooperating POSIX writers.

Capture a snapshot before computing an observation, then publish against those exact bytes. A stable
parent-directory lock serializes the comparison and replacement, including concurrent first writes.
This is a local filesystem protocol, not protection against writers that ignore the lock or a transaction
with an execution ledger. A killed writer may leave a hidden staging file; it is never used as state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from exacly.core import schema_for

try:
    import fcntl
except ImportError:  # pragma: no cover - this protocol requires POSIX directory locks.
    fcntl = None  # type: ignore[assignment]

Doc = dict[str, Any]
Identity = tuple[int, int]
Header = tuple[str, str, str, int]
DEFAULT_LOCK_TIMEOUT = 30.0
LOCK_RETRY_INTERVAL = 0.01
FORMATS = jsonschema.FormatChecker()


@FORMATS.checks("date-time")
def _date_time(value: Any) -> bool:
    """The optional jsonschema RFC3339 dependency is not required by the package."""
    if not isinstance(value, str):
        return True  # Schema type validation handles nonstrings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("t", "T").replace("z", "Z"))
        return parsed.utcoffset() is not None
    except ValueError:
        return False


class StateWriteError(ValueError):
    """State or its destination cannot participate in conditional publication."""


class StateConflictError(StateWriteError):
    """The captured state or its directory changed; recompute from a fresh snapshot."""


class StateLockTimeoutError(StateWriteError):
    """Another writer held the lock beyond the caller's bounded wait."""


class PublicationDurabilityError(StateWriteError):
    """Replacement succeeded but directory synchronization failed; inspect before retrying."""

    published = True


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise StateWriteError(message)


def _identity(info: os.stat_result) -> Identity:
    return info.st_dev, info.st_ino


def _unique_object(pairs: list[tuple[str, Any]]) -> Doc:
    result: Doc = {}
    for key, value in pairs:
        _require(key not in result, "state JSON contains duplicate object keys")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise StateWriteError("state JSON contains a nonfinite number")


def _decode(content: bytes) -> Doc:
    try:
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        if not isinstance(document, dict):
            raise StateWriteError("state must be a JSON object")
        _json_values(document)
        _require(isinstance(document.get("exacly"), str), "state must declare its specification version")
        validator = jsonschema.Draft202012Validator(schema_for(document["exacly"], "state"), format_checker=FORMATS)
        error = next(validator.iter_errors(document), None)
        if error is not None:
            where = "/" + "/".join(str(part) for part in error.absolute_path)
            raise StateWriteError(f"state does not match its schema at {where}: {error.message}")
        return document
    except StateWriteError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise StateWriteError(f"state is not supported JSON: {error}") from error


def _json_values(value: Any) -> None:
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value), "state JSON object keys must be strings")
        for child in value.values():
            _json_values(child)
    elif isinstance(value, list):
        for child in value:
            _json_values(child)
    else:
        _require(
            value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value)),
            "state must contain finite JSON values",
        )


def _encode(document: Doc) -> bytes:
    try:
        _json_values(document)
        return (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except StateWriteError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise StateWriteError(f"state is not supported JSON: {error}") from error


def _header(document: Doc) -> Header:
    return document["namespace"], document["context"], document["lineage"], document["serial"]


@dataclass(frozen=True)
class Snapshot:
    """Immutable expected bytes, header and directory identity for one publication.

    document returns a fresh object so an observer cannot mutate the stored precondition by carrying
    observations forward. digest hashes the exact file bytes, including formatting.
    """

    path: Path
    _content: bytes | None
    _header: Header | None
    _anchor: Path
    _anchor_identity: Identity

    @property
    def document(self) -> Doc | None:
        return json.loads(self._content) if self._content is not None else None

    @property
    def digest(self) -> str | None:
        return "sha256:" + hashlib.sha256(self._content).hexdigest() if self._content is not None else None

    @property
    def content(self) -> bytes | None:
        """The immutable, exact captured bytes, or None for captured absence."""
        return self._content


def _supported() -> None:
    _require(os.name == "posix" and fcntl is not None, "conditional state publication requires POSIX directory locks")


def _read_bytes(directory_fd: int, name: str) -> tuple[bytes | None, int]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, 0o600
    with os.fdopen(descriptor, "rb") as file:
        before = os.fstat(file.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "state must be a regular file with one link")
        content = file.read()
        after = os.fstat(file.fileno())
        if (_identity(before), before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            _identity(after),
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(content) != after.st_size:
            raise StateConflictError("state changed while being read; capture a fresh snapshot and recompute")
        return content, stat.S_IMODE(after.st_mode)


def read(path: Path) -> Snapshot:
    """Capture state before computation without creating the file or missing parent directories."""
    _supported()
    _require(isinstance(path, Path) and path.name not in ("", ".", ".."), "invalid state path")
    try:
        absolute = path.parent.resolve() / path.name
        anchor = absolute.parent
        while not anchor.exists():
            anchor = anchor.parent
        descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            anchor_identity = _identity(os.fstat(descriptor))
            if _identity(anchor.stat()) != anchor_identity:
                raise StateConflictError("state directory changed while capturing the snapshot")
            content, _ = _read_bytes(descriptor, absolute.name) if anchor == absolute.parent else (None, 0o600)
        finally:
            os.close(descriptor)
        header = _header(_decode(content)) if content is not None else None
        return Snapshot(absolute, content, header, anchor, anchor_identity)
    except StateWriteError:
        raise
    except (OSError, RuntimeError) as error:
        raise StateWriteError(f"cannot capture state at {path}: {error}") from error


def _open_parent(snapshot: Snapshot) -> int:
    """Walk from the captured existing directory, creating missing components durably."""
    try:
        descriptor = os.open(snapshot._anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError as error:
        raise StateConflictError("state directory changed; capture a fresh snapshot and recompute") from error
    try:
        if _identity(os.fstat(descriptor)) != snapshot._anchor_identity:
            raise StateConflictError("state directory changed; capture a fresh snapshot and recompute")
        if snapshot._content is None and snapshot._anchor.parent != snapshot._anchor:
            # A creator may have been killed after mkdir but before syncing this directory's entry.
            # A snapshot captured afterwards sees it as existing. Older ancestors were synced before
            # that creator made this child, so finishing this one entry closes the first-write race.
            parent = os.open(snapshot._anchor.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            if _identity(snapshot._anchor.stat()) != snapshot._anchor_identity:
                raise StateConflictError("state directory changed; capture a fresh snapshot and recompute")
        for component in snapshot.path.parent.relative_to(snapshot._anchor).parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(component, dir_fd=descriptor)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                # Finish synchronization even when a competing creator made this entry but was killed
                # before syncing it. Both writers captured this component as absent.
                os.fsync(descriptor)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_directory(snapshot: Snapshot, descriptor: int) -> None:
    try:
        changed = (
            snapshot.path.parent.resolve() != snapshot.path.parent
            or _identity(snapshot._anchor.stat()) != snapshot._anchor_identity
            or _identity(snapshot.path.parent.stat()) != _identity(os.fstat(descriptor))
        )
    except FileNotFoundError:
        changed = True
    if changed:
        raise StateConflictError("state directory changed; capture a fresh snapshot and recompute")


def _duration(lock_timeout: float) -> float:
    _require(type(lock_timeout) in (int, float), "lock timeout must be a finite nonnegative number of seconds")
    try:
        duration = float(lock_timeout)
    except (ValueError, OverflowError) as error:
        raise StateWriteError("lock timeout must be a finite nonnegative number of seconds") from error
    _require(math.isfinite(duration) and duration >= 0, "lock timeout must be finite and nonnegative")
    return duration


def _lock(descriptor: int, duration: float) -> None:
    expires = time.monotonic() + duration
    _require(fcntl is not None, "conditional state publication requires POSIX directory locks")
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, InterruptedError):
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise StateLockTimeoutError(
                    "state writer lock timed out; retry after other writers finish or increase --lock-timeout"
                ) from None
            time.sleep(min(LOCK_RETRY_INTERVAL, remaining))


def _stage(descriptor: int, content: bytes, mode: int) -> str:
    name = f".exacly-state-{secrets.token_hex(16)}.tmp"
    file_descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=descriptor)
        raise
    return name


def serialize(state: Doc) -> bytes:
    """Return validated 0.2 state bytes suitable for a retained publication intent."""
    content = _encode(state)
    _decode(content)
    return content


def decode(content: bytes) -> Doc:
    """Validate exact retained state bytes without changing their representation."""
    _require(isinstance(content, bytes), "state content must be bytes")
    return _decode(content)


def _successor(snapshot: Snapshot, content: bytes) -> None:
    _require(isinstance(content, bytes), "state content must be bytes")
    next_header = _header(_decode(content))
    if snapshot._content is None:
        _require(next_header[3] == 1, "initial state must have serial 1")
    else:
        previous_header = _header(_decode(snapshot._content))
        _require(previous_header == snapshot._header, "invalid snapshot header")
        _require(next_header[:3] == previous_header[:3], "state namespace, context and lineage must stay the same")
        _require(next_header[3] == previous_header[3] + 1, "state serial must increase by exactly one")


def check_parent(snapshot: Snapshot, parent_fd: int) -> None:
    """Check a pinned parent supplied by a caller holding the shared directory lock."""
    _supported()
    _check_directory(snapshot, parent_fd)


def read_locked(snapshot: Snapshot, parent_fd: int) -> Snapshot:
    """Read the current state through its pinned, already locked parent directory.

    File contents may have advanced since snapshot capture. A replaced parent still conflicts.
    The caller owns the lock and must retain it through any conditional publication.
    """
    check_parent(snapshot, parent_fd)
    content, _ = _read_bytes(parent_fd, snapshot.path.name)
    header = _header(_decode(content)) if content is not None else None
    _check_directory(snapshot, parent_fd)
    return Snapshot(snapshot.path, content, header, snapshot.path.parent, _identity(os.fstat(parent_fd)))


def publish_locked(snapshot: Snapshot, content: bytes, parent_fd: int) -> None:
    """Conditionally publish the exact retained bytes while the caller keeps the parent locked.

    This operation never acquires or releases a lock. It allows a completion coordinator to retain
    the same lock through intent preparation, publication and receipt finalization.
    """
    _supported()
    _successor(snapshot, content)
    staged: str | None = None
    try:
        _check_directory(snapshot, parent_fd)
        current, mode = _read_bytes(parent_fd, snapshot.path.name)
        if current != snapshot._content:
            raise StateConflictError("state changed since capture; capture a fresh snapshot and recompute")
        # Compare frozen headers as well as exact bytes; the digest is available for audit, not authority.
        if current is not None and _header(_decode(current)) != snapshot._header:
            raise StateConflictError("state identity changed since capture; capture a fresh snapshot and recompute")
        staged = _stage(parent_fd, content, mode)
        _check_directory(snapshot, parent_fd)
        os.replace(staged, snapshot.path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        staged = None
        try:
            os.fsync(parent_fd)
        except OSError as error:
            raise PublicationDurabilityError(
                f"state was atomically published at {snapshot.path}, but directory sync failed; "
                "inspect current state before retrying because crash durability is uncertain"
            ) from error
    except StateWriteError:
        raise
    except OSError as error:
        raise StateWriteError(f"cannot publish state at {snapshot.path}: {error}") from error
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                os.unlink(staged, dir_fd=parent_fd)


def sync_locked(snapshot: Snapshot, parent_fd: int) -> None:
    """Confirm and synchronize an already visible state before finalizing recovery.

    A prior process can die after rename but before directory fsync. Exact after-state bytes alone
    do not prove durability; recovery must finish file and directory synchronization under the lock.
    """
    check_parent(snapshot, parent_fd)
    _require(snapshot.content is not None, "cannot synchronize absent state")
    current, _ = _read_bytes(parent_fd, snapshot.path.name)
    if current != snapshot.content:
        raise StateConflictError("state changed before recovery synchronization")
    descriptor: int | None = None
    try:
        descriptor = os.open(snapshot.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "state must be a regular file with one link")
        os.fsync(descriptor)
        if _identity(info) != _identity(os.stat(snapshot.path.name, dir_fd=parent_fd, follow_symlinks=False)):
            raise StateConflictError("state changed during recovery synchronization")
        current, _ = _read_bytes(parent_fd, snapshot.path.name)
        if current != snapshot.content:
            raise StateConflictError("state changed during recovery synchronization")
        _check_directory(snapshot, parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise PublicationDurabilityError(
            f"state is visible at {snapshot.path}, but recovery synchronization failed; "
            "retain the prepared intent and retry recovery"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def publish(snapshot: Snapshot, state: Doc, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
    """Publish one successor while owning the shared directory lock for this call.

    After a conflict, recompute from a fresh snapshot. A PublicationDurabilityError means the new state
    is already visible and must be inspected. Coordinators use publish_locked to keep locks longer.
    """
    _supported()
    duration = _duration(lock_timeout)
    content = serialize(state)
    _successor(snapshot, content)
    descriptor: int | None = None
    try:
        descriptor = _open_parent(snapshot)
        _lock(descriptor, duration)
        publish_locked(snapshot, content, descriptor)
    except StateWriteError:
        raise
    except OSError as error:
        raise StateWriteError(f"cannot publish state at {snapshot.path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

"""An owner-private, append-only local binding store.

This is an operational boundary between cooperating processes of one user. It does not defend against
that user modifying the database or its private directory. Reads never initialize or recover a store.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - the filesystem boundary requires POSIX ownership and locks.
    fcntl = None  # type: ignore[assignment]

Doc = dict[str, Any]
APPLICATION_ID = 0x4558434C  # EXCL
USER_VERSION = 1
COMPLETION_VERSION = 2
INITIAL_VERSION = COMPLETION_VERSION
INDEX_VERSION = 4
MAX_USER_VERSION = INDEX_VERSION
DEFAULT_LOCK_TIMEOUT = 30.0
MAX_LOCK_TIMEOUT = (2**31 - 1) / 1000
LOCK_RETRY_INTERVAL = 0.01
LOCK_TIMEOUT_MESSAGE = (
    "local execution store lock timed out; retry after other store operations finish or increase --lock-timeout"
)
RECORD_LIMIT = 8 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
KEYS = {
    "schema_version",
    "id",
    "pending_key",
    "contract",
    "state_snapshot",
    "registered_at",
    "as_of",
    "supersedes",
    "claims",
}
SCHEMA = {
    "bindings": (
        "CREATE TABLE bindings (id TEXT PRIMARY KEY NOT NULL, pending_key TEXT NOT NULL, "
        "supersedes TEXT UNIQUE REFERENCES bindings(id), record_json TEXT NOT NULL)"
    ),
    "active": (
        "CREATE TABLE active (pending_key TEXT PRIMARY KEY NOT NULL, "
        "binding_id TEXT UNIQUE NOT NULL REFERENCES bindings(id))"
    ),
    "watermarks": (
        "CREATE TABLE watermarks (pending_key TEXT PRIMARY KEY NOT NULL, "
        "serial INTEGER NOT NULL CHECK (serial >= 0), digest TEXT NOT NULL)"
    ),
}


class StoreError(ValueError):
    """The local store or proposed registration does not meet the storage contract."""


class LockTimeoutError(StoreError):
    """The shared wait budget expired; the caller can retry without repairing the store."""


class Deadline:
    """One monotonic lock-wait budget shared by directory and SQLite operations.

    A zero timeout permits an immediate lock attempt. Call check only after a failed attempt or when
    an operation explicitly needs time left; remaining can always configure a nonblocking SQLite wait.
    """

    def __init__(self, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        require(type(lock_timeout) in (int, float), "lock timeout must be a finite nonnegative number of seconds")
        try:
            duration = float(lock_timeout)
        except (ValueError, OverflowError):
            raise StoreError("lock timeout exceeds the supported SQLite wait") from None
        require(math.isfinite(duration) and duration >= 0, "lock timeout must be finite and nonnegative")
        require(duration <= MAX_LOCK_TIMEOUT, "lock timeout exceeds the supported SQLite wait")
        self._expires = time.monotonic() + duration

    def remaining(self) -> float:
        return max(0.0, self._expires - time.monotonic())

    def check(self) -> None:
        if self.remaining() <= 0:
            raise LockTimeoutError(LOCK_TIMEOUT_MESSAGE)


def busy_timeout(connection: sqlite3.Connection, deadline: Deadline) -> None:
    """Refresh SQLite's millisecond wait from the same remaining directory-lock budget."""
    milliseconds = min(2**31 - 1, int(deadline.remaining() * 1000))
    connection.execute(f"PRAGMA busy_timeout={milliseconds}")


def sqlite_lock_error(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise StoreError(message)


def canonical(value: Any) -> str:
    def plain(item: Any) -> None:
        if isinstance(item, dict):
            require(all(isinstance(key, str) for key in item), "execution records require JSON object keys")
            for child in item.values():
                plain(child)
        elif isinstance(item, list):
            for child in item:
                plain(child)
        else:
            require(
                item is None or type(item) in (str, bool, int) or (type(item) is float and math.isfinite(item)),
                "execution records require finite JSON values",
            )

    try:
        plain(value)
        result = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        require(len(result.encode("utf-8")) <= RECORD_LIMIT, "execution record exceeds the permitted size")
        return result
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise StoreError("execution record is not supported JSON") from None


def unique_object(pairs: list[tuple[str, Any]]) -> Doc:
    result: Doc = {}
    for key, value in pairs:
        require(key not in result, "stored execution record contains duplicate keys")
        result[key] = value
    return result


def invalid_constant(value: str) -> None:
    raise StoreError("stored execution record contains a nonfinite value")


def record(value: Any) -> Doc:
    require(isinstance(value, dict) and set(value) == KEYS, "invalid execution record fields")
    require(value.get("schema_version") == "0.1.0", "unsupported execution record version")
    require(
        all(isinstance(value[key], str) and DIGEST.fullmatch(value[key]) for key in ("id", "pending_key")),
        "invalid execution record identity",
    )
    require(
        value["supersedes"] is None or (isinstance(value["supersedes"], str) and DIGEST.fullmatch(value["supersedes"])),
        "invalid superseded binding identity",
    )
    require(value["id"] != value["supersedes"], "a binding cannot supersede itself")
    require(
        isinstance(value["contract"], dict) and isinstance(value["claims"], dict), "invalid execution record contract"
    )
    snapshot = value["state_snapshot"]
    require(
        isinstance(snapshot, dict) and set(snapshot) == {"serial", "digest", "config_revision"},
        "invalid state snapshot fields",
    )
    require(type(snapshot["serial"]) is int and 0 <= snapshot["serial"] <= 2**63 - 1, "invalid state snapshot serial")
    require(
        isinstance(snapshot["digest"], str) and DIGEST.fullmatch(snapshot["digest"]), "invalid state snapshot digest"
    )
    require(
        isinstance(snapshot["config_revision"], str) and bool(snapshot["config_revision"].strip()),
        "invalid snapshot model revision",
    )
    try:
        require(
            isinstance(value["registered_at"], str) and isinstance(value["as_of"], str),
            "invalid registration timestamp",
        )
        captured = dt.datetime.fromisoformat(value["registered_at"].replace("Z", "+00:00"))
        require(captured.utcoffset() is not None, "registration timestamp requires a timezone")
        require(dt.date.fromisoformat(value["as_of"]).isoformat() == value["as_of"], "invalid registration date")
    except (ValueError, OverflowError):
        raise StoreError("invalid registration timestamp or date") from None
    return dict(json.loads(canonical(value)))


def decoded(text: Any) -> Doc:
    require(isinstance(text, str) and len(text.encode("utf-8")) <= RECORD_LIMIT, "invalid stored execution record")
    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        result = record(value)
        require(canonical(result) == text, "stored execution record is not canonical")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError("invalid stored execution record") from None


def location(path: Path) -> Path:
    require(os.name == "posix" and fcntl is not None, "local execution storage requires POSIX ownership and locks")
    require(
        isinstance(path, Path) and path.name not in ("", ".", "..") and ".." not in path.parts,
        "invalid execution store path",
    )
    absolute = path.absolute()
    parent = absolute.parent
    # A managed mount can expose its root as a mapped UID (for example nobody), rather than UID 0.
    root_owner = Path("/").lstat().st_uid
    for directory in (parent, *parent.parents):
        marker = directory / ".git"
        # Managed workspaces can have empty .git guard directories. A real Git directory has HEAD;
        # linked worktrees and submodules use a .git pointer file instead.
        git_tree = marker.is_file() or (marker.is_dir() and os.path.lexists(marker / "HEAD"))
        require(not git_tree, "execution stores must be outside Git working trees")
        info = directory.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
            "execution store parents must be real directories",
        )
        require(info.st_uid in (0, root_owner, os.geteuid()), "execution store ancestor belongs to another user")
        sticky_root = info.st_uid in (0, root_owner) and bool(info.st_mode & stat.S_ISVTX)
        require(not info.st_mode & 0o022 or sticky_root, "execution store has an unsafe writable ancestor")
    info = parent.lstat()
    require(
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        "execution store directory must be owned and mode 0700",
    )
    return absolute


@contextmanager
def locked(
    path: Path,
    *,
    write: bool,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    deadline: Deadline | None = None,
) -> Iterator[tuple[Path, int]]:
    budget = deadline if deadline is not None else Deadline(lock_timeout)
    absolute = location(path)
    directory_fd = os.open(absolute.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Lock before creation so a concurrent caller cannot see a half-initialized database.
        require(fcntl is not None, "local execution storage requires POSIX locks")
        mode = (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(directory_fd, mode)
                break
            except (BlockingIOError, InterruptedError):
                budget.check()
                time.sleep(min(LOCK_RETRY_INTERVAL, budget.remaining()))
                budget.check()
        require(location(path) == absolute, "execution store path changed")
        yield absolute, directory_fd
    finally:
        os.close(directory_fd)


def auxiliary_files(path: Path) -> None:
    require(
        not any(os.path.lexists(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")),
        "execution store has existing journal or WAL sidecars",
    )


def file_info(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    check_file_info(info)
    return info


def check_file_info(info: os.stat_result) -> None:
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "execution store must be a regular single-link file")
    require(
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600,
        "execution store file must be owned and mode 0600",
    )


def _header(fd: int) -> int:
    content = os.pread(fd, 100, 0)
    require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0",
        "execution store is not a recognized SQLite database",
    )
    require(content[18:20] == b"\x01\x01", "execution store requires DELETE journaling")
    require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized execution store application")
    version = int.from_bytes(content[60:64], "big")
    return version


def _operational_version(version: int) -> None:
    require(
        version != 3,
        "version 3 is an audit-only ledger; use executions audit, and select the original v2 ledger for operations",
    )
    require(version in (USER_VERSION, COMPLETION_VERSION, INDEX_VERSION), "unsupported execution store schema version")


def header(fd: int) -> int:
    version = _header(fd)
    _operational_version(version)
    return version


@contextmanager
def database(
    path: Path, *, create: bool, deadline: Deadline | None = None
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    budget = deadline if deadline is not None else Deadline()
    auxiliary_files(path)
    if os.path.lexists(path):
        check_file_info(path.lstat())
    created = False
    connection: sqlite3.Connection | None = None
    fd: int | None = None
    success = False
    try:
        flags = (os.O_RDWR if create else os.O_RDONLY) | os.O_NONBLOCK
        try:
            fd = os.open(path, flags | os.O_NOFOLLOW)
        except FileNotFoundError:
            require(create, "execution store does not exist")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        identity = file_info(fd)
        if not created:
            header(fd)
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rw" if create else "?mode=ro"),
            uri=True,
            timeout=budget.remaining(),
            isolation_level=None,
        )
        current = path.lstat()
        require((current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino), "execution store file changed")
        yield connection, created
        success = True
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if fd is not None:
            if created and not success:
                current = path.lstat()
                original = os.fstat(fd)
                if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    path.unlink()
            os.close(fd)


def _structure(
    connection: sqlite3.Connection, schema: dict[str, str], version: int, *, deadline: Deadline | None = None
) -> None:
    def execute(statement: str) -> sqlite3.Cursor:
        # busy_timeout is connection-local. It does not write or migrate the inspected database.
        if deadline is not None:
            busy_timeout(connection, deadline)
        return connection.execute(statement)

    require(
        execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,),
        "unrecognized execution store application",
    )
    require(execute("PRAGMA user_version").fetchone() == (version,), "unsupported execution store schema version")
    require(
        execute("PRAGMA journal_mode").fetchone() == ("delete",),
        "execution store requires DELETE journaling",
    )
    objects = execute("SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
    expected = sorted(("table", name, name, sql) for name, sql in schema.items())
    require(sorted(objects) == expected, "unrecognized execution store schema")
    require(execute("PRAGMA quick_check").fetchall() == [("ok",)], "execution store integrity check failed")
    require(not execute("PRAGMA foreign_key_check").fetchall(), "execution store relationships are invalid")


def _base(
    connection: sqlite3.Connection, *, deadline: Deadline | None = None
) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
    def execute(statement: str) -> sqlite3.Cursor:
        if deadline is not None:
            busy_timeout(connection, deadline)
        return connection.execute(statement)

    records: dict[str, Doc] = {}
    for identity, pending, supersedes, text in execute("SELECT id, pending_key, supersedes, record_json FROM bindings"):
        value = decoded(text)
        require(
            (value["id"], value["pending_key"], value["supersedes"]) == (identity, pending, supersedes),
            "stored binding identity is inconsistent",
        )
        records[identity] = value
    active = dict(execute("SELECT pending_key, binding_id FROM active"))
    watermarks = {
        pending: {"serial": serial, "digest": digest}
        for pending, serial, digest in execute("SELECT pending_key, serial, digest FROM watermarks")
    }
    members: dict[str, set[str]] = {}
    for identity, value in records.items():
        members.setdefault(value["pending_key"], set()).add(identity)
    pending_keys = set(members)
    require(set(active) == set(watermarks) == pending_keys, "execution store active bindings are incomplete")
    for pending in pending_keys:
        watermark = watermarks[pending]
        require(
            type(watermark["serial"]) is int
            and 0 <= watermark["serial"] <= 2**63 - 1
            and isinstance(watermark["digest"], str)
            and DIGEST.fullmatch(watermark["digest"]),
            "invalid state watermark",
        )
        current = active[pending]
        seen = set()
        while current is not None:
            require(
                current in records and current not in seen and records[current]["pending_key"] == pending,
                "invalid execution supersession chain",
            )
            seen.add(current)
            snapshot = records[current]["state_snapshot"]
            require(
                snapshot["serial"] <= watermark["serial"]
                and (snapshot["serial"] != watermark["serial"] or snapshot["digest"] == watermark["digest"]),
                "execution state watermark is inconsistent",
            )
            current = records[current]["supersedes"]
        require(
            seen == members[pending],
            "execution supersession chain is incomplete",
        )
    return records, active, watermarks


def validate(
    connection: sqlite3.Connection, *, deadline: Deadline | None = None
) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
    if deadline is not None:
        busy_timeout(connection, deadline)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    _operational_version(version)
    if version == INDEX_VERSION:
        from exacly import execution_index

        owned = not connection.in_transaction
        if owned:
            connection.execute("BEGIN")
        try:
            return execution_index.check(connection, deadline=deadline).snapshot
        finally:
            if owned and connection.in_transaction:
                connection.rollback()
    schema = dict(SCHEMA)
    if version == COMPLETION_VERSION:
        from exacly import completion_store

        schema.update(completion_store.SCHEMA)
    _structure(connection, schema, version, deadline=deadline)
    records, active, watermarks = _base(connection, deadline=deadline)
    if version == COMPLETION_VERSION:
        completion_store.validate(connection, records, active, watermarks, deadline=deadline)
    return records, active, watermarks


def configure(connection: sqlite3.Connection, *, deadline: Deadline | None = None) -> None:
    if deadline is not None:
        busy_timeout(connection, deadline)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    # Configure an empty database for its intended format before beginning initialization.
    if version == 0:
        version = INITIAL_VERSION
    synchronous = "EXTRA" if version in (COMPLETION_VERSION, INDEX_VERSION) else "FULL"
    for statement in ("PRAGMA journal_mode=DELETE", f"PRAGMA synchronous={synchronous}", "PRAGMA foreign_keys=ON"):
        if deadline is not None:
            busy_timeout(connection, deadline)
        connection.execute(statement)


@contextmanager
def _registration_lock(path: Path, directory_fd: int | None, deadline: Deadline) -> Iterator[tuple[Path, int]]:
    if directory_fd is None:
        with locked(path, write=True, deadline=deadline) as pinned:
            yield pinned
    else:
        absolute = location(path)
        info = os.fstat(directory_fd)
        parent = absolute.parent.stat(follow_symlinks=False)
        require(
            (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino),
            "registration directory does not match the caller's pinned lock",
        )
        yield absolute, directory_fd


def put(path: Path, value: Doc, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Atomically register under the execution store's own bounded directory lock."""
    return _put(path, value, lock_timeout=lock_timeout)


def put_locked(path: Path, value: Doc, *, directory_fd: int, deadline: Deadline) -> Doc:
    """Register while a coordinator retains the matching directory lock through finalization."""
    return _put(path, value, directory_fd=directory_fd, deadline=deadline)


def _put(
    path: Path,
    value: Doc,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    directory_fd: int | None = None,
    deadline: Deadline | None = None,
) -> Doc:
    """Atomically append a binding, explicitly supersede the active binding, or retry its identity."""
    deadline = deadline if deadline is not None else Deadline(lock_timeout)
    proposed = record(value)
    try:
        with (
            _registration_lock(path, directory_fd, deadline) as (absolute, pinned_fd),
            database(absolute, create=True, deadline=deadline) as (connection, created),
        ):
            indexed = None
            if created:
                from exacly import completion_store

                configure(connection, deadline=deadline)
                busy_timeout(connection, deadline)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={INITIAL_VERSION}")
                for statement in {**SCHEMA, **completion_store.SCHEMA}.values():
                    connection.execute(statement)
                records: dict[str, Doc] = {}
                active: dict[str, str] = {}
                watermarks: dict[str, Doc] = {}
            else:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != INDEX_VERSION:
                    records, active, watermarks = validate(connection, deadline=deadline)
                configure(connection, deadline=deadline)
                busy_timeout(connection, deadline)
                connection.execute("BEGIN IMMEDIATE")
                if version == INDEX_VERSION:
                    from exacly import execution_index

                    indexed = execution_index.check(connection, deadline=deadline)
                    execution_index.require_writable(indexed)
                    records, active, watermarks = indexed.snapshot
            identity, pending = proposed["id"], proposed["pending_key"]
            if connection.execute("PRAGMA user_version").fetchone()[0] in (COMPLETION_VERSION, INDEX_VERSION):
                from exacly import completion_store

                completion_store.registration_allowed(connection, pending, deadline=deadline)
            current = active.get(pending)
            existing = records.get(identity)
            if existing is not None:
                require(
                    all(existing[key] == proposed[key] for key in ("pending_key", "contract", "supersedes")),
                    "execution binding identity collides with another contract",
                )
                require(current == identity, "execution binding has been superseded")
            else:
                require(
                    proposed["supersedes"] == current,
                    "registration must explicitly supersede the current active binding",
                )
            capture = proposed["state_snapshot"]
            watermark = watermarks.get(pending)
            if watermark is not None:
                require(
                    capture["serial"] >= watermark["serial"], "state serial would roll back the registered watermark"
                )
                require(
                    capture["serial"] != watermark["serial"] or capture["digest"] == watermark["digest"],
                    "state digest changed at the registered serial",
                )
            expected: dict[tuple[str, str], tuple[Any, ...]] = {}
            captured: Mapping[tuple[str, str], tuple[Any, ...] | None] = {}
            if indexed is not None:
                expected = {
                    ("watermarks", pending): (pending, capture["serial"], capture["digest"]),
                }
                if existing is None:
                    expected[("bindings", identity)] = (identity, pending, proposed["supersedes"], canonical(proposed))
                    expected[("active", pending)] = (pending, identity)
                captured = indexed.capture(expected)
            before_changes = connection.total_changes
            if existing is None:
                connection.execute(
                    "INSERT INTO bindings (id, pending_key, supersedes, record_json) VALUES (?, ?, ?, ?)",
                    (identity, pending, proposed["supersedes"], canonical(proposed)),
                )
                connection.execute(
                    "INSERT INTO active (pending_key, binding_id) VALUES (?, ?) "
                    "ON CONFLICT(pending_key) DO UPDATE SET binding_id=excluded.binding_id",
                    (pending, identity),
                )
            connection.execute(
                "INSERT INTO watermarks (pending_key, serial, digest) VALUES (?, ?, ?) "
                "ON CONFLICT(pending_key) DO UPDATE SET serial=excluded.serial, digest=excluded.digest",
                (pending, capture["serial"], capture["digest"]),
            )
            if indexed is not None:
                source_writes = 3 if existing is None else 1
                require(connection.total_changes == before_changes + source_writes, "unexpected registration writes")
                changes = {key: execution_index.Change(captured[key], after) for key, after in expected.items()}
                index_writes, _ = execution_index.maintain(connection, indexed, changes=changes, deadline=deadline)
                require(
                    connection.total_changes == before_changes + source_writes + index_writes,
                    "unexpected execution index writes",
                )
            busy_timeout(connection, deadline)
            connection.commit()
            if created:
                os.fsync(pinned_fd)
            return existing if existing is not None else proposed
    except StoreError:
        raise
    except sqlite3.Error as error:
        if sqlite_lock_error(error):
            raise LockTimeoutError(LOCK_TIMEOUT_MESSAGE) from None
        raise StoreError("local execution store registration failed") from None
    except (OSError, UnicodeError, OverflowError, RecursionError):
        raise StoreError("local execution store registration failed") from None


def get(path: Path, identity: str, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Read an existing binding and its current status without creating or recovering any file."""
    deadline = Deadline(lock_timeout)
    require(isinstance(identity, str) and DIGEST.fullmatch(identity), "invalid execution binding identity")
    try:
        with (
            locked(path, write=False, deadline=deadline) as (absolute, _),
            database(absolute, create=False, deadline=deadline) as (connection, _created),
        ):
            busy_timeout(connection, deadline)
            connection.execute("BEGIN")
            indexed = None
            if connection.execute("PRAGMA user_version").fetchone() == (INDEX_VERSION,):
                from exacly import execution_index

                indexed = execution_index.check(connection, deadline=deadline)
                records, active, watermarks = indexed.snapshot
            else:
                records, active, watermarks = validate(connection, deadline=deadline)
            require(identity in records, "execution binding does not exist")
            binding = records[identity]
            pending = binding["pending_key"]
            successors = [value["id"] for value in records.values() if value["supersedes"] == identity]
            result = {
                "binding": binding,
                "active": active[pending] == identity,
                "superseded_by": successors[0] if successors else None,
                "state_watermark": watermarks[pending],
            }
            if indexed is not None:
                result["validation"] = indexed.facts
            return result
    except StoreError:
        raise
    except sqlite3.Error as error:
        if sqlite_lock_error(error):
            raise LockTimeoutError(LOCK_TIMEOUT_MESSAGE) from None
        raise StoreError("local execution store read failed") from None
    except (OSError, UnicodeError, OverflowError, RecursionError):
        raise StoreError("local execution store read failed") from None

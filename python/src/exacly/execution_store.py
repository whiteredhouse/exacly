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
from collections.abc import Iterator
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
def locked(path: Path, *, write: bool) -> Iterator[tuple[Path, int]]:
    absolute = location(path)
    directory_fd = os.open(absolute.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Lock before creation so a concurrent caller cannot see a half-initialized database.
        require(fcntl is not None, "local execution storage requires POSIX locks")
        fcntl.flock(directory_fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
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


def header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0",
        "execution store is not a recognized SQLite database",
    )
    require(content[18:20] == b"\x01\x01", "execution store requires DELETE journaling")
    require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized execution store application")
    require(int.from_bytes(content[60:64], "big") == USER_VERSION, "unsupported execution store schema version")


@contextmanager
def database(path: Path, *, create: bool) -> Iterator[tuple[sqlite3.Connection, bool]]:
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
            path.as_uri() + ("?mode=rw" if create else "?mode=ro"), uri=True, timeout=30, isolation_level=None
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


def validate(connection: sqlite3.Connection) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
    require(
        connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,),
        "unrecognized execution store application",
    )
    require(
        connection.execute("PRAGMA user_version").fetchone() == (USER_VERSION,),
        "unsupported execution store schema version",
    )
    require(
        connection.execute("PRAGMA journal_mode").fetchone() == ("delete",),
        "execution store requires DELETE journaling",
    )
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    expected = sorted(("table", name, name, sql) for name, sql in SCHEMA.items())
    require(sorted(objects) == expected, "unrecognized execution store schema")
    require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "execution store integrity check failed")
    require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "execution store relationships are invalid")
    records: dict[str, Doc] = {}
    for identity, pending, supersedes, text in connection.execute(
        "SELECT id, pending_key, supersedes, record_json FROM bindings"
    ):
        value = decoded(text)
        require(
            (value["id"], value["pending_key"], value["supersedes"]) == (identity, pending, supersedes),
            "stored binding identity is inconsistent",
        )
        records[identity] = value
    active = dict(connection.execute("SELECT pending_key, binding_id FROM active"))
    watermarks = {
        pending: {"serial": serial, "digest": digest}
        for pending, serial, digest in connection.execute("SELECT pending_key, serial, digest FROM watermarks")
    }
    pending_keys = {value["pending_key"] for value in records.values()}
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
            seen == {identity for identity, value in records.items() if value["pending_key"] == pending},
            "execution supersession chain is incomplete",
        )
    return records, active, watermarks


def configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")


def put(path: Path, value: Doc) -> Doc:
    """Atomically append a binding, explicitly supersede the active binding, or retry its identity."""
    proposed = record(value)
    try:
        with (
            locked(path, write=True) as (absolute, directory_fd),
            database(absolute, create=True) as (connection, created),
        ):
            if created:
                configure(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={USER_VERSION}")
                for statement in SCHEMA.values():
                    connection.execute(statement)
                records: dict[str, Doc] = {}
                active: dict[str, str] = {}
                watermarks: dict[str, Doc] = {}
            else:
                records, active, watermarks = validate(connection)
                configure(connection)
                connection.execute("BEGIN IMMEDIATE")
            identity, pending = proposed["id"], proposed["pending_key"]
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
            connection.commit()
            if created:
                os.fsync(directory_fd)
            return existing if existing is not None else proposed
    except StoreError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, OverflowError, RecursionError):
        raise StoreError("local execution store registration failed") from None


def get(path: Path, identity: str) -> Doc:
    """Read an existing binding and its current status without creating or recovering any file."""
    require(isinstance(identity, str) and DIGEST.fullmatch(identity), "invalid execution binding identity")
    try:
        with locked(path, write=False) as (absolute, _), database(absolute, create=False) as (connection, _created):
            connection.execute("BEGIN")
            records, active, watermarks = validate(connection)
            require(identity in records, "execution binding does not exist")
            binding = records[identity]
            pending = binding["pending_key"]
            successors = [value["id"] for value in records.values() if value["supersedes"] == identity]
            return {
                "binding": binding,
                "active": active[pending] == identity,
                "superseded_by": successors[0] if successors else None,
                "state_watermark": watermarks[pending],
            }
    except StoreError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, OverflowError, RecursionError):
        raise StoreError("local execution store read failed") from None

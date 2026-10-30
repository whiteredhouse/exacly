"""Private durable local handoff intentions, exact Git objects and publication receipts.

The prepared object package is retained in the same FULL-synchronous SQLite transaction as its
authorization. Recovery therefore does not depend on the original candidate files or unreachable
objects in a Git repository. This journal is separate from execution and binding storage.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
StoreError = store.StoreError
APPLICATION_ID = 0x4558484F  # EXHO; independent of binding, dispatch and executor journals.
USER_VERSION = 1
VERSION = "0.1.0"
JSON_LIMIT = 32 * 1024 * 1024
PREPARED_LIMIT = 256 * 1024 * 1024
_ROW_LIMIT = PREPARED_LIMIT + 3 * JSON_LIMIT + 64 * 1024
_INTENT_KEYS = {"schema_version", "id", "identity", "authorization", "contract", "created_at"}
SCHEMA = {
    "handoff_journal": "CREATE TABLE handoff_journal (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "handoffs": (
        "CREATE TABLE handoffs (id TEXT PRIMARY KEY NOT NULL, intent_json TEXT NOT NULL, "
        "intent_digest TEXT NOT NULL, prepared_json TEXT NOT NULL, prepared_digest TEXT NOT NULL, "
        "phase TEXT NOT NULL, receipt_json TEXT, receipt_digest TEXT, head_digest TEXT NOT NULL)"
    ),
}


def _canonical(value: Any, *, limit: int = JSON_LIMIT) -> str:
    def check(item: Any) -> None:
        if isinstance(item, dict):
            store.require(all(isinstance(key, str) for key in item), "handoff records require string object keys")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        else:
            store.require(item is None or type(item) in (str, bool, int, float), "handoff records require JSON values")

    try:
        check(value)
        result = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        store.require(len(result.encode()) <= limit, "handoff journal JSON exceeds the size limit")
        return result
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise StoreError("handoff journal does not contain supported bounded JSON") from None


def _decoded(text: Any, *, limit: int = JSON_LIMIT) -> Doc:
    store.require(isinstance(text, str) and len(text.encode()) <= limit, "invalid handoff journal JSON")
    try:
        value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(
            isinstance(value, dict) and _canonical(value, limit=limit) == text,
            "handoff journal JSON is not canonical",
        )
        return dict(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError("invalid handoff journal JSON") from None


def _intent(value: Any) -> Doc:
    normalized = _decoded(_canonical(value))
    store.require(set(normalized) == _INTENT_KEYS and normalized["schema_version"] == VERSION, "invalid handoff intent")
    store.require(
        all(isinstance(normalized[key], dict) for key in ("identity", "authorization", "contract")),
        "invalid handoff intent objects",
    )
    store.require(
        isinstance(normalized["id"], str) and normalized["id"] == planning.digest(normalized["identity"]),
        "handoff identity differs from its content",
    )
    try:
        timestamp = dt.datetime.fromisoformat(normalized["created_at"].replace("Z", "+00:00"))
        store.require(timestamp.utcoffset() is not None, "handoff timestamp requires a timezone")
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise StoreError("invalid handoff timestamp") from None
    return normalized


def _prepared(value: Any) -> Doc:
    normalized = _decoded(_canonical(value, limit=PREPARED_LIMIT), limit=PREPARED_LIMIT)
    store.require(isinstance(normalized.get("objects"), list), "handoff preparation requires retained Git objects")
    return normalized


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    store.require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0" and content[18:20] == b"\x01\x01",
        "handoff journal is not a DELETE-mode SQLite database",
    )
    store.require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized handoff journal application")
    store.require(int.from_bytes(content[60:64], "big") == USER_VERSION, "unsupported handoff journal schema version")


def _sidecars(path: Path, *, recover: bool) -> Path | None:
    store.require(
        not any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm")),
        "handoff journal WAL sidecars are unsupported",
    )
    rollback = Path(str(path) + "-journal")
    if not os.path.lexists(rollback):
        return None
    store.require(recover, "handoff journal has a rollback sidecar; use explicit handoff journal recovery")
    store.check_file_info(rollback.lstat())
    fd = os.open(rollback, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = store.file_info(fd)
        prefix = os.pread(fd, 28, 0)
        store.require(
            info.st_size == 0 or (len(prefix) == 28 and prefix[:8] in (b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7", b"\0" * 8)),
            "unrecognized handoff rollback journal",
        )
    finally:
        os.close(fd)
    return rollback


def _copy(source: Path, destination: Path) -> str:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = store.file_info(descriptor)
        digest = hashlib.sha256()
        output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output_fd, "wb") as output:
            while block := os.read(descriptor, 1024 * 1024):
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        store.require(_identity(os.fstat(descriptor)) == _identity(before), "handoff recovery input changed")
        store.require(_identity(source.lstat()) == _identity(before), "handoff recovery path changed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _backup(path: Path, rollback: Path, parent_fd: int) -> Path:
    folder = Path(tempfile.mkdtemp(prefix=".handoff-recovery-", dir=path.parent))
    files = {
        "database.sqlite3": _copy(path, folder / "database.sqlite3"),
        "database.sqlite3-journal": _copy(rollback, folder / "database.sqlite3-journal"),
    }
    receipt = {"schema_version": VERSION, "operation": "handoff-recovery-backup", "source": str(path), "files": files}
    descriptor = os.open(folder / "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(_canonical(receipt) + "\n")
        output.flush()
        os.fsync(output.fileno())
    folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(folder_fd)
    finally:
        os.close(folder_fd)
    os.fsync(parent_fd)
    return folder


def _structure(connection: sqlite3.Connection) -> tuple[str, Doc]:
    store.require(
        connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,),
        "unrecognized handoff journal application",
    )
    store.require(
        connection.execute("PRAGMA user_version").fetchone() == (USER_VERSION,),
        "unsupported handoff journal schema version",
    )
    found = connection.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in found} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "handoff journal schema differs from the installed format",
    )
    store.require(
        connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "handoff journal requires DELETE mode"
    )
    metadata = connection.execute("SELECT key, value FROM handoff_journal LIMIT 3").fetchall()
    store.require(
        len(metadata) == 2 and {key for key, _ in metadata} == {"journal_id", "repository"},
        "handoff journal identity metadata is missing or ambiguous",
    )
    values = dict(metadata)
    journal_id = values["journal_id"]
    try:
        identity = uuid.UUID(hex=journal_id)
        store.require(
            identity.hex == journal_id and identity.version == 4 and identity.variant == uuid.RFC_4122,
            "invalid handoff journal instance identity",
        )
    except (ValueError, TypeError, AttributeError):
        raise StoreError("invalid handoff journal instance identity") from None
    repository = _decoded(values["repository"])
    store.require(bool(repository), "handoff repository identity is empty")
    return str(journal_id), repository


def _head(intent_digest: str, prepared_digest: str, phase: str, receipt_digest: str | None) -> str:
    return planning.digest(
        {"intent": intent_digest, "prepared": prepared_digest, "phase": phase, "receipt": receipt_digest}
    )


def _check_recovery_backup(backup: Path, deadline: store.Deadline) -> None:
    """Recognize the recovered format on a disposable copy before touching original inputs."""
    with tempfile.TemporaryDirectory(prefix=".handoff-recovery-check-", dir=backup.parent) as directory:
        folder = Path(directory)
        path = folder / "database.sqlite3"
        _copy(backup / "database.sqlite3", path)
        _copy(backup / "database.sqlite3-journal", folder / "database.sqlite3-journal")
        parent_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _ROW_LIMIT)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA trusted_schema=OFF")
            journal_id, repository = _structure(connection)
            journal = Journal(
                connection, parent_fd, journal_id=journal_id, repository=repository, write=False, deadline=deadline
            )
            for (identity,) in connection.execute("SELECT id FROM handoffs").fetchall():
                journal.get(identity)
        finally:
            if connection is not None:
                connection.close()
            os.close(parent_fd)


class Journal:
    """A checked session whose mutations retain one immutable authorization and object package."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        parent_fd: int,
        *,
        journal_id: str,
        repository: Doc,
        write: bool,
        deadline: store.Deadline,
        recovery_backup: Path | None = None,
    ) -> None:
        self.connection = connection
        self.parent_fd = parent_fd
        self.journal_id = journal_id
        self._repository = _decoded(_canonical(repository))
        self.write = write
        self.deadline = deadline
        self.recovery_backup = recovery_backup
        self._active = True

    @property
    def repository(self) -> Doc:
        self._require_active()
        return _decoded(_canonical(self._repository))

    def _require_active(self) -> None:
        store.require(self._active, "handoff journal session is closed")

    def get(self, identity: str) -> Doc | None:
        self._require_active()
        store.require(isinstance(identity, str) and store.DIGEST.fullmatch(identity), "invalid handoff identity")
        store.busy_timeout(self.connection, self.deadline)
        row = self.connection.execute(
            "SELECT intent_json, intent_digest, prepared_json, prepared_digest, phase, receipt_json, "
            "receipt_digest, head_digest FROM handoffs WHERE id=?",
            (identity,),
        ).fetchone()
        if row is None:
            return None
        intent = _intent(_decoded(row[0]))
        prepared = _decoded(row[2], limit=PREPARED_LIMIT)
        store.require(isinstance(prepared.get("objects"), list), "handoff preparation requires retained Git objects")
        store.require(intent["id"] == identity and planning.digest(intent) == row[1], "stored handoff intent changed")
        store.require(planning.digest(prepared) == row[3], "stored handoff Git objects changed")
        phase = row[4]
        store.require(phase in {"prepared", "published"}, "invalid handoff phase")
        receipt: Doc | None = None
        if phase == "prepared":
            store.require(row[5] is None and row[6] is None, "prepared handoff contains a publication receipt")
        else:
            receipt = _decoded(row[5])
            store.require(planning.digest(receipt) == row[6], "stored handoff receipt changed")
        digest = _head(row[1], row[3], phase, row[6])
        store.require(digest == row[7], "handoff head differs from retained facts")
        return {"intent": intent, "prepared": prepared, "phase": phase, "receipt": receipt, "digest": digest}

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._require_active()
        store.require(self.write, "handoff journal is read-only")
        store.busy_timeout(self.connection, self.deadline)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        try:
            os.fsync(self.parent_fd)
        except OSError as error:
            raise StoreError(
                "handoff journal commit completed but directory durability is uncertain; inspect before retrying"
            ) from error

    def prepare(self, proposed: Doc, prepared: Doc) -> Doc:
        intent, prepared = _intent(proposed), _prepared(prepared)
        identity = intent["id"]
        with self._transaction():
            current = self.get(identity)
            if current is not None:
                store.require(
                    current["intent"] == intent and current["prepared"] == prepared,
                    "handoff identity is already associated with another intent or object package",
                )
            else:
                intent_digest, prepared_digest = planning.digest(intent), planning.digest(prepared)
                self.connection.execute(
                    "INSERT INTO handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity,
                        _canonical(intent),
                        intent_digest,
                        _canonical(prepared, limit=PREPARED_LIMIT),
                        prepared_digest,
                        "prepared",
                        None,
                        None,
                        _head(intent_digest, prepared_digest, "prepared", None),
                    ),
                )
        result = self.get(identity)
        assert result is not None
        return result

    def finish(self, identity: str, value: Doc) -> Doc:
        receipt = _decoded(_canonical(value))
        with self._transaction():
            current = self.get(identity)
            store.require(current is not None, "handoff intent does not exist")
            assert current is not None
            if current["phase"] == "published":
                store.require(current["receipt"] == receipt, "handoff publication receipt is immutable")
            else:
                digest = planning.digest(receipt)
                self.connection.execute(
                    "UPDATE handoffs SET phase=?, receipt_json=?, receipt_digest=?, head_digest=? WHERE id=?",
                    (
                        "published",
                        _canonical(receipt),
                        digest,
                        _head(
                            planning.digest(current["intent"]),
                            planning.digest(current["prepared"]),
                            "published",
                            digest,
                        ),
                        identity,
                    ),
                )
        result = self.get(identity)
        assert result is not None
        return result


@contextmanager
def opened(
    path: Path,
    *,
    write: bool,
    deadline: store.Deadline | None = None,
    already_locked: bool = False,
    recover: bool = False,
) -> Iterator[Journal]:
    """Open existing storage. Explicit repair first retains a protected exact input backup."""
    budget = deadline if deadline is not None else store.Deadline()
    if not already_locked:
        with (
            store.locked(path, write=write, deadline=budget) as (absolute, _),
            opened(absolute, write=write, deadline=budget, already_locked=True, recover=recover) as selected_journal,
        ):
            yield selected_journal
        return
    store.require(type(write) is bool and type(recover) is bool and (write or not recover), "invalid handoff open mode")
    absolute = store.location(path)
    parent_fd = os.open(absolute.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    connection: sqlite3.Connection | None = None
    journal: Journal | None = None
    try:
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = store.file_info(descriptor)
        _header(descriptor)
        rollback = _sidecars(absolute, recover=recover)
        backup = _backup(absolute, rollback, parent_fd) if rollback is not None else None
        if backup is not None:
            _check_recovery_backup(backup, budget)
        connection = sqlite3.connect(
            absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=budget.remaining(),
            isolation_level=None,
        )
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _ROW_LIMIT)
        store.busy_timeout(connection, budget)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        journal_id, repository = _structure(connection)
        store.require(
            (absolute.lstat().st_dev, absolute.lstat().st_ino) == (before.st_dev, before.st_ino),
            "handoff journal file changed",
        )
        journal = Journal(
            connection,
            parent_fd,
            journal_id=journal_id,
            repository=repository,
            write=write,
            deadline=budget,
            recovery_backup=backup,
        )
        if rollback is not None:
            for (identity,) in connection.execute("SELECT id FROM handoffs").fetchall():
                journal.get(identity)
            with journal._transaction():
                connection.execute(f"PRAGMA user_version={USER_VERSION}")
            _sidecars(absolute, recover=False)
        yield journal
        store.require(store.location(path) == absolute, "handoff journal path changed")
        current = absolute.lstat()
        store.require(
            (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "handoff journal file changed"
        )
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(
                "handoff journal lock timed out; retry after other operations finish"
            ) from None
        raise StoreError("handoff journal database operation failed") from error
    except (OSError, UnicodeError, RecursionError) as error:
        raise StoreError("cannot open the private handoff journal") from error
    finally:
        if journal is not None:
            journal._active = False
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def initialize(path: Path, *, repository: Doc, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Explicitly create a new journal pinned to one reviewed repository identity."""
    selected = _decoded(_canonical(repository))
    store.require(bool(selected), "handoff repository identity is empty")
    repository_path = selected.get("path")
    store.require(isinstance(repository_path, str) and "\0" not in repository_path, "invalid handoff repository path")
    assert isinstance(repository_path, str)
    destination = Path(repository_path)
    store.require(
        destination.is_absolute()
        and str(destination) == repository_path
        and ".." not in destination.parts
        and destination == destination.resolve(),
        "handoff repository path must be canonical and absolute",
    )
    journal_path = store.location(path)
    store.require(
        journal_path != destination
        and journal_path not in destination.parents
        and destination not in journal_path.parents,
        "handoff repository overlaps protected journal storage",
    )
    budget = store.Deadline(lock_timeout)
    journal_id = uuid.uuid4().hex
    with store.locked(path, write=True, deadline=budget) as (absolute, parent_fd):
        _sidecars(absolute, recover=False)
        store.require(not os.path.lexists(absolute), "handoff journal initialization requires a new destination")
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        before = os.fstat(descriptor)
        connection: sqlite3.Connection | None = None
        complete = False
        try:
            store.file_info(descriptor)
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _ROW_LIMIT)
            store.busy_timeout(connection, budget)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO handoff_journal VALUES (?, ?)",
                (("journal_id", journal_id), ("repository", _canonical(selected))),
            )
            connection.commit()
            os.fsync(parent_fd)
            store.require(
                _structure(connection) == (journal_id, selected),
                "handoff journal identity changed during initialization",
            )
            complete = True
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            current = absolute.lstat()
            if not complete and (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino):
                absolute.unlink()
            os.close(descriptor)
    return {
        "schema_version": VERSION,
        "operation": "initialize-handoff-journal",
        "status": "initialized",
        "journal": str(absolute),
        "journal_id": journal_id,
        "repository": selected,
        "path": str(absolute),
        "user_version": USER_VERSION,
    }


init = initialize

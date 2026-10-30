"""A separately initialized, owner-private dispatch outbox.

Intents and event payloads are immutable. A committed head anchors each selected event chain; ordinary
reads and writes never initialize or recover a database. Explicit rollback-journal recovery first
preserves exact input bytes in private storage. This coordinates cooperating processes of one user,
not an adversary able to modify that user's private files.
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
APPLICATION_ID = 0x45584450  # EXDP; independent of the binding ledger's EXCL application.
USER_VERSION = 1
VERSION = "0.1.0"
MAX_EVENTS = 256
JSON_LIMIT = 8 * 1024 * 1024
_INTENT_KEYS = {"schema_version", "id", "identity", "contract", "paths", "created_at", "authorization", "effect"}
_PHASES = {
    "prepared": {"prepared", "creating", "remote_confirmed", "superseded"},
    "creating": {"creating", "ambiguous", "remote_confirmed", "superseded"},
    "ambiguous": {"ambiguous", "remote_confirmed", "superseded"},
    "remote_confirmed": {"remote_confirmed", "registration_prepared", "superseded"},
    "registration_prepared": {"registration_prepared", "registered", "superseded"},
    "registered": {"registered", "superseded"},
    "superseded": {"superseded"},
}
SCHEMA = {
    "dispatch_journal": "CREATE TABLE dispatch_journal (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "dispatch_intents": (
        "CREATE TABLE dispatch_intents (id TEXT PRIMARY KEY NOT NULL, intent_json TEXT NOT NULL, "
        "intent_digest TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence >= 1), head_digest TEXT NOT NULL)"
    ),
    "dispatch_events": (
        "CREATE TABLE dispatch_events (dispatch_id TEXT NOT NULL REFERENCES dispatch_intents(id), "
        "sequence INTEGER NOT NULL CHECK (sequence >= 1), phase TEXT NOT NULL, at TEXT NOT NULL, "
        "data_json TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (dispatch_id, sequence))"
    ),
}


def _canonical(value: Any) -> str:
    result = store.canonical(value)
    store.require(len(result.encode()) <= JSON_LIMIT, "dispatch journal JSON exceeds the size limit")
    return result


def _decoded(text: Any) -> Doc:
    store.require(isinstance(text, str) and len(text.encode()) <= JSON_LIMIT, "invalid dispatch journal JSON")
    try:
        value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(isinstance(value, dict) and _canonical(value) == text, "dispatch journal JSON is not canonical")
        return dict(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError("invalid dispatch journal JSON") from None


def _timestamp(value: Any) -> str:
    store.require(isinstance(value, str), "invalid dispatch journal timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        store.require(parsed.utcoffset() is not None, "dispatch journal timestamp requires a timezone")
    except (ValueError, OverflowError):
        raise StoreError("invalid dispatch journal timestamp") from None
    return str(value)


def _intent(value: Any) -> Doc:
    normalized = _decoded(_canonical(value))
    store.require(
        set(normalized) == _INTENT_KEYS and normalized["schema_version"] == VERSION, "invalid dispatch intent"
    )
    store.require(
        all(isinstance(normalized[key], dict) for key in ("identity", "contract", "paths", "authorization", "effect")),
        "invalid dispatch intent objects",
    )
    store.require(
        isinstance(normalized["id"], str) and normalized["id"] == planning.digest(normalized["identity"]),
        "dispatch identity differs from its content",
    )
    _timestamp(normalized["created_at"])
    return normalized


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    store.require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0" and content[18:20] == b"\x01\x01",
        "dispatch journal is not a DELETE-mode SQLite database",
    )
    store.require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized dispatch journal application")
    store.require(int.from_bytes(content[60:64], "big") == USER_VERSION, "unsupported dispatch journal schema version")


def _sidecars(path: Path, *, recover: bool) -> Path | None:
    store.require(
        not any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm")),
        "dispatch journal WAL sidecars are unsupported",
    )
    journal = Path(str(path) + "-journal")
    if not os.path.lexists(journal):
        return None
    store.require(recover, "dispatch journal has a rollback sidecar; use explicit dispatch recovery")
    store.check_file_info(journal.lstat())
    fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = store.file_info(fd)
        prefix = os.pread(fd, 28, 0)
        store.require(
            info.st_size == 0 or (len(prefix) == 28 and prefix[:8] in (b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7", b"\0" * 8)),
            "unrecognized dispatch rollback journal",
        )
    finally:
        os.close(fd)
    return journal


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
        store.require(_identity(os.fstat(descriptor)) == _identity(before), "dispatch recovery input changed")
        current = source.lstat()
        store.require(_identity(current) == _identity(before), "dispatch recovery path changed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _backup(path: Path, journal: Path, parent_fd: int) -> Path:
    folder = Path(tempfile.mkdtemp(prefix=".dispatch-recovery-", dir=path.parent))
    files = {
        "database.sqlite3": _copy(path, folder / "database.sqlite3"),
        "database.sqlite3-journal": _copy(journal, folder / "database.sqlite3-journal"),
    }
    receipt = {"schema_version": VERSION, "operation": "dispatch-recovery-backup", "source": str(path), "files": files}
    receipt_fd = os.open(folder / "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(receipt_fd, "w", encoding="utf-8") as output:
        output.write(_canonical(receipt) + "\n")
        output.flush()
        os.fsync(output.fileno())
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)
    return folder


def _structure(connection: sqlite3.Connection) -> str:
    found = connection.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in found} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "dispatch journal schema differs from the installed format",
    )
    store.require(
        connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "dispatch journal requires DELETE mode"
    )
    metadata = connection.execute("SELECT key, value FROM dispatch_journal LIMIT 2").fetchall()
    store.require(
        len(metadata) == 1 and metadata[0][0] == "journal_id" and isinstance(metadata[0][1], str),
        "dispatch journal identity metadata is missing or ambiguous",
    )
    journal_id = str(metadata[0][1])
    try:
        identity = uuid.UUID(hex=journal_id)
    except ValueError:
        raise StoreError("invalid dispatch journal instance identity") from None
    store.require(
        identity.hex == journal_id and identity.version == 4 and identity.variant == uuid.RFC_4122,
        "invalid dispatch journal instance identity",
    )
    return journal_id


class Journal:
    """A checked connection used while the caller holds the directory's operation lock."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        parent_fd: int,
        *,
        journal_id: str,
        write: bool,
        deadline: store.Deadline,
        recovery_backup: Path | None = None,
    ) -> None:
        self.connection = connection
        self._journal_id = journal_id
        self.parent_fd = parent_fd
        self.write = write
        self.deadline = deadline
        self.recovery_backup = recovery_backup

    @property
    def journal_id(self) -> str:
        """The immutable UUID4 instance identity retained when this outbox was initialized."""
        return self._journal_id

    def get(self, identity: str) -> Doc | None:
        store.require(
            isinstance(identity, str) and store.DIGEST.fullmatch(identity), "invalid dispatch journal identity"
        )
        row = self.connection.execute(
            "SELECT intent_json, intent_digest, sequence, head_digest FROM dispatch_intents WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            return None
        intent = _intent(_decoded(row[0]))
        store.require(intent["id"] == identity and planning.digest(intent) == row[1], "stored dispatch intent changed")
        store.require(type(row[2]) is int and 1 <= row[2] <= MAX_EVENTS, "invalid dispatch event count")
        rows = self.connection.execute(
            "SELECT sequence, phase, at, data_json, previous, digest FROM dispatch_events "
            "WHERE dispatch_id=? ORDER BY sequence LIMIT ?",
            (identity, MAX_EVENTS + 1),
        ).fetchall()
        store.require(len(rows) == row[2], "dispatch event history is incomplete")
        previous = row[1]
        phase: str | None = None
        events: list[Doc] = []
        for sequence, event in enumerate(rows, 1):
            number, next_phase, at, data, expected, digest = event
            store.require(type(number) is int and number == sequence, "dispatch event sequence changed")
            store.require(
                next_phase in ({"prepared"} if phase is None else _PHASES[phase]), "invalid dispatch phase transition"
            )
            store.require(expected == previous, "dispatch event chain changed")
            value = {
                "seq": sequence,
                "phase": next_phase,
                "at": _timestamp(at),
                "data": _decoded(data),
                "previous": expected,
            }
            store.require(planning.digest(value) == digest, "dispatch event digest differs from its content")
            events.append({**value, "digest": digest})
            previous, phase = digest, next_phase
        store.require(previous == row[3], "dispatch journal head differs from its history")
        return {"intent": intent, "events": events, "phase": phase, "digest": previous}

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        store.require(self.write, "dispatch journal is read-only")
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
                "dispatch journal commit completed but directory durability is uncertain; inspect before retrying"
            ) from error

    def prepare(self, proposed: Doc) -> Doc:
        intent = _intent(proposed)
        identity = intent["id"]
        with self._transaction():
            current = self.get(identity)
            if current is not None:
                store.require(
                    current["intent"] == intent, "dispatch identity is already associated with another intent"
                )
            else:
                initial = {
                    "seq": 1,
                    "phase": "prepared",
                    "at": intent["created_at"],
                    "data": {"kind": "prepared"},
                    "previous": planning.digest(intent),
                }
                digest = planning.digest(initial)
                self.connection.execute(
                    "INSERT INTO dispatch_intents VALUES (?, ?, ?, ?, ?)",
                    (identity, _canonical(intent), initial["previous"], 1, digest),
                )
                self.connection.execute(
                    "INSERT INTO dispatch_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity,
                        1,
                        initial["phase"],
                        initial["at"],
                        _canonical(initial["data"]),
                        initial["previous"],
                        digest,
                    ),
                )
        result = self.get(identity)
        assert result is not None
        return result

    def append(self, identity: str, phase: str, data: Doc) -> Doc:
        normalized = _decoded(_canonical(data))
        with self._transaction():
            current = self.get(identity)
            store.require(current is not None, "dispatch intent does not exist")
            assert current is not None
            store.require(
                isinstance(phase, str) and phase in _PHASES[current["phase"]], "invalid dispatch phase transition"
            )
            sequence = len(current["events"]) + 1
            store.require(sequence <= MAX_EVENTS, "dispatch event history limit reached")
            value = {
                "seq": sequence,
                "phase": phase,
                "at": dt.datetime.now(dt.UTC).isoformat(),
                "data": normalized,
                "previous": current["digest"],
            }
            digest = planning.digest(value)
            self.connection.execute(
                "INSERT INTO dispatch_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identity, sequence, phase, value["at"], _canonical(normalized), current["digest"], digest),
            )
            self.connection.execute(
                "UPDATE dispatch_intents SET sequence=?, head_digest=? WHERE id=?", (sequence, digest, identity)
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
    """Open existing storage; recovery is an explicit write operation with a retained backup."""
    budget = deadline if deadline is not None else store.Deadline()
    if not already_locked:
        with (
            store.locked(path, write=write, deadline=budget) as (absolute, _),
            opened(absolute, write=write, deadline=budget, already_locked=True, recover=recover) as journal,
        ):
            yield journal
        return
    store.require(
        type(write) is bool and type(recover) is bool and (write or not recover), "invalid dispatch open mode"
    )
    absolute = store.location(path)
    parent_fd = os.open(absolute.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    connection: sqlite3.Connection | None = None
    try:
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = store.file_info(descriptor)
        _header(descriptor)
        rollback = _sidecars(absolute, recover=recover)
        backup = _backup(absolute, rollback, parent_fd) if rollback is not None else None
        connection = sqlite3.connect(
            absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=budget.remaining(),
            isolation_level=None,
        )
        # Bound SQLite's allocation before fetching a corrupt oversized row, not only the JSON
        # decoder after materialization. A row also contains the small immutable metadata columns.
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, JSON_LIMIT + 64 * 1024)
        store.busy_timeout(connection, budget)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        journal_id = _structure(connection)
        store.require(
            (absolute.lstat().st_dev, absolute.lstat().st_ino) == (before.st_dev, before.st_ino),
            "dispatch journal file changed",
        )
        journal = Journal(
            connection, parent_fd, journal_id=journal_id, write=write, deadline=budget, recovery_backup=backup
        )
        if rollback is not None:
            for (identity,) in connection.execute("SELECT id FROM dispatch_intents").fetchall():
                journal.get(identity)
            # A cold, zero-header journal may be ignored by SQLite on read. Rewriting the same
            # recognized format marker completes a normal SQLite transaction and DELETE cleanup.
            with journal._transaction():
                connection.execute(f"PRAGMA user_version={USER_VERSION}")
            _sidecars(absolute, recover=False)
        yield journal
        store.require(store.location(path) == absolute, "dispatch journal path changed")
        current = absolute.lstat()
        store.require(
            (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "dispatch journal file changed"
        )
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(
                "dispatch journal lock timed out; retry after other operations finish"
            ) from None
        raise StoreError("dispatch journal database operation failed") from error
    except (OSError, UnicodeError, RecursionError) as error:
        raise StoreError("cannot open the private dispatch journal") from error
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def initialize(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Create a new empty outbox explicitly, without replacing or migrating existing storage."""
    budget = store.Deadline(lock_timeout)
    journal_id = uuid.uuid4().hex
    with store.locked(path, write=True, deadline=budget) as (absolute, parent_fd):
        _sidecars(absolute, recover=False)
        store.require(not os.path.lexists(absolute), "dispatch journal initialization requires a new destination")
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        before = os.fstat(descriptor)
        connection: sqlite3.Connection | None = None
        complete = False
        try:
            store.file_info(descriptor)
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, JSON_LIMIT + 64 * 1024)
            store.busy_timeout(connection, budget)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO dispatch_journal VALUES (?, ?)", ("journal_id", journal_id))
            connection.commit()
            os.fsync(parent_fd)
            store.require(
                _structure(connection) == journal_id, "dispatch journal identity changed during initialization"
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
        "status": "initialized",
        "operation": "initialize-dispatch-journal",
        "journal": str(absolute),
        "journal_id": journal_id,
        "path": str(absolute),
        "user_version": USER_VERSION,
    }


init = initialize

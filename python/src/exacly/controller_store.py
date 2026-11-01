"""Private single-machine workflow intents; native component journals remain authoritative."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from exacly import execution_store as store
from exacly import planning

Doc = dict[str, Any]
APPLICATION_ID = 0x45584354  # EXCT
SCHEMA = {
    "metadata": "CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "events": (
        "CREATE TABLE events (sequence INTEGER PRIMARY KEY NOT NULL, previous TEXT, "
        "event TEXT NOT NULL, digest TEXT NOT NULL UNIQUE)"
    ),
}
MAX_EVENTS = 10000


def _json(text: str) -> Doc:
    value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    store.require(isinstance(value, dict) and store.canonical(value) == text, "invalid controller journal JSON")
    return dict(value)


class Journal:
    def __init__(self, connection: sqlite3.Connection, parent_fd: int, identity: Doc, *, write: bool) -> None:
        self.connection, self.parent_fd, self.identity, self.write = connection, parent_fd, identity, write
        self.active = True

    def read(self) -> Doc:
        store.require(self.active, "controller journal scope ended")
        rows = self.connection.execute(
            "SELECT sequence, previous, event, digest FROM events ORDER BY sequence"
        ).fetchall()
        store.require(0 < len(rows) <= MAX_EVENTS, "controller history is missing or exceeds its finite capacity")
        head = None
        event: Doc = {}
        for index, (sequence, previous, text, digest) in enumerate(rows):
            event = _json(text)
            store.require(
                sequence == index
                and previous == head
                and planning.digest({"sequence": sequence, "previous": previous, "event": event}) == digest,
                "controller event chain changed",
            )
            head = digest
        return {"identity": self.identity, "event": event, "sequence": len(rows) - 1, "head": head}

    def append(self, event: Doc) -> Doc:
        store.require(self.active and self.write, "controller journal is read-only or closed")
        current = self.read()
        sequence = current["sequence"] + 1
        store.require(sequence < MAX_EVENTS, "controller history capacity exhausted; do not replace its journal")
        text = store.canonical(event)
        digest = planning.digest({"sequence": sequence, "previous": current["head"], "event": event})
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (sequence, current["head"], text, digest))
        self.connection.commit()
        os.fsync(self.parent_fd)
        return self.read()


def _metadata(connection: sqlite3.Connection) -> Doc:
    store.require(
        connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        and connection.execute("PRAGMA user_version").fetchone() == (1,)
        and connection.execute("PRAGMA journal_mode").fetchone() == ("delete",),
        "unrecognized controller journal format",
    )
    rows = connection.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in rows} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "controller journal schema changed",
    )
    metadata = connection.execute("SELECT key, value FROM metadata LIMIT 2").fetchall()
    store.require(len(metadata) == 1 and metadata[0][0] == "identity", "controller journal identity is missing")
    value = _json(metadata[0][1])
    store.require(set(value) == {"journal_id", "workflow_id", "contract"}, "invalid controller journal identity")
    try:
        parsed = uuid.UUID(hex=value["journal_id"])
        store.require(parsed.hex == value["journal_id"] and parsed.version == 4, "invalid controller instance identity")
    except (ValueError, TypeError, AttributeError):
        raise store.StoreError("invalid controller instance identity") from None
    store.require(value["workflow_id"] == planning.digest(value["contract"]), "controller workflow contract changed")
    return value


@contextmanager
def opened(
    path: Path, journal_id: str, *, write: bool, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Iterator[Journal]:
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=write, deadline=deadline) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        connection: sqlite3.Connection | None = None
        journal: Journal | None = None
        try:
            before = store.file_info(descriptor)
            header = os.pread(descriptor, 100, 0)
            store.require(
                len(header) == 100
                and header[:16] == b"SQLite format 3\0"
                and header[18:20] == b"\x01\x01"
                and int.from_bytes(header[68:72], "big") == APPLICATION_ID,
                "unrecognized controller journal header",
            )
            connection = sqlite3.connect(
                absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                timeout=deadline.remaining(),
                isolation_level=None,
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, store.RECORD_LIMIT)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=EXTRA")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            identity = _metadata(connection)
            store.require(identity["journal_id"] == journal_id, "controller journal instance changed")
            current = absolute.lstat()
            store.require(
                (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino), "controller journal file changed"
            )
            journal = Journal(connection, parent_fd, identity, write=write)
            journal.read()
            yield journal
            current = absolute.lstat()
            store.require(
                (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino), "controller journal file changed"
            )
        except sqlite3.Error as error:
            if store.sqlite_lock_error(error):
                raise store.LockTimeoutError("controller journal lock timed out") from None
            raise store.StoreError("controller journal database operation failed") from error
        finally:
            if journal is not None:
                journal.active = False
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            os.close(descriptor)


def initialize(
    path: Path,
    contract: Doc,
    event: Doc,
    *,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
    journal_id: str | None = None,
) -> Doc:
    selected_id = journal_id if journal_id is not None else uuid.uuid4().hex
    try:
        parsed = uuid.UUID(hex=selected_id)
        store.require(parsed.hex == selected_id and parsed.version == 4, "invalid controller initialized identity")
    except (ValueError, AttributeError, TypeError):
        raise store.StoreError("invalid controller initialized identity") from None
    identity = {"journal_id": selected_id, "workflow_id": planning.digest(contract), "contract": contract}
    with store.locked(path, write=True, lock_timeout=lock_timeout) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        connection: sqlite3.Connection | None = None
        try:
            store.file_info(descriptor)
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO metadata VALUES ('identity', ?)", (store.canonical(identity),))
            digest = planning.digest({"sequence": 0, "previous": None, "event": event})
            connection.execute("INSERT INTO events VALUES (0, NULL, ?, ?)", (store.canonical(event), digest))
            connection.commit()
            os.fsync(parent_fd)
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            os.close(descriptor)
    return {"journal": str(absolute), **identity}


_ANCHOR_KEYS = {"schema_version", "anchor_path", "instance_id", "ledger", "device", "inode", "user_version", "digest"}
_ANCHOR_LIMIT = 16 * 1024


def _anchor_path(path: Path) -> Path:
    store.require(
        isinstance(path, Path) and path.is_absolute() and path == path.resolve() and ".." not in path.parts,
        "ledger anchor paths must be canonical and absolute",
    )
    return store.location(path)


def _anchor_document(value: Any) -> Doc:
    store.require(isinstance(value, dict) and set(value) == _ANCHOR_KEYS, "invalid ledger anchor fields")
    normalized = _json(store.canonical(value))
    store.require(normalized["schema_version"] == "0.1.0", "unsupported ledger anchor version")
    for key in ("anchor_path", "ledger"):
        store.require(isinstance(normalized[key], str), "invalid ledger anchor path")
        _anchor_path(Path(normalized[key]))
    anchor, ledger = Path(normalized["anchor_path"]), Path(normalized["ledger"])
    protected = {str(ledger) + suffix for suffix in ("", "-journal", "-wal", "-shm")}
    store.require(
        str(anchor) not in protected
        and str(ledger) not in {str(anchor) + suffix for suffix in ("-journal", "-wal", "-shm")}
        and anchor not in ledger.parents
        and ledger not in anchor.parents,
        "ledger and its protected anchor must be separate resources",
    )
    try:
        identifier = uuid.UUID(hex=normalized["instance_id"])
        store.require(
            identifier.hex == normalized["instance_id"] and identifier.version == 4,
            "invalid ledger anchor initialized identity",
        )
    except (ValueError, TypeError, AttributeError):
        raise store.StoreError("invalid ledger anchor initialized identity") from None
    store.require(
        all(type(normalized[key]) is int and normalized[key] >= 0 for key in ("device", "inode"))
        and type(normalized["user_version"]) is int
        and normalized["user_version"] in (store.COMPLETION_VERSION, store.INDEX_VERSION),
        "ledger anchor requires a native completion-ready v2 or operational v4 identity",
    )
    store.require(
        normalized["digest"] == planning.digest({key: item for key, item in normalized.items() if key != "digest"}),
        "ledger anchor content differs from its approved digest",
    )
    return normalized


def _anchor_ledger(ledger: Path, parent_fd: int, deadline: store.Deadline) -> Doc:
    """Caller retains the canonical native ledger parent lock through validation and return."""
    from exacly import completion_store

    store.require(_anchor_path(ledger) == ledger, "native ledger path changed")
    store.auxiliary_files(ledger)
    descriptor = os.open(ledger.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = store.file_info(descriptor)
        version = store.header(descriptor)
        store.require(
            version in (store.COMPLETION_VERSION, store.INDEX_VERSION),
            "ledger anchor requires a native completion-ready v2 or operational v4 ledger",
        )
        with store.database(ledger, create=False, deadline=deadline) as (connection, _created):
            completion_store.require_v2(connection, deadline=deadline)
            store.validate(connection, deadline=deadline)
        current = ledger.stat(follow_symlinks=False)
        store.check_file_info(current)
        store.require(
            (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino) and store.header(descriptor) == version,
            "native ledger changed during anchor inspection",
        )
        return {"device": before.st_dev, "inode": before.st_ino, "user_version": version}
    finally:
        os.close(descriptor)


def _initialize_native_ledger(ledger: Path, parent_fd: int, deadline: store.Deadline) -> None:
    """Explicitly initialize only a new empty native v2 ledger; no observed state is created."""
    from exacly import completion_store

    store.require(not os.path.lexists(ledger), "native ledger initialization requires a new destination")
    with store.database(ledger, create=True, deadline=deadline) as (connection, created):
        store.require(created, "native ledger initialization requires a new destination")
        store.configure(connection, deadline=deadline)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={store.COMPLETION_VERSION}")
        for statement in {**store.SCHEMA, **completion_store.SCHEMA}.values():
            connection.execute(statement)
        connection.commit()
        store.require(store.validate(connection, deadline=deadline) == ({}, {}, {}), "new native ledger is not empty")
        completion_store.require_v2(connection, deadline=deadline)
        os.fsync(parent_fd)


def initialize_ledger_anchor(
    anchor_path: Path,
    *,
    ledger: Path,
    create_ledger: bool = False,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Explicitly pin one physical native ledger using a new protected companion identity.

    Legacy native ledgers have no initialized UUID. This companion retains their canonical path,
    device/inode and supported schema version under its own UUID and digest. An explicit optional
    bootstrap initializes only a new empty native v2 ledger; it never creates pending work or model
    state, replaces an existing file, migrates a ledger or grants effect authority. This cooperating
    local-user boundary does not defend against its owner rewriting both protected resources.
    """
    from exacly import operation_locks

    store.require(type(create_ledger) is bool, "native ledger initialization selection must be explicit")
    anchor, native = _anchor_path(anchor_path), _anchor_path(ledger)
    candidate = {
        "schema_version": "0.1.0",
        "anchor_path": str(anchor),
        "instance_id": uuid.uuid4().hex,
        "ledger": str(native),
        "device": 0,
        "inode": 0,
        "user_version": store.COMPLETION_VERSION,
    }
    _anchor_document({**candidate, "digest": planning.digest(candidate)})
    deadline = store.Deadline(lock_timeout)
    with operation_locks.directories((anchor.parent, native.parent), deadline=deadline) as pinned:
        store.require(_anchor_path(anchor) == anchor and _anchor_path(native) == native, "ledger anchor paths changed")
        store.require(not os.path.lexists(anchor), "ledger anchor initialization requires a new destination")
        if create_ledger:
            _initialize_native_ledger(native, pinned[native.parent], deadline)
        identity = _anchor_ledger(native, pinned[native.parent], deadline)
        content = {**candidate, **identity}
        approved = _anchor_document({**content, "digest": planning.digest(content)})
        descriptor = os.open(
            anchor.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pinned[anchor.parent]
        )
        try:
            store.file_info(descriptor)
            encoded = store.canonical(approved).encode()
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
            os.fsync(pinned[anchor.parent])
        finally:
            os.close(descriptor)
    return approved


def check_ledger_anchor(anchor: Doc, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Check exact private companion bytes and actual native file identity without effects or repair."""
    from exacly import operation_locks

    approved = _anchor_document(anchor)
    selected, ledger = Path(approved["anchor_path"]), Path(approved["ledger"])
    deadline = store.Deadline(lock_timeout)
    with operation_locks.directories((selected.parent, ledger.parent), deadline=deadline) as pinned:
        store.require(
            _anchor_path(selected) == selected and _anchor_path(ledger) == ledger, "ledger anchor paths changed"
        )
        descriptor = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pinned[selected.parent])
        try:
            before = store.file_info(descriptor)
            store.require(before.st_size <= _ANCHOR_LIMIT, "protected ledger anchor exceeds its size bound")
            content = os.read(descriptor, _ANCHOR_LIMIT + 1)
            store.require(content == store.canonical(approved).encode(), "protected ledger anchor content changed")
            current = selected.stat(follow_symlinks=False)
            store.check_file_info(current)
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                "protected ledger anchor file changed",
            )
        finally:
            os.close(descriptor)
        identity = _anchor_ledger(ledger, pinned[ledger.parent], deadline)
        store.require(
            identity == {key: approved[key] for key in ("device", "inode", "user_version")},
            "anchored native ledger physical identity or format changed",
        )
    return approved

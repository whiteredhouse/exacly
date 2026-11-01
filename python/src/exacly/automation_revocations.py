"""Monotonic, private revocations for independently selected standing delegations.

Only trusted operator code may initialize or write this store. Revocation has no undo or migration
operation. The UUID is pinned independently alongside the delegation; a replacement cannot un-revoke
an objective. Recovery never silently opens SQLite sidecars or recreates missing storage.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from exacly import execution_store as store

Doc = dict[str, Any]
VERSION = "0.1.0"
APPLICATION_ID = 0x45585256  # EXRV
SCHEMA = {
    "revocation_store": "CREATE TABLE revocation_store (id TEXT PRIMARY KEY NOT NULL)",
    "revocations": (
        "CREATE TABLE revocations (delegation_id TEXT PRIMARY KEY NOT NULL, document_json TEXT NOT NULL, "
        "digest TEXT NOT NULL)"
    ),
}


def _uuid(value: Any) -> str:
    store.require(isinstance(value, str), "invalid revocation store identity")
    try:
        identity = uuid.UUID(hex=value)
    except ValueError:
        raise store.StoreError("invalid revocation store identity") from None
    store.require(identity.hex == value and identity.version == 4, "invalid revocation store identity")
    return str(value)


def _text(value: Any) -> str:
    store.require(
        isinstance(value, str) and bool(value.strip()), "revocation identity and reason require nonempty text"
    )
    return str(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(store.canonical(value).encode()).hexdigest()


def _structure(connection: sqlite3.Connection) -> str:
    found = connection.execute("SELECT name, type, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in found} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "unrecognized revocation store schema",
    )
    store.require(connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "invalid revocation journaling")
    store.require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "revocation store integrity failed")
    identities = connection.execute("SELECT id FROM revocation_store").fetchall()
    store.require(len(identities) == 1, "missing or ambiguous revocation store identity")
    return _uuid(identities[0][0])


def _records(connection: sqlite3.Connection) -> dict[str, Doc]:
    records: dict[str, Doc] = {}
    for identity, text, digest in connection.execute("SELECT delegation_id, document_json, digest FROM revocations"):
        try:
            value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
            store.require(
                isinstance(value, dict)
                and set(value) == {"schema_version", "delegation_id", "reason", "revoked_at"}
                and value["schema_version"] == VERSION
                and value["delegation_id"] == identity
                and store.canonical(value) == text
                and _digest(value) == digest,
                "invalid retained revocation",
            )
            _text(identity)
            _text(value["reason"])
            timestamp = dt.datetime.fromisoformat(value["revoked_at"].replace("Z", "+00:00"))
            store.require(timestamp.utcoffset() is not None, "revocation timestamp requires a timezone")
            records[identity] = value
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
            raise store.StoreError("invalid retained revocation") from None
    return records


@contextmanager
def _opened(path: Path, store_id: str, *, write: bool, lock_timeout: float) -> Iterator[tuple[sqlite3.Connection, int]]:
    _uuid(store_id)
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=write, deadline=deadline) as (absolute, parent_fd):
        descriptor: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            store.auxiliary_files(absolute)
            descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = store.file_info(descriptor)
            header = os.pread(descriptor, 100, 0)
            store.require(
                len(header) == 100
                and header[:16] == b"SQLite format 3\0"
                and header[18:20] == b"\x01\x01"
                and int.from_bytes(header[68:72], "big") == APPLICATION_ID
                and int.from_bytes(header[60:64], "big") == 1,
                "unrecognized revocation store format",
            )
            connection = sqlite3.connect(
                absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                timeout=deadline.remaining(),
                isolation_level=None,
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, store.RECORD_LIMIT + 64 * 1024)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=EXTRA")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            store.require(_structure(connection) == store_id, "revocation store differs from trusted pinned identity")
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "revocation store changed"
            )
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            _records(connection)
            yield connection, parent_fd
            if write:
                connection.commit()
                os.fsync(parent_fd)
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "revocation store changed"
            )
        except sqlite3.Error as error:
            if store.sqlite_lock_error(error):
                raise store.LockTimeoutError("revocation store lock timed out") from None
            raise store.StoreError("revocation database operation failed") from error
        except (OSError, UnicodeError, RecursionError) as error:
            raise store.StoreError("cannot open private revocation storage") from error
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            if descriptor is not None:
                os.close(descriptor)


def initialize(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Create a fresh operator store; pin the returned UUID before approving a delegation."""
    identity = uuid.uuid4().hex
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=True, deadline=deadline) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        store.require(not os.path.lexists(absolute), "revocation initialization requires a new destination")
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        before = store.file_info(descriptor)
        connection: sqlite3.Connection | None = None
        complete = False
        try:
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO revocation_store VALUES (?)", (identity,))
            connection.commit()
            os.fsync(parent_fd)
            store.require(_structure(connection) == identity, "revocation identity changed during initialization")
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
    return identity


def status(
    path: Path, *, store_id: str, delegation_id: str, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc | None:
    """Read a revocation, failing closed when its protected history cannot be established."""
    _text(delegation_id)
    with _opened(path, store_id, write=False, lock_timeout=lock_timeout) as (connection, _):
        return _records(connection).get(delegation_id)


def revoke(
    path: Path, *, store_id: str, delegation_id: str, reason: str, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Monotonically revoke a delegation; this does not recall an already sent native request."""
    _text(delegation_id)
    _text(reason)
    with _opened(path, store_id, write=True, lock_timeout=lock_timeout) as (connection, _):
        prior = _records(connection).get(delegation_id)
        if prior is not None:
            store.require(prior["reason"] == reason, "revocation conflicts with retained history")
            return prior
        document = {
            "schema_version": VERSION,
            "delegation_id": delegation_id,
            "reason": reason,
            "revoked_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        connection.execute(
            "INSERT INTO revocations VALUES (?, ?, ?)", (delegation_id, store.canonical(document), _digest(document))
        )
        return document

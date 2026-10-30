"""An explicitly initialized private executor-attempt extension.

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
APPLICATION_ID = 0x45584541  # EXEA; independent of the binding ledger's EXCL application.
USER_VERSION = 1
VERSION = "0.1.0"
MAX_ATTEMPTS = 10
MAX_CONCURRENCY = 16
MAX_EVENTS = 32
JSON_LIMIT = 8 * 1024 * 1024
_INTENT_KEYS = {"schema_version", "id", "identity", "contract", "created_at", "max_attempts"}
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "output_invalid", "timed_out", "output_limit"})
_PHASES = {
    "reserved": {"reserved", "running", *TERMINAL},
    "running": {"running", *TERMINAL},
    **{phase: set() for phase in TERMINAL},
}
SCHEMA = {
    "executor_journal": "CREATE TABLE executor_journal (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "executor_intents": (
        "CREATE TABLE executor_intents (id TEXT PRIMARY KEY NOT NULL, intent_json TEXT NOT NULL, "
        "intent_digest TEXT NOT NULL, "
        "attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0 AND attempt_count <= 10), "
        "head_digest TEXT NOT NULL)"
    ),
    "executor_attempts": (
        "CREATE TABLE executor_attempts (execution_id TEXT NOT NULL REFERENCES executor_intents(id), "
        "number INTEGER NOT NULL CHECK (number >= 1 AND number <= 10), phase TEXT NOT NULL, "
        "sequence INTEGER NOT NULL CHECK (sequence >= 1 AND sequence <= 32), head_digest TEXT NOT NULL, "
        "PRIMARY KEY (execution_id, number))"
    ),
    "executor_events": (
        "CREATE TABLE executor_events (execution_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
        "sequence INTEGER NOT NULL CHECK (sequence >= 1 AND sequence <= 32), phase TEXT NOT NULL, at TEXT NOT NULL, "
        "data_json TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL, "
        "PRIMARY KEY (execution_id, attempt, sequence), "
        "FOREIGN KEY (execution_id,attempt) REFERENCES executor_attempts(execution_id,number))"
    ),
}


def _canonical(value: Any) -> str:
    result = store.canonical(value)
    store.require(len(result.encode()) <= JSON_LIMIT, "executor journal JSON exceeds the size limit")
    return result


def _decoded(text: Any) -> Doc:
    store.require(isinstance(text, str) and len(text.encode()) <= JSON_LIMIT, "invalid executor journal JSON")
    try:
        value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(isinstance(value, dict) and _canonical(value) == text, "executor journal JSON is not canonical")
        return dict(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError("invalid executor journal JSON") from None


def _timestamp(value: Any) -> str:
    store.require(isinstance(value, str), "invalid executor journal timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        store.require(parsed.utcoffset() is not None, "executor journal timestamp requires a timezone")
    except (ValueError, OverflowError):
        raise StoreError("invalid executor journal timestamp") from None
    return str(value)


def _intent(value: Any) -> Doc:
    normalized = _decoded(_canonical(value))
    store.require(
        set(normalized) == _INTENT_KEYS and normalized["schema_version"] == VERSION, "invalid executor intent"
    )
    store.require(
        all(isinstance(normalized[key], dict) for key in ("identity", "contract")),
        "invalid executor intent objects",
    )
    store.require(
        isinstance(normalized["id"], str) and normalized["id"] == planning.digest(normalized["identity"]),
        "executor identity differs from its content",
    )
    store.require(
        type(normalized["max_attempts"]) is int and 1 <= normalized["max_attempts"] <= MAX_ATTEMPTS,
        "executor max_attempts must be an integer from 1 to 10",
    )
    _timestamp(normalized["created_at"])
    return normalized


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    store.require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0" and content[18:20] == b"\x01\x01",
        "executor journal is not a DELETE-mode SQLite database",
    )
    store.require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized executor journal application")
    store.require(int.from_bytes(content[60:64], "big") == USER_VERSION, "unsupported executor journal schema version")


def _sidecars(path: Path, *, recover: bool) -> Path | None:
    store.require(
        not any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm")),
        "executor journal WAL sidecars are unsupported",
    )
    journal = Path(str(path) + "-journal")
    if not os.path.lexists(journal):
        return None
    store.require(recover, "executor journal has a rollback sidecar; use explicit executor recovery")
    store.check_file_info(journal.lstat())
    fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = store.file_info(fd)
        prefix = os.pread(fd, 28, 0)
        store.require(
            info.st_size == 0 or (len(prefix) == 28 and prefix[:8] in (b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7", b"\0" * 8)),
            "unrecognized executor rollback journal",
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
        store.require(_identity(os.fstat(descriptor)) == _identity(before), "executor recovery input changed")
        current = source.lstat()
        store.require(_identity(current) == _identity(before), "executor recovery path changed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _backup(path: Path, journal: Path, parent_fd: int) -> Path:
    folder = Path(tempfile.mkdtemp(prefix=".executor-recovery-", dir=path.parent))
    files = {
        "database.sqlite3": _copy(path, folder / "database.sqlite3"),
        "database.sqlite3-journal": _copy(journal, folder / "database.sqlite3-journal"),
    }
    receipt = {"schema_version": VERSION, "operation": "executor-recovery-backup", "source": str(path), "files": files}
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


def _structure(connection: sqlite3.Connection) -> Doc:
    found = connection.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in found} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "executor journal schema differs from the installed format",
    )
    store.require(
        connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "executor journal requires DELETE mode"
    )
    rows = connection.execute("SELECT key, value FROM executor_journal LIMIT 4").fetchall()
    metadata = dict(rows)
    store.require(
        len(rows) == 3 and set(metadata) == {"journal_id", "ledger", "max_concurrency"},
        "executor journal identity metadata is missing or ambiguous",
    )
    try:
        identity = uuid.UUID(hex=metadata["journal_id"])
    except (ValueError, TypeError, AttributeError):
        raise StoreError("invalid executor journal instance identity") from None
    store.require(
        identity.hex == metadata["journal_id"] and identity.version == 4 and identity.variant == uuid.RFC_4122,
        "invalid executor journal instance identity",
    )
    ledger = metadata["ledger"]
    store.require(
        isinstance(ledger, str)
        and Path(ledger).is_absolute()
        and str(Path(ledger)) == ledger
        and ".." not in Path(ledger).parts,
        "invalid executor journal ledger identity",
    )
    limit = metadata["max_concurrency"]
    store.require(
        isinstance(limit, str)
        and limit.isascii()
        and limit.isdecimal()
        and str(int(limit)) == limit
        and 1 <= int(limit) <= MAX_CONCURRENCY,
        "invalid executor journal concurrency limit",
    )
    return {"journal_id": identity.hex, "ledger": ledger, "max_concurrency": int(limit)}


class Journal:
    """A checked connection held under its canonical directory's operation lock.

    Reservation commits before launch and retains its concurrency slot until a terminal receipt.
    The supervisor must establish process termination before appending such a receipt. A clock or
    expired lease is never proof that execution stopped.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        parent_fd: int,
        *,
        metadata: Doc,
        write: bool,
        deadline: store.Deadline,
        recovery_backup: Path | None = None,
    ) -> None:
        self.connection = connection
        self.parent_fd = parent_fd
        self.journal_id = str(metadata["journal_id"])
        self.ledger = str(metadata["ledger"])
        self.max_concurrency = int(metadata["max_concurrency"])
        self.write = write
        self.deadline = deadline
        self.recovery_backup = recovery_backup

    def get(self, identity: str) -> Doc | None:
        store.require(
            isinstance(identity, str) and store.DIGEST.fullmatch(identity), "invalid executor journal identity"
        )
        row = self.connection.execute(
            "SELECT intent_json,intent_digest,attempt_count,head_digest FROM executor_intents WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            return None
        intent = _intent(_decoded(row[0]))
        store.require(intent["id"] == identity and planning.digest(intent) == row[1], "stored executor intent changed")
        attempts: list[Doc] = []
        headers = self.connection.execute(
            "SELECT number,phase,sequence,head_digest FROM executor_attempts WHERE execution_id=? "
            "ORDER BY number LIMIT ?",
            (identity, MAX_ATTEMPTS + 1),
        ).fetchall()
        store.require(
            type(row[2]) is int and 0 <= row[2] <= intent["max_attempts"] and len(headers) == row[2],
            "executor attempt budget or retained membership is inconsistent",
        )
        previous_attempt = row[1]
        for expected_number, (number, final_phase, count, head) in enumerate(headers, 1):
            store.require(type(number) is int and number == expected_number, "executor attempt sequence changed")
            store.require(type(count) is int and 1 <= count <= MAX_EVENTS, "invalid executor event count")
            if attempts:
                store.require(
                    attempts[-1]["phase"] in TERMINAL - {"succeeded"},
                    "executor attempt follows an unresolved or successful attempt",
                )
            events = self.connection.execute(
                "SELECT sequence,phase,at,data_json,previous,digest FROM executor_events "
                "WHERE execution_id=? AND attempt=? ORDER BY sequence LIMIT ?",
                (identity, number, MAX_EVENTS + 1),
            ).fetchall()
            store.require(len(events) == count, "executor event history is incomplete")
            previous = previous_attempt
            phase: str | None = None
            decoded_events: list[Doc] = []
            for sequence, event in enumerate(events, 1):
                found_sequence, next_phase, at, data, expected, digest = event
                store.require(
                    type(found_sequence) is int and found_sequence == sequence, "executor event sequence changed"
                )
                store.require(
                    isinstance(next_phase, str) and next_phase in ({"reserved"} if phase is None else _PHASES[phase]),
                    "invalid executor phase transition",
                )
                store.require(expected == previous, "executor event chain changed")
                value = {
                    "seq": sequence,
                    "phase": next_phase,
                    "at": _timestamp(at),
                    "data": _decoded(data),
                    "previous": expected,
                }
                store.require(planning.digest(value) == digest, "executor event digest differs from its content")
                decoded_events.append({**value, "digest": digest})
                previous, phase = digest, next_phase
            store.require(previous == head and phase == final_phase, "executor journal head differs from its history")
            attempts.append({"number": number, "events": decoded_events, "phase": phase, "digest": previous})
            previous_attempt = previous
        store.require(previous_attempt == row[3], "executor work head differs from its history")
        return {
            "intent": intent,
            "attempts": attempts,
            "phase": attempts[-1]["phase"] if attempts else "prepared",
            "digest": previous_attempt,
        }

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        store.require(self.write, "executor journal is read-only")
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
                "executor journal commit completed but directory durability is uncertain; inspect before retrying"
            ) from error

    def prepare(self, proposed: Doc) -> Doc:
        """Retain one immutable work contract; changed policy cannot reset the same work budget."""
        intent = _intent(proposed)
        identity = intent["id"]
        with self._transaction():
            current = self.get(identity)
            if current is not None:
                store.require(
                    current["intent"] == intent, "executor identity is already associated with another intent"
                )
            else:
                self.connection.execute(
                    "INSERT INTO executor_intents VALUES (?,?,?,?,?)",
                    (identity, _canonical(intent), planning.digest(intent), 0, planning.digest(intent)),
                )
        result = self.get(identity)
        assert result is not None
        return result

    def _capacity(self) -> int:
        # Check committed membership and phase metadata globally. Terminal output JSON can be
        # large: only unresolved heads are decoded here. Selected work is fully validated by get;
        # explicit journal recovery also validates every retained chain before repairing storage.
        roots = self.connection.execute(
            "SELECT i.id,i.intent_digest,i.attempt_count,i.head_digest,a.head_digest,"
            "(SELECT COUNT(*) FROM executor_attempts members WHERE members.execution_id=i.id) "
            "FROM executor_intents i LEFT JOIN executor_attempts a "
            "ON a.execution_id=i.id AND a.number=i.attempt_count"
        ).fetchall()
        for identity, intent_digest, count, head, attempt_head, found in roots:
            store.require(
                isinstance(identity, str)
                and store.DIGEST.fullmatch(identity)
                and isinstance(intent_digest, str)
                and store.DIGEST.fullmatch(intent_digest)
                and type(count) is int
                and 0 <= count <= MAX_ATTEMPTS
                and count == found
                and head == (intent_digest if count == 0 else attempt_head),
                "executor work membership metadata is inconsistent",
            )
        rows = self.connection.execute(
            "SELECT a.execution_id,a.number,a.phase,a.sequence,a.head_digest,"
            "e.phase,e.digest,i.id FROM executor_attempts a "
            "LEFT JOIN executor_events e ON a.execution_id=e.execution_id AND a.number=e.attempt "
            "AND a.sequence=e.sequence LEFT JOIN executor_intents i ON i.id=a.execution_id"
        ).fetchall()
        unresolved: list[tuple[str, int, str, int, str]] = []
        for identity, number, phase, sequence, head, event_phase, digest, retained_identity in rows:
            store.require(
                isinstance(identity, str)
                and store.DIGEST.fullmatch(identity)
                and type(number) is int
                and 1 <= number <= MAX_ATTEMPTS
                and type(sequence) is int
                and 1 <= sequence <= MAX_EVENTS
                and isinstance(phase, str)
                and phase in _PHASES
                and event_phase == phase
                and isinstance(head, str)
                and store.DIGEST.fullmatch(head)
                and retained_identity == identity
                and head == digest,
                "executor reservation metadata is inconsistent",
            )
            if phase not in TERMINAL:
                unresolved.append((identity, number, phase, sequence, head))
        store.require(
            len(unresolved) <= self.max_concurrency,
            "executor concurrency reservations exceed the initialized limit",
        )
        for identity, number, phase, sequence, digest in unresolved:
            row = self.connection.execute(
                "SELECT at,data_json,previous FROM executor_events WHERE execution_id=? AND attempt=? AND sequence=?",
                (identity, number, sequence),
            ).fetchone()
            store.require(row is not None, "executor reservation event is missing")
            assert row is not None
            at, data, previous = row
            value = {
                "seq": sequence,
                "phase": phase,
                "at": _timestamp(at),
                "data": _decoded(data),
                "previous": previous,
            }
            store.require(planning.digest(value) == digest, "executor reservation head digest changed")
        return len(unresolved)

    def reserve(self, identity: str, data: Doc) -> Doc:
        """Consume one attempt and capacity slot atomically, before an adapter may start."""
        normalized = _decoded(_canonical(data))
        with self._transaction():
            current = self.get(identity)
            store.require(current is not None, "executor intent does not exist")
            assert current is not None
            attempts = current["attempts"]
            store.require(
                not attempts or attempts[-1]["phase"] in TERMINAL - {"succeeded"},
                "executor work already has an unresolved or successful attempt",
            )
            store.require(len(attempts) < current["intent"]["max_attempts"], "executor attempt budget exhausted")
            store.require(self._capacity() < self.max_concurrency, "executor concurrency limit reached")
            number = len(attempts) + 1
            value = {
                "seq": 1,
                "phase": "reserved",
                "at": dt.datetime.now(dt.UTC).isoformat(),
                "data": normalized,
                "previous": current["digest"],
            }
            digest = planning.digest(value)
            self.connection.execute(
                "INSERT INTO executor_attempts VALUES (?,?,?,?,?)", (identity, number, "reserved", 1, digest)
            )
            self.connection.execute(
                "INSERT INTO executor_events VALUES (?,?,?,?,?,?,?,?)",
                (identity, number, 1, "reserved", value["at"], _canonical(normalized), current["digest"], digest),
            )
            self.connection.execute(
                "UPDATE executor_intents SET attempt_count=?,head_digest=? WHERE id=?",
                (number, digest, identity),
            )
        result = self.get(identity)
        assert result is not None
        return result

    def append(self, identity: str, attempt: int, phase: str, data: Doc) -> Doc:
        """Append a lifecycle fact; terminal receipts permit only exact immutable replay."""
        normalized = _decoded(_canonical(data))
        with self._transaction():
            current = self.get(identity)
            store.require(current is not None, "executor intent does not exist")
            assert current is not None
            store.require(
                type(attempt) is int and 1 <= attempt <= len(current["attempts"]), "executor attempt does not exist"
            )
            selected = current["attempts"][attempt - 1]
            if selected["phase"] in TERMINAL:
                store.require(
                    phase == selected["phase"] and normalized == selected["events"][-1]["data"],
                    "executor terminal receipt is immutable",
                )
            else:
                store.require(attempt == len(current["attempts"]), "only the current executor attempt may advance")
                store.require(
                    isinstance(phase, str) and phase in _PHASES[selected["phase"]], "invalid executor phase transition"
                )
                sequence = len(selected["events"]) + 1
                # Leave one slot for the terminal fact when recording intermediate lifecycle data.
                store.require(
                    sequence <= MAX_EVENTS if phase in TERMINAL else sequence < MAX_EVENTS,
                    "executor event history limit reached",
                )
                value = {
                    "seq": sequence,
                    "phase": phase,
                    "at": dt.datetime.now(dt.UTC).isoformat(),
                    "data": normalized,
                    "previous": selected["digest"],
                }
                digest = planning.digest(value)
                self.connection.execute(
                    "INSERT INTO executor_events VALUES (?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        attempt,
                        sequence,
                        phase,
                        value["at"],
                        _canonical(normalized),
                        selected["digest"],
                        digest,
                    ),
                )
                self.connection.execute(
                    "UPDATE executor_attempts SET phase=?,sequence=?,head_digest=? WHERE execution_id=? AND number=?",
                    (phase, sequence, digest, identity, attempt),
                )
                self.connection.execute("UPDATE executor_intents SET head_digest=? WHERE id=?", (digest, identity))
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
        type(write) is bool and type(recover) is bool and (write or not recover), "invalid executor open mode"
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
        metadata = _structure(connection)
        store.require(
            (absolute.lstat().st_dev, absolute.lstat().st_ino) == (before.st_dev, before.st_ino),
            "executor journal file changed",
        )
        journal = Journal(
            connection, parent_fd, metadata=metadata, write=write, deadline=budget, recovery_backup=backup
        )
        if rollback is not None:
            for (identity,) in connection.execute("SELECT id FROM executor_intents").fetchall():
                journal.get(identity)
            journal._capacity()
            # A cold, zero-header journal may be ignored by SQLite on read. Rewriting the same
            # recognized format marker completes a normal SQLite transaction and DELETE cleanup.
            with journal._transaction():
                connection.execute(f"PRAGMA user_version={USER_VERSION}")
            _sidecars(absolute, recover=False)
        yield journal
        store.require(store.location(path) == absolute, "executor journal path changed")
        current = absolute.lstat()
        store.require(
            (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "executor journal file changed"
        )
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(
                "executor journal lock timed out; retry after other operations finish"
            ) from None
        raise StoreError("executor journal database operation failed") from error
    except (OSError, UnicodeError, RecursionError) as error:
        raise StoreError("cannot open the private executor journal") from error
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def initialize(
    path: Path, *, ledger: Path, max_concurrency: int = 1, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Create a new empty outbox explicitly, without replacing or migrating existing storage."""
    budget = store.Deadline(lock_timeout)
    store.require(
        type(max_concurrency) is int and 1 <= max_concurrency <= MAX_CONCURRENCY,
        "executor max_concurrency must be an integer from 1 to 16",
    )
    canonical_ledger = store.location(ledger)
    store.require(canonical_ledger != store.location(path), "executor journal and binding ledger must be separate")
    journal_id = uuid.uuid4().hex
    with store.locked(path, write=True, deadline=budget) as (absolute, parent_fd):
        _sidecars(absolute, recover=False)
        store.require(not os.path.lexists(absolute), "executor journal initialization requires a new destination")
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
            connection.executemany(
                "INSERT INTO executor_journal VALUES (?, ?)",
                (
                    ("journal_id", journal_id),
                    ("ledger", str(canonical_ledger)),
                    ("max_concurrency", str(max_concurrency)),
                ),
            )
            connection.commit()
            os.fsync(parent_fd)
            store.require(
                _structure(connection)
                == {"journal_id": journal_id, "ledger": str(canonical_ledger), "max_concurrency": max_concurrency},
                "executor journal identity changed during initialization",
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
        "operation": "initialize-executor-journal",
        "journal": str(absolute),
        "journal_id": journal_id,
        "ledger": str(canonical_ledger),
        "max_concurrency": max_concurrency,
        "path": str(absolute),
        "user_version": USER_VERSION,
    }


init = initialize

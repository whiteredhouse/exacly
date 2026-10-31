"""Explicit recoverable association of a native published handoff with its original goal.

This local operator API authorizes one binding replacement. It never admits evidence, changes
observed state, completes work, publishes remotely or resets an execution/authorization budget.
The association journal and native binding ledger remain separate durable resources.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exacly import (
    admission,
    completion_store,
    executions,
    handoff,
    handoff_store,
    operation_locks,
    planning,
    reconciliation,
    state_writer,
)
from exacly import execution_store as store
from exacly.applicability import Target

Doc = dict[str, Any]
VERSION = "0.1.0"
APPLICATION_ID = 0x45584F41  # EXOA: separate from native effect journals.
STORAGE_KEYS = {"journal", "journal_id", "ledger", "state", "handoff_journal", "handoff_journal_id"}
SCHEMA = {
    "identity": "CREATE TABLE identity (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "associations": (
        "CREATE TABLE associations (id TEXT PRIMARY KEY NOT NULL, intent TEXT NOT NULL, "
        "intent_digest TEXT NOT NULL, receipt TEXT, receipt_digest TEXT)"
    ),
}
CLAIMS = {"binding_associated": True, "evidence_admitted": False, "completion_authorized": False}


@dataclass(frozen=True)
class Request:
    storage: Mapping[str, Any]
    handoff_id: str
    handoff_receipt_digest: str
    binding_id: str
    documents: Mapping[str, Any]
    plan: Any
    action_id: str
    readiness_policy: Any
    trust_policy: Any
    date: dt.date


def _path(value: Any) -> Path:
    store.require(isinstance(value, str) and "\0" not in value, "association paths must be canonical absolute paths")
    path = Path(value)
    store.require(
        path.is_absolute() and str(path) == value and ".." not in path.parts and path.resolve() == path,
        "association paths must be canonical absolute paths",
    )
    return path


def _uuid(value: Any) -> None:
    try:
        identity = uuid.UUID(hex=value)
        store.require(identity.hex == value and identity.version == 4, "invalid initialized association identity")
    except (ValueError, TypeError, AttributeError):
        raise store.StoreError("invalid initialized association identity") from None


def _storage(value: Any) -> Doc:
    store.require(isinstance(value, Mapping) and set(value) == STORAGE_KEYS, "invalid association storage fields")
    result = dict(json.loads(store.canonical(dict(value))))
    for key in ("journal_id", "handoff_journal_id"):
        _uuid(result[key])
    paths = [_path(result[key]) for key in ("journal", "ledger", "state", "handoff_journal")]
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            store.require(
                path != other
                and path not in other.parents
                and other not in path.parents
                and all(
                    str(path) != str(other) + suffix and str(other) != str(path) + suffix
                    for suffix in ("-journal", "-wal", "-shm")
                ),
                "association resources must be separate",
            )
    for key in ("journal", "ledger", "handoff_journal"):
        store.require(store.location(Path(result[key])) == Path(result[key]), "association storage path changed")
    return result


def _json(value: str) -> Doc:
    parsed = json.loads(value, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    store.require(isinstance(parsed, dict) and store.canonical(parsed) == value, "invalid association journal JSON")
    return dict(parsed)


def _structure(connection: sqlite3.Connection) -> Doc:
    store.require(
        connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        and connection.execute("PRAGMA user_version").fetchone() == (1,)
        and connection.execute("PRAGMA journal_mode").fetchone() == ("delete",),
        "unrecognized association journal format",
    )
    found = connection.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in found} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "association journal schema changed",
    )
    rows = connection.execute("SELECT key, value FROM identity LIMIT 2").fetchall()
    store.require(len(rows) == 1 and rows[0][0] == "storage", "association journal identity is missing")
    return _storage(_json(rows[0][1]))


@contextmanager
def _opened(storage: Doc, *, write: bool, deadline: store.Deadline) -> Iterator[sqlite3.Connection]:
    """Caller holds the journal's parent lock in the global directory order."""
    path = Path(storage["journal"])
    store.auxiliary_files(path)  # Never silently recover an interrupted SQLite transaction.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    connection: sqlite3.Connection | None = None
    try:
        before = store.file_info(descriptor)
        header = os.pread(descriptor, 100, 0)
        store.require(
            len(header) == 100
            and header[:16] == b"SQLite format 3\0"
            and header[18:20] == b"\x01\x01"
            and int.from_bytes(header[68:72], "big") == APPLICATION_ID,
            "unrecognized association journal header",
        )
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=deadline.remaining(),
            isolation_level=None,
        )
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, store.RECORD_LIMIT)
        store.busy_timeout(connection, deadline)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        store.require(_structure(connection) == storage, "association journal instance or canonical stores changed")
        current = path.lstat()
        store.require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "association journal changed")
        yield connection
        current = path.lstat()
        store.require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "association journal changed")
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError("association journal lock timed out") from None
        raise store.StoreError("association journal database operation failed") from error
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        os.close(descriptor)


def initialize(
    journal: Path,
    *,
    ledger: Path,
    state_path: Path,
    handoff_journal: Path,
    handoff_journal_id: str,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Explicitly initialize private storage pinned to the selected native resources."""
    storage = _storage(
        {
            "journal": str(journal),
            "journal_id": uuid.uuid4().hex,
            "ledger": str(ledger),
            "state": str(state_path),
            "handoff_journal": str(handoff_journal),
            "handoff_journal_id": handoff_journal_id,
        }
    )
    deadline = store.Deadline(lock_timeout)
    with store.locked(journal, write=True, deadline=deadline) as (path, parent_fd):
        store.auxiliary_files(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        connection: sqlite3.Connection | None = None
        try:
            store.file_info(descriptor)
            connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO identity VALUES ('storage', ?)", (store.canonical(storage),))
            connection.commit()
            os.fsync(parent_fd)
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            os.close(descriptor)
    return storage


def _request(request: Request) -> Doc:
    storage = _storage(request.storage)
    store.require(type(request.date) is dt.date, "association requires an explicit date")
    for value in (request.handoff_id, request.handoff_receipt_digest, request.binding_id, request.action_id):
        store.require(
            isinstance(value, str) and store.DIGEST.fullmatch(value), "invalid association correlation identity"
        )
    plan, action = reconciliation.saved_action(request.plan, request.action_id, request.date)
    chosen = admission.validate_policy(request.trust_policy)
    reconciliation.approved_model(dict(request.documents), plan, action, plan["target"], request.date)
    for key in ("journal", "ledger", "handoff_journal"):
        executions.location(Path(storage[key]), chosen)
    return dict(
        json.loads(
            store.canonical(
                {
                    "schema_version": VERSION,
                    "storage": storage,
                    "handoff_id": request.handoff_id,
                    "handoff_receipt_digest": request.handoff_receipt_digest,
                    "binding_id": request.binding_id,
                    "documents": dict(request.documents),
                    "plan": plan,
                    "action_id": request.action_id,
                    "readiness_policy": request.readiness_policy,
                    "trust_policy": chosen,
                    "date": request.date.isoformat(),
                }
            )
        )
    )


def _read(connection: sqlite3.Connection, identity: str) -> tuple[Doc, Doc | None] | None:
    row = connection.execute(
        "SELECT intent, intent_digest, receipt, receipt_digest FROM associations WHERE id=?", (identity,)
    ).fetchone()
    if row is None:
        return None
    intent = _json(row[0])
    store.require(
        planning.digest(intent) == row[1] and planning.digest(intent["request"]) == identity,
        "association intent identity changed",
    )
    replacement = executions.validate_record(intent["replacement"])
    store.require(replacement["supersedes"] == intent["request"]["binding_id"], "association replacement changed")
    receipt = None if row[2] is None else _json(row[2])
    store.require(
        (receipt is None and row[3] is None)
        or (receipt is not None and receipt == _receipt(identity, intent) and planning.digest(receipt) == row[3]),
        "association receipt changed",
    )
    return intent, receipt


def _receipt(identity: str, intent: Doc) -> Doc:
    value = {
        "schema_version": VERSION,
        "association_id": identity,
        "handoff_id": intent["request"]["handoff_id"],
        "handoff_receipt_digest": intent["request"]["handoff_receipt_digest"],
        "original_binding": intent["request"]["binding_id"],
        "binding": intent["replacement"]["id"],
        "pending_key": intent["replacement"]["pending_key"],
        "target": intent["replacement"]["contract"]["target"],
        "storage": intent["request"]["storage"],
        "claims": CLAIMS,
    }
    value["digest"] = planning.digest(value)
    return value


def _result(identity: str, status: str, reason: str, receipt: Doc | None = None) -> Doc:
    return {
        "schema_version": VERSION,
        "association_id": identity,
        "status": status,
        "reason": reason,
        "receipt": receipt,
        "claims": {**CLAIMS, "binding_associated": status == "associated"},
    }


def _ledger(storage: Doc, deadline: store.Deadline) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
    with store.database(Path(storage["ledger"]), create=False, deadline=deadline) as (connection, _):
        completion_store.require_v2(connection, deadline=deadline)
        return store.validate(connection, deadline=deadline)


def _envelope(snapshot: tuple[dict[str, Doc], dict[str, str], dict[str, Doc]], identity: str) -> Doc:
    records, active, watermarks = snapshot
    store.require(identity in records, "original native binding is unavailable")
    binding = executions.validate_record(records[identity])
    pending = binding["pending_key"]
    successors = [record["id"] for record in records.values() if record["supersedes"] == identity]
    return {
        "binding": binding,
        "active": active[pending] == identity,
        "superseded_by": successors[0] if successors else None,
        "state_watermark": watermarks[pending],
    }


def _native_handoff(request: Doc, deadline: store.Deadline) -> tuple[Doc, Doc]:
    storage = request["storage"]
    with handoff_store.opened(
        Path(storage["handoff_journal"]), write=False, deadline=deadline, already_locked=True
    ) as journal:
        store.require(journal.journal_id == storage["handoff_journal_id"], "native handoff journal instance changed")
        retained = handoff._checked(journal, request["handoff_id"])
        store.require(retained is not None and retained["phase"] == "published", "native handoff is not published")
        assert retained is not None
        receipt = retained["receipt"]
        store.require(receipt["digest"] == request["handoff_receipt_digest"], "native handoff receipt differs")
        source = retained["intent"]["contract"]["source"]["contract"]
        store.require(
            retained["intent"]["contract"]["request"]["policy"]["storage"]["journal"] == storage["handoff_journal"],
            "native handoff selects another canonical journal",
        )
    destination = Path(storage["journal"])
    protected = source["policy"]["storage"]
    for name in ("journal", "ledger", "state", "output_root"):
        other = Path(protected[name])
        store.require(
            destination != other
            and destination not in other.parents
            and other not in destination.parents
            and all(str(destination) != str(other) + suffix for suffix in ("-journal", "-wal", "-shm")),
            "association journal overlaps executor authority or artifact storage",
        )
    for other in (Path(receipt["repository"]["path"]), Path(source["policy"]["runtime"]["rootfs"])):
        store.require(
            destination != other and other not in destination.parents,
            "association journal overlaps candidate runtime or Git storage",
        )
    return dict(receipt), dict(source)


def _prepare(request: Doc, snapshot: state_writer.Snapshot, deadline: store.Deadline) -> Doc:
    storage = request["storage"]
    receipt, source = _native_handoff(request, deadline)
    native = _envelope(_ledger(storage, deadline), request["binding_id"])
    store.require(
        source["binding"] == native["binding"]
        and source["plan"] == request["plan"]["digest"]
        and source["action"]["id"] == request["action_id"]
        and source["policy"]["storage"]["ledger"] == storage["ledger"]
        and source["policy"]["storage"]["state"] == storage["state"],
        "native handoff provenance differs from the approved original binding or stores",
    )
    date = dt.date.fromisoformat(request["date"])
    matched, reason, _ = executions.match_envelope(
        Path(storage["ledger"]),
        native,
        plan_doc=request["plan"],
        action_id=request["action_id"],
        state_doc=snapshot.document,
        readiness_policy=request["readiness_policy"],
        trust_policy=request["trust_policy"],
        output_target=Target(source["target"]),
        date=date,
    )
    store.require(matched, reason)
    pending = native["binding"]["contract"]["pending"]
    replacement = executions.build_registration(
        executions.Registration(
            request["plan"],
            request["action_id"],
            snapshot.document,
            pending["task"],
            pending["opened_at"],
            request["readiness_policy"],
            request["trust_policy"],
            Target(receipt["target"]),
            date,
            native["binding"]["contract"]["adopted_verification"],
            request["binding_id"],
        )
    )
    store.require(
        replacement["pending_key"] == native["binding"]["pending_key"], "replacement changed pending identity"
    )
    return {"request": request, "before_state_digest": snapshot.digest, "replacement": replacement}


def _publish(
    connection: sqlite3.Connection,
    identity: str,
    intent: Doc,
    snapshot: state_writer.Snapshot,
    pinned: dict[Path, int],
    deadline: store.Deadline,
) -> Doc:
    request, replacement = intent["request"], intent["replacement"]
    storage = request["storage"]
    native = _ledger(storage, deadline)
    records, _, _ = native
    if replacement["id"] in records:
        store.require(
            records[replacement["id"]] == replacement, "native replacement record differs from retained intent"
        )
    else:
        original = _envelope(native, request["binding_id"])
        if not original["active"] or snapshot.digest != intent["before_state_digest"]:
            return _result(
                identity, "conflict", "Current state or active binding changed; retain intent for explicit review."
            )
        receipt, source = _native_handoff(request, deadline)
        store.require(
            source["binding"] == original["binding"] and replacement["contract"]["target"] == receipt["target"],
            "retained association differs from native handoff provenance",
        )
        # FULL synchronizes SQLite data; retain directory durability before crossing stores.
        # Repeat on recovery because an earlier caller may have lost this synchronization result.
        os.fsync(pinned[Path(storage["journal"]).parent])
        store.put_locked(
            Path(storage["ledger"]), replacement, directory_fd=pinned[Path(storage["ledger"]).parent], deadline=deadline
        )
    receipt = _receipt(identity, intent)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE associations SET receipt=?, receipt_digest=? WHERE id=? AND receipt IS NULL",
        (store.canonical(receipt), planning.digest(receipt), identity),
    )
    connection.commit()
    os.fsync(pinned[Path(storage["journal"]).parent])
    return _result(
        identity, "associated", "Exact native binding replacement is retained; evidence remains pending.", receipt
    )


def apply(request: Request, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Explicitly authorize only this exact native output association, with current-state checks."""
    selected = _request(request)
    storage = selected["storage"]
    identity = planning.digest(selected)
    deadline = store.Deadline(lock_timeout)
    snapshot = state_writer.read(Path(storage["state"]))
    store.require(snapshot.content is not None, "association requires existing current state")
    directories = [Path(storage[key]).parent for key in ("journal", "ledger", "state", "handoff_journal")]
    with operation_locks.directories(directories, deadline=deadline) as pinned:
        current = state_writer.read_locked(snapshot, pinned[snapshot.path.parent])
        with _opened(storage, write=True, deadline=deadline) as connection:
            prior = _read(connection, identity)
            if prior is not None:
                intent, receipt = prior
                if receipt is not None:
                    os.fsync(pinned[Path(storage["journal"]).parent])
                    return _result(
                        identity, "associated", "Historical exact association receipt was retained.", receipt
                    )
                return _publish(connection, identity, intent, current, pinned, deadline)
            intent = _prepare(selected, current, deadline)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO associations VALUES (?, ?, ?, NULL, NULL)",
                (identity, store.canonical(intent), planning.digest(intent)),
            )
            connection.commit()  # Intent, including registration timestamp, precedes the ledger effect.
            return _publish(connection, identity, intent, current, pinned, deadline)


def recover(
    storage: Mapping[str, Any], association_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Reconcile the retained exact binding before any new proposal; never rebuild registration."""
    selected = _storage(storage)
    store.require(
        isinstance(association_id, str) and store.DIGEST.fullmatch(association_id), "invalid association identity"
    )
    deadline = store.Deadline(lock_timeout)
    snapshot = state_writer.read(Path(selected["state"]))
    store.require(snapshot.content is not None, "association recovery requires existing current state")
    directories = [Path(selected[key]).parent for key in ("journal", "ledger", "state", "handoff_journal")]
    with operation_locks.directories(directories, deadline=deadline) as pinned:
        current = state_writer.read_locked(snapshot, pinned[snapshot.path.parent])
        with _opened(selected, write=True, deadline=deadline) as connection:
            retained = _read(connection, association_id)
            store.require(retained is not None, "association intent is unavailable")
            assert retained is not None
            intent, receipt = retained
            if receipt is not None:
                os.fsync(pinned[Path(selected["journal"]).parent])
                return _result(
                    association_id, "associated", "Historical exact association receipt was retained.", receipt
                )
            return _publish(connection, association_id, intent, current, pinned, deadline)


def identity(request: Request) -> str:
    """Identify this exact proposed association without approving or writing it."""
    return planning.digest(_request(request))


def show(storage: Mapping[str, Any], association_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Inspect durable intent/receipt without changing native bindings or state."""
    selected = _storage(storage)
    store.require(
        isinstance(association_id, str) and store.DIGEST.fullmatch(association_id), "invalid association identity"
    )
    deadline = store.Deadline(lock_timeout)
    with (
        store.locked(Path(selected["journal"]), write=False, deadline=deadline),
        _opened(selected, write=False, deadline=deadline) as connection,
    ):
        retained = _read(connection, association_id)
        if retained is None:
            return _result(association_id, "missing", "No association intent is retained.")
        intent, receipt = retained
        report = _result(
            association_id,
            "associated" if receipt else "prepared",
            "Inspect the native binding before recovering this retained association.",
            receipt,
        )
        report["intent"] = intent
        return report

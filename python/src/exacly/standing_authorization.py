"""Private objective-wide accounting for a trusted standing authorizer.

This foundation records a trusted caller's decisions; it does not approve a delegation, evaluate its
scope, check live preconditions, or grant permission to invoke a native effect. A retained decision is
not permission to retry an effect. The eventual authorizer must independently do those things before
using this accounting, and must reconcile native outcomes before retrying uncertain operations.

The caller pins a store UUID and a stable original-goal digest in trusted operator configuration.
Another policy, action, revision, or workflow alias cannot reset that objective's immutable limits.
Changing the trusted store pin requires an independently reviewed migration, which is not supported
here. Like the existing local journals, this protects cooperating processes of one POSIX user, not
against that user rewriting private storage. Reservations are never refunded, including cancellation.
"""

from __future__ import annotations

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
APPLICATION_ID = 0x45585341  # EXSA; not an execution ledger or component journal.
USER_VERSION = 1
DIMENSIONS = ("attempts", "wall_time_ms", "model_requests", "model_spend_microunits")
MAX_UNITS = 2**63 - 1
SCHEMA = {
    "authority_store": "CREATE TABLE authority_store (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "objectives": (
        "CREATE TABLE objectives (id TEXT PRIMARY KEY NOT NULL, original_goal TEXT UNIQUE NOT NULL, "
        "document_json TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
    "decisions": (
        "CREATE TABLE decisions (id TEXT PRIMARY KEY NOT NULL, objective_id TEXT NOT NULL REFERENCES objectives(id), "
        "document_json TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
    "uncertainties": (
        "CREATE TABLE uncertainties (id TEXT PRIMARY KEY NOT NULL REFERENCES decisions(id), "
        "document_json TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
    "outcomes": (
        "CREATE TABLE outcomes (id TEXT PRIMARY KEY NOT NULL REFERENCES decisions(id), "
        "document_json TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
}


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(store.canonical(value).encode()).hexdigest()


def _copy(value: Any) -> Doc:
    store.require(isinstance(value, dict), "accounting documents must be JSON objects")
    result: Doc = json.loads(store.canonical(value))
    return result


def _decoded(value: Any) -> Doc:
    store.require(isinstance(value, str), "invalid accounting JSON")
    try:
        result = json.loads(value, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(isinstance(result, dict) and store.canonical(result) == value, "noncanonical accounting JSON")
        return dict(result)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise store.StoreError("invalid accounting JSON") from None


def _identity(value: Any, label: str) -> str:
    store.require(isinstance(value, str) and store.DIGEST.fullmatch(value), f"invalid {label} digest")
    return str(value)


def _uuid(value: Any) -> str:
    store.require(isinstance(value, str), "invalid accounting store identity")
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError:
        raise store.StoreError("invalid accounting store identity") from None
    store.require(parsed.version == 4 and parsed.hex == value, "invalid accounting store identity")
    return str(value)


def _text(value: Any) -> str:
    store.require(isinstance(value, str) and bool(value.strip()), "accounting reason requires nonempty text")
    return str(value)


def _units(value: Any) -> Doc:
    value = _copy(value)
    store.require(set(value) == set(DIMENSIONS), "accounting units require exactly the supported dimensions")
    store.require(
        all(type(item) is int and 0 <= item <= MAX_UNITS for item in value.values()),
        "accounting units must be finite nonnegative bounded integers",
    )
    return dict(value)


def _objective(value: Any, limits: Any) -> Doc:
    value = _copy(value)
    store.require(set(value) == {"schema_version", "id", "original_goal"}, "invalid objective fields")
    store.require(value["schema_version"] == VERSION, "unsupported objective version")
    _identity(value["id"], "objective")
    _identity(value["original_goal"], "original goal")
    return {"objective": value, "limits": _units(limits)}


def _proposal(value: Any) -> Doc:
    value = _copy(value)
    store.require(
        set(value)
        == {
            "schema_version",
            "id",
            "delegation_id",
            "policy_digest",
            "effect",
            "preconditions",
            "authorization",
            "cost",
        },
        "invalid effect proposal fields",
    )
    store.require(value["schema_version"] == VERSION, "unsupported effect proposal version")
    _identity(value["id"], "proposal")
    _identity(value["policy_digest"], "policy")
    store.require(
        isinstance(value["delegation_id"], str) and bool(value["delegation_id"].strip()), "invalid delegation identity"
    )
    store.require(
        all(isinstance(value[key], dict) for key in ("effect", "preconditions", "authorization")),
        "effect, preconditions and exact authorization must be JSON objects",
    )
    _units(value["cost"])
    return dict(value)


def _outcome(value: Any) -> Doc:
    value = _copy(value)
    store.require(set(value) == {"status", "receipt"}, "invalid native outcome fields")
    store.require(value["status"] in ("succeeded", "failed", "cancelled"), "unknown native outcome status")
    store.require(isinstance(value["receipt"], dict) and bool(value["receipt"]), "native outcome requires a receipt")
    return dict(value)


def _header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    store.require(
        len(content) == 100 and content[:16] == b"SQLite format 3\0" and content[18:20] == b"\x01\x01",
        "accounting store is not a DELETE-mode SQLite database",
    )
    store.require(int.from_bytes(content[68:72], "big") == APPLICATION_ID, "unrecognized accounting application")
    store.require(int.from_bytes(content[60:64], "big") == USER_VERSION, "unsupported accounting schema version")


def _structure(connection: sqlite3.Connection) -> str:
    objects = connection.execute("SELECT name, type, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
    store.require(
        {name: (kind, sql) for name, kind, sql in objects} == {name: ("table", sql) for name, sql in SCHEMA.items()},
        "accounting store schema differs from the installed format",
    )
    store.require(connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "unsupported accounting journal")
    store.require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "accounting integrity check failed")
    store.require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "invalid accounting relationships")
    metadata = connection.execute("SELECT key, value FROM authority_store").fetchall()
    store.require(len(metadata) == 1 and metadata[0][0] == "store_id", "missing or ambiguous accounting identity")
    return _uuid(metadata[0][1])


def _snapshot(connection: sqlite3.Connection, store_id: str) -> tuple[dict[str, Doc], dict[str, Doc]]:
    """Validate all retained rows before releasing a decision; counters are derived from immutable costs."""
    objectives: dict[str, Doc] = {}
    for identity, original_goal, document, digest in connection.execute(
        "SELECT id, original_goal, document_json, digest FROM objectives"
    ):
        value = _decoded(document)
        store.require(set(value) == {"objective", "limits"}, "invalid retained objective fields")
        value = _objective(value["objective"], value["limits"])
        store.require(_digest(value) == digest, "retained objective content changed")
        store.require(
            (value["objective"]["id"], value["objective"]["original_goal"]) == (identity, original_goal),
            "retained objective identity changed",
        )
        objectives[identity] = value
    decisions: dict[str, Doc] = {}
    totals = {identity: dict.fromkeys(DIMENSIONS, 0) for identity in objectives}
    for identity, objective_id, document, digest in connection.execute(
        "SELECT id, objective_id, document_json, digest FROM decisions"
    ):
        value = _decoded(document)
        store.require(
            set(value) == {"schema_version", "store_id", "objective_id", "request", "approved", "reason"}
            and value["schema_version"] == VERSION
            and value["store_id"] == store_id
            and value["objective_id"] == objective_id
            and objective_id in objectives
            and _digest(value) == digest,
            "invalid retained accounting decision",
        )
        request = value["request"]
        store.require(
            isinstance(request, dict) and set(request) == {"proposal", "approved", "reason"},
            "invalid retained decision request",
        )
        proposal = _proposal(request["proposal"])
        store.require(proposal["id"] == identity, "retained proposal identity changed")
        store.require(
            type(request["approved"]) is bool and type(value["approved"]) is bool, "invalid retained decision"
        )
        _text(request["reason"])
        _text(value["reason"])
        store.require(not value["approved"] or request["approved"], "denied request became an approved decision")
        if value["approved"]:
            for dimension in DIMENSIONS:
                totals[objective_id][dimension] += proposal["cost"][dimension]
        decisions[identity] = {**value, "digest": digest, "status": "reserved" if value["approved"] else "denied"}
    for identity, document, digest in connection.execute("SELECT id, document_json, digest FROM uncertainties"):
        value = _decoded(document)
        store.require(set(value) == {"reason"} and _digest(value) == digest, "invalid uncertainty fields or digest")
        _text(value["reason"])
        store.require(identity in decisions and decisions[identity]["approved"], "invalid uncertain decision")
        decisions[identity].update(status="uncertain", uncertainty=value)
    for identity, document, digest in connection.execute("SELECT id, document_json, digest FROM outcomes"):
        value = _outcome(_decoded(document))
        store.require(_digest(value) == digest, "retained outcome content changed")
        store.require(identity in decisions and decisions[identity]["approved"], "invalid settled decision")
        decisions[identity].update(status="settled", outcome=value)
    for identity, objective in objectives.items():
        store.require(
            all(totals[identity][key] <= objective["limits"][key] for key in DIMENSIONS),
            "retained reservations exceed objective limits",
        )
        objective["reserved"] = totals[identity]
    return objectives, decisions


@contextmanager
def _opened(path: Path, store_id: str, *, write: bool, lock_timeout: float) -> Iterator[tuple[sqlite3.Connection, int]]:
    _uuid(store_id)
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=write, deadline=deadline) as (absolute, parent_fd):
        descriptor: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            store.auxiliary_files(absolute)  # Never trigger implicit SQLite rollback or WAL recovery.
            descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = store.file_info(descriptor)
            _header(descriptor)
            connection = sqlite3.connect(
                absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                timeout=deadline.remaining(),
                isolation_level=None,
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, store.RECORD_LIMIT + 64 * 1024)
            store.busy_timeout(connection, deadline)
            for statement in ("PRAGMA trusted_schema=OFF", "PRAGMA foreign_keys=ON", "PRAGMA synchronous=EXTRA"):
                connection.execute(statement)
            if not write:
                connection.execute("PRAGMA query_only=ON")
            store.require(_structure(connection) == store_id, "accounting store differs from trusted pinned identity")
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "accounting store changed"
            )
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection, parent_fd
            if write:
                connection.commit()
                os.fsync(parent_fd)
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "accounting store changed"
            )
        except sqlite3.Error as error:
            if store.sqlite_lock_error(error):
                raise store.LockTimeoutError("accounting store lock timed out") from None
            raise store.StoreError("accounting database operation failed") from error
        except (OSError, UnicodeError, RecursionError) as error:
            raise store.StoreError("cannot open the private accounting store") from error
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            if descriptor is not None:
                os.close(descriptor)


def initialize(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Explicitly create a new private store; retain its UUID in trusted operator configuration."""
    store_id = uuid.uuid4().hex
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=True, deadline=deadline) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        store.require(not os.path.lexists(absolute), "accounting initialization requires a new destination")
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        before = store.file_info(descriptor)
        connection: sqlite3.Connection | None = None
        complete = False
        try:
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO authority_store VALUES ('store_id', ?)", (store_id,))
            connection.commit()
            os.fsync(parent_fd)
            store.require(_structure(connection) == store_id, "accounting initialization identity changed")
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
    return {"schema_version": VERSION, "status": "initialized", "store_id": store_id, "path": str(absolute)}


def enroll(
    path: Path,
    *,
    store_id: str,
    objective: Any,
    limits: Any,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Bind immutable limits to the stable logical goal; another alias cannot enroll that goal again."""
    requested = _objective(objective, limits)
    identity = requested["objective"]["id"]
    with _opened(path, store_id, write=True, lock_timeout=lock_timeout) as (connection, _):
        objectives, _ = _snapshot(connection, store_id)
        existing = objectives.get(identity)
        if existing is not None:
            store.require(
                {key: existing[key] for key in ("objective", "limits")} == requested,
                "objective enrollment conflicts with immutable goal or limits",
            )
            return existing
        store.require(
            all(
                item["objective"]["original_goal"] != requested["objective"]["original_goal"]
                for item in objectives.values()
            ),
            "original goal is already enrolled under another objective identity",
        )
        connection.execute(
            "INSERT INTO objectives VALUES (?, ?, ?, ?)",
            (identity, requested["objective"]["original_goal"], store.canonical(requested), _digest(requested)),
        )
        return {**requested, "reserved": dict.fromkeys(DIMENSIONS, 0)}


def reserve(
    path: Path,
    *,
    store_id: str,
    objective_id: str,
    proposal: Any,
    approved: bool,
    reason: str,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Atomically retain a trusted decision and reserve its cost before returning it.

    ``approved`` is the trusted authorizer's input, never a candidate approval. This API does not
    evaluate standing scope or authorize native effects. Replaying an existing decision returns its
    current retained status and does not authorize another invocation or retry.
    """
    _identity(objective_id, "objective")
    proposal = _proposal(proposal)
    store.require(type(approved) is bool, "accounting approval must be an explicit boolean")
    _text(reason)
    request = {"proposal": proposal, "approved": approved, "reason": reason}
    with _opened(path, store_id, write=True, lock_timeout=lock_timeout) as (connection, _):
        objectives, decisions = _snapshot(connection, store_id)
        store.require(objective_id in objectives, "objective must be explicitly enrolled before decisions")
        existing = decisions.get(proposal["id"])
        if existing is not None:
            store.require(
                existing["objective_id"] == objective_id and existing["request"] == request,
                "proposal identity conflicts with another retained decision",
            )
            return existing
        objective = objectives[objective_id]
        exhausted = [
            key for key in DIMENSIONS if objective["reserved"][key] + proposal["cost"][key] > objective["limits"][key]
        ]
        value = {
            "schema_version": VERSION,
            "store_id": store_id,
            "objective_id": objective_id,
            "request": request,
            "approved": approved and not exhausted,
            "reason": "objective budget exhausted: " + ", ".join(exhausted) if approved and exhausted else reason,
        }
        digest = _digest(value)
        connection.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?)",
            (proposal["id"], objective_id, store.canonical(value), digest),
        )
        return {**value, "digest": digest, "status": "reserved" if value["approved"] else "denied"}


def inspect(path: Path, *, store_id: str, objective_id: str, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Read validated history and total reservations, including uncertain and terminal effects."""
    _identity(objective_id, "objective")
    with _opened(path, store_id, write=False, lock_timeout=lock_timeout) as (connection, _):
        objectives, decisions = _snapshot(connection, store_id)
        store.require(objective_id in objectives, "unknown accounting objective")
        return {
            "schema_version": VERSION,
            "store_id": store_id,
            **objectives[objective_id],
            "decisions": [value for value in decisions.values() if value["objective_id"] == objective_id],
        }


def mark_uncertain(
    path: Path,
    *,
    store_id: str,
    objective_id: str,
    proposal_id: str,
    reason: str,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Retain an ambiguous native outcome without freeing capacity or permitting another effect."""
    return _record_outcome(
        path,
        store_id=store_id,
        objective_id=objective_id,
        proposal_id=proposal_id,
        document={"reason": _text(reason)},
        terminal=False,
        lock_timeout=lock_timeout,
    )


def settle(
    path: Path,
    *,
    store_id: str,
    objective_id: str,
    proposal_id: str,
    outcome: Any,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Retain an established native terminal receipt; consumed reservations are never refunded."""
    return _record_outcome(
        path,
        store_id=store_id,
        objective_id=objective_id,
        proposal_id=proposal_id,
        document=_outcome(outcome),
        terminal=True,
        lock_timeout=lock_timeout,
    )


def _record_outcome(
    path: Path,
    *,
    store_id: str,
    objective_id: str,
    proposal_id: str,
    document: Doc,
    terminal: bool,
    lock_timeout: float,
) -> Doc:
    _identity(objective_id, "objective")
    _identity(proposal_id, "proposal")
    with _opened(path, store_id, write=True, lock_timeout=lock_timeout) as (connection, _):
        _, decisions = _snapshot(connection, store_id)
        value = decisions.get(proposal_id)
        store.require(
            value is not None and value["objective_id"] == objective_id and value["approved"],
            "outcome requires an approved retained decision for the exact objective",
        )
        assert value is not None
        key = "outcome" if terminal else "uncertainty"
        if key in value:
            store.require(value[key] == document, "native outcome conflicts with retained history")
            return dict(value)
        store.require(value["status"] != "settled", "a settled decision cannot become uncertain")
        table = "outcomes" if terminal else "uncertainties"
        connection.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?)", (proposal_id, store.canonical(document), _digest(document))
        )
        return {**value, "status": "settled" if terminal else "uncertain", key: document}

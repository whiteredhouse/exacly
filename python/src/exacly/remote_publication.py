"""Durable exact-ref publication through a separately selected trusted transport.

This is coordination infrastructure, not a concrete GitHub transport or CI selector. The operator
selects the transport and reviews each exact authorization. Candidate code cannot choose either.
An uncertain send is discover-only: even observing the old ref never permits a second send.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from exacly import execution_store as store
from exacly import handoff, handoff_git, operation_locks, planning, publication_key

Doc = dict[str, Any]
VERSION = "0.1.0"
APPLICATION_ID = 0x45585250  # EXRP
SCHEMA = {
    "metadata": "CREATE TABLE metadata (id TEXT PRIMARY KEY NOT NULL)",
    "operations": (
        "CREATE TABLE operations (id TEXT PRIMARY KEY NOT NULL, document TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
}
MAX_EVENTS = 128
CLAIMS = {"evidence_admitted": False, "completion_authorized": False, "merge_authorized": False}


class PublicationRejected(store.StoreError):
    """The trusted transport authenticated an explicit atomic server rejection, not a lost reply."""


class _Connection(sqlite3.Connection):
    """A checked database connection borrowing its still-locked parent directory descriptor."""

    parent_fd: int


class RemoteTransport(Protocol):
    """A trusted, bounded adapter; inspection must prove objects and operation correlation.

    publish must atomically compare the exact expected ref, transfer only the new handoff objects and
    set the requested ref. The approved base must already exist remotely: importing base history is
    excluded and needs a separate explicit contract. Neither method may select a different repository
    or load candidate config.
    A supported concrete implementation and live tests are separate work.
    """

    @property
    def adapter(self) -> str: ...

    def inspect(self, operation: Doc, *, timeout: float) -> Doc:
        """Return repository_id, ref, revision, commit_present, and operation_id (or None)."""
        ...

    def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
        """Perform one checked CAS; retain/discover operation['id'] across ambiguous outcomes."""
        ...


@dataclass(frozen=True)
class Request:
    journal: Path
    journal_id: str
    handoff_journal: Path
    handoff_journal_id: str
    handoff_id: str
    repository_id: str
    adapter: str
    ref: str
    expected_revision: str | None
    operation_marker_ref: str | None = None
    operation_key: Path | None = None
    operation_key_id: str | None = None


def _uuid(value: Any) -> None:
    try:
        parsed = uuid.UUID(hex=value)
        store.require(parsed.hex == value and parsed.version == 4, "invalid remote journal identity")
    except (ValueError, TypeError, AttributeError):
        raise store.StoreError("invalid remote journal identity") from None


def _request(request: Request) -> Doc:
    store.require(isinstance(request, Request), "an explicit remote publication Request is required")
    for path in (request.journal, request.handoff_journal):
        store.require(
            isinstance(path, Path) and path.is_absolute() and path == path.absolute() and ".." not in path.parts,
            "remote publication requires canonical absolute journal paths",
        )
    store.require(
        request.journal.parent != request.handoff_journal.parent, "remote and handoff storage must be separate"
    )
    _uuid(request.journal_id)
    _uuid(request.handoff_journal_id)
    store.require(
        isinstance(request.handoff_id, str) and store.DIGEST.fullmatch(request.handoff_id), "invalid handoff identity"
    )
    store.require(
        isinstance(request.repository_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", request.repository_id),
        "an immutable numeric remote repository identity is required",
    )
    store.require(
        isinstance(request.adapter, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", request.adapter),
        "invalid trusted remote adapter identity",
    )
    handoff_git.reference(request.ref)
    store.require(
        request.expected_revision is None
        or (isinstance(request.expected_revision, str) and re.fullmatch(r"[0-9a-f]{40}", request.expected_revision)),
        "remote publication requires an exact expected revision or absent ref",
    )
    document = {
        "schema_version": VERSION,
        "journal": str(request.journal),
        "journal_id": request.journal_id,
        "handoff_journal": str(request.handoff_journal),
        "handoff_journal_id": request.handoff_journal_id,
        "handoff_id": request.handoff_id,
        "repository_id": request.repository_id,
        "adapter": request.adapter,
        "ref": request.ref,
        "expected_revision": request.expected_revision,
    }
    if request.operation_marker_ref is not None:
        marker = request.operation_marker_ref
        store.require(
            isinstance(marker, str)
            and marker.startswith(("refs/tags/exacly-operations/", "refs/tags/req039-operations/")),
            "operation markers require an explicitly authorized operation-tag namespace",
        )
        handoff_git.reference("refs/heads/" + marker.removeprefix("refs/tags/"))
        key = request.operation_key
        store.require(
            isinstance(key, Path)
            and key.is_absolute()
            and ".." not in key.parts
            and key.parent not in (request.journal.parent, request.handoff_journal.parent),
            "operation signing key requires its own canonical protected storage directory",
        )
        store.require(
            isinstance(request.operation_key_id, str) and store.DIGEST.fullmatch(request.operation_key_id),
            "operation signing key requires its exact initialized identity",
        )
        document["operation_marker_ref"] = marker
        document["operation_key"] = str(key)
        document["operation_key_id"] = request.operation_key_id
    else:
        store.require(
            request.operation_key is None and request.operation_key_id is None,
            "operation key scope requires an explicit marker effect",
        )
    return document


def request_document(request: Request) -> Doc:
    """Return the closed exact request contract, preserving pre-marker historical identities."""
    return _request(request)


def request_from_document(value: Any) -> Request:
    store.require(isinstance(value, dict), "invalid remote request document")
    try:
        request = Request(
            Path(value["journal"]),
            value["journal_id"],
            Path(value["handoff_journal"]),
            value["handoff_journal_id"],
            value["handoff_id"],
            value["repository_id"],
            value["adapter"],
            value["ref"],
            value["expected_revision"],
            value.get("operation_marker_ref"),
            Path(value["operation_key"]) if "operation_key" in value else None,
            value.get("operation_key_id"),
        )
        store.require(_request(request) == value, "remote request has unsupported fields or values")
        return request
    except (KeyError, TypeError, AttributeError) as error:
        raise store.StoreError("invalid remote request document") from error


def _decoded(value: str) -> Doc:
    store.require(
        isinstance(value, str) and len(value.encode()) <= store.RECORD_LIMIT, "remote record exceeds its limit"
    )
    result = json.loads(value, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
    store.require(isinstance(result, dict) and store.canonical(result) == value, "noncanonical remote journal record")
    return dict(result)


def _header(fd: int) -> None:
    content = os.pread(fd, 100, 0)
    store.require(
        len(content) == 100
        and content[:16] == b"SQLite format 3\0"
        and content[18:20] == b"\x01\x01"
        and int.from_bytes(content[68:72], "big") == APPLICATION_ID
        and int.from_bytes(content[60:64], "big") == 1,
        "unrecognized remote publication journal",
    )


@contextmanager
def _opened(
    path: Path, identity: str, *, write: bool, deadline: store.Deadline, parent_fd: int | None = None
) -> Iterator[_Connection]:
    _uuid(identity)
    locking = (
        nullcontext((store.location(path), parent_fd))
        if parent_fd is not None
        else store.locked(path, write=write, deadline=deadline)
    )
    with locking as (absolute, directory_fd):
        store.auxiliary_files(absolute)
        fd = os.open(absolute, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK)
        connection: _Connection | None = None
        try:
            original = store.file_info(fd)
            _header(fd)
            connection = _Connection(
                absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                isolation_level=None,
                timeout=deadline.remaining(),
            )
            connection.parent_fd = directory_fd
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, store.RECORD_LIMIT + 128 * 1024)
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino), "remote journal changed"
            )
            actual = connection.execute("SELECT name, type, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
            store.require(
                {name: (kind, sql) for name, kind, sql in actual}
                == {name: ("table", sql) for name, sql in SCHEMA.items()},
                "remote journal schema changed",
            )
            store.require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "corrupt remote journal")
            store.require(
                connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "remote journal mode changed"
            )
            store.require(
                connection.execute("SELECT id FROM metadata").fetchall() == [(identity,)],
                "remote journal instance changed",
            )
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            # A previous FULL commit may have succeeded before its directory sync failed. Even a
            # read-only historical replay must establish that barrier before exposing retained work.
            os.fsync(directory_fd)
            yield connection
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            os.close(fd)


def initialize(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Explicitly create a separate private journal; existing paths are never replaced."""
    identity = uuid.uuid4().hex
    with store.locked(path, write=True, lock_timeout=lock_timeout) as (absolute, directory_fd):
        store.auxiliary_files(absolute)
        fd = os.open(absolute, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("PRAGMA user_version=1")
                connection.execute("BEGIN IMMEDIATE")
                for sql in SCHEMA.values():
                    connection.execute(sql)
                connection.execute("INSERT INTO metadata VALUES (?)", (identity,))
                connection.commit()
            finally:
                connection.close()
            os.fsync(fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)
    return identity


def _load(connection: sqlite3.Connection, identity: str, *, deadline: store.Deadline) -> Doc | None:
    store.require(isinstance(identity, str) and store.DIGEST.fullmatch(identity), "invalid remote operation identity")
    row = connection.execute("SELECT document, digest FROM operations WHERE id=?", (identity,)).fetchone()
    if row is None:
        return None
    record = _decoded(row[0])
    store.require(planning.digest(record) == row[1], "remote journal record digest changed")
    store.require(set(record) == {"intent", "events"}, "invalid remote operation fields")
    intent = record["intent"]
    store.require(
        isinstance(intent, dict)
        and set(intent) == {"id", "request", "handoff", "authorization"}
        and intent["id"] == identity
        and intent["authorization"]["operation_id"] == identity,
        "remote operation identity changed",
    )
    request = intent["request"]
    reconstructed = request_from_document(request)
    store.require(_request(reconstructed) == request, "retained remote request changed")
    expected = _authorization(request, intent["handoff"], lock_timeout=deadline.remaining())
    store.require(intent["authorization"] == expected, "retained remote authorization changed")
    events = record["events"]
    store.require(isinstance(events, list) and 1 <= len(events) <= MAX_EVENTS, "invalid remote history size")
    previous = planning.digest(intent)
    phase = "prepared"
    sent = False
    rejected = False
    for index, event in enumerate(events):
        store.require(
            isinstance(event, dict)
            and set(event) == {"phase", "data", "previous", "digest"}
            and isinstance(event["data"], dict)
            and event["previous"] == previous
            and event["digest"] == planning.digest({key: value for key, value in event.items() if key != "digest"}),
            "remote history changed",
        )
        allowed = {
            "prepared": {"prepared", "sent", "conflict"},
            "sent": {"ambiguous", "published", "conflict"},
            "ambiguous": {"ambiguous", "published", "conflict"},
            "conflict": {"conflict", "published"},
            "published": {"published"},
        }
        store.require(
            event["phase"] in allowed[phase] and (index != 0 or event["phase"] == "prepared"),
            "invalid remote history transition",
        )
        if event["phase"] == "sent":
            observed = _observation(event["data"].get("precondition"), intent)
            store.require(observed["revision"] == request["expected_revision"], "retained send precondition changed")
            sent = True
        if event["data"].get("server_rejected") is True:
            store.require(event["phase"] == "conflict" and sent, "retained server rejection lacks a sent operation")
            rejected = True
        if event["phase"] == "published":
            observed = _observation(event["data"].get("observation"), intent)
            store.require(
                sent
                and not rejected
                and observed["revision"] == expected["commit"]
                and observed["commit_present"]
                and observed["operation_id"] == identity,
                "retained publication lacks its exact sent operation and object proof",
            )
        previous, phase = event["digest"], event["phase"]
    return record


def _save(connection: _Connection, record: Doc) -> None:
    value = store.canonical(record)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT OR REPLACE INTO operations VALUES (?, ?, ?)", (record["intent"]["id"], value, planning.digest(record))
    )
    connection.commit()
    # FULL synchronizes SQLite's pages and rollback journal; the unlink also needs the parent
    # directory durability barrier before another system may receive this operation's authority.
    os.fsync(connection.parent_fd)


def _event(connection: _Connection, record: Doc, phase: str, data: Doc) -> Doc:
    if record["events"] and record["events"][-1]["phase"] == phase and record["events"][-1]["data"] == data:
        return record
    store.require(len(record["events"]) < MAX_EVENTS, "remote history limit reached; explicit review is required")
    previous = record["events"][-1]["digest"] if record["events"] else planning.digest(record["intent"])
    event = {"phase": phase, "data": copy.deepcopy(data), "previous": previous}
    event["digest"] = planning.digest(event)
    record["events"].append(event)
    _save(connection, record)
    return record


def _authorization(request: Doc, receipt: Doc, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    store.require(
        isinstance(receipt, dict)
        and receipt.get("handoff_id") == request["handoff_id"]
        and receipt.get("digest") == planning.digest({key: value for key, value in receipt.items() if key != "digest"})
        and receipt.get("claims") == handoff.CLAIMS
        and receipt["authorization"]["journal"]
        == {"path": request["handoff_journal"], "instance_id": request["handoff_journal_id"]},
        "remote publication needs its exact native published handoff receipt",
    )
    store.require(re.fullmatch(r"[0-9a-f]{40}", receipt["commit"]), "invalid retained handoff commit")
    identity = {"schema_version": VERSION, "request": request, "handoff_receipt": receipt["digest"]}
    authorization = {
        "schema_version": VERSION,
        "operation_id": planning.digest(identity),
        "effect": "publish_remote_git_ref",
        "object_scope": "handoff-delta-only-existing-base",
        "base_revision": receipt["base_revision"],
        "identity": identity,
        "commit": receipt["commit"],
        "tree": receipt["tree"],
        "repository": copy.deepcopy(receipt["repository"]),
    }
    if "operation_marker_ref" in request:
        marker = request["operation_marker_ref"]
        payload = {
            "schema_version": VERSION,
            "operation_id": authorization["operation_id"],
            "repository_id": request["repository_id"],
            "ref": request["ref"],
            "expected_revision": request["expected_revision"],
            "commit": receipt["commit"],
            "base_revision": receipt["base_revision"],
            "handoff_receipt": receipt["digest"],
            "key_id": request["operation_key_id"],
        }
        key = Path(request["operation_key"])
        repository = Path(receipt["repository"]["path"])
        store.require(
            repository not in key.parents and key.parent != repository.parent,
            "operation signing key overlaps source Git storage or its lock domain",
        )
        payload["proof"] = publication_key.sign(payload, key, request["operation_key_id"], lock_timeout=lock_timeout)
        content = (
            f"object {receipt['commit']}\ntype commit\ntag {marker.removeprefix('refs/tags/')}\n"
            "tagger Exacly publication <publication@exacly.invalid> 0 +0000\n\n" + store.canonical(payload) + "\n"
        )
        raw = content.encode()
        authorization["operation_marker"] = {
            "effect": "create_operation_tag",
            "ref": marker,
            "expected_revision": None,
            "object": hashlib.sha1(b"tag " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
            "content": content,
        }
    return authorization


def validate_operation(value: Any, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Check the exact retained grant passed to a trusted transport; this establishes no approval."""
    store.require(
        isinstance(value, dict) and set(value) == {"id", "request", "handoff", "authorization"},
        "invalid remote operation",
    )
    request = request_document(request_from_document(value["request"]))
    expected = _authorization(request, value["handoff"], lock_timeout=lock_timeout)
    store.require(
        value["id"] == expected["operation_id"] and value["authorization"] == expected,
        "remote operation differs from its exact handoff and authorization",
    )
    return copy.deepcopy(value)


def _source(request: Doc, timeout: float) -> Doc:
    deadline = store.Deadline(timeout)
    report = handoff.show(Path(request["handoff_journal"]), request["handoff_id"], lock_timeout=deadline.remaining())
    store.require(report["status"] == "published", "remote publication needs a native published handoff")
    receipt: Doc = report["receipt"]
    _authorization(request, receipt, lock_timeout=deadline.remaining())
    return copy.deepcopy(receipt)


def _view(record: Doc) -> Doc:
    phase = record["events"][-1]["phase"]
    report = {
        "schema_version": VERSION,
        "operation_id": record["intent"]["id"],
        "status": phase,
        "intent": copy.deepcopy(record["intent"]),
        "history": copy.deepcopy(record["events"]),
        "claims": {**CLAIMS, "remote_published": phase == "published"},
    }
    report["digest"] = planning.digest(report)
    return report


def preview(request: Request, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Derive an exact proposal without authorizing a remote effect."""
    document = _request(request)
    deadline = store.Deadline(lock_timeout)
    with _opened(request.journal, request.journal_id, write=False, deadline=deadline):
        pass
    receipt = _source(document, deadline.remaining())
    authorization = _authorization(document, receipt, lock_timeout=deadline.remaining())
    return {
        "schema_version": VERSION,
        "status": "ready",
        "authorization_template": authorization,
        "claims": {**CLAIMS, "remote_published": False},
    }


def _timeout(value: float) -> float:
    store.require(
        type(value) in (int, float) and math.isfinite(value) and 0 < value <= 300,
        "remote transport timeout must be finite, positive and at most 300 seconds",
    )
    return float(value)


def _observation(value: Any, intent: Doc) -> Doc:
    request = intent["request"]
    store.require(
        isinstance(value, dict)
        and set(value) == {"repository_id", "ref", "revision", "commit_present", "operation_id"}
        and value["repository_id"] == request["repository_id"]
        and value["ref"] == request["ref"]
        and type(value["commit_present"]) is bool
        and (
            value["revision"] is None
            or (isinstance(value["revision"], str) and re.fullmatch(r"[0-9a-f]{40}", value["revision"]))
        )
        and (
            value["operation_id"] is None
            or (isinstance(value["operation_id"], str) and store.DIGEST.fullmatch(value["operation_id"]))
        ),
        "remote observation does not match the exact repository and ref contract",
    )
    return copy.deepcopy(value)


def _discover(connection: _Connection, record: Doc, transport: RemoteTransport, timeout: float) -> Doc:
    intent = record["intent"]
    if not any(event["phase"] == "sent" for event in record["events"]):
        return _view(record)
    if any(event["data"].get("server_rejected") is True for event in record["events"]):
        return _view(record)
    try:
        observed = _observation(transport.inspect(copy.deepcopy(intent), timeout=timeout), intent)
    except Exception:
        phase = "conflict" if record["events"][-1]["phase"] == "conflict" else "ambiguous"
        return _view(_event(connection, record, phase, {"reason": "Remote inspection unavailable or invalid."}))
    exact = (
        observed["revision"] == intent["authorization"]["commit"]
        and observed["commit_present"]
        and observed["operation_id"] == intent["id"]
    )
    if exact:
        return _view(_event(connection, record, "published", {"observation": observed}))
    conflict = record["events"][-1]["phase"] == "conflict" or observed["revision"] not in (
        intent["request"]["expected_revision"],
        intent["authorization"]["commit"],
    )
    return _view(_event(connection, record, "conflict" if conflict else "ambiguous", {"observation": observed}))


def apply(
    request: Request,
    *,
    authorization: Any,
    transport: RemoteTransport,
    timeout: float = 30,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Use an exact separately reviewed grant for at most one remote send."""
    document = _request(request)
    timeout = _timeout(timeout)
    store.require(transport.adapter == request.adapter, "trusted remote adapter identity changed")
    store.require(
        isinstance(authorization, dict) and isinstance(authorization.get("operation_id"), str),
        "separate exact remote authorization is required",
    )
    deadline = store.Deadline(lock_timeout)
    with _opened(request.journal, request.journal_id, write=False, deadline=deadline) as connection:
        retained = _load(connection, authorization["operation_id"], deadline=deadline)
        if retained is not None:
            store.require(
                retained["intent"]["request"] == document and retained["intent"]["authorization"] == authorization,
                "remote request differs from retained authority",
            )
            if retained["events"][-1]["phase"] != "prepared":
                return _view(retained)
    receipt = _source(document, deadline.remaining())
    store.require(
        authorization == _authorization(document, receipt, lock_timeout=deadline.remaining()),
        "remote authorization differs from current handoff",
    )
    repository = Path(receipt["repository"]["path"])
    store.require(repository not in request.journal.parents, "remote journal overlaps the handoff repository")
    with (
        operation_locks.directories(
            [request.journal.parent, repository.parent, repository], deadline=deadline
        ) as directories,
        _opened(
            request.journal,
            request.journal_id,
            write=True,
            deadline=deadline,
            parent_fd=directories[request.journal.parent],
        ) as connection,
    ):
        retained = _load(connection, authorization["operation_id"], deadline=deadline)
        if retained is not None:
            store.require(
                retained["intent"]["request"] == document and retained["intent"]["authorization"] == authorization,
                "remote request differs from retained authority",
            )
            if retained["events"][-1]["phase"] != "prepared":
                return _view(retained)
            record = retained
            intent = retained["intent"]
        else:
            intent = {
                "id": authorization["operation_id"],
                "request": document,
                "handoff": receipt,
                "authorization": copy.deepcopy(authorization),
            }
            record = _event(connection, {"intent": intent, "events": []}, "prepared", {})
        try:
            observed = _observation(transport.inspect(copy.deepcopy(intent), timeout=timeout), intent)
        except Exception:
            # Prepared means nothing was sent. Recovery is still discover-only and cannot invent a grant.
            return _view(record)
        if observed["revision"] != request.expected_revision:
            return _view(_event(connection, record, "conflict", {"observation": observed}))
        store.require(
            handoff_git.inspect_repository(repository, timeout=deadline.remaining()) == receipt["repository"],
            "local handoff repository changed before remote publication",
        )
        _event(connection, record, "sent", {"precondition": observed})
        try:
            transport.publish(copy.deepcopy(intent), repository, timeout=timeout)
        except PublicationRejected:
            return _view(
                _event(
                    connection,
                    record,
                    "conflict",
                    {
                        "reason": "The remote server explicitly rejected the atomic operation; retain the conflict.",
                        "server_rejected": True,
                    },
                )
            )
        except Exception:
            return _view(_event(connection, record, "ambiguous", {"reason": "Remote send outcome requires discovery."}))
        return _discover(connection, record, transport, timeout)


def lookup(
    journal: Path, journal_id: str, identity: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc | None:
    """Read an exact initialized journal; only a validated missing operation returns None.

    Missing/replaced storage, invalid identities, unsupported schemas and unavailable durability
    barriers remain errors. Callers must not interpret them as permission for a new effect.
    """
    deadline = store.Deadline(lock_timeout)
    with _opened(journal, journal_id, write=False, deadline=deadline) as connection:
        record = _load(connection, identity, deadline=deadline)
        return _view(record) if record is not None else None


def show(journal: Path, journal_id: str, identity: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    retained = lookup(journal, journal_id, identity, lock_timeout=lock_timeout)
    store.require(retained is not None, "remote operation is not retained")
    assert retained is not None
    return retained


def recover(
    journal: Path,
    journal_id: str,
    identity: str,
    *,
    transport: RemoteTransport,
    timeout: float = 30,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Discover an uncertain exact effect. No recovery path sends or retries publication."""
    timeout = _timeout(timeout)
    deadline = store.Deadline(lock_timeout)
    with _opened(journal, journal_id, write=True, deadline=deadline) as connection:
        record = _load(connection, identity, deadline=deadline)
        store.require(record is not None, "remote operation is not retained")
        assert record is not None
        store.require(transport.adapter == record["intent"]["request"]["adapter"], "trusted remote adapter changed")
        if record["events"][-1]["phase"] in ("published", "prepared"):
            return _view(record)
        return _discover(connection, record, transport, timeout)

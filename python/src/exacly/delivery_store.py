"""Private product-delivery intent and conservative budget journal for one local objective.

Storage does not authorize effects. The engine independently checks current approval and installed
native adapters. This journal is separate from verification history and never silently initializes,
repairs, migrates or resets an existing objective. The protected operator pins its path and UUID.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema

from exacly import delivery_automation as contracts
from exacly import delivery_runtime, executor_runtime, planning
from exacly import execution_store as store

Doc = dict[str, Any]
VERSION = "0.1.0"
PROFILE = "product-delivery-journal-v1"
APPLICATION_ID = 0x4558444C  # EXDL; never an execution/controller journal.
USER_VERSION = 1
MAX_OPERATIONS = 1000
MAX_CALLS = 10000
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PROVENANCE_BYTES = 256 * 1024
MAX_HISTORY_BYTES = 8 * 1024 * 1024
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_LOGICAL_BYTES = 16 * 1024 * 1024
PHASES = ("prepared", "may_have_started", "uncertain", "applied", "rejected")
METHODS = ("inspect", "invoke", "reconcile")
NATIVE_LIMITS = planning.fields(
    {
        "wall_time_ms": {"type": "integer", "minimum": 1, "maximum": 300000},
        "read_calls": {"type": "integer", "minimum": 0, "maximum": 10000},
        "write_calls": {"type": "integer", "minimum": 0, "maximum": 1024},
    }
)
SCHEMA = {
    "metadata": "CREATE TABLE metadata (document TEXT NOT NULL, digest TEXT NOT NULL)",
    "operations": (
        "CREATE TABLE operations (operation_key TEXT PRIMARY KEY NOT NULL, "
        "document TEXT NOT NULL, digest TEXT NOT NULL)"
    ),
    "revocations": "CREATE TABLE revocations (document TEXT NOT NULL, digest TEXT NOT NULL)",
}
INSPECTION = planning.fields({"ready": {"type": "boolean"}, "reason": contracts.TEXT, "provenance": {"type": "object"}})
RESULT = planning.fields(
    {
        "outcome": {"enum": ["applied", "rejected", "uncertain"]},
        "effect": {"type": ["object", "null"]},
        "reason": contracts.TEXT,
        "provenance": {"type": "object"},
    }
)
READ_RECOVERY_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": contracts.PROFILE},
        "kind": {"const": "delivery-read-recovery"},
        "issuer": contracts.TEXT,
        "approval_id": contracts.TEXT,
        "objective": contracts.OBJECTIVE,
        "journal": contracts.JOURNAL,
        "operation_key": contracts.DIGEST,
        "intent_id": contracts.DIGEST,
        "request_digest": contracts.DIGEST,
        "native_payload_digest": contracts.DIGEST,
        "old_configuration_digest": contracts.DIGEST,
        "new_configuration_digest": contracts.DIGEST,
        "operation_profile_digest": contracts.DIGEST,
        "policy_digest": contracts.DIGEST,
        "contract": contracts.CONTRACT,
        "limits": contracts.BUDGET,
        "read_limits": planning.fields({**NATIVE_LIMITS["properties"], "write_calls": {"type": "integer", "const": 0}}),
        "not_before": contracts.TEXT,
        "expires_at": contracts.TEXT,
    }
)


def copied(value: Any, *, limit: int = MAX_PAYLOAD_BYTES) -> Doc:
    """Copy bounded finite JSON; native payload strings may contain literal Git object newlines."""
    try:
        selected = planning.plain(value)
        store.require(isinstance(selected, dict), "delivery journal documents require objects")
        text = store.canonical(selected)
        store.require(len(text.encode()) <= limit, "delivery journal document exceeds its bound")
        return dict(json.loads(text))
    except (TypeError, UnicodeError, RecursionError) as error:
        raise store.StoreError("invalid delivery journal JSON") from error


def validate_native_limits(value: Any) -> Doc:
    selected = copied(value)
    try:
        jsonschema.validate(selected, NATIVE_LIMITS)
    except jsonschema.ValidationError as error:
        raise store.StoreError("invalid delivery native call limits") from error
    return selected


def _schema(value: Any, schema: Doc, *, limit: int = MAX_PAYLOAD_BYTES) -> Doc:
    selected = copied(value, limit=limit)
    try:
        jsonschema.validate(selected, schema)
    except jsonschema.ValidationError as error:
        raise store.StoreError("invalid retained delivery document") from error
    return selected


def _text(value: Any) -> str:
    store.require(isinstance(value, str) and 0 < len(value) <= 1024 and bool(value.strip()), "invalid delivery reason")
    store.require(not any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value), "invalid delivery reason")
    return str(value)


def timestamp(current: dt.datetime) -> str:
    store.require(type(current) is dt.datetime and current.utcoffset() is not None, "delivery time must be aware UTC")
    return current.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _time(value: Any) -> dt.datetime:
    store.require(isinstance(value, str), "invalid retained delivery timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        store.require(parsed.utcoffset() is not None, "invalid retained delivery timezone")
        return parsed.astimezone(dt.UTC)
    except (ValueError, OverflowError) as error:
        raise store.StoreError("invalid retained delivery timestamp") from error


def _uuid(value: Any) -> str:
    store.require(isinstance(value, str), "invalid delivery journal identity")
    try:
        parsed = uuid.UUID(hex=value)
        store.require(parsed.version == 4 and parsed.hex == value, "invalid delivery journal UUID")
    except (ValueError, AttributeError) as error:
        raise store.StoreError("invalid delivery journal UUID") from error
    return str(value)


def _decoded(text: Any, digest: Any, *, limit: int = MAX_HISTORY_BYTES) -> Doc:
    store.require(isinstance(text, str) and len(text.encode()) <= limit, "invalid retained delivery JSON")
    try:
        value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        selected = copied(value, limit=limit)
        store.require(
            store.canonical(selected) == text and contracts.digest(selected) == digest, "delivery history changed"
        )
        return selected
    except (ValueError, UnicodeError, RecursionError) as error:
        raise store.StoreError("invalid retained delivery history") from error


def _pin(value: Any) -> contracts.ApprovalPin:
    selected = copied(value)
    store.require(set(selected) == {"issuer", "approval_id", "digest"}, "invalid retained approval pin")
    return contracts.ApprovalPin(selected["issuer"], selected["approval_id"], selected["digest"])


def pin_document(pin: contracts.ApprovalPin) -> Doc:
    store.require(isinstance(pin, contracts.ApprovalPin), "external delivery approval pin required")
    return {"issuer": pin.issuer, "approval_id": pin.approval_id, "digest": pin.digest}


def validate_read_recovery(value: Any, *, pin: contracts.ApprovalPin, now: dt.datetime) -> Doc:
    """Validate separately selected read recovery content, never a delivery write grant."""
    selected = contracts._schema(value, READ_RECOVERY_SCHEMA)
    store.require(isinstance(pin, contracts.ApprovalPin), "external recovery approval pin required")
    store.require(
        (selected["issuer"], selected["approval_id"], contracts.digest(selected))
        == (pin.issuer, pin.approval_id, pin.digest),
        "read recovery differs from its independently selected pin",
    )
    current = _time(timestamp(now))
    store.require(
        contracts._timestamp(selected["not_before"]) <= current < contracts._timestamp(selected["expires_at"]),
        "read recovery approval is not current",
    )
    return selected


def recovery_scope(approved: Doc, operation: Doc, metadata: Doc, bounds: Doc) -> None:
    intent, original = operation["intent"], operation["approval"]
    expected = {
        "objective": metadata["objective"],
        "journal": {"path": metadata["path"], "id": metadata["id"]},
        "operation_key": intent["request"]["operation_key"],
        "intent_id": intent["id"],
        "request_digest": intent["request_digest"],
        "native_payload_digest": intent["request"]["native_payload_digest"],
        "old_configuration_digest": intent["adapter"]["configuration_digest"],
        "operation_profile_digest": operation["payload"].get("operation_profile_digest"),
        "policy_digest": original["policy_digest"],
        "contract": original["contract"],
        "limits": metadata["limits"],
        "read_limits": bounds,
    }
    store.require(
        all(approved[key] == value for key, value in expected.items()), "read recovery changes retained scope"
    )
    store.require(
        (approved["issuer"], approved["approval_id"]) != (original["issuer"], original["approval_id"]),
        "read recovery requires a separate approval decision",
    )


def _prepared(value: Any, metadata: Doc) -> Doc:
    selected = copied(value, limit=MAX_HISTORY_BYTES)
    store.require(
        set(selected) == {"intent", "payload", "approval", "pin", "prepared_at", "calls", "phase", "receipt", "reason"},
        "invalid retained operation fields",
    )
    approval = contracts.validate_approval(
        selected["approval"], pin=_pin(selected["pin"]), now=_time(selected["prepared_at"])
    )
    intent = contracts.validate_intent(selected["intent"], approval=approval)
    store.require(
        intent["journal"] == {"path": metadata["path"], "id": metadata["id"]}
        and intent["request"]["objective"] == metadata["objective"]
        and approval["limits"] == metadata["limits"],
        "retained delivery objective, journal or immutable limits changed",
    )
    payload = copied(selected["payload"])
    store.require(contracts.digest(payload) == intent["request"]["native_payload_digest"], "native payload changed")
    store.require(isinstance(selected["calls"], list) and len(selected["calls"]) <= MAX_CALLS, "invalid call history")
    invoked = False
    phase = "prepared"
    receipt = None
    keys: set[str] = set()
    for ordinal, call in enumerate(selected["calls"]):
        store.require(
            isinstance(call, dict)
            and set(call)
            == {"id", "method", "bounds", "approval", "pin", "at", "owner", "cleanup", "result", "receipt"},
            "invalid delivery call record",
        )
        method = call["method"]
        store.require(method in METHODS, "unknown delivery call method")
        bounds = validate_native_limits(call["bounds"])
        store.require(all(bounds[key] <= metadata["native_limits"][key] for key in bounds), "native limits changed")
        store.require(method == "invoke" or bounds["write_calls"] == 0, "read-only delivery call has write capacity")
        expected_id = contracts.digest({"operation": intent["id"], "ordinal": ordinal, "method": method})
        store.require(call["id"] == expected_id and call["id"] not in keys, "delivery call identity changed")
        keys.add(call["id"])
        store.require(isinstance(call["approval"], dict), "invalid retained call approval")
        if call["approval"].get("kind") == "delivery-read-recovery":
            store.require(method == "reconcile", "read recovery cannot authorize another native method")
            current_approval = validate_read_recovery(call["approval"], pin=_pin(call["pin"]), now=_time(call["at"]))
            recovery_scope(current_approval, selected, metadata, bounds)
        else:
            current_approval = contracts.validate_approval(
                call["approval"], pin=_pin(call["pin"]), now=_time(call["at"])
            )
            ignored = {"issuer", "approval_id", "not_before", "expires_at"}
            store.require(
                {key: item for key, item in current_approval.items() if key not in ignored}
                == {key: item for key, item in approval.items() if key not in ignored},
                "delivery recovery approval changed scope",
            )
        if method == "invoke":
            store.require(not invoked and phase == "prepared", "a delivery operation was invoked twice")
            store.require(
                bounds["wall_time_ms"] == intent["cost"]["wall_time_ms"]
                and bounds["read_calls"] == intent["cost"]["reads"],
                "retained invocation differs from its reserved cost",
            )
            invoked, phase = True, "may_have_started"
        elif method == "inspect":
            store.require(not invoked, "inspection cannot enable a second invocation")
        else:
            store.require(invoked and phase not in ("applied", "rejected"), "terminal delivery is not rediscovered")
        if call["owner"] is not None:
            owner = copied(call["owner"])
            store.require(set(owner) == {"process", "deadline_monotonic"}, "invalid native ownership record")
            executor_runtime.validate_identity(owner["process"])
            deadline = owner["deadline_monotonic"]
            store.require(
                type(deadline) in (int, float) and math.isfinite(deadline) and deadline > 0, "invalid deadline"
            )
        store.require(call["cleanup"] is None or type(call["cleanup"]) is bool, "invalid native cleanup fact")
        if call["cleanup"] is not None:
            store.require(call["owner"] is not None, "cleanup has no established native owner")
        if call["result"] is not None:
            native_result = _schema(
                call["result"], INSPECTION if method == "inspect" else RESULT, limit=MAX_PROVENANCE_BYTES
            )
            _text(native_result["reason"])
        if call["receipt"] is not None:
            store.require(method != "inspect", "inspection cannot establish a delivery effect")
            receipt = contracts.validate_receipt(call["receipt"], intent=intent)
            store.require(call["result"] is not None, "native receipt has no retained provenance")
            store.require(
                contracts.digest(call["result"]["provenance"]) == receipt["provenance_digest"],
                "native receipt provenance changed",
            )
            store.require(
                all(receipt[field] == call["result"][field] for field in ("outcome", "effect", "reason")),
                "native receipt differs from its retained result",
            )
            if receipt["outcome"] in ("applied", "rejected"):
                store.require(call["cleanup"] is True, "terminal delivery lacks proved native cleanup")
            phase = receipt["outcome"]
        elif call["result"] is not None:
            store.require(method == "inspect", "native delivery result has no immutable receipt")
    store.require(selected["phase"] == phase and selected["receipt"] == receipt, "delivery phase or receipt changed")
    _text(selected["reason"])
    return selected


class Journal:
    def __init__(self, connection: sqlite3.Connection, parent_fd: int, metadata: Doc, *, write: bool) -> None:
        self.connection, self.parent_fd, self.metadata, self.write = connection, parent_fd, metadata, write
        self.active = True

    def read(self) -> Doc:
        store.require(self.active, "delivery journal session ended")
        operations: dict[str, Doc] = {}
        reserved = dict.fromkeys(("operations", "wall_time_ms", "reads", "rollbacks"), 0)
        history_bytes = call_count = 0
        for key, text, digest in self.connection.execute("SELECT operation_key, document, digest FROM operations"):
            history_bytes += len(text.encode())
            store.require(history_bytes <= MAX_LOGICAL_BYTES, "delivery retained history capacity exhausted")
            store.require(len(operations) < MAX_OPERATIONS, "delivery journal operation capacity exhausted")
            operation = _prepared(_decoded(text, digest), self.metadata)
            store.require(key == operation["intent"]["request"]["operation_key"], "operation key changed")
            operations[key] = operation
            # Preparation permanently reserves the first and only invocation, even when later
            # preflight blocks it. Inspection/recovery calls each require additional slots.
            for dimension, units in operation["intent"]["cost"].items():
                reserved[dimension] += units
            for call in operation["calls"]:
                call_count += 1
                store.require(call_count <= MAX_CALLS, "delivery call capacity exhausted")
                if call["method"] != "invoke":
                    reserved["wall_time_ms"] += call["bounds"]["wall_time_ms"]
                    reserved["reads"] += call["bounds"]["read_calls"]
        store.require(
            all(reserved[key] <= self.metadata["limits"][key] for key in reserved), "delivery budget exceeded"
        )
        revocations = [_decoded(*row) for row in self.connection.execute("SELECT document, digest FROM revocations")]
        store.require(len(revocations) <= 1, "delivery revocation history is ambiguous")
        if revocations:
            revoked = revocations[0]
            store.require(set(revoked) == {"reason", "at"}, "invalid delivery revocation")
            _text(revoked["reason"])
            _time(revoked["at"])
        return {
            "metadata": copy.deepcopy(self.metadata),
            "reserved": reserved,
            "operations": operations,
            "revoked": bool(revocations),
            "revocation": copy.deepcopy(revocations[0]) if revocations else None,
        }

    def _put(self, key: str, operation: Doc, *, create: bool = False) -> Doc:
        store.require(self.active and self.write, "delivery journal is read-only")
        value = _prepared(operation, self.metadata)
        text = store.canonical(value)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if create:
                self.connection.execute("INSERT INTO operations VALUES (?, ?, ?)", (key, text, contracts.digest(value)))
            else:
                changed = self.connection.execute(
                    "UPDATE operations SET document=?, digest=? WHERE operation_key=?",
                    (text, contracts.digest(value), key),
                ).rowcount
                store.require(changed == 1, "delivery operation disappeared")
            self.read()
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        try:
            os.fsync(self.parent_fd)
        except OSError as error:
            raise store.StoreError(
                "delivery commit durability is uncertain; inspect exact history before retrying"
            ) from error
        return copy.deepcopy(value)

    def prepare(
        self, intent: Doc, payload: Doc, approval: Doc, pin: contracts.ApprovalPin, current: dt.datetime
    ) -> Doc:
        checked = contracts.validate_intent(
            intent, approval=contracts.validate_approval(approval, pin=pin, now=current)
        )
        key = checked["request"]["operation_key"]
        history = self.read()
        existing = history["operations"].get(key)
        if existing is not None:
            store.require(
                existing["intent"] == checked and existing["payload"] == payload, "operation identity conflicts"
            )
            return copy.deepcopy(existing)
        store.require(not history["revoked"], "delivery objective is revoked")
        for operation in history["operations"].values():
            store.require(
                not (
                    operation["phase"] not in ("applied", "rejected")
                    and operation["intent"]["resource_key"] == checked["resource_key"]
                ),
                "an unresolved delivery operation blocks this resource",
            )
            store.require(
                all(call["owner"] is None or call["cleanup"] is True for call in operation["calls"]),
                "unproved native cleanup blocks new delivery work",
            )
        selected: Doc = {
            "intent": checked,
            "payload": copied(payload),
            "approval": copied(approval),
            "pin": pin_document(pin),
            "prepared_at": timestamp(current),
            "calls": [],
            "phase": "prepared",
            "receipt": None,
            "reason": "Exact intent and logical-stage reservation retained; no provider mutation released.",
        }
        return self._put(key, selected, create=True)

    def reserve_call(
        self, key: str, method: str, bounds: Doc, approval: Doc, pin: contracts.ApprovalPin, current: dt.datetime
    ) -> Doc:
        history = self.read()
        store.require(key in history["operations"], "unknown delivery operation")
        operation = history["operations"][key]
        store.require(operation["phase"] not in ("applied", "rejected"), "delivery outcome is historical")
        store.require(method == "reconcile" or not history["revoked"], "delivery objective is revoked")
        store.require(
            not history["revoked"] or approval.get("kind") == "delivery-read-recovery",
            "withdrawn scope needs separately approved read-only recovery",
        )
        if method != "reconcile":
            store.require(
                all(call["owner"] is None or call["cleanup"] is True for call in operation["calls"]),
                "prior native cleanup remains uncertain",
            )
        store.require(len(operation["calls"]) < MAX_CALLS, "delivery call capacity exhausted")
        checked_bounds = validate_native_limits(bounds)
        if method == "invoke":
            store.require(
                operation["intent"]["cost"]["wall_time_ms"] == checked_bounds["wall_time_ms"]
                and operation["intent"]["cost"]["reads"] == checked_bounds["read_calls"],
                "invocation differs from its original reserved slot",
            )
        call = {
            "id": contracts.digest(
                {"operation": operation["intent"]["id"], "ordinal": len(operation["calls"]), "method": method}
            ),
            "method": method,
            "bounds": checked_bounds,
            "approval": copied(approval),
            "pin": pin_document(pin),
            "at": timestamp(current),
            "owner": None,
            "cleanup": None,
            "result": None,
            "receipt": None,
        }
        operation["calls"].append(call)
        if method == "invoke":
            operation["phase"] = "may_have_started"
        self._put(key, operation)
        return copy.deepcopy(call)

    def update_call(self, key: str, call_id: str, update: Callable[[Doc], None]) -> Doc:
        history = self.read()
        store.require(key in history["operations"], "unknown delivery operation")
        operation = history["operations"][key]
        calls = [call for call in operation["calls"] if call["id"] == call_id]
        store.require(len(calls) == 1, "unknown delivery call")
        before = copy.deepcopy(calls[0])
        update(calls[0])
        for field, original in before.items():
            store.require(original is None or calls[0][field] == original, "retained delivery call facts are immutable")
        return self._put(key, operation)

    def finish_call(self, key: str, call_id: str, result: Doc, receipt: Doc | None) -> Doc:
        operation = self.read()["operations"].get(key)
        store.require(operation is not None, "unknown delivery operation")
        calls = [call for call in operation["calls"] if call["id"] == call_id]
        store.require(len(calls) == 1, "unknown delivery call")
        call = calls[0]
        if call["result"] is not None:
            store.require(
                call["result"] == result and call["receipt"] == receipt, "native delivery result is immutable"
            )
            return copy.deepcopy(operation)
        call["result"], call["receipt"] = copied(result, limit=MAX_PROVENANCE_BYTES), copy.deepcopy(receipt)
        operation["reason"] = _text(result["reason"])
        if receipt is not None:
            operation["receipt"], operation["phase"] = copy.deepcopy(receipt), receipt["outcome"]
        return self._put(key, operation)

    def revoke(self, reason: str, current: dt.datetime) -> Doc:
        store.require(self.active and self.write, "delivery journal is read-only")
        history = self.read()
        if history["revoked"]:
            return history
        value = {"reason": _text(reason), "at": timestamp(current)}
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO revocations VALUES (?, ?)", (store.canonical(value), contracts.digest(value))
            )
            self.connection.commit()
            os.fsync(self.parent_fd)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        return self.read()


@contextmanager
def opened(
    path: Path, journal_id: str, *, write: bool = False, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Iterator[Journal]:
    _uuid(journal_id)
    deadline = store.Deadline(lock_timeout)
    with store.locked(path, write=write, deadline=deadline) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        connection: sqlite3.Connection | None = None
        journal: Journal | None = None
        try:
            before = store.file_info(descriptor)
            store.require(before.st_size <= MAX_DATABASE_BYTES, "delivery journal exceeds its physical bound")
            header = os.pread(descriptor, 100, 0)
            store.require(
                len(header) == 100
                and header[:16] == b"SQLite format 3\0"
                and header[18:20] == b"\x01\x01"
                and int.from_bytes(header[68:72], "big") == APPLICATION_ID
                and int.from_bytes(header[60:64], "big") == USER_VERSION,
                "unrecognized delivery journal format",
            )
            connection = sqlite3.connect(
                absolute.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                isolation_level=None,
                timeout=deadline.remaining(),
            )
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_HISTORY_BYTES)
            store.busy_timeout(connection, deadline)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=EXTRA")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            objects = connection.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            store.require(
                {name: (kind, sql) for name, kind, sql in objects}
                == {name: ("table", sql) for name, sql in SCHEMA.items()},
                "delivery journal schema changed",
            )
            store.require(
                connection.execute("PRAGMA journal_mode").fetchone() == ("delete",), "delivery journal mode changed"
            )
            store.require(
                connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "delivery journal integrity failed"
            )
            rows = connection.execute("SELECT document, digest FROM metadata").fetchall()
            store.require(len(rows) == 1, "delivery journal identity is ambiguous")
            metadata = _decoded(*rows[0])
            store.require(
                set(metadata)
                == {
                    "schema_version",
                    "profile",
                    "id",
                    "path",
                    "device",
                    "inode",
                    "objective",
                    "limits",
                    "native_limits",
                },
                "delivery metadata fields changed",
            )
            store.require(
                metadata["schema_version"] == VERSION
                and metadata["profile"] == PROFILE
                and metadata["id"] == journal_id
                and metadata["path"] == str(absolute)
                and (metadata["device"], metadata["inode"]) == (before.st_dev, before.st_ino),
                "delivery journal canonical identity changed",
            )
            _schema(metadata["objective"], contracts.OBJECTIVE)
            _schema(metadata["limits"], contracts.BUDGET)
            validate_native_limits(metadata["native_limits"])
            delivery_runtime.validate_identity(metadata)
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "delivery journal was replaced"
            )
            journal = Journal(connection, parent_fd, metadata, write=write)
            journal.read()
            yield journal
            current = absolute.lstat()
            store.require(
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "delivery journal was replaced"
            )
        except sqlite3.Error as error:
            if store.sqlite_lock_error(error):
                raise store.LockTimeoutError("delivery journal lock timed out") from None
            raise store.StoreError("delivery journal database operation failed") from error
        finally:
            if journal is not None:
                journal.active = False
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            os.close(descriptor)


def initialize(
    path: Path, *, objective: Any, limits: Any, native_limits: Any, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Explicitly initialize fresh private history; this grants no adapter or effect authority."""
    objective = _schema(objective, contracts.OBJECTIVE)
    limits = _schema(limits, contracts.BUDGET)
    native_limits = validate_native_limits(native_limits)
    store.require(1 <= limits["operations"] <= MAX_OPERATIONS and limits["wall_time_ms"] >= 1, "invalid journal limits")
    journal_id = uuid.uuid4().hex
    with store.locked(path, write=True, lock_timeout=lock_timeout) as (absolute, parent_fd):
        store.auxiliary_files(absolute)
        descriptor = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        connection: sqlite3.Connection | None = None
        try:
            info = store.file_info(descriptor)
            metadata = {
                "schema_version": VERSION,
                "profile": PROFILE,
                "id": journal_id,
                "path": str(absolute),
                "device": info.st_dev,
                "inode": info.st_ino,
                "objective": objective,
                "limits": limits,
                "native_limits": native_limits,
            }
            delivery_runtime.initialize(metadata, parent_fd)
            connection = sqlite3.connect(absolute.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?)", (store.canonical(metadata), contracts.digest(metadata))
            )
            connection.commit()
            os.fsync(parent_fd)
        finally:
            if connection is not None:
                connection.close()
            os.close(descriptor)
    return {
        "schema_version": VERSION,
        "profile": PROFILE,
        "status": "initialized",
        "journal": str(absolute),
        "id": journal_id,
        "objective": objective,
        "limits": limits,
        "native_limits": native_limits,
        "claims": copy.deepcopy(contracts.CLAIMS),
    }


def show(
    path: Path, journal_id: str, operation_key: str | None = None, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Read exact local history without provider access, credentials or current delivery authority."""
    with opened(path, journal_id, lock_timeout=lock_timeout) as journal:
        history = journal.read()
    metadata = history["metadata"]
    result = {
        "schema_version": VERSION,
        "profile": PROFILE,
        "status": "inspected",
        "journal": metadata["path"],
        "id": journal_id,
        "objective": metadata["objective"],
        "limits": metadata["limits"],
        "native_limits": metadata["native_limits"],
        "reserved": history["reserved"],
        "revoked": history["revoked"],
        "revocation": history["revocation"],
        "operations": list(history["operations"].values()),
        "claims": copy.deepcopy(contracts.CLAIMS),
    }
    if operation_key is not None:
        store.require(isinstance(operation_key, str) and store.DIGEST.fullmatch(operation_key), "invalid operation key")
        result["operation"] = history["operations"].get(operation_key)
        if result["operation"] is None:
            result["status"] = "missing"
    return result


def revoke(path: Path, journal_id: str, *, reason: str, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Withdraw after the invocation fence; an admitted call may finish before this takes effect.

    Waiting has a finite lock timeout. No revocation is recorded if that wait times out. A
    previously sent remote request can still be uncertain; withdrawal does not undo or resend it.
    """
    with opened(path, journal_id, lock_timeout=lock_timeout) as journal:
        metadata = copy.deepcopy(journal.metadata)
    with (
        delivery_runtime.invocation(metadata, lock_timeout=lock_timeout),
        opened(path, journal_id, write=True, lock_timeout=lock_timeout) as journal,
    ):
        journal.revoke(reason, dt.datetime.now(dt.UTC))
    return show(path, journal_id, lock_timeout=lock_timeout)

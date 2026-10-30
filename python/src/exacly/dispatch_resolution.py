"""Explicitly abandon stale dispatch coordination without changing its external resources.

Resolution is a new, snapshot-bound authorization. It never reinterprets the old approval as a
permission to register against changed criteria, remove pending work or complete a task.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from exacly import dispatch, dispatch_store, execution_store, executions, operation_locks, planning, state_writer
from exacly.applicability import Target

Doc = dict[str, Any]
VERSION = "0.1.0"
EFFECT = "resolve_dispatch_coordination"
HISTORY = planning.fields(
    {"seq": {"type": "integer", "minimum": 1}, "digest": planning.DIGEST, "phase": {"const": "registration_prepared"}}
)
STATE = planning.fields(
    {
        "path": dispatch.TEXT,
        "exists": {"type": "boolean"},
        "digest": {"anyOf": [planning.DIGEST, {"type": "null"}]},
        "header": {"type": ["object", "null"]},
    }
)
AUTHORIZATION_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "effect": {"const": EFFECT},
        "dispatch_id": planning.DIGEST,
        "storage": dispatch.STORAGE,
        "history": HISTORY,
        "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
        "facts_digest": planning.DIGEST,
        "state": STATE,
    }
)
CLAIMS = {
    **dispatch.CLAIMS,
    "registration_finalized": False,
    "pending_work_cleared": False,
    "state_modified": False,
    "binding_ledger_modified": False,
    "remote_task_modified": False,
}
_RECEIPT_KEYS = {
    "schema_version",
    "status",
    "effect",
    "dispatch_id",
    "reason",
    "storage",
    "history",
    "authorization",
    "facts",
    "state_proof",
    "resolved_at",
    "claims",
    "digest",
}


def _request(record: Doc) -> dispatch.Request:
    retained = record["intent"]["contract"]
    date = max(
        dt.datetime.now(dt.UTC).date(), *(executions.timestamp(event["at"]).date() for event in record["events"])
    )
    return dispatch.Request(
        retained["plan"],
        record["intent"]["identity"]["action_id"],
        retained["readiness_policy"],
        retained["trust_policy"],
        retained["dispatch_policy"],
        Target(retained["target"]),
        date,
    )


def _storage(record: Doc, journal_path: Path, journal_id: str) -> Doc:
    intent = record["intent"]
    chosen = dispatch.validate_policy(intent["contract"]["dispatch_policy"])
    storage = chosen["storage"]
    dispatch._require(
        storage["journal"] == str(journal_path) and storage["journal_id"] == journal_id,
        "resolution requires the original reviewed journal path and instance",
    )
    dispatch._require(
        intent["paths"] == {key: storage[key] for key in ("journal", "ledger", "state")},
        "retained dispatch storage differs from the reviewed policy",
    )
    return dict(storage)


def _load(journal: Path, dispatch_id: str, lock_timeout: float) -> tuple[Path, Doc, Doc]:
    absolute = execution_store.location(journal)
    with dispatch_store.opened(absolute, write=False, deadline=execution_store.Deadline(lock_timeout)) as outbox:
        record = outbox.get(dispatch_id)
        dispatch._require(record is not None, "dispatch does not exist")
        assert record is not None
        storage = _storage(record, absolute, outbox.journal_id)
        dispatch._validate_history(record, _request(record))
        return absolute, record, storage


def _reason(value: Any) -> str:
    dispatch._require(
        isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= 4096,
        "resolution requires a nonblank reason of at most 4096 UTF-8 bytes",
    )
    return str(value)


def _header(state: Doc) -> Doc:
    return {key: state[key] for key in ("namespace", "context", "config_revision", "lineage", "serial", "written_at")}


def _state(snapshot: state_writer.Snapshot) -> Doc:
    state = snapshot.document
    return {
        "path": str(snapshot.path),
        "exists": state is not None,
        "digest": snapshot.digest,
        "header": None if state is None else _header(state),
    }


def _selected(record: Doc) -> tuple[Doc, Doc, Doc]:
    prepared = dispatch._event(record, "registration_prepared")
    remote = dispatch._event(record, "remote_confirmed")
    dispatch._require(
        prepared is not None and remote is not None, "resolution requires a retained partial registration"
    )
    assert prepared is not None and remote is not None
    return prepared["pending"], executions.validate_record(prepared["binding"]), remote["receipt"]


def _pending(state: Doc | None, expected: Doc, binding: Doc) -> Doc:
    entries = (
        []
        if state is None
        else [
            copy.deepcopy(entry)
            for entry in state.get("pending", [])
            if entry["record"].removeprefix(state["namespace"] + ":") == expected["record"]
        ]
    )
    keys = {binding["pending_key"]}
    if state is not None:
        for entry in entries:
            identity = {key: state[key] for key in ("namespace", "context", "lineage")}
            identity.update(
                record=entry["record"].removeprefix(state["namespace"] + ":"),
                task=entry["task"],
                opened_at=executions.timestamp(entry["opened_at"]).isoformat(),
            )
            keys.add(planning.digest(identity))
    return {
        "retained_entry": expected,
        "current_entries": entries,
        "retained_entry_present": expected in entries,
        "binding_pending_keys": sorted(keys),
    }


def _ledger(path: Path, keys: list[str], deadline: execution_store.Deadline) -> Doc:
    if not os.path.lexists(path):
        return {
            "path": str(path),
            "exists": False,
            "format_version": None,
            "records": {},
            "active": {key: None for key in keys},
            "watermarks": {key: None for key in keys},
            "completion_intents": [],
            "completion_receipts": [],
        }
    with execution_store.database(path, create=False, deadline=deadline) as (connection, _):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        dispatch._require(
            version in (execution_store.COMPLETION_VERSION, execution_store.INDEX_VERSION),
            "resolution inspection requires a recognized v2 or v4 binding ledger",
        )
        connection.execute("BEGIN")
        try:
            records, active, watermarks = execution_store.validate(connection, deadline=deadline)
            relevant = {identity: value for identity, value in records.items() if value["pending_key"] in keys}
            intents = [
                {"id": row[0], "binding_id": row[1], "pending_key": row[2], "request_digest": row[3], "phase": row[4]}
                for row in connection.execute(
                    "SELECT id, binding_id, pending_key, request_digest, phase FROM completion_intents ORDER BY id"
                ).fetchall()
                if row[2] in keys
            ]
            receipts = [
                json.loads(row[1])
                for row in connection.execute(
                    "SELECT binding_id, receipt_json FROM completion_receipts ORDER BY binding_id"
                ).fetchall()
                if row[0] in relevant
            ]
            return {
                "path": str(path),
                "exists": True,
                "format_version": version,
                "records": relevant,
                "active": {key: active.get(key) for key in keys},
                "watermarks": {key: watermarks.get(key) for key in keys},
                "completion_intents": intents,
                "completion_receipts": receipts,
            }
        finally:
            connection.rollback()


def _facts(record: Doc, storage: Doc, snapshot: state_writer.Snapshot, timeout: float) -> Doc:
    expected, binding, remote = _selected(record)
    pending = _pending(snapshot.document, expected, binding)
    return {
        "state": _state(snapshot),
        "pending": pending,
        "bindings": _ledger(
            Path(storage["ledger"]), pending["binding_pending_keys"], execution_store.Deadline(timeout)
        ),
        "retained": {
            "pending": expected,
            "binding_id": binding["id"],
            "pending_key": binding["pending_key"],
            "remote": remote,
        },
    }


def _history(record: Doc) -> Doc:
    return {"seq": len(record["events"]), "digest": record["digest"], "phase": record["phase"]}


def _authorization(record: Doc, storage: Doc, facts: Doc, reason: str) -> Doc:
    return {
        "schema_version": VERSION,
        "effect": EFFECT,
        "dispatch_id": record["intent"]["id"],
        "storage": storage,
        "history": _history(record),
        "reason": reason,
        "facts_digest": planning.digest(facts),
        "state": facts["state"],
    }


def _receipt(record: Doc, storage: Doc, facts: Doc, authorization: Doc, proof: bytes | None) -> Doc:
    receipt = {
        "schema_version": VERSION,
        "status": "resolved",
        "effect": EFFECT,
        "dispatch_id": record["intent"]["id"],
        "reason": authorization["reason"],
        "storage": storage,
        "history": _history(record),
        "authorization": authorization,
        "facts": facts,
        "state_proof": None if proof is None else base64.b64encode(proof).decode(),
        "resolved_at": dispatch.now(),
        "claims": copy.deepcopy(CLAIMS),
    }
    receipt["digest"] = planning.digest(receipt)
    return receipt


def _size(record: Doc, receipt: Doc) -> None:
    dispatch._require(
        len(record["events"]) < dispatch_store.MAX_EVENTS, "dispatch history has no terminal slot for resolution"
    )
    data = {"kind": "resolved", "receipt": receipt}
    dispatch._require(
        len(planning.canonical(data).encode()) <= dispatch_store.JSON_LIMIT,
        "resolution receipt exceeds the dispatch journal size limit",
    )


def _report(
    operation: str, record: Doc, storage: Doc, facts: Doc, authorization: Doc, *, receipt: Doc | None = None
) -> Doc:
    report = dispatch._report(operation, record["intent"]["id"], dry_run=receipt is None)
    expected, binding, _ = _selected(record)
    report.update(
        action=copy.deepcopy(binding["contract"]["goal"]),
        target=binding["contract"]["target"],
        storage=storage,
        preconditions=facts,
        authorization_template=authorization,
        effect={
            "operation": EFFECT,
            "reason": authorization["reason"],
            "journal_transition": "registration_prepared -> resolved",
            "state_modified": False,
            "binding_ledger_modified": False,
            "remote_task_modified": False,
        },
        unresolved={
            "pending_entry": expected,
            "pending_entry_still_present": facts["pending"]["retained_entry_present"],
            "retained_binding_present": binding["id"] in facts["bindings"]["records"],
            "requires_separate_reviewed_registration_or_completion": True,
        },
    )
    return dispatch._finish(
        report,
        "ready" if receipt is None else "resolved",
        "Review captured facts to abandon only this dispatch coordination; pending work and bindings stay unchanged."
        if receipt is None
        else "Old coordination is resolved; registration was not finalized and external resources remain unchanged.",
        receipt=receipt,
    )


@contextmanager
def _locked(
    journal: Path, dispatch_id: str, storage: Doc, *, write: bool, lock_timeout: float
) -> Iterator[tuple[dispatch_store.Journal, Doc, state_writer.Snapshot]]:
    ledger = execution_store.location(Path(storage["ledger"]))
    snapshot = state_writer.read(Path(storage["state"]))
    dispatch._require(str(snapshot.path) == storage["state"], "resolution state path changed")
    protected = {str(path) + suffix for path in (journal, ledger) for suffix in ("", "-journal", "-wal", "-shm")}
    dispatch._require(str(snapshot.path) not in protected and journal != ledger, "resolution storage paths overlap")
    dispatch._require(
        not any(
            str(journal) == str(ledger) + suffix or str(ledger) == str(journal) + suffix
            for suffix in ("-journal", "-wal", "-shm")
        ),
        "resolution SQLite sidecars overlap",
    )
    budget = execution_store.Deadline(lock_timeout)
    with operation_locks.directories(
        (journal.parent, ledger.parent, snapshot.path.parent), write=write, deadline=budget
    ) as pinned:
        dispatch._require(
            execution_store.location(journal) == journal and execution_store.location(ledger) == ledger,
            "resolution storage directory changed",
        )
        with dispatch_store.opened(
            journal, write=write, already_locked=True, deadline=execution_store.Deadline(lock_timeout)
        ) as outbox:
            record = outbox.get(dispatch_id)
            dispatch._require(record is not None, "dispatch disappeared")
            assert record is not None
            dispatch._require(
                _storage(record, journal, outbox.journal_id) == storage, "reviewed dispatch storage changed"
            )
            dispatch._validate_history(record, _request(record))
            yield outbox, record, state_writer.read_locked(snapshot, pinned[snapshot.path.parent])


def preview(
    journal: Path, dispatch_id: str, *, reason: str, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    reason = _reason(reason)
    absolute, record, storage = _load(journal, dispatch_id, lock_timeout)
    dispatch._require(record["phase"] == "registration_prepared", "only a partial registration can be resolved")
    with _locked(absolute, dispatch_id, storage, write=False, lock_timeout=lock_timeout) as (_, current, snapshot):
        dispatch._require(
            current["phase"] == "registration_prepared", "dispatch phase changed before resolution preview"
        )
        facts = _facts(current, storage, snapshot, lock_timeout)
        authorization = _authorization(current, storage, facts, reason)
        _size(current, _receipt(current, storage, facts, authorization, snapshot.content))
        return _report("resolution_preview", current, storage, facts, authorization)


def resolve(
    journal: Path, dispatch_id: str, *, authorization: Any, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    approved = dispatch._validated(authorization, AUTHORIZATION_SCHEMA, "dispatch resolution authorization")
    reason = _reason(approved["reason"])
    absolute, record, storage = _load(journal, dispatch_id, lock_timeout)
    if record["phase"] == "resolved":
        receipt = validate_receipt(record)
        dispatch._require(
            receipt["authorization"] == approved, "resolution replay requires the original exact authorization"
        )
        return _report("resolve", record, storage, receipt["facts"], approved, receipt=receipt)
    dispatch._require(record["phase"] == "registration_prepared", "only a partial registration can be resolved")
    with _locked(absolute, dispatch_id, storage, write=True, lock_timeout=lock_timeout) as (outbox, current, snapshot):
        if current["phase"] == "resolved":
            receipt = validate_receipt(current)
            dispatch._require(
                receipt["authorization"] == approved, "resolution replay requires the original exact authorization"
            )
            return _report("resolve", current, storage, receipt["facts"], approved, receipt=receipt)
        dispatch._require(current["phase"] == "registration_prepared", "dispatch phase changed before resolution")
        facts = _facts(current, storage, snapshot, lock_timeout)
        expected = _authorization(current, storage, facts, reason)
        dispatch._require(
            approved == expected, "resolution authorization is stale or approves different captured facts"
        )
        receipt = _receipt(current, storage, facts, expected, snapshot.content)
        _size(current, receipt)
        terminal = outbox.append(dispatch_id, "resolved", {"kind": "resolved", "receipt": receipt})
        validate_receipt(terminal)
        return _report("resolve", terminal, storage, facts, expected, receipt=receipt)


def validate_receipt(record: Doc) -> Doc:
    """Validate one terminal resolution against its actual immutable predecessor and facts."""
    dispatch._require(
        record["phase"] == "resolved" and len(record["events"]) >= 2, "dispatch has no terminal resolution"
    )
    event = record["events"][-1]
    dispatch._require(
        event["phase"] == "resolved"
        and set(event["data"]) == {"kind", "receipt"}
        and event["data"]["kind"] == "resolved",
        "invalid terminal resolution event",
    )
    dispatch._require(
        sum(item["data"].get("kind") == "resolved" for item in record["events"]) == 1,
        "resolution history contains multiple terminal receipts",
    )
    previous = record["events"][-2]
    dispatch._require(previous["phase"] == "registration_prepared", "resolution does not follow a partial registration")
    receipt = event["data"]["receipt"]
    dispatch._require(isinstance(receipt, dict) and set(receipt) == _RECEIPT_KEYS, "invalid resolution receipt fields")
    dispatch._require(
        receipt["digest"] == planning.digest({key: value for key, value in receipt.items() if key != "digest"}),
        "resolution receipt digest differs from its content",
    )
    intent = record["intent"]
    storage = intent["contract"]["dispatch_policy"]["storage"]
    dispatch._require(
        receipt["schema_version"] == VERSION
        and receipt["status"] == "resolved"
        and receipt["effect"] == EFFECT
        and receipt["dispatch_id"] == intent["id"]
        and receipt["storage"] == storage
        and receipt["claims"] == CLAIMS,
        "resolution receipt changes its authority or scope",
    )
    history = {"seq": len(record["events"]) - 1, "digest": previous["digest"], "phase": "registration_prepared"}
    dispatch._require(
        event["previous"] == previous["digest"] and receipt["history"] == history,
        "resolution receipt does not bind the actual previous history head",
    )
    approved = dispatch._validated(receipt["authorization"], AUTHORIZATION_SCHEMA, "retained resolution authorization")
    dispatch._require(
        approved
        == {
            "schema_version": VERSION,
            "effect": EFFECT,
            "dispatch_id": intent["id"],
            "storage": storage,
            "history": history,
            "reason": _reason(receipt["reason"]),
            "facts_digest": planning.digest(receipt["facts"]),
            "state": receipt["facts"]["state"],
        },
        "resolution receipt differs from the reviewed authorization",
    )
    executions.timestamp(receipt["resolved_at"])
    _validate_facts(receipt["facts"], receipt["state_proof"], record, storage)
    return dict(receipt)


def _validate_facts(facts: Doc, proof: Any, record: Doc, storage: Doc) -> None:
    dispatch._require(
        isinstance(facts, dict) and set(facts) == {"state", "pending", "bindings", "retained"},
        "invalid retained resolution facts",
    )
    expected, binding, remote = _selected(record)
    dispatch._require(
        facts["retained"]
        == {"pending": expected, "binding_id": binding["id"], "pending_key": binding["pending_key"], "remote": remote},
        "resolution changes retained task facts",
    )
    state: Doc | None = None
    digest: str | None = None
    if proof is not None:
        content = base64.b64decode(proof, validate=True)
        state = state_writer.decode(content)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
    dispatch._require(
        facts["state"]
        == {
            "path": storage["state"],
            "exists": state is not None,
            "digest": digest,
            "header": None if state is None else _header(state),
        },
        "resolution state proof differs from captured identity",
    )
    pending = _pending(state, expected, binding)
    dispatch._require(facts["pending"] == pending, "resolution pending facts differ from the captured state")
    ledger = facts["bindings"]
    dispatch._require(
        isinstance(ledger, dict)
        and set(ledger)
        == {
            "path",
            "exists",
            "format_version",
            "records",
            "active",
            "watermarks",
            "completion_intents",
            "completion_receipts",
        }
        and ledger["path"] == storage["ledger"]
        and type(ledger["exists"]) is bool,
        "invalid resolution binding facts",
    )
    keys = set(pending["binding_pending_keys"])
    dispatch._require(
        set(ledger["active"]) == keys and set(ledger["watermarks"]) == keys,
        "resolution changes reviewed pending binding identities",
    )
    for identity, value in ledger["records"].items():
        checked = executions.validate_record(value)
        dispatch._require(
            checked["id"] == identity and checked["pending_key"] in keys, "resolution includes an unrelated binding"
        )
    for pending_key, active in ledger["active"].items():
        dispatch._require(
            active is None or (active in ledger["records"] and ledger["records"][active]["pending_key"] == pending_key),
            "resolution active binding has no captured record",
        )
    if not ledger["exists"]:
        dispatch._require(
            ledger["format_version"] is None
            and ledger["records"] == {}
            and all(value is None for value in (*ledger["active"].values(), *ledger["watermarks"].values()))
            and ledger["completion_intents"] == []
            and ledger["completion_receipts"] == [],
            "absent resolution ledger contains fabricated facts",
        )
    else:
        dispatch._require(
            ledger["format_version"] in (execution_store.COMPLETION_VERSION, execution_store.INDEX_VERSION),
            "resolution contains an unsupported binding ledger",
        )


def recover_journal(
    journal: Path, *, authorize: bool = False, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    dispatch._require(authorize is True, "explicit authorization is required for journal-only recovery")
    with dispatch_store.opened(
        journal, write=True, recover=True, deadline=execution_store.Deadline(lock_timeout)
    ) as outbox:
        for (identity,) in outbox.connection.execute("SELECT id FROM dispatch_intents ORDER BY id").fetchall():
            record = outbox.get(identity)
            assert record is not None
            dispatch._validate_history(record, _request(record))
        report = {
            "schema_version": VERSION,
            "operation": "recover_journal",
            "status": "repaired",
            "dry_run": False,
            "dispatch_id": None,
            "reason": "Only the existing dispatch journal was physically recovered; no dispatch was advanced.",
            "journal": str(execution_store.location(journal)),
            "journal_id": outbox.journal_id,
            "recovery_backup": None if outbox.recovery_backup is None else str(outbox.recovery_backup),
            "claims": copy.deepcopy(CLAIMS),
        }
        report["digest"] = planning.digest(report)
        return report

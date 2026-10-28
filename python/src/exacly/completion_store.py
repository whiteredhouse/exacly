"""Durable local completion intents on already locked execution-ledger connections.

SQLite and the JSON state file are separate durability domains. Prepare retains the exact publication
and proof bytes before the caller publishes state; finalize records the outcome after durable publication.
These helpers never publish state, fetch evidence, choose a canonical ledger, or repair source files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from exacly import admission, completion_state, executions, planning, query, state_writer
from exacly import execution_store as store
from exacly.applicability import Target
from exacly.core import is_state
from exacly.github_evidence import ARCHIVE_LIMIT
from exacly.refresh import Run

Doc = dict[str, Any]
VERSION = "0.1.0"
STATE_LIMIT = 16 * 1024 * 1024
JSON_LIMIT = store.RECORD_LIMIT
ID = re.compile(r"[0-9a-f]{32}\Z")
REQUEST_KEYS = {
    "binding_id",
    "state_path",
    "plan_digest",
    "action_id",
    "readiness_policy_digest",
    "trust_policy_digest",
    "target",
    "run_id",
    "attempt",
    "as_of",
}
CLAIMS = {
    "operator_authorized": True,
    "task_completion_recorded": True,
    "external_task_closed": False,
    "executor_output_link_verified": False,
    "test_honesty_verified": False,
    "coverage_verified": False,
    "self_authenticating": False,
    "latest_state_verified": False,
}
SCHEMA = {
    "completion_intents": (
        "CREATE TABLE completion_intents (id TEXT PRIMARY KEY NOT NULL, "
        "binding_id TEXT NOT NULL REFERENCES bindings(id), pending_key TEXT NOT NULL, "
        "request_digest TEXT NOT NULL, phase TEXT NOT NULL CHECK (phase IN ('prepared','committed','aborted')), "
        "intent_json TEXT NOT NULL, before_state BLOB NOT NULL, after_state BLOB NOT NULL, "
        "archive BLOB NOT NULL, report BLOB NOT NULL, admission_json TEXT NOT NULL, model_json TEXT NOT NULL, "
        "admitted_state_json TEXT NOT NULL)"
    ),
    "completion_receipts": (
        "CREATE TABLE completion_receipts (binding_id TEXT PRIMARY KEY NOT NULL REFERENCES bindings(id), "
        "intent_id TEXT UNIQUE NOT NULL REFERENCES completion_intents(id), "
        "request_digest TEXT UNIQUE NOT NULL, receipt_json TEXT NOT NULL)"
    ),
}
PAYLOAD_COLUMNS = (
    "intent_json, phase, before_state, after_state, archive, report, admission_json, model_json, admitted_state_json"
)


def _execute(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
    deadline: store.Deadline | None = None,
) -> sqlite3.Cursor:
    if deadline is not None:
        store.busy_timeout(connection, deadline)
    return connection.execute(statement, parameters)


def _json(text: Any) -> Doc:
    store.require(
        isinstance(text, str) and len(text.encode("utf-8")) <= JSON_LIMIT, "completion JSON exceeds its bound"
    )
    try:
        value = json.loads(text, object_pairs_hook=store.unique_object, parse_constant=store.invalid_constant)
        store.require(isinstance(value, dict) and store.canonical(value) == text, "completion JSON is not canonical")
        return dict(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise store.StoreError("invalid retained completion JSON") from None


def _digest_document(document: Doc) -> None:
    store.require(
        document.get("digest") == planning.digest({key: value for key, value in document.items() if key != "digest"}),
        "retained completion document digest does not match",
    )


def _bytes(value: Any, limit: int) -> bytes:
    store.require(type(value) is bytes and 0 < len(value) <= limit, "retained completion bytes exceed their bound")
    return bytes(value)


def _request(value: Any, binding: Doc) -> Doc:
    store.require(isinstance(value, dict) and set(value) == REQUEST_KEYS, "invalid completion request fields")
    normalized = _json(store.canonical(value))
    for key in ("binding_id", "plan_digest", "action_id", "readiness_policy_digest", "trust_policy_digest"):
        store.require(
            isinstance(value[key], str) and store.DIGEST.fullmatch(value[key]), "invalid completion request digest"
        )
    path = value["state_path"]
    store.require(
        isinstance(path, str) and Path(path).is_absolute() and ".." not in Path(path).parts,
        "completion state path must be absolute",
    )
    store.require(
        type(value["run_id"]) is int and value["run_id"] > 0 and type(value["attempt"]) is int and value["attempt"] > 0,
        "invalid completion run or attempt",
    )
    # The registration already commits to full source targets; this also rejects malformed imported JSON.
    target = Target(value["target"]).data
    store.require(
        set(target) == {"context", "model_revision", "revision"}, "completion requires a source verification target"
    )
    try:
        import datetime as dt

        store.require(
            isinstance(value["as_of"], str) and dt.date.fromisoformat(value["as_of"]).isoformat() == value["as_of"],
            "invalid completion evaluation date",
        )
    except ValueError:
        raise store.StoreError("invalid completion evaluation date") from None
    contract = binding["contract"]
    store.require(
        value["binding_id"] == binding["id"]
        and value["plan_digest"] == contract["plan"]["digest"]
        and value["action_id"] == contract["plan"]["action_id"]
        and value["readiness_policy_digest"] == contract["policy"]["digest"]
        and value["trust_policy_digest"] == contract["trust_policy"]["digest"]
        and target == contract["target"],
        "completion request differs from its registered contract",
    )
    return normalized


def _checked_payload(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
    """Validate retained hashes, evidence, approved goal and the exact pending transition."""
    import datetime as dt

    executions.validate_record(binding)
    metadata = envelope["intent"]
    request = _request(metadata["request"], binding)
    before_bytes = _bytes(envelope["before_state"], STATE_LIMIT)
    after_bytes = _bytes(envelope["after_state"], STATE_LIMIT)
    before, after = state_writer.decode(before_bytes), state_writer.decode(after_bytes)
    pending = binding["contract"]["pending"]
    fixed = ("namespace", "context", "lineage", "config_revision")
    store.require(all(before[key] == after[key] for key in fixed), "completion changed the state identity")
    store.require(
        before["namespace"] == pending["namespace"]
        and before["context"] == pending["context"]
        and before["lineage"] == pending["lineage"]
        and before["config_revision"] == request["target"]["model_revision"]
        and before["context"] == request["target"]["context"]
        and type(after["serial"]) is int
        and after["serial"] == before["serial"] + 1,
        "completion state lineage or serial does not match its binding",
    )
    selected, entry = executions.selected_pending(
        before, pending["record"], pending["task"], pending["opened_at"], binding["contract"]["adopted_verification"]
    )
    store.require(selected == pending, "retained pending identity differs from the registered entry")
    expected = list(before.get("pending", []))
    expected.remove(entry)
    store.require(after.get("pending", []) == expected, "completion changed unrelated pending work")
    archive = _bytes(envelope["archive"], ARCHIVE_LIMIT)
    report = _bytes(envelope["report"], admission.REPORT_LIMIT)
    manifest, retained_report = admission.unpack(archive)
    store.require(
        retained_report == report and manifest["check_outcome"] == "success",
        "completion report differs from its successful archive",
    )
    cases = list(ET.fromstring(report).iter("testcase"))
    store.require(
        any(not {child.tag for child in case} & {"failure", "error", "skipped"} for case in cases)
        and not any({child.tag for child in case} & {"failure", "error"} for case in cases),
        "completion report does not establish successful executed checks",
    )
    admitted = envelope["admission_receipt"]
    _digest_document(admitted)
    origin = admitted.get("origin", {})
    store.require(
        admitted.get("status") == "admitted"
        and admitted.get("target") == request["target"]
        and admitted.get("policy") == binding["contract"]["trust_policy"]
        and admitted.get("as_of") == request["as_of"]
        and origin.get("run_id") == request["run_id"]
        and origin.get("run_attempt") == request["attempt"]
        and origin.get("archive_digest") == admission.sha256(archive)
        and origin.get("report_digest") == admission.sha256(report)
        and manifest["run_id"] == request["run_id"]
        and manifest["run_attempt"] == request["attempt"]
        and manifest["implementation_revision"] == request["target"]["revision"]
        and manifest["repository_id"] == origin.get("repository_id")
        and manifest["repository"] == origin.get("repository")
        and manifest["workflow_path"] == origin.get("workflow_path")
        and manifest["job"] == origin.get("job_key"),
        "retained admission provenance differs from its completion request",
    )
    observed = envelope["admitted_state"]
    store.require(
        isinstance(observed, dict) and planning.digest(observed) == admitted.get("state_digest"),
        "retained admitted state differs from its admission receipt",
    )
    state_writer.decode(store.canonical(observed).encode("utf-8"))
    observed_at = admission.timestamp(observed["written_at"])
    store.require(
        observed_at.astimezone(dt.UTC).date() <= dt.date.fromisoformat(request["as_of"]),
        "retained producer completed after the evaluation date",
    )
    documents = envelope["model_documents"]
    store.require(
        isinstance(documents, dict)
        and bool(documents)
        and all(
            admission.safe_path(path) and isinstance(value, dict) and not is_state(value)
            for path, value in documents.items()
        ),
        "invalid approved completion model documents",
    )
    rederived = admission.observe_report(
        documents,
        report,
        Run(
            revision=request["target"]["revision"],
            model_revision=request["target"]["model_revision"],
            context=request["target"]["context"],
            observed_at=observed_at,
            workflow=origin["workflow_path"],
            job=origin["job_key"],
            job_status="success",
            url=f"https://github.com/{origin['repository']}/actions/runs/{request['run_id']}/attempts/{request['attempt']}",
            source_url=f"https://github.com/{origin['repository']}/blob/{request['target']['revision']}",
        ),
    )
    rederived["lineage"] = observed["lineage"]
    store.require(rederived == observed, "retained admitted state was not derived from its report and approved model")
    expected_after = completion_state.merge(
        documents,
        before,
        observed,
        target=request["target"],
        workflow=origin["workflow_path"],
        job=origin["job_key"],
        written_at=after["written_at"],
    )
    if expected:
        expected_after["pending"] = expected
    else:
        expected_after.pop("pending", None)
    store.require(expected_after == after, "retained after-state is not the exact admitted observer transition")
    before_model = query.load(
        {**documents, "__completion_before_state__": before},
        dt.date.fromisoformat(request["as_of"]),
        revision=request["target"]["model_revision"],
    )
    store.require(not before_model.report.errors, "retained before-state does not conform to the approved model")
    model = query.load(
        {**documents, "__completion_state__": after},
        dt.date.fromisoformat(request["as_of"]),
        revision=request["target"]["model_revision"],
    )
    store.require(not model.report.errors, "retained completion model does not conform")
    goal = binding["contract"]["goal"]
    item = model.record(goal["record"])
    computed = planning.proposal(
        {"record": item["id"], "classification": "collect_evidence", "reason": ""},
        item,
        model.graph.namespace,
        binding["contract"]["original_target"],
        binding["contract"]["policy"],
    )
    store.require(
        item["type"] == "Requirement"
        and item["lifecycle"] != "retired"
        and all(computed[key] == goal[key] for key in ("criteria_digest", "bindings_digest")),
        "retained model differs from its approved completion goal",
    )
    assessment = query.assess(model, goal["record"], Target(request["target"]))
    store.require(
        assessment["status"]["verification"] == "passed"
        and not any(missing["dimension"] == "verification" for missing in assessment["missing"]),
        "retained completion state does not establish verification",
    )
    return request, before, after


def _payload(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
    try:
        return _checked_payload(envelope, binding)
    except store.StoreError:
        raise
    except (admission.Rejected, query.QueryError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise store.StoreError("invalid retained completion payload") from None


def _metadata(
    identity: str, request: Doc, prepared_at: str, binding: Doc, envelope: Doc, before: Doc, after: Doc
) -> Doc:
    store.require(isinstance(identity, str) and ID.fullmatch(identity), "invalid completion intent identity")
    executions.timestamp(prepared_at)
    state = {
        "path": request["state_path"],
        "namespace": before["namespace"],
        "context": before["context"],
        "lineage": before["lineage"],
        "config_revision": before["config_revision"],
        "before_serial": before["serial"],
        "after_serial": after["serial"],
        "before_digest": admission.sha256(envelope["before_state"]),
        "before_size": len(envelope["before_state"]),
        "after_digest": admission.sha256(envelope["after_state"]),
        "after_size": len(envelope["after_state"]),
    }
    value = {
        "schema_version": VERSION,
        "id": identity,
        "request": request,
        "request_digest": planning.digest(request),
        "binding_id": binding["id"],
        "pending_key": binding["pending_key"],
        "prepared_at": prepared_at,
        "state": state,
        "evidence": {
            "admission_digest": envelope["admission_receipt"]["digest"],
            "archive_digest": admission.sha256(envelope["archive"]),
            "report_digest": admission.sha256(envelope["report"]),
            "model_digest": admission.config_digest(envelope["model_documents"]),
            "admitted_state_digest": planning.digest(envelope["admitted_state"]),
        },
    }
    value["digest"] = planning.digest(value)
    return value


def _receipt(receipt: Doc, metadata: Doc, binding: Doc) -> Doc:
    required = {
        "schema_version",
        "id",
        "status",
        "request_digest",
        "binding_id",
        "pending_key",
        "plan",
        "goal",
        "target",
        "policy",
        "trust_policy",
        "state",
        "evidence",
        "completed_at",
        "claims",
        "digest",
    }
    store.require(isinstance(receipt, dict) and set(receipt) == required, "invalid completion receipt fields")
    normalized = _json(store.canonical(receipt))
    _digest_document(normalized)
    contract = binding["contract"]
    expected_state = {
        key: metadata["state"][key]
        for key in (
            "path",
            "namespace",
            "context",
            "lineage",
            "before_serial",
            "after_serial",
            "before_digest",
            "after_digest",
        )
    }
    expected_evidence = {
        key: metadata["evidence"][key] for key in ("admission_digest", "archive_digest", "report_digest")
    }
    store.require(
        receipt["schema_version"] == VERSION
        and receipt["id"] == metadata["id"]
        and receipt["status"] == "completed"
        and receipt["request_digest"] == metadata["request_digest"]
        and receipt["binding_id"] == binding["id"]
        and receipt["pending_key"] == binding["pending_key"]
        and receipt["plan"] == contract["plan"]
        and receipt["goal"] == contract["goal"]
        and receipt["target"] == metadata["request"]["target"]
        and receipt["policy"] == contract["policy"]
        and receipt["trust_policy"] == contract["trust_policy"]
        and receipt["state"] == expected_state
        and receipt["evidence"] == expected_evidence
        and receipt["claims"] == CLAIMS,
        "completion receipt differs from its protected intent",
    )
    store.require(
        executions.timestamp(receipt["completed_at"]) >= executions.timestamp(metadata["prepared_at"]),
        "completion predates its prepared intent",
    )
    return normalized


def _load(connection: sqlite3.Connection, identity: str, deadline: store.Deadline | None = None) -> Doc:
    sizes = _execute(
        connection,
        "SELECT length(CAST(intent_json AS BLOB)), length(before_state), length(after_state), "
        "length(archive), length(report), length(CAST(admission_json AS BLOB)), "
        "length(CAST(model_json AS BLOB)), length(CAST(admitted_state_json AS BLOB)) "
        "FROM completion_intents WHERE id=?",
        (identity,),
        deadline,
    ).fetchone()
    store.require(sizes is not None, "completion intent does not exist")
    limits = (
        JSON_LIMIT,
        STATE_LIMIT,
        STATE_LIMIT,
        ARCHIVE_LIMIT,
        admission.REPORT_LIMIT,
        JSON_LIMIT,
        JSON_LIMIT,
        JSON_LIMIT,
    )
    store.require(
        all(type(size) is int and 0 < size <= limit for size, limit in zip(sizes, limits, strict=True)),
        "completion intent payload exceeds its bound",
    )
    row = _execute(
        connection, f"SELECT {PAYLOAD_COLUMNS} FROM completion_intents WHERE id=?", (identity,), deadline
    ).fetchone()
    metadata, phase, before, after, archive, report, admitted, model, admitted_state = row
    receipt_size = _execute(
        connection,
        "SELECT length(CAST(receipt_json AS BLOB)) FROM completion_receipts WHERE intent_id=?",
        (identity,),
        deadline,
    ).fetchone()
    if receipt_size is not None:
        store.require(
            type(receipt_size[0]) is int and 0 < receipt_size[0] <= JSON_LIMIT, "completion receipt exceeds its bound"
        )
    receipt_row = (
        _execute(
            connection, "SELECT receipt_json FROM completion_receipts WHERE intent_id=?", (identity,), deadline
        ).fetchone()
        if receipt_size is not None
        else None
    )
    return {
        "intent": _json(metadata),
        "phase": phase,
        "before_state": _bytes(before, STATE_LIMIT),
        "after_state": _bytes(after, STATE_LIMIT),
        "archive": _bytes(archive, ARCHIVE_LIMIT),
        "report": _bytes(report, admission.REPORT_LIMIT),
        "admission_receipt": _json(admitted),
        "model_documents": _json(model),
        "admitted_state": _json(admitted_state),
        "receipt": _json(receipt_row[0]) if receipt_row else None,
    }


def _validate(
    connection: sqlite3.Connection,
    records: dict[str, Doc],
    active: dict[str, str],
    watermarks: dict[str, Doc],
    *,
    deadline: store.Deadline | None = None,
) -> None:
    """Validate every retained intent and receipt after the exact base/v2 schema has been checked."""
    rows = _execute(
        connection,
        "SELECT id, binding_id, pending_key, request_digest, phase FROM completion_intents",
        deadline=deadline,
    ).fetchall()
    committed: dict[str, tuple[str, str]] = {}
    prepared = 0
    for identity, binding_id, pending, request_digest, phase in rows:
        store.require(
            binding_id in records and phase in ("prepared", "committed", "aborted"),
            "invalid completion binding or phase",
        )
        binding = records[binding_id]
        envelope = _load(connection, identity, deadline)
        metadata = envelope["intent"]
        request, before, after = _payload(envelope, binding)
        expected = _metadata(identity, request, metadata["prepared_at"], binding, envelope, before, after)
        store.require(
            metadata == expected
            and (binding_id, pending, request_digest)
            == (metadata["binding_id"], metadata["pending_key"], metadata["request_digest"]),
            "completion intent identity or retained bytes changed",
        )
        if phase != "aborted":
            store.require(active.get(pending) == binding_id, "completion binding is no longer active")
        if phase == "prepared":
            prepared += 1
            store.require(envelope["receipt"] is None, "uncommitted intent has a completion receipt")
            watermark = watermarks[pending]
            store.require(
                before["serial"] >= watermark["serial"]
                and (before["serial"] != watermark["serial"] or planning.digest(before) == watermark["digest"]),
                "prepared completion violates the registered state watermark",
            )
        elif phase == "committed":
            store.require(
                binding_id not in committed and envelope["receipt"] is not None,
                "completion receipt is missing or duplicated",
            )
            _receipt(envelope["receipt"], metadata, binding)
            committed[binding_id] = (identity, request_digest)
            store.require(
                watermarks[pending] == {"serial": after["serial"], "digest": planning.digest(after)},
                "completed state watermark differs from its receipt",
            )
        else:
            store.require(envelope["receipt"] is None, "aborted intent has a completion receipt")
    store.require(prepared <= 1, "multiple unresolved completion intents require repair")
    receipts = _execute(
        connection, "SELECT binding_id, intent_id, request_digest FROM completion_receipts", deadline=deadline
    ).fetchall()
    store.require(
        {binding: (identity, request) for binding, identity, request in receipts} == committed,
        "completion receipts and journal phases disagree",
    )


def validate(
    connection: sqlite3.Connection,
    records: dict[str, Doc],
    active: dict[str, str],
    watermarks: dict[str, Doc],
    *,
    deadline: store.Deadline | None = None,
) -> None:
    try:
        _validate(connection, records, active, watermarks, deadline=deadline)
    except store.StoreError:
        raise
    except (admission.Rejected, query.QueryError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise store.StoreError("invalid retained completion intent or receipt") from None


def require_v2(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> None:
    store.require(
        _execute(connection, "PRAGMA user_version", deadline=deadline).fetchone() == (store.COMPLETION_VERSION,),
        "completion requires an explicitly upgraded v2 ledger",
    )
    store.validate(connection, deadline=deadline)


def registration_allowed(
    connection: sqlite3.Connection, pending_key: str, *, deadline: store.Deadline | None = None
) -> None:
    store.require(
        _execute(
            connection, "SELECT 1 FROM completion_intents WHERE phase='prepared' LIMIT 1", deadline=deadline
        ).fetchone()
        is None,
        "an unresolved completion intent blocks registration; inspect and recover it explicitly",
    )
    store.require(
        _execute(
            connection,
            "SELECT 1 FROM completion_receipts r JOIN bindings b ON b.id=r.binding_id WHERE b.pending_key=? LIMIT 1",
            (pending_key,),
            deadline,
        ).fetchone()
        is None,
        "completed pending identity cannot be registered again",
    )


def envelope(connection: sqlite3.Connection, binding_id: str, *, deadline: store.Deadline | None = None) -> Doc:
    require_v2(connection, deadline=deadline)
    records, active, watermarks = store.validate(connection, deadline=deadline)
    store.require(isinstance(binding_id, str) and binding_id in records, "execution binding does not exist")
    binding = records[binding_id]
    successors = [record["id"] for record in records.values() if record["supersedes"] == binding_id]
    current_row = _execute(
        connection,
        "SELECT id, phase, request_digest FROM completion_intents WHERE binding_id=? AND phase!='aborted'",
        (binding_id,),
        deadline,
    ).fetchone()
    completed = completed_for_binding(connection, binding_id, deadline=deadline)
    return {
        "binding": binding,
        "active": active[binding["pending_key"]] == binding_id,
        "superseded_by": successors[0] if successors else None,
        "state_watermark": watermarks[binding["pending_key"]],
        "completion": {"id": current_row[0], "phase": current_row[1], "request_digest": current_row[2]}
        if current_row
        else None,
        "completed_receipt": completed,
    }


def completed_for_binding(
    connection: sqlite3.Connection, binding_id: str, *, deadline: store.Deadline | None = None
) -> Doc | None:
    require_v2(connection, deadline=deadline)
    row = _execute(
        connection, "SELECT receipt_json FROM completion_receipts WHERE binding_id=?", (binding_id,), deadline
    ).fetchone()
    return _json(row[0]) if row else None


def find_completed(
    connection: sqlite3.Connection,
    request_digest: str,
    *,
    binding_id: str | None = None,
    deadline: store.Deadline | None = None,
) -> Doc | None:
    require_v2(connection, deadline=deadline)
    if binding_id is not None:
        previous = completed_for_binding(connection, binding_id, deadline=deadline)
        if previous is not None:
            store.require(
                previous["request_digest"] == request_digest, "binding is already completed for a different request"
            )
            return previous
    row = _execute(
        connection, "SELECT receipt_json FROM completion_receipts WHERE request_digest=?", (request_digest,), deadline
    ).fetchone()
    return _json(row[0]) if row else None


def current(
    connection: sqlite3.Connection, binding_id: str | None = None, *, deadline: store.Deadline | None = None
) -> Doc | None:
    require_v2(connection, deadline=deadline)
    statement = "SELECT id FROM completion_intents WHERE phase='prepared'"
    parameters: tuple[Any, ...] = ()
    if binding_id is not None:
        statement += " AND binding_id=?"
        parameters = (binding_id,)
    row = _execute(connection, statement, parameters, deadline).fetchone()
    return _load(connection, row[0], deadline) if row else None


def get(connection: sqlite3.Connection, intent_id: str, *, deadline: store.Deadline | None = None) -> Doc:
    require_v2(connection, deadline=deadline)
    return _load(connection, intent_id, deadline)


@contextmanager
def _transaction(connection: sqlite3.Connection, deadline: store.Deadline | None) -> Iterator[None]:
    store.require(not connection.in_transaction, "completion journal requires an unoccupied SQLite connection")
    store.configure(connection, deadline=deadline)
    # EXTRA also synchronizes the directory after deleting the rollback journal. Prepare must finish
    # this durability boundary before a different file can publish its recorded after-state bytes.
    _execute(connection, "PRAGMA synchronous=EXTRA", deadline=deadline)
    _execute(connection, "BEGIN IMMEDIATE", deadline=deadline)
    try:
        yield
        if deadline is not None:
            store.busy_timeout(connection, deadline)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def upgrade(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> None:
    """Upgrade only the caller's protected working copy; maintenance retains its original source."""
    store.require(
        _execute(connection, "PRAGMA user_version", deadline=deadline).fetchone() == (store.USER_VERSION,),
        "upgrade requires an exact v1 source ledger",
    )
    records, _, _ = store.validate(connection, deadline=deadline)
    for binding in records.values():
        executions.validate_record(binding)
    with _transaction(connection, deadline):
        for statement in SCHEMA.values():
            _execute(connection, statement, deadline=deadline)
        _execute(connection, f"PRAGMA user_version={store.COMPLETION_VERSION}", deadline=deadline)
    require_v2(connection, deadline=deadline)


def prepare(
    connection: sqlite3.Connection,
    intent: Doc,
    *,
    before_state: bytes,
    after_state: bytes,
    archive: bytes,
    report: bytes,
    admission_receipt: Doc,
    model_documents: Doc,
    admitted_state: Doc,
    deadline: store.Deadline | None = None,
) -> Doc:
    require_v2(connection, deadline=deadline)
    store.require(
        isinstance(intent, dict) and set(intent) in ({"request", "prepared_at"}, {"request", "prepared_at", "id"}),
        "invalid proposed completion intent",
    )
    identity = intent.get("id", uuid.uuid4().hex)
    records, active, watermarks = store.validate(connection, deadline=deadline)
    binding_id = intent["request"].get("binding_id") if isinstance(intent["request"], dict) else None
    store.require(binding_id in records, "execution binding does not exist")
    binding = records[cast(str, binding_id)]
    value = {
        "intent": {"request": intent["request"]},
        "phase": "prepared",
        "before_state": before_state,
        "after_state": after_state,
        "archive": archive,
        "report": report,
        "admission_receipt": _json(store.canonical(admission_receipt)),
        "model_documents": _json(store.canonical(model_documents)),
        "admitted_state": _json(store.canonical(admitted_state)),
        "receipt": None,
    }
    request, before, after = _payload(value, binding)
    metadata = _metadata(identity, request, intent["prepared_at"], binding, value, before, after)
    value["intent"] = metadata
    store.require(active.get(binding["pending_key"]) == binding_id, "completion binding has been superseded")
    watermark = watermarks[binding["pending_key"]]
    store.require(
        before["serial"] >= watermark["serial"]
        and (before["serial"] != watermark["serial"] or planning.digest(before) == watermark["digest"]),
        "completion state would roll back the registered watermark",
    )
    if _execute(connection, "SELECT 1 FROM completion_intents WHERE id=?", (identity,), deadline).fetchone():
        existing = get(connection, identity, deadline=deadline)
        store.require(existing == value, "completion intent identity is already used")
        return existing
    registration_allowed(connection, binding["pending_key"], deadline=deadline)
    with _transaction(connection, deadline):
        _execute(
            connection,
            "INSERT INTO completion_intents (id,binding_id,pending_key,request_digest,phase,intent_json,"
            "before_state,after_state,archive,report,admission_json,model_json,admitted_state_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                binding_id,
                binding["pending_key"],
                metadata["request_digest"],
                "prepared",
                store.canonical(metadata),
                before_state,
                after_state,
                archive,
                report,
                store.canonical(value["admission_receipt"]),
                store.canonical(value["model_documents"]),
                store.canonical(value["admitted_state"]),
            ),
            deadline,
        )
    return get(connection, identity, deadline=deadline)


def finalize(
    connection: sqlite3.Connection, intent_id: str, receipt: Doc, *, deadline: store.Deadline | None = None
) -> Doc:
    value = get(connection, intent_id, deadline=deadline)
    metadata = value["intent"]
    records, _, _ = store.validate(connection, deadline=deadline)
    retained = _receipt(receipt, metadata, records[metadata["binding_id"]])
    if value["phase"] == "committed":
        store.require(value["receipt"] == retained, "immutable completion receipt differs from its retry")
        return dict(value["receipt"])
    store.require(value["phase"] == "prepared", "only a prepared intent can be finalized")
    after = state_writer.decode(value["after_state"])
    with _transaction(connection, deadline):
        _execute(
            connection,
            "INSERT INTO completion_receipts (binding_id,intent_id,request_digest,receipt_json) VALUES (?,?,?,?)",
            (metadata["binding_id"], intent_id, metadata["request_digest"], store.canonical(retained)),
            deadline,
        )
        _execute(
            connection,
            "UPDATE completion_intents SET phase='committed' WHERE id=? AND phase='prepared'",
            (intent_id,),
            deadline,
        )
        _execute(
            connection,
            "UPDATE watermarks SET serial=?, digest=? WHERE pending_key=?",
            (after["serial"], planning.digest(after), metadata["pending_key"]),
            deadline,
        )
    return dict(get(connection, intent_id, deadline=deadline)["receipt"])


def abort(connection: sqlite3.Connection, intent_id: str, *, deadline: store.Deadline | None = None) -> Doc:
    value = get(connection, intent_id, deadline=deadline)
    store.require(value["phase"] in ("prepared", "aborted"), "a committed completion cannot be aborted")
    if value["phase"] == "prepared":
        with _transaction(connection, deadline):
            _execute(
                connection,
                "UPDATE completion_intents SET phase='aborted' WHERE id=? AND phase='prepared'",
                (intent_id,),
                deadline,
            )
    return get(connection, intent_id, deadline=deadline)

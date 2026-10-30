"""Explicit durable GitHub issue dispatch, separate from verification and execution authority.

One authoritative private journal serializes cooperating local dispatchers. Once a create may have
been sent, absence from GitHub discovery never grants another create. Recovery retains every effect
and reconciles conditional state publication with the existing execution binding registry.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from exacly import (
    dispatch_store,
    execution_store,
    executions,
    github_dispatch,
    operation_locks,
    planning,
    policy,
    reconciliation,
    state_writer,
)
from exacly.applicability import Target
from exacly.core import is_state

Doc = dict[str, Any]
VERSION = "0.1.0"
ADAPTER = "github-issue-v1"
TEXT: Doc = {"type": "string", "minLength": 1}
REPOSITORY = planning.fields(
    {
        "full_name": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
        "id": {"type": "integer", "minimum": 1},
    }
)
STORAGE = planning.fields(
    {"journal": TEXT, "ledger": TEXT, "state": TEXT, "journal_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"}}
)
POLICY_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "name": TEXT,
        "version": TEXT,
        "adapter": {"const": ADAPTER},
        "repository": REPOSITORY,
        "storage": STORAGE,
        "issue": planning.fields(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 256},
                "body": {"type": "string", "maxLength": 60000},
            }
        ),
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
    }
)
AUTHORIZATION_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "dispatch_id": planning.DIGEST,
        "action_id": planning.DIGEST,
        "adapter": {"const": ADAPTER},
        "repository": REPOSITORY,
        "storage": STORAGE,
        "effect": {"const": "create_github_issue"},
        "policy_digest": planning.DIGEST,
        "effect_digest": planning.DIGEST,
    }
)
CLAIMS = {
    "task_completion_authorized": False,
    "verification_established": False,
    "executor_output_link_verified": False,
    "self_authenticating": False,
}


class IssueAdapter(Protocol):
    def repository(self, repository: Doc) -> Doc: ...

    def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]: ...

    def create(self, repository: Doc, effect: Doc) -> Doc: ...


@dataclass(frozen=True)
class Request:
    plan: Any
    action_id: str
    readiness_policy: Any
    trust_policy: Any
    dispatch_policy: Any
    target: Target
    date: dt.date


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validated(value: Any, schema: Doc, label: str) -> Doc:
    try:
        plain = planning.plain(value)
        jsonschema.validate(plain, schema, format_checker=jsonschema.FormatChecker())
        return dict(plain)
    except (jsonschema.ValidationError, RecursionError, TypeError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def validate_policy(value: Any) -> Doc:
    chosen = _validated(value, POLICY_SCHEMA, "dispatch policy")
    _require(
        all(part not in {".", ".."} for part in chosen["repository"]["full_name"].split("/")),
        "invalid dispatch repository",
    )
    _require("<!-- exacly-dispatch:" not in chosen["issue"]["body"], "issue body already contains a dispatch marker")
    for key in ("journal", "ledger", "state"):
        path = Path(chosen["storage"][key])
        _require(
            path.is_absolute() and str(path) == str(path.parent.resolve() / path.name),
            "dispatch storage policy requires canonical absolute paths",
        )
    return chosen


def _inputs(request: Request, state: Doc) -> tuple[Doc, Doc, Doc, Doc]:
    _require(isinstance(request, Request), "an explicit dispatch Request is required")
    registration = executions.Registration(
        request.plan,
        request.action_id,
        state,
        "",
        "",
        request.readiness_policy,
        request.trust_policy,
        request.target,
        request.date,
    )
    plan, action, target, trust, _ = executions.contract_inputs(registration)
    chosen = validate_policy(request.dispatch_policy)
    return plan, action, target, {"dispatch": chosen, "trust": trust}


def _documents(documents: Mapping[str, Any], state: Doc) -> Doc:
    _require(isinstance(documents, Mapping), "named approved model documents are required")
    result = {name: copy.deepcopy(document) for name, document in documents.items() if not is_state(document)}
    result["<dispatch-current-state>"] = copy.deepcopy(state)
    return result


def _current(documents: Mapping[str, Any], state: Doc, request: Request) -> tuple[Doc, Doc, Doc, Doc, Doc]:
    plan, action, target, chosen = _inputs(request, state)
    merged = _documents(documents, state)
    model = reconciliation.approved_model(merged, plan, action, target, request.date)
    readiness = policy.evaluate(model, request.readiness_policy, request.target)
    checks = [check for check in readiness["checks"] if check["record"] == action["record"]]
    _require(len(checks) == 1, "selected action has no unique current verification check")
    return plan, action, target, chosen, checks[0]


def _proposal(plan: Doc, action: Doc, chosen: Doc) -> tuple[str, Doc, Doc, Doc]:
    identity = {
        "schema_version": VERSION,
        "namespace": plan["namespace"],
        "action_id": action["id"],
        "adapter": ADAPTER,
        "repository_id": chosen["repository"]["id"],
    }
    dispatch_id = planning.digest(identity)
    marker = f"<!-- exacly-dispatch:v1 {dispatch_id} -->"
    effect = {"title": chosen["issue"]["title"], "body": chosen["issue"]["body"] + "\n\n" + marker}
    try:
        effect = github_dispatch.validate_effect(effect)
    except github_dispatch.GitHubDispatchError as error:
        raise ValueError(f"invalid reviewed issue effect: {error}") from error
    authorization = {
        "schema_version": VERSION,
        "dispatch_id": dispatch_id,
        "action_id": action["id"],
        "adapter": ADAPTER,
        "repository": chosen["repository"],
        "storage": chosen["storage"],
        "effect": "create_github_issue",
        "policy_digest": planning.digest(chosen),
        "effect_digest": planning.digest(effect),
    }
    return dispatch_id, identity, effect, authorization


def _report(operation: str, dispatch_id: str, *, dry_run: bool = False) -> Doc:
    return {
        "schema_version": VERSION,
        "operation": operation,
        "dry_run": dry_run,
        "dispatch_id": dispatch_id,
        "status": "blocked",
        "reason": "",
        "authorization_template": None,
        "effect": None,
        "checks": [],
        "receipt": None,
        "claims": copy.deepcopy(CLAIMS),
    }


def _finish(report: Doc, status: str, reason: str, *, receipt: Doc | None = None) -> Doc:
    report.update(status=status, reason=reason, receipt=receipt)
    report["digest"] = planning.digest(report)
    return report


def preview(documents: Mapping[str, Any], state_path: Path, request: Request) -> Doc:
    """Render the exact selected effect and authorization template without writes or remote calls."""
    snapshot = state_writer.read(state_path)
    current_state = snapshot.document
    _require(current_state is not None, "dispatch requires an existing authoritative state file")
    assert current_state is not None
    plan, action, target, chosen, check = _current(documents, current_state, request)
    _require(
        str(snapshot.path) == chosen["dispatch"]["storage"]["state"],
        "state destination differs from reviewed dispatch storage",
    )
    identity, _, effect, authorization = _proposal(plan, action, chosen["dispatch"])
    report = _report("preview", identity, dry_run=True)
    report.update(effect=effect, authorization_template=authorization)
    _preconditions(report, plan, action, target, chosen, snapshot)
    return _finish(
        report,
        "superseded" if check["result"] == "satisfied" else "ready",
        "The goal is already satisfied; no task is needed."
        if check["result"] == "satisfied"
        else "Review this one GitHub issue effect and supply explicit authorization to apply it.",
    )


def _event(record: Doc, kind: str) -> Doc | None:
    matches = [event["data"] for event in record["events"] if event["data"].get("kind") == kind]
    return matches[-1] if matches else None


def _preconditions(
    report: Doc, plan: Doc, action: Doc, target: Doc, chosen: Doc, snapshot: state_writer.Snapshot
) -> None:
    state = snapshot.document
    assert state is not None
    report.update(
        action=copy.deepcopy(action), target=copy.deepcopy(target), storage=copy.deepcopy(chosen["dispatch"]["storage"])
    )
    report["preconditions"] = {
        "state": {
            "path": str(snapshot.path),
            "digest": snapshot.digest,
            **{key: state[key] for key in ("namespace", "context", "config_revision", "lineage", "serial")},
        },
        "config_digest": plan["inputs"]["config"]["digest"],
        "criteria_digest": action["criteria_digest"],
        "bindings_digest": action["bindings_digest"],
        "readiness_policy": plan["policy"],
        "trust_policy": executions.descriptor(chosen["trust"]),
        "dispatch_policy": executions.descriptor(chosen["dispatch"]),
    }


def _ledger(path: Path, deadline: execution_store.Deadline) -> tuple[Doc, Doc, Doc]:
    if not os.path.lexists(path):
        return {}, {}, {}
    with execution_store.database(path, create=False, deadline=deadline) as (connection, _):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        _require(
            version in (execution_store.COMPLETION_VERSION, execution_store.INDEX_VERSION),
            "dispatch requires a completion-ready v2 or operational v4 ledger; upgrade an existing v1 copy first",
        )
        if version == execution_store.INDEX_VERSION:
            from exacly import execution_index

            connection.execute("BEGIN")
            try:
                indexed = execution_index.check(connection, deadline=deadline)
                execution_index.require_writable(indexed)
                return indexed.snapshot
            finally:
                connection.rollback()
        return execution_store.validate(connection, deadline=deadline)


def _check_binding(path: Path, binding: Doc, deadline: execution_store.Deadline) -> None:
    records, active, watermarks = _ledger(path, deadline)
    identity, pending = binding["id"], binding["pending_key"]
    _require(active.get(pending) in (None, identity), "another active execution binding owns this pending task")
    _require(
        identity not in records or active.get(pending) == identity, "the retained execution binding was superseded"
    )
    capture = binding["state_snapshot"]
    previous = watermarks.get(pending)
    if previous is not None:
        _require(capture["serial"] >= previous["serial"], "dispatch state would roll back the binding watermark")
        _require(
            capture["serial"] != previous["serial"] or capture["digest"] == previous["digest"],
            "dispatch state rewrites the registered serial",
        )
    if os.path.lexists(path):
        from exacly import completion_store

        with execution_store.database(path, create=False, deadline=deadline) as (connection, _):
            connection.execute("BEGIN")
            try:
                completion_store.registration_allowed(connection, pending, deadline=deadline)
            finally:
                connection.rollback()


def _receipt(value: Any, intent: Doc) -> Doc:
    return github_dispatch.validate_receipt(
        value, intent["contract"]["dispatch_policy"]["repository"], intent["effect"]
    )


def _pending(state: Doc, record: str) -> list[Doc]:
    return [
        entry
        for entry in state.get("pending", [])
        if entry["record"].removeprefix(state["namespace"] + ":") == record
        and entry.get("dimension") in (None, "verification")
    ]


def _registration(state: Doc, remote: Doc, request: Request) -> tuple[Doc, Doc, Doc]:
    action = reconciliation.saved_action(request.plan, request.action_id, request.date)[1]
    entry = {
        "record": action["record"],
        "dimension": "verification",
        "task": remote["url"],
        "opened_at": executions.timestamp(remote["created_at"]).isoformat(),
    }
    _require(not _pending(state, action["record"]), "pending verification work already exists for the selected goal")
    after = copy.deepcopy(state)
    after.setdefault("pending", []).append(entry)
    after["serial"] += 1
    written = max(
        executions.timestamp(now()), executions.timestamp(state["written_at"]), executions.timestamp(entry["opened_at"])
    )
    _require(written.date() <= request.date, "dispatch timestamps are after the explicit evaluation date")
    after["written_at"] = written.isoformat()
    binding = executions.build_registration(
        executions.Registration(
            request.plan,
            request.action_id,
            after,
            remote["url"],
            entry["opened_at"],
            request.readiness_policy,
            request.trust_policy,
            request.target,
            request.date,
        )
    )
    return after, binding, entry


def _prepared(value: Doc, intent: Doc, request: Request) -> tuple[bytes, bytes, Doc, Doc]:
    _require(set(value) == {"kind", "before", "after", "binding", "pending"}, "invalid registration intent fields")
    try:
        before = base64.b64decode(value["before"], validate=True)
        after = base64.b64decode(value["after"], validate=True)
        previous = state_writer.decode(before)
        following = state_writer.decode(after)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid retained registration state") from error
    _require(len(before) <= 8 * 1024 * 1024 and len(after) <= 8 * 1024 * 1024, "registration state exceeds its limit")
    _require(following["serial"] == previous["serial"] + 1, "retained registration has an invalid state serial")
    expected = copy.deepcopy(previous)
    expected.setdefault("pending", []).append(value["pending"])
    expected["serial"] += 1
    expected["written_at"] = following["written_at"]
    _require(following == expected, "retained registration changes unrelated state")
    binding = executions.validate_record(value["binding"])
    remote = _receipt(intent["remote"], intent)
    action = reconciliation.saved_action(request.plan, request.action_id, request.date)[1]
    expected_pending = {
        "record": action["record"],
        "dimension": "verification",
        "task": remote["url"],
        "opened_at": executions.timestamp(remote["created_at"]).isoformat(),
    }
    _require(value["pending"] == expected_pending, "retained pending entry differs from the approved task")
    _require(not _pending(previous, action["record"]), "retained before-state already contains selected pending work")
    _require(
        executions.timestamp(following["written_at"]) >= executions.timestamp(previous["written_at"]),
        "retained registration moves the state timestamp backwards",
    )
    rebuilt = executions.build_registration(
        executions.Registration(
            request.plan,
            request.action_id,
            following,
            value["pending"]["task"],
            value["pending"]["opened_at"],
            request.readiness_policy,
            request.trust_policy,
            request.target,
            request.date,
        )
    )
    _require(
        all(binding[key] == rebuilt[key] for key in ("id", "pending_key", "contract", "state_snapshot", "supersedes")),
        "retained registration binding differs from the approved contract",
    )
    return before, after, binding, value["pending"]


def _validate_history(record: Doc, request: Request) -> Doc | None:
    """Validate retained facts independently of their byte-chain integrity."""
    intent = record["intent"]
    remote: Doc | None = None
    prepared: tuple[bytes, bytes, Doc, Doc] | None = None
    proof: bytes | None = None
    bound: Doc | None = None
    result: Doc | None = None
    for event in record["events"]:
        value = event["data"]
        kind = value.get("kind")
        if kind == "remote_confirmed":
            candidate = _receipt(value["receipt"], intent)
            _require(remote is None or candidate == remote, "retained remote task identity changed")
            remote = candidate
        elif kind == "registration_prepared":
            _require(remote is not None, "registration precedes remote confirmation")
            prepared = _prepared(value, {**intent, "remote": remote}, request)
            proof, bound = prepared[1], prepared[2]
        elif kind == "registration_rebased":
            _require(prepared is not None and remote is not None, "registration rebase has no retained preparation")
            assert prepared is not None and remote is not None
            _require(set(value) == {"kind", "state", "binding"}, "invalid retained registration rebase")
            proof = base64.b64decode(value["state"], validate=True)
            state = state_writer.decode(proof)
            initial = state_writer.decode(prepared[1])
            _require(
                all(state[key] == initial[key] for key in ("namespace", "context", "lineage", "config_revision")),
                "registration rebase changes state identity",
            )
            _require(state["serial"] > initial["serial"], "registration rebase did not advance state")
            _require(
                _pending(state, prepared[2]["contract"]["goal"]["record"]) == [prepared[3]],
                "registration rebase changes selected pending work",
            )
            bound = executions.validate_record(value["binding"])
            rebuilt = executions.build_registration(
                executions.Registration(
                    request.plan,
                    request.action_id,
                    state,
                    remote["url"],
                    prepared[3]["opened_at"],
                    request.readiness_policy,
                    request.trust_policy,
                    request.target,
                    request.date,
                )
            )
            _require(
                all(
                    bound[key] == rebuilt[key]
                    for key in ("id", "pending_key", "contract", "state_snapshot", "supersedes")
                ),
                "retained rebased binding differs from current pending work",
            )
        elif kind == "registered":
            _require(
                remote is not None and bound is not None and proof is not None,
                "registration receipt lacks retained preparation",
            )
            assert remote is not None and bound is not None and proof is not None
            receipt = value["receipt"]
            _require(
                isinstance(receipt, dict)
                and set(receipt)
                == {
                    "schema_version",
                    "dispatch_id",
                    "remote",
                    "binding_id",
                    "pending_key",
                    "state",
                    "registered_at",
                    "claims",
                    "digest",
                },
                "invalid retained dispatch receipt",
            )
            _require(
                receipt["digest"] == planning.digest({key: item for key, item in receipt.items() if key != "digest"}),
                "dispatch receipt digest differs from its content",
            )
            _require(
                receipt["schema_version"] == VERSION
                and receipt["dispatch_id"] == intent["id"]
                and receipt["remote"] == remote
                and receipt["binding_id"] == bound["id"]
                and receipt["pending_key"] == bound["pending_key"]
                and receipt["claims"] == CLAIMS,
                "dispatch receipt differs from the retained task and binding",
            )
            state = state_writer.decode(proof)
            _require(
                receipt["state"]
                == {
                    "path": intent["paths"]["state"],
                    "digest": "sha256:" + hashlib.sha256(proof).hexdigest(),
                    "serial": state["serial"],
                },
                "dispatch receipt names another state publication",
            )
            _require(
                executions.timestamp(receipt["registered_at"]).date() <= request.date,
                "dispatch receipt is after the evaluation date",
            )
            _require(result is None or receipt == result, "dispatch receipt was replaced")
            result = receipt
    _require(record["phase"] != "registered" or result is not None, "registered dispatch lacks a receipt")
    _require(
        not (record["phase"] == "superseded" and prepared is not None and result is None),
        "superseded dispatch abandons an incomplete local registration",
    )
    if record["phase"] == "resolved":
        from exacly import dispatch_resolution

        dispatch_resolution.validate_receipt(record)
    return result


def _registered_receipt(identity: str, remote: Doc, binding: Doc, snapshot: state_writer.Snapshot) -> Doc:
    receipt = {
        "schema_version": VERSION,
        "dispatch_id": identity,
        "remote": remote,
        "binding_id": binding["id"],
        "pending_key": binding["pending_key"],
        "state": {
            "path": str(snapshot.path),
            "digest": snapshot.digest,
            "serial": snapshot.document["serial"] if snapshot.document else None,
        },
        "registered_at": now(),
        "claims": copy.deepcopy(CLAIMS),
    }
    receipt["digest"] = planning.digest(receipt)
    return receipt


def _finalize(
    journal: dispatch_store.Journal,
    record: Doc,
    ledger: Path,
    snapshot: state_writer.Snapshot,
    parent_fd: int,
    ledger_fd: int,
    request: Request,
) -> Doc:
    identity, intent = record["intent"]["id"], record["intent"]
    remote_event = _event(record, "remote_confirmed")
    _require(remote_event is not None, "dispatch has no confirmed remote receipt")
    assert remote_event is not None
    remote = _receipt(remote_event["receipt"], intent)
    prepared = _event(record, "registration_prepared")
    current = snapshot.document
    _require(current is not None, "authoritative state disappeared")
    assert current is not None
    if prepared is not None:
        before, after, binding, entry = _prepared(prepared, {**intent, "remote": remote}, request)
        if snapshot.content == after:
            state_writer.sync_locked(snapshot, parent_fd)
        elif snapshot.content == before:
            _check_binding(ledger, binding, execution_store.Deadline())
            state_writer.publish_locked(snapshot, after, parent_fd)
            snapshot = state_writer.read_locked(snapshot, parent_fd)
        else:
            reference = state_writer.decode(before)
            _require(
                all(current[key] == reference[key] for key in ("namespace", "context", "lineage", "config_revision")),
                "registration state identity changed",
            )
            _require(
                current["serial"] > state_writer.decode(after)["serial"],
                "registration state rolled back or rewrote a serial",
            )
            pending = _pending(current, binding["contract"]["goal"]["record"])
            _require(pending == [entry], "pending verification work changed during registration recovery")
            # A cooperating observer can advance unrelated state after publication. Retain the
            # original attempt and use this current snapshot only to advance its binding watermark.
            rebuilt = executions.build_registration(
                executions.Registration(
                    request.plan,
                    request.action_id,
                    current,
                    remote["url"],
                    entry["opened_at"],
                    request.readiness_policy,
                    request.trust_policy,
                    request.target,
                    request.date,
                )
            )
            _require(rebuilt["id"] == binding["id"], "current pending entry changes the retained binding")
            binding = rebuilt
            rebased = {
                "kind": "registration_rebased",
                "state": base64.b64encode(snapshot.content or b"").decode(),
                "binding": binding,
            }
            previous_rebase = _event(record, "registration_rebased")
            # Registered-at is not identity. Reusing the retained exact binding keeps a local
            # retry from consuming history merely because the wall clock advanced.
            if previous_rebase is not None and previous_rebase["state"] == rebased["state"]:
                binding = executions.validate_record(previous_rebase["binding"])
            else:
                _require(
                    len(record["events"]) <= dispatch_store.MAX_EVENTS - 2,
                    "dispatch history has no room to finalize registration",
                )
                journal.append(identity, "registration_prepared", rebased)
    else:
        _require(
            len(record["events"]) <= dispatch_store.MAX_EVENTS - 2,
            "dispatch history has no room to prepare and finalize registration",
        )
        after_doc, binding, entry = _registration(current, remote, request)
        after = state_writer.serialize(after_doc)
        _check_binding(ledger, binding, execution_store.Deadline())
        record = journal.append(
            identity,
            "registration_prepared",
            {
                "kind": "registration_prepared",
                "before": base64.b64encode(snapshot.content or b"").decode(),
                "after": base64.b64encode(after).decode(),
                "binding": binding,
                "pending": entry,
            },
        )
        state_writer.publish_locked(snapshot, after, parent_fd)
        snapshot = state_writer.read_locked(snapshot, parent_fd)
    _check_binding(ledger, binding, execution_store.Deadline())
    registered = execution_store.put_locked(
        ledger, binding, directory_fd=ledger_fd, deadline=execution_store.Deadline()
    )
    receipt = _registered_receipt(identity, remote, registered, snapshot)
    journal.append(identity, "registered", {"kind": "registered", "receipt": receipt})
    return receipt


def apply(
    journal: Path,
    ledger: Path,
    documents: Mapping[str, Any],
    state_path: Path,
    request: Request,
    *,
    authorization: Any,
    client: IssueAdapter,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
    recover: bool = False,
) -> Doc:
    """Apply or reconcile one approved effect, retaining intent before any external create."""
    _require(type(recover) is bool, "recovery selection must be a boolean")
    original = state_writer.read(state_path)
    current_state = original.document
    _require(current_state is not None, "dispatch requires an existing authoritative state file")
    assert current_state is not None
    plan, action, target, chosen, _ = _current(documents, current_state, request)
    identity, stable, effect, expected = _proposal(plan, action, chosen["dispatch"])
    reviewed = _validated(authorization, AUTHORIZATION_SCHEMA, "dispatch authorization")
    _require(reviewed == expected, "authorization does not approve this exact action, destination, policy and effect")
    journal_path, ledger_path = execution_store.location(journal), execution_store.location(ledger)
    selected_paths = {"journal": str(journal_path), "ledger": str(ledger_path), "state": str(original.path)}
    _require(
        all(chosen["dispatch"]["storage"][key] == value for key, value in selected_paths.items()),
        "command destinations differ from reviewed dispatch storage",
    )
    executions.location(journal_path, chosen["trust"])
    executions.location(ledger_path, chosen["trust"])
    protected = {
        str(path) + suffix for path in (journal_path, ledger_path) for suffix in ("", "-journal", "-wal", "-shm")
    }
    _require(
        str(original.path) not in protected and journal_path != ledger_path,
        "dispatch journal, binding ledger and authoritative state must be separate files",
    )
    _require(
        not any(
            str(journal_path) == str(ledger_path) + suffix or str(ledger_path) == str(journal_path) + suffix
            for suffix in ("-journal", "-wal", "-shm")
        ),
        "dispatch storage paths overlap SQLite sidecars",
    )
    report = _report("recover" if recover else "apply", identity)
    report.update(effect=effect, authorization_template=expected)
    _preconditions(report, plan, action, target, chosen, original)
    budget = execution_store.Deadline(lock_timeout)
    with operation_locks.directories(
        (journal_path.parent, ledger_path.parent, original.path.parent), deadline=budget
    ) as pinned:
        _require(
            execution_store.location(journal) == journal_path and execution_store.location(ledger) == ledger_path,
            "dispatch storage path changed",
        )
        parent_fd = pinned[original.path.parent]
        state = state_writer.read_locked(original, parent_fd)
        _require(state.document is not None, "authoritative state disappeared")
        assert state.document is not None
        _, _, _, _, check = _current(documents, state.document, request)
        _ledger(ledger_path, execution_store.Deadline(lock_timeout))
        with dispatch_store.opened(
            journal_path,
            write=True,
            already_locked=True,
            recover=recover,
            deadline=execution_store.Deadline(lock_timeout),
        ) as outbox:
            _require(
                outbox.journal_id == chosen["dispatch"]["storage"]["journal_id"],
                "dispatch journal instance differs from the reviewed policy",
            )
            if outbox.recovery_backup is not None:
                report["recovery_backup"] = str(outbox.recovery_backup)
            record = outbox.get(identity)
            contract = {
                "plan": planning.plain(request.plan),
                "readiness_policy": planning.plain(request.readiness_policy),
                "trust_policy": chosen["trust"],
                "dispatch_policy": chosen["dispatch"],
                "target": target,
            }
            paths = selected_paths
            if record is None:
                intent = {
                    "schema_version": VERSION,
                    "id": identity,
                    "identity": stable,
                    "contract": contract,
                    "paths": paths,
                    "created_at": now(),
                    "authorization": reviewed,
                    "effect": effect,
                }
                record = outbox.prepare(intent)
            else:
                intent = record["intent"]
                if any(
                    intent[key] != value
                    for key, value in (
                        ("identity", stable),
                        ("contract", contract),
                        ("paths", paths),
                        ("authorization", reviewed),
                        ("effect", effect),
                    )
                ):
                    return _finish(
                        report,
                        "conflict",
                        "Existing dispatch has another retained contract; it cannot authorize another task.",
                    )
            retained_receipt = _validate_history(record, request)
            if record["phase"] == "resolved":
                from exacly import dispatch_resolution

                return _finish(
                    report,
                    "resolved",
                    "Old dispatch coordination was explicitly resolved; no new task is authorized.",
                    receipt=dispatch_resolution.validate_receipt(record),
                )
            if record["phase"] == "registered":
                _require(retained_receipt is not None, "registered dispatch lacks a validated receipt")
                return _finish(
                    report,
                    "registered",
                    "The original task and registration are already retained.",
                    receipt=retained_receipt,
                )
            if record["phase"] == "superseded":
                return _finish(report, "superseded", "This dispatch was superseded; no new task is authorized.")
            if check["result"] == "satisfied" and record["phase"] == "prepared":
                outbox.append(identity, "superseded", {"kind": "superseded", "reason": "goal_already_satisfied"})
                return _finish(report, "superseded", "The current goal is satisfied; no external task was created.")
            if record["phase"] in {"prepared", "creating", "ambiguous"}:
                attempts = sum(event["data"].get("kind") == "attempt" for event in record["events"])
                if attempts >= chosen["dispatch"]["max_attempts"]:
                    return _finish(
                        report,
                        "attempts_exhausted",
                        "Remote reconciliation budget is exhausted; retain this identity for explicit resolution.",
                    )
                record = outbox.append(identity, record["phase"], {"kind": "attempt", "number": attempts + 1})
                repository = chosen["dispatch"]["repository"]
                try:
                    _require(client.repository(repository) == repository, "GitHub repository identity changed")
                    matches = client.discover(repository, f"<!-- exacly-dispatch:v1 {identity} -->", effect)
                    _require(isinstance(matches, list), "GitHub discovery did not return a complete result")
                    if len(matches) > 1:
                        outbox.append(
                            identity, record["phase"], {"kind": "conflict", "reason": "multiple_remote_matches"}
                        )
                        return _finish(report, "conflict", "Multiple GitHub tasks carry this dispatch identity.")
                    if matches:
                        remote = _receipt(matches[0], record["intent"])
                    elif record["phase"] != "prepared":
                        outbox.append(
                            identity, "ambiguous", {"kind": "ambiguous", "reason": "no_confirmed_remote_match"}
                        )
                        return _finish(
                            report,
                            "ambiguous",
                            "A create may have been sent; zero matches do not authorize another create.",
                        )
                    else:
                        _require(
                            not _pending(state.document, action["record"]),
                            "pending verification work already exists for the selected goal",
                        )
                        record = outbox.append(identity, "creating", {"kind": "creating"})
                        remote = _receipt(client.create(repository, effect), record["intent"])
                    record = outbox.append(
                        identity, "remote_confirmed", {"kind": "remote_confirmed", "receipt": remote}
                    )
                except github_dispatch.GitHubDispatchError as error:
                    phase = "ambiguous" if record["phase"] in {"creating", "ambiguous"} else record["phase"]
                    outbox.append(identity, phase, {"kind": "remote_error", "reason": str(error)})
                    return _finish(report, "ambiguous" if phase == "ambiguous" else "blocked", str(error))
            _, _, _, _, check = _current(documents, state.document, request)
            if check["result"] == "satisfied" and record["phase"] != "registration_prepared":
                outbox.append(
                    identity,
                    "superseded",
                    {"kind": "superseded", "reason": "goal_already_satisfied_after_remote_confirmation"},
                )
                return _finish(
                    report,
                    "superseded",
                    "The task receipt is retained, but the current goal needs no new pending work.",
                )
            try:
                receipt = _finalize(outbox, record, ledger_path, state, parent_fd, pinned[ledger_path.parent], request)
            except (ValueError, execution_store.StoreError) as error:
                return _finish(report, "conflict", str(error))
            return _finish(
                report,
                "registered",
                "One confirmed task is associated with pending verification and its execution binding.",
                receipt=receipt,
            )


def recover(
    journal: Path,
    ledger: Path,
    documents: Mapping[str, Any],
    state_path: Path,
    request: Request,
    *,
    authorization: Any,
    client: IssueAdapter,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    return apply(
        journal,
        ledger,
        documents,
        state_path,
        request,
        authorization=authorization,
        client=client,
        lock_timeout=lock_timeout,
        recover=True,
    )


def show(journal: Path, dispatch_id: str, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    with dispatch_store.opened(journal, write=False, deadline=execution_store.Deadline(lock_timeout)) as outbox:
        record = outbox.get(dispatch_id)
        if record is not None:
            retained = record["intent"]["contract"]
            inspected_date = max(
                dt.datetime.now(dt.UTC).date(),
                *(executions.timestamp(event["at"]).date() for event in record["events"]),
            )
            request = Request(
                retained["plan"],
                record["intent"]["identity"]["action_id"],
                retained["readiness_policy"],
                retained["trust_policy"],
                retained["dispatch_policy"],
                Target(retained["target"]),
                inspected_date,
            )
            _validate_history(record, request)
    report = _report("show", dispatch_id)
    report["record"] = record
    return _finish(
        report,
        "missing" if record is None else record["phase"],
        "Dispatch history is advisory, not completion authority.",
    )


def supersede(
    journal: Path,
    dispatch_id: str,
    *,
    reason: str,
    authorize: bool = False,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    _require(
        authorize is True and isinstance(reason, str) and reason.strip(),
        "explicit supersession authorization and reason are required",
    )
    with dispatch_store.opened(journal, write=True, deadline=execution_store.Deadline(lock_timeout)) as outbox:
        record = outbox.get(dispatch_id)
        _require(record is not None, "dispatch does not exist")
        assert record is not None
        _require(
            record["phase"] != "registration_prepared",
            "complete local registration recovery before superseding this dispatch",
        )
        _require(record["phase"] != "resolved", "a resolved dispatch retains its terminal resolution")
        if record["phase"] != "superseded":
            outbox.append(dispatch_id, "superseded", {"kind": "superseded", "reason": reason})
    return _finish(
        _report("supersede", dispatch_id),
        "superseded",
        "History is retained; no remote task, binding or implementation was deleted.",
    )


def resolution_preview(
    journal: Path, dispatch_id: str, *, reason: str, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    from exacly import dispatch_resolution

    return dispatch_resolution.preview(journal, dispatch_id, reason=reason, lock_timeout=lock_timeout)


def resolve(
    journal: Path, dispatch_id: str, *, authorization: Any, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    from exacly import dispatch_resolution

    return dispatch_resolution.resolve(journal, dispatch_id, authorization=authorization, lock_timeout=lock_timeout)


def recover_journal(
    journal: Path, *, authorize: bool = False, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    from exacly import dispatch_resolution

    return dispatch_resolution.recover_journal(journal, authorize=authorize, lock_timeout=lock_timeout)


def as_text(report: Doc) -> str:
    lines = [f"Dispatch {report.get('dispatch_id') or report.get('journal')}: {report['status']}", report["reason"]]
    for key in ("action", "target", "storage", "preconditions", "effect", "unresolved", "recovery_backup"):
        if report.get(key) is not None:
            # JSON escaping keeps an explicitly supplied issue body reviewable without emitting
            # terminal control sequences from untrusted candidate or remote text.
            lines.append(f"{key}: " + json.dumps(report[key], ensure_ascii=True, sort_keys=True))
    return "\n".join(lines)

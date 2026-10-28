"""Explicit local verification completion backed by retained admission and a recoverable journal.

Calling complete authorizes only its selected binding, policies, target and state destination. A
registration or advisory preview cannot invoke this operation. The ledger and JSON state remain two
resources: a durable prepared intent precedes publication, and a receipt follows synchronization.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema

from exacly import (
    admission,
    completion_state,
    completion_store,
    executions,
    operation_locks,
    planning,
    policy,
    query,
    state_writer,
)
from exacly import execution_store as store
from exacly.applicability import Target
from exacly.github_evidence import GitHubError
from exacly.reconciliation import saved_action, source_target

Doc = dict[str, Any]
VERSION = "0.1.0"
CLAIMS: Doc = {
    "operator_authorized": False,
    "task_completion_recorded": False,
    "fresh_evidence_admitted": False,
    "external_task_closed": False,
    "executor_output_link_verified": False,
    "test_honesty_verified": False,
    "coverage_verified": False,
    "self_authenticating": False,
    "latest_state_verified": False,
}
RECEIPT_CLAIMS = {key: value for key, value in CLAIMS.items() if key != "fresh_evidence_admitted"}
NULL: Doc = {"type": "null"}
OBJECT: Doc = {"type": "object"}
TEXT: Doc = {"type": "string", "minLength": 1}
IDENTITY: Doc = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
DESCRIPTOR = planning.fields({"name": TEXT, "version": TEXT, "digest": planning.DIGEST})
SOURCE_TARGET = planning.fields({"context": TEXT, "model_revision": admission.SHA, "revision": admission.SHA})
GOAL = planning.fields(
    {
        "record": TEXT,
        "dimension": {"const": "verification"},
        "criteria_digest": planning.DIGEST,
        "bindings_digest": planning.DIGEST,
    }
)
RECEIPT_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "id": IDENTITY,
        "status": {"const": "completed"},
        "request_digest": planning.DIGEST,
        "binding_id": planning.DIGEST,
        "pending_key": planning.DIGEST,
        "plan": planning.fields({"digest": planning.DIGEST, "action_id": planning.DIGEST}),
        "goal": GOAL,
        "target": SOURCE_TARGET,
        "policy": DESCRIPTOR,
        "trust_policy": DESCRIPTOR,
        "state": planning.fields(
            {
                "path": TEXT,
                "namespace": TEXT,
                "context": TEXT,
                "lineage": TEXT,
                "before_serial": {"type": "integer", "minimum": 1},
                "after_serial": {"type": "integer", "minimum": 2},
                "before_digest": planning.DIGEST,
                "after_digest": planning.DIGEST,
            }
        ),
        "evidence": planning.fields(
            {"admission_digest": planning.DIGEST, "archive_digest": planning.DIGEST, "report_digest": planning.DIGEST}
        ),
        "completed_at": {"type": "string", "format": "date-time"},
        "claims": planning.fields(
            {key: {"const": key in {"operator_authorized", "task_completion_recorded"}} for key in RECEIPT_CLAIMS}
        ),
        "digest": planning.DIGEST,
    }
)
SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "operation": {"enum": ["complete", "show", "recover"]},
        "status": {"enum": ["completed", "pending", "stale", "prepared", "aborted", "conflict"]},
        "binding_id": planning.DIGEST,
        "completion_id": {"anyOf": [IDENTITY, NULL]},
        "request_digest": {"anyOf": [planning.DIGEST, NULL]},
        "checks": {
            "type": "array",
            "items": planning.fields({"code": TEXT, "passed": {"type": "boolean"}, "reason": TEXT}),
        },
        "admission": {"anyOf": [OBJECT, NULL]},
        "readiness": {"anyOf": [OBJECT, NULL]},
        "intent": {"anyOf": [OBJECT, NULL]},
        "receipt": {"anyOf": [RECEIPT_SCHEMA, NULL]},
        "claims": planning.fields(
            {
                key: {"type": "boolean"}
                if key in {"operator_authorized", "task_completion_recorded", "fresh_evidence_admitted"}
                else {"const": False}
                for key in CLAIMS
            }
        ),
        "digest": planning.DIGEST,
    }
)


class CompletionInterruptedError(ValueError):
    """A retained intent requires explicit recovery before another completion attempt."""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _report(operation: str, binding_id: str, request_digest: str | None = None) -> Doc:
    return {
        "schema_version": VERSION,
        "operation": operation,
        "status": "pending",
        "binding_id": binding_id,
        "completion_id": None,
        "request_digest": request_digest,
        "checks": [],
        "admission": None,
        "readiness": None,
        "intent": None,
        "receipt": None,
        "claims": {**CLAIMS, "operator_authorized": operation != "show"},
    }


def _finish(report: Doc, status: str, code: str, reason: str, *, receipt: Doc | None = None) -> Doc:
    report["status"] = status
    report["checks"].append({"code": code, "passed": status in {"completed", "aborted"}, "reason": reason})
    report["receipt"] = receipt
    if receipt is not None:
        report["completion_id"] = receipt["id"]
        report["claims"]["task_completion_recorded"] = True
    report["digest"] = planning.digest(report)
    return report


@contextmanager
def _database(path: Path, *, write: bool, deadline: store.Deadline) -> Iterator[sqlite3.Connection]:
    try:
        store.require(os.path.lexists(path), "completion requires an existing upgraded execution ledger")
        with store.database(path, create=write, deadline=deadline) as (connection, created):
            store.require(not created, "completion cannot initialize an execution ledger")
            if write:
                store.configure(connection, deadline=deadline)
            completion_store.require_v2(connection, deadline=deadline)
            yield connection
    except sqlite3.Error as error:
        if store.sqlite_lock_error(error):
            raise store.LockTimeoutError(store.LOCK_TIMEOUT_MESSAGE) from None
        raise store.StoreError("local completion journal operation failed; inspect the selected ledger") from error


def _path(path: Path) -> Path:
    if not isinstance(path, Path) or path.name in {"", ".", ".."}:
        raise ValueError("completion requires an explicit state file path")
    try:
        return path.parent.resolve() / path.name
    except (OSError, RuntimeError) as error:
        raise ValueError("completion state path cannot be resolved") from error


def _inputs(
    plan_doc: Any,
    action_id: str,
    readiness_policy: Any,
    trust_policy: Any,
    output_target: Target,
    run_id: int,
    attempt: int,
    date: dt.date,
    binding_id: str,
    state_path: Path,
) -> tuple[Doc, Doc, Doc, Doc, Doc, Doc]:
    if type(date) is not dt.date or type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1:
        raise ValueError("an explicit evaluation date and positive run and attempt are required")
    if not isinstance(output_target, Target):
        raise ValueError("an explicit output target is required")
    if not isinstance(binding_id, str) or not store.DIGEST.fullmatch(binding_id):
        raise ValueError("select an existing execution binding ID")
    target = source_target(Target(copy.deepcopy(output_target.data)))
    plan, action = saved_action(plan_doc, action_id, date)
    chosen = admission.validate_policy(trust_policy)
    readiness = planning.plain(readiness_policy)
    try:
        jsonschema.validate(readiness, policy.SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid readiness policy: {error.message}") from error
    if any(entry["dimensions"] != ["verification"] for entry in readiness["requirements"]):
        raise ValueError("completion supports verification-only readiness policies")
    request = {
        "binding_id": binding_id,
        "state_path": str(_path(state_path)),
        "plan_digest": plan["digest"],
        "action_id": action_id,
        "readiness_policy_digest": planning.digest(readiness),
        "trust_policy_digest": planning.digest(chosen),
        "target": target,
        "run_id": run_id,
        "attempt": attempt,
        "as_of": date.isoformat(),
    }
    return plan, action, readiness, chosen, target, request


def _contract(envelope: Doc, plan: Doc, action: Doc, readiness: Doc, chosen: Doc, target: Doc) -> str | None:
    binding = executions.validate_record(envelope["binding"])
    if not envelope["active"]:
        return "The execution binding was superseded; select its active replacement."
    expected = {
        "plan": {"digest": plan["digest"], "action_id": action["id"]},
        "goal": {key: action[key] for key in ("record", "dimension", "criteria_digest", "bindings_digest")},
        "original_target": plan["target"],
        "target": target,
        "policy": executions.descriptor(readiness),
        "trust_policy": executions.descriptor(chosen),
    }
    if any(binding["contract"][key] != value for key, value in expected.items()):
        return "Registered plan, action, policies or output association differs from this completion request."
    if (
        target["context"] != plan["target"]["context"]
        or target["model_revision"] != plan["target"]["model_revision"]
        or chosen["model"]["revision"] != target["model_revision"]
        or expected["policy"] != plan["policy"]
        or binding["contract"]["pending"]["namespace"] != plan["namespace"]
    ):
        return "The approved model, namespace, context or policy changed; replan and register explicitly."
    return None


def _conforming(documents: Doc, date: dt.date, revision: str, label: str) -> query.Model:
    model = query.load(documents, date, revision=revision)
    if model.report.errors:
        raise query.QueryError(f"{label} does not conform: {model.report.errors[0].message}")
    return model


def _model(documents: Doc, plan: Doc, action: Doc, target: Doc, date: dt.date) -> query.Model:
    model = _conforming(documents, date, target["model_revision"], "approved model")
    if (
        model.graph.namespace != plan["namespace"]
        or admission.config_digest(documents) != plan["inputs"]["config"]["digest"]
    ):
        raise ValueError("approved model content or namespace differs from the saved plan")
    record = model.record(action["record"])
    if record["type"] != "Requirement" or record["lifecycle"] == "retired" or record["id"] != action["record"]:
        raise ValueError("the saved action does not identify an eligible Requirement")
    candidate = planning.proposal(
        {"record": action["record"], "classification": action["kind"], "reason": action["reason"]},
        record,
        model.graph.namespace,
        plan["target"],
        plan["policy"],
    )
    if any(candidate[key] != action[key] for key in ("id", "criteria_digest", "bindings_digest")):
        raise ValueError("approved criteria, bindings or action identity changed")
    return model


def _merged(documents: Doc, previous: Doc, admitted: admission.AdmissionResult, chosen: Doc, target: Doc) -> Doc:
    """Carry current state and replace precisely the tests bindings this admitted producer observed."""
    assert admitted.state is not None
    return completion_state.merge(
        documents,
        previous,
        admitted.state,
        workflow=chosen["workflow"]["path"],
        job=chosen["workflow"]["job_id"],
        target=target,
        written_at=now(),
    )


def _receipt(intent: Doc, binding: Doc) -> Doc:
    contract = binding["contract"]
    state = intent["state"]
    result = {
        "schema_version": VERSION,
        "id": intent["id"],
        "status": "completed",
        "request_digest": intent["request_digest"],
        "binding_id": binding["id"],
        "pending_key": binding["pending_key"],
        "plan": copy.deepcopy(contract["plan"]),
        "goal": copy.deepcopy(contract["goal"]),
        "target": copy.deepcopy(contract["target"]),
        "policy": copy.deepcopy(contract["policy"]),
        "trust_policy": copy.deepcopy(contract["trust_policy"]),
        "state": {
            key: state[key]
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
        },
        "evidence": {key: intent["evidence"][key] for key in ("admission_digest", "archive_digest", "report_digest")},
        "completed_at": now(),
        "claims": {**RECEIPT_CLAIMS, "operator_authorized": True, "task_completion_recorded": True},
    }
    result["digest"] = planning.digest(result)
    jsonschema.validate(result, RECEIPT_SCHEMA, format_checker=state_writer.FORMATS)
    return result


def complete(
    source: admission.Source,
    plan_doc: Any,
    action_id: str,
    readiness_policy: Any,
    trust_policy: Any,
    output_target: Target,
    run_id: int,
    attempt: int,
    date: dt.date,
    *,
    ledger: Path,
    binding_id: str,
    state_path: Path,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Explicitly authorize and complete one registered local verification task.

    Exact historical replay reads its immutable receipt without fetching GitHub or requiring the task
    still to be pending. Every new evidence evaluation obtains fresh live admission. Neither path
    closes an external ticket or grants release authority.
    """
    store.Deadline(lock_timeout)
    plan, action, readiness, chosen, target, request = _inputs(
        plan_doc,
        action_id,
        readiness_policy,
        trust_policy,
        output_target,
        run_id,
        attempt,
        date,
        binding_id,
        state_path,
    )
    executions.location(ledger, chosen)
    request_digest = planning.digest(request)
    report = _report("complete", binding_id, request_digest)
    budget = store.Deadline(lock_timeout)
    with (
        store.locked(ledger, write=False, deadline=budget) as (absolute, _),
        _database(absolute, write=False, deadline=budget) as connection,
    ):
        envelope = completion_store.envelope(connection, binding_id, deadline=budget)
        mismatch = _contract(envelope, plan, action, readiness, chosen, target)
        if mismatch:
            return _finish(report, "stale", "contract", mismatch)
        receipt = completion_store.find_completed(connection, request_digest, binding_id=binding_id, deadline=budget)
        if receipt is not None:
            return _finish(
                report,
                "completed",
                "replay",
                "The original historical completion receipt was retained; no evidence or state was changed.",
                receipt=receipt,
            )
        pending = completion_store.current(connection, deadline=budget)
        if pending is not None:
            report["completion_id"] = pending["intent"]["id"]
            return _finish(
                report,
                "conflict",
                "recovery",
                "A prepared completion requires explicit recovery before another attempt.",
            )

    snapshot = state_writer.read(state_path)
    if snapshot.content is None:
        raise ValueError("completion requires an existing state file")
    matched, reason, _ = executions.match_envelope(
        ledger,
        envelope,
        plan_doc=plan,
        action_id=action_id,
        state_doc=snapshot.document,
        readiness_policy=readiness,
        trust_policy=chosen,
        output_target=Target(target),
        date=date,
    )
    if not matched:
        return _finish(report, "stale", "binding", reason)
    admitted = admission.admit(source, chosen, Target(target), run_id, attempt, date)
    report["admission"] = admitted.receipt
    if (
        admitted.receipt["status"] != "admitted"
        or admitted.state is None
        or admitted.archive is None
        or admitted.report is None
    ):
        return _finish(
            report, "pending", "admission", "Fresh evidence was not admitted; inspect the admission receipt."
        )
    report["claims"]["fresh_evidence_admitted"] = True
    try:
        documents = admission.model_files(source, f"/repos/{chosen['repository']['full_name']}", chosen["model"], date)
    except (admission.Rejected, GitHubError, query.QueryError, ValueError) as error:
        return _finish(report, "pending", "model", f"The approved model could not be fetched: {error}")
    try:
        _model(documents, plan, action, target, date)
    except (query.QueryError, ValueError) as error:
        return _finish(report, "stale", "model", f"The approved model could not be evaluated: {error}")

    budget = store.Deadline(lock_timeout)
    with (
        operation_locks.locked(ledger, snapshot, deadline=budget) as (absolute, parent_fd),
        _database(absolute, write=True, deadline=budget) as connection,
    ):
        envelope = completion_store.envelope(connection, binding_id, deadline=budget)
        mismatch = _contract(envelope, plan, action, readiness, chosen, target)
        if mismatch:
            return _finish(report, "stale", "contract", mismatch)
        receipt = completion_store.find_completed(connection, request_digest, binding_id=binding_id, deadline=budget)
        if receipt is not None:
            return _finish(
                report,
                "completed",
                "replay",
                "Another caller already completed this exact request.",
                receipt=receipt,
            )
        pending = completion_store.current(connection, deadline=budget)
        if pending is not None:
            report["completion_id"] = pending["intent"]["id"]
            return _finish(
                report,
                "conflict",
                "recovery",
                "A prepared completion requires explicit recovery before another attempt.",
            )
        current = state_writer.read_locked(snapshot, parent_fd)
        previous = current.document
        if previous is None:
            return _finish(report, "stale", "state", "The current state file was removed during evidence admission.")
        matched, reason, _ = executions.match_envelope(
            ledger,
            envelope,
            plan_doc=plan,
            action_id=action_id,
            state_doc=previous,
            readiness_policy=readiness,
            trust_policy=chosen,
            output_target=Target(target),
            date=date,
        )
        if not matched:
            return _finish(report, "stale", "binding", reason)
        # Reject model-invalid current observations before carry() can remove anything.
        _conforming({**documents, "__current_state__": previous}, date, target["model_revision"], "current state")
        candidate = _merged(documents, previous, admitted, chosen, target)
        model = _conforming(
            {**documents, "__candidate_state__": candidate}, date, target["model_revision"], "merged candidate state"
        )
        assessed = policy.evaluate(model, readiness, Target(target))
        report["readiness"] = assessed
        selected = [
            check
            for check in assessed["checks"]
            if check["record"] == action["record"] and check["dimension"] == "verification"
        ]
        if len(selected) != 1:
            raise ValueError("the selected action is not a unique prerequisite of the readiness policy")
        if selected[0]["result"] != "satisfied":
            return _finish(report, "pending", "verification", selected[0]["reason"])
        bound = envelope["binding"]["contract"]["pending"]
        _, entry = executions.selected_pending(
            previous,
            bound["record"],
            bound["task"],
            bound["opened_at"],
            envelope["binding"]["contract"]["adopted_verification"],
        )
        remaining = [item for item in candidate.get("pending", []) if item != entry]
        if len(remaining) != len(candidate.get("pending", [])) - 1:
            raise ValueError("completion must remove exactly one unambiguous registered pending item")
        if remaining:
            candidate["pending"] = remaining
        else:
            candidate.pop("pending", None)
        _conforming({**documents, "__completed_state__": candidate}, date, target["model_revision"], "completed state")
        content = state_writer.serialize(candidate)
        assert current.content is not None
        intent_id = uuid.uuid4().hex
        report["completion_id"] = intent_id
        try:
            prepared = completion_store.prepare(
                connection,
                {"id": intent_id, "request": request, "prepared_at": now()},
                before_state=current.content,
                after_state=content,
                archive=admitted.archive,
                report=admitted.report,
                admission_receipt=admitted.receipt,
                admitted_state=admitted.state,
                model_documents=planning.plain(documents),
                deadline=budget,
            )
        except Exception as error:
            raise CompletionInterruptedError(
                f"preparation for completion {intent_id} failed; the journal may contain a committed intent. "
                f"Inspect it before retrying: {error}"
            ) from error
        report["intent"] = copy.deepcopy(prepared["intent"])
        try:
            state_writer.publish_locked(current, content, parent_fd)
            receipt = _receipt(prepared["intent"], envelope["binding"])
            receipt = completion_store.finalize(connection, intent_id, receipt, deadline=budget)
        except Exception as error:
            raise CompletionInterruptedError(
                f"completion {intent_id} has a retained prepared intent; inspect it and run explicit recovery: {error}"
            ) from error
        return _finish(
            report,
            "completed",
            "completion",
            "Matching admitted observations completed only the registered pending item; "
            "its durable receipt is retained.",
            receipt=receipt,
        )


def show(ledger: Path, completion_id: str, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Read retained completion metadata and its historical receipt, without exposing report bytes."""
    budget = store.Deadline(lock_timeout)
    with (
        store.locked(ledger, write=False, deadline=budget) as (absolute, _),
        _database(absolute, write=False, deadline=budget) as connection,
    ):
        value = completion_store.get(connection, completion_id, deadline=budget)
    intent = value["intent"]
    report = _report("show", intent["binding_id"], intent["request_digest"])
    report["completion_id"] = intent["id"]
    report["intent"] = copy.deepcopy(intent)
    status = "completed" if value["phase"] == "committed" else value["phase"]
    return _finish(
        report,
        status,
        "journal",
        "Retained journal metadata describes a historical operation; "
        "it does not establish the latest filesystem state.",
        receipt=value["receipt"],
    )


def recover(
    ledger: Path,
    completion_id: str,
    *,
    state_path: Path,
    lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Abort an unpublished intent or finalize its exact synchronized after-state, without HTTP."""
    budget = store.Deadline(lock_timeout)
    with (
        store.locked(ledger, write=False, deadline=budget) as (absolute, _),
        _database(absolute, write=False, deadline=budget) as connection,
    ):
        value = completion_store.get(connection, completion_id, deadline=budget)
    intent = value["intent"]
    report = _report("recover", intent["binding_id"], intent["request_digest"])
    report["completion_id"] = intent["id"]
    report["intent"] = copy.deepcopy(intent)
    if str(_path(state_path)) != intent["request"]["state_path"]:
        raise ValueError("recovery state path must match the retained completion authorization")
    if value["phase"] == "committed":
        return _finish(
            report,
            "completed",
            "replay",
            "The original historical completion receipt was already finalized.",
            receipt=value["receipt"],
        )
    if value["phase"] == "aborted":
        return _finish(
            report,
            "aborted",
            "replay",
            "This unpublished intent was already aborted; a new completion requires fresh admission.",
        )
    try:
        snapshot = state_writer.read(state_path)
    except state_writer.StateWriteError as error:
        return _finish(report, "conflict", "state", f"The current state cannot match the retained intent: {error}")
    if snapshot.content is None:
        return _finish(
            report,
            "conflict",
            "state",
            "The current state is absent; preserve the prepared intent and resolve the conflict explicitly.",
        )
    budget = store.Deadline(lock_timeout)
    with (
        operation_locks.locked(ledger, snapshot, deadline=budget) as (absolute, parent_fd),
        _database(absolute, write=True, deadline=budget) as connection,
    ):
        value = completion_store.get(connection, completion_id, deadline=budget)
        if value["phase"] == "committed":
            return _finish(
                report,
                "completed",
                "replay",
                "Another caller already finalized this historical completion.",
                receipt=value["receipt"],
            )
        if value["phase"] == "aborted":
            return _finish(report, "aborted", "replay", "Another caller already aborted this unpublished intent.")
        current = state_writer.read_locked(snapshot, parent_fd)
        if current.content == value["before_state"]:
            completion_store.abort(connection, completion_id, deadline=budget)
            return _finish(
                report,
                "aborted",
                "before",
                "Nothing was published. The intent is aborted; a new completion requires fresh live admission.",
            )
        if current.content != value["after_state"]:
            return _finish(
                report,
                "conflict",
                "state",
                "Current state differs from both retained versions; no state or journal transition was applied.",
            )
        envelope = completion_store.envelope(connection, intent["binding_id"], deadline=budget)
        if not envelope["active"]:
            return _finish(report, "conflict", "binding", "The retained completion binding is no longer active.")
        state_writer.sync_locked(current, parent_fd)
        receipt = completion_store.finalize(
            connection, completion_id, _receipt(intent, envelope["binding"]), deadline=budget
        )
        return _finish(
            report,
            "completed",
            "after",
            "The exact authorized after-state was synchronized and its retained completion receipt finalized; "
            "no new evidence evaluation occurred.",
            receipt=receipt,
        )


def as_text(report: Doc) -> str:
    lines = [f"Exacly local completion: {report['status']}", f"Binding: {report['binding_id']}"]
    if report["completion_id"] is not None:
        lines.append(f"Completion: {report['completion_id']}")
    if report["intent"] is not None:
        intent = report["intent"]
        lines.append(f"State: {intent['state']['path']}")
        lines.append(f"Goal: {intent['request']['action_id']}")
        lines.append(
            f"Publication: serial {intent['state']['before_serial']} -> {intent['state']['after_serial']} "
            f"({intent['state']['after_digest']})"
        )
    lines += [f"  {check['code']}: {check['reason']}" for check in report["checks"]]
    if report["readiness"] is not None:
        lines.append(f"Whole-policy readiness: {report['readiness']['result']}")
    lines.append(
        "Local verification history only; external task closure, executor provenance, "
        "test honesty and acceptance coverage remain unverified."
    )
    return "\n".join(lines)

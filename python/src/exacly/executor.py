"""Durable, explicitly approved local attempts without evidence or completion authority.

One initialized companion journal coordinates the selected binding ledger. Reservation consumes
the attempt and concurrency budget before any worker starts. A retained exact process identity,
not a timeout or expired lease, establishes when an interrupted slot can be released.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - the local runtime requires Linux.
    fcntl = None  # type: ignore[assignment]

from exacly import (
    completion_store,
    execution_store,
    executions,
    executor_runtime,
    executor_store,
    isolation,
    operation_locks,
    planning,
    policy,
    reconciliation,
    state_writer,
)
from exacly.core import is_state
from exacly.executor_contract import (
    ADAPTER,
    CLAIMS,
    TASK_INPUT,
    VERSION,
    Request,
    canonical_path,
    output_path,
    require,
    roster,
    snapshots,
    validate_history,
    validate_policy,
)

__all__ = ["Request", "apply", "as_text", "cancel", "preview", "recover", "recover_journal", "show", "validate_policy"]

Doc = dict[str, Any]
TERMINAL = executor_store.TERMINAL


def _checked_get(journal: executor_store.Journal, identity: str) -> Doc | None:
    work = journal.get(identity)
    return (
        validate_history(
            work, journal_id=journal.journal_id, ledger=journal.ledger, max_concurrency=journal.max_concurrency
        )
        if work is not None
        else None
    )


def _report(operation: str, identity: str, *, dry_run: bool = False) -> Doc:
    return {
        "schema_version": VERSION,
        "operation": operation,
        "dry_run": dry_run,
        "execution_id": identity,
        "status": "blocked",
        "reason": "",
        "authorization_template": None,
        "effect": None,
        "receipt": None,
        "claims": copy.deepcopy(CLAIMS),
    }


def _finish(report: Doc, status: str, reason: str, *, receipt: Doc | None = None) -> Doc:
    report.update(status=status, reason=reason, receipt=receipt)
    report.pop("digest", None)
    report["digest"] = planning.digest(report)
    return report


def _envelope(ledger: Path, identity: str, deadline: execution_store.Deadline) -> Doc:
    with execution_store.database(ledger, create=False, deadline=deadline) as (connection, _):
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        require(
            version in (execution_store.COMPLETION_VERSION, execution_store.INDEX_VERSION),
            "executor requires a completion-ready v2 or operational v4 binding ledger",
        )
        records, active, watermarks = execution_store.validate(connection, deadline=deadline)
        require(identity in records, "execution binding does not exist")
        binding = records[identity]
        pending = binding["pending_key"]
        completion_store.registration_allowed(connection, pending, deadline=deadline)
        return {
            "binding": binding,
            "active": active[pending] == identity,
            "superseded_by": None,
            "state_watermark": watermarks[pending],
        }


def _current(
    documents: Mapping[str, Any],
    snapshot: state_writer.Snapshot,
    request: Request,
    ledger: Path,
    deadline: execution_store.Deadline,
) -> tuple[Doc, Doc, Doc, Doc]:
    require(isinstance(request, Request), "an explicit executor Request is required")
    require(snapshot.document is not None, "executor requires an existing actual state file")
    state = snapshot.document
    assert state is not None
    plan, action = reconciliation.saved_action(request.plan, request.action_id, request.date)
    target = reconciliation.source_target(request.target)
    merged = {name: copy.deepcopy(value) for name, value in documents.items() if not is_state(value)}
    merged["<executor-current-state>"] = copy.deepcopy(state)
    model = reconciliation.approved_model(merged, plan, action, target, request.date)
    envelope = _envelope(ledger, request.binding_id, deadline)
    matched, reason, _ = executions.match_envelope(
        ledger,
        envelope,
        plan_doc=request.plan,
        action_id=request.action_id,
        state_doc=state,
        readiness_policy=request.readiness_policy,
        trust_policy=request.trust_policy,
        output_target=request.target,
        date=request.date,
    )
    require(matched, reason)
    readiness = policy.evaluate(model, request.readiness_policy, request.target)
    checks = [check for check in readiness["checks"] if check["record"] == action["record"]]
    require(
        len(checks) == 1 and checks[0]["result"] != "satisfied",
        "the selected verification goal is already satisfied; no execution is authorized",
    )
    record = model.record(action["record"])
    task = {
        "schema_version": VERSION,
        "task": envelope["binding"]["contract"]["pending"]["task"],
        "binding_id": envelope["binding"]["id"],
        "criteria_digest": action["criteria_digest"],
        "bindings_digest": action["bindings_digest"],
        "requirements": [
            {
                "id": action["record"],
                **{
                    key: copy.deepcopy(record.get(key))
                    for key in ("statement", "acceptance_criteria", "verification_method")
                },
                "criteria_digest": action["criteria_digest"],
                "bindings_digest": action["bindings_digest"],
            }
        ],
        "input_implementation": target,
        "dependencies": roster(snapshots(request.inputs)),
        "expected_observations": copy.deepcopy(record.get("observe", {})),
    }
    return plan, action, envelope["binding"], task


def _selection(
    documents: Mapping[str, Any],
    snapshot: state_writer.Snapshot,
    request: Request,
    journal: executor_store.Journal,
    chosen: Doc,
    deadline: execution_store.Deadline,
) -> tuple[str, Doc, dict[str, bytes], Doc | None, Doc]:
    storage = chosen["storage"]
    require(
        journal.journal_id == storage["journal_id"] and journal.ledger == storage["ledger"],
        "executor journal instance or canonical binding ledger differs from reviewed storage",
    )
    require(str(snapshot.path) == storage["state"], "state path differs from reviewed executor storage")
    plan, action, binding, task = _current(documents, snapshot, request, Path(storage["ledger"]), deadline)
    executions.location(Path(storage["journal"]), request.trust_policy)
    executions.location(Path(storage["output_root"]) / ".executor-output", request.trust_policy)
    inputs = snapshots(request.inputs)
    inputs[TASK_INPUT] = (planning.canonical(task) + "\n").encode()
    require(
        sum(len(content) for content in inputs.values()) <= isolation.MAX_INPUT_BYTES,
        "approved task metadata and snapshots exceed the input byte limit",
    )
    identity = {
        "schema_version": VERSION,
        "adapter": ADAPTER,
        "pending_key": binding["pending_key"],
        "action_id": action["id"],
    }
    execution_id = planning.digest(identity)
    contract = {
        "policy": chosen,
        "binding": binding,
        "plan": plan["digest"],
        "action": copy.deepcopy(action),
        "config_digest": plan["inputs"]["config"]["digest"],
        "readiness_policy": executions.descriptor(request.readiness_policy),
        "trust_policy": executions.descriptor(request.trust_policy),
        "target": copy.deepcopy(request.target.data),
        "task": task,
        "inputs": roster(inputs),
    }
    work = _checked_get(journal, execution_id)
    if work is not None:
        require(
            work["intent"]["identity"] == identity
            and work["intent"]["contract"] == contract
            and work["intent"]["max_attempts"] == chosen["max_attempts"],
            "executor work has another retained contract; changed policy cannot reset its limits",
        )
    intent = {
        "schema_version": VERSION,
        "id": execution_id,
        "identity": identity,
        "contract": contract,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "max_attempts": chosen["max_attempts"],
    }
    attempts = work["attempts"] if work is not None else []
    number = len(attempts) + 1
    authorization = {
        "schema_version": VERSION,
        "execution_id": execution_id,
        "attempt": number,
        "effect": "execute_local_snapshot",
        "storage": copy.deepcopy(storage),
        "max_concurrency": journal.max_concurrency,
        "contract_digest": planning.digest(contract),
        "history_head": work["digest"] if work is not None and attempts else None,
        "state_digest": snapshot.digest,
        "evaluation_date": request.date.isoformat(),
        "output": str(output_path(chosen, execution_id, number)),
    }
    return execution_id, intent, inputs, work, authorization


def _receipt(attempt: Doc) -> Doc | None:
    if attempt["phase"] not in TERMINAL:
        return None
    value = attempt["events"][-1]["data"]
    require(
        set(value) == {"kind", "receipt"} and value["kind"] == "receipt" and isinstance(value["receipt"], dict),
        "terminal executor history has no immutable receipt",
    )
    receipt = value["receipt"]
    require(
        receipt.get("digest") == planning.digest({key: val for key, val in receipt.items() if key != "digest"}),
        "executor receipt digest differs from its content",
    )
    require(
        receipt.get("claims") == CLAIMS and receipt.get("outcome") == attempt["phase"],
        "executor receipt claims or phase changed",
    )
    return dict(receipt)


def _view(operation: str, work: Doc, *, dry_run: bool = False) -> Doc:
    validate_history(work)
    report = _report(operation, work["intent"]["id"], dry_run=dry_run)
    report["history"] = copy.deepcopy(work)
    attempts = work["attempts"]
    if not attempts:
        return _finish(report, "prepared", "The work contract is retained; no candidate has started.")
    attempt = attempts[-1]
    report["attempt"] = attempt["number"]
    receipt = _receipt(attempt)
    return _finish(
        report,
        attempt["phase"],
        receipt["reason"] if receipt else "The attempt retains its concurrency slot until verified termination.",
        receipt=receipt,
    )


def preview(
    documents: Mapping[str, Any],
    state_path: Path,
    request: Request,
    *,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    """Read the exact next attempt and authorization without launching or writing anything."""
    chosen = validate_policy(request.executor_policy)
    storage = chosen["storage"]
    require(str(canonical_path(str(state_path))) == storage["state"], "state path differs from reviewed policy")
    deadline = execution_store.Deadline(lock_timeout)
    journal_path = Path(storage["journal"])
    with (
        operation_locks.directories(
            (journal_path.parent, Path(storage["ledger"]).parent, state_path.parent), write=False, deadline=deadline
        ),
        executor_store.opened(journal_path, write=False, deadline=deadline, already_locked=True) as journal,
    ):
        snapshot = state_writer.read(state_path)
        identity, _intent, _inputs, work, authorization = _selection(
            documents, snapshot, request, journal, chosen, deadline
        )
        if work is not None and work["attempts"]:
            last = work["attempts"][-1]
            if last["phase"] not in TERMINAL or last["phase"] == "succeeded":
                return _view("preview", work, dry_run=True)
            if len(work["attempts"]) >= chosen["max_attempts"]:
                return _finish(
                    _view("preview", work, dry_run=True),
                    "attempts_exhausted",
                    "The approved attempt budget is exhausted; no further launch is permitted.",
                )
        report = _report("preview", identity, dry_run=True)
        report.update(
            authorization_template=authorization,
            attempt=authorization["attempt"],
            effect={
                "adapter": ADAPTER,
                "argv": chosen["argv"],
                "runtime": chosen["runtime"],
                "inputs": _intent["contract"]["inputs"],
                "task": _intent["contract"]["task"],
                "expected_outputs": chosen["expected_outputs"],
                "permitted_repository_effects": [],
                "output": authorization["output"],
                "max_attempts": chosen["max_attempts"],
                "timeout_seconds": chosen["timeout_seconds"],
                "max_concurrency": journal.max_concurrency,
            },
        )
        return _finish(report, "ready", "Review this exact attempt and supply separate authorization to execute it.")


def _retained(work: Doc, authorization: Any) -> Doc | None:
    for attempt in work["attempts"]:
        if attempt["events"][0]["data"].get("authorization") == authorization:
            report = _view("apply", work)
            report["attempt"] = attempt["number"]
            receipt = _receipt(attempt)
            return _finish(
                report,
                attempt["phase"],
                receipt["reason"]
                if receipt
                else "The retained attempt is still unresolved; replay starts no candidate.",
                receipt=receipt,
            )
    return None


def _event(attempt: Doc, kind: str) -> Doc | None:
    matching = [event["data"] for event in attempt["events"] if event["data"].get("kind") == kind]
    return matching[-1] if matching else None


def _cancelled(attempt: Doc) -> bool:
    return _event(attempt, "cancellation_requested") is not None


def _output_folder(path: Path) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    info = path.parent.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid(),
        "executor attempt parent must remain an owner-private directory",
    )
    require(not os.path.lexists(path), "executor attempt output already exists; inspect retained work")
    descriptor = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _lease(output: str, *, create: bool) -> Iterator[int | None]:
    """Distinguish an active invocation from an idle, still-living API process."""
    require(fcntl is not None, "executor ownership leases require POSIX locks")
    path = Path(output).with_name("." + Path(output).name + ".owner")
    require(path.parent == path.parent.resolve(), "executor ownership directory traverses a symbolic link")
    parent_info = path.parent.lstat()
    require(
        stat.S_ISDIR(parent_info.st_mode)
        and parent_info.st_uid == os.getuid()
        and stat.S_IMODE(parent_info.st_mode) == 0o700,
        "executor ownership directory must remain owner-private",
    )
    descriptor = os.open(
        path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_CREAT if create else 0), 0o600
    )
    try:
        before = execution_store.file_info(descriptor)
        acquired = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        current = path.lstat()
        require((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino), "executor ownership lease changed")
        if create:
            os.fsync(descriptor)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        yield descriptor if acquired else None
    finally:
        os.close(descriptor)


def _file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
            "retained executor output must be a bounded regular single-link file",
        )
        result = bytearray()
        while block := os.read(descriptor, min(128 * 1024, limit + 1 - len(result))):
            result.extend(block)
            require(len(result) <= limit, "retained executor output exceeded its limit")
        after = os.fstat(descriptor)
        require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "executor output changed while inspected",
        )
        return bytes(result)
    finally:
        os.close(descriptor)


def _bundle(intent: Doc, authorization: Doc) -> tuple[str, Doc | None, str | None, str]:
    """Validate the private trusted collector's bundle, never candidate revision claims."""
    path = Path(authorization["output"])
    directory = path.lstat()
    require(
        path == path.resolve()
        and stat.S_ISDIR(directory.st_mode)
        and directory.st_uid == os.getuid()
        and stat.S_IMODE(directory.st_mode) == 0o700,
        "retained executor bundle must be a plain canonical owner-private directory",
    )
    content = _file(path / "report.json", execution_store.RECORD_LIMIT)
    value = json.loads(
        content, object_pairs_hook=execution_store.unique_object, parse_constant=execution_store.invalid_constant
    )
    require(isinstance(value, dict), "invalid retained isolation report")
    digest = value.get("digest")
    require(
        digest == planning.digest({key: val for key, val in value.items() if key != "digest"}),
        "retained isolation report digest differs from its content",
    )
    contract = intent["contract"]
    chosen = contract["policy"]
    require(
        value.get("schema_version") == isolation.VERSION
        and value.get("backend") == isolation.BACKEND
        and value.get("argv") == chosen["argv"]
        and value.get("inputs") == contract["inputs"]
        and value.get("runtime") == {key: chosen["runtime"][key] for key in ("rootfs_digest", "python")}
        and value.get("claims")
        == {
            "outputs_trusted": False,
            "evidence_admitted": False,
            "completion_authorized": False,
            "self_authenticating": False,
        }
        and value.get("isolation", {}).get("namespace_terminated") is True,
        "retained isolation bundle differs from the authorized attempt or verified termination",
    )
    artifacts = value.get("artifacts")
    require(
        isinstance(artifacts, list) and len(artifacts) <= isolation.MAX_ARTIFACT_FILES,
        "invalid retained artifact roster",
    )
    seen: set[str] = set()
    total = 0
    for item in [value.get("stdout"), value.get("stderr"), *artifacts]:
        require(isinstance(item, dict) and set(item) == {"path", "size", "digest"}, "invalid retained output reference")
        name = isolation._relative(item["path"])
        require(
            name not in seen and (name in ("stdout.bin", "stderr.bin") or name.startswith("artifacts/")),
            "ambiguous retained output reference",
        )
        seen.add(name)
        # Every parent must be plain; O_NOFOLLOW on the final file alone is insufficient.
        cursor = path
        for part in Path(name).parts[:-1]:
            cursor /= part
            require(stat.S_ISDIR(cursor.lstat().st_mode), "retained output traverses a link or special node")
        bound = isolation.MAX_CAPTURE_BYTES if name in ("stdout.bin", "stderr.bin") else isolation.MAX_ARTIFACT_BYTES
        require(
            type(item["size"]) is int and 0 <= item["size"] <= bound,
            "retained output reference exceeds its file size limit",
        )
        total += item["size"]
        require(
            total <= isolation.MAX_ARTIFACT_BYTES + 2 * isolation.MAX_CAPTURE_BYTES,
            "retained output bytes exceed the bundle limit",
        )
        data = _file(path / name, bound)
        require(
            type(item["size"]) is int
            and len(data) == item["size"]
            and "sha256:" + hashlib.sha256(data).hexdigest() == item["digest"],
            "retained output bytes differ from their trusted collector reference",
        )
    require(
        total <= isolation.MAX_ARTIFACT_BYTES + 2 * isolation.MAX_CAPTURE_BYTES,
        "retained output bytes exceed the bundle limit",
    )
    selected = {item["path"] for item in artifacts}
    missing = [name for name in chosen["expected_outputs"] if "artifacts/" + name not in selected]
    status = value.get("status")
    require(status in {"completed", "candidate_failed", "timed_out", "output_limit"}, "unrecognized isolation outcome")
    if status != "completed":
        return (
            ("failed" if status == "candidate_failed" else str(status)),
            None,
            str(digest),
            "The isolated candidate did not complete successfully; retained outputs remain untrusted.",
        )
    if missing:
        return "output_invalid", None, str(digest), "Required output artifacts are missing: " + ", ".join(missing)
    build = {"kind": "build", "artifacts": artifacts}
    build["id"] = planning.digest({"schema_version": VERSION, "artifacts": artifacts})
    return (
        "succeeded",
        build,
        str(digest),
        "The isolated attempt produced an identified build; evidence and completion remain independently authorized.",
    )


def _terminal(
    intent: Doc, attempt: Doc, outcome: str, reason: str, termination: Any, *, inspect_output: bool = False
) -> Doc:
    authorization = attempt["events"][0]["data"]["authorization"]
    build: Doc | None = None
    isolation_digest: str | None = None
    bounded_outcome = outcome if outcome in {"timed_out", "output_limit", "cancelled"} else None
    bounded_reason = reason
    if inspect_output and os.path.lexists(authorization["output"]):
        try:
            outcome, build, isolation_digest, reason = _bundle(intent, authorization)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            outcome, reason = "output_invalid", "Cannot identify the retained output bundle: " + str(error)
    if bounded_outcome is not None:
        outcome, build, reason = bounded_outcome, None, bounded_reason
    if _cancelled(attempt):
        outcome, build, reason = "cancelled", None, "The authorized attempt was cancelled and its processes terminated."
    receipt = {
        "schema_version": VERSION,
        "execution_id": intent["id"],
        "attempt": attempt["number"],
        "authorization": authorization,
        "task": intent["contract"]["task"]["task"],
        "binding_id": intent["contract"]["binding"]["id"],
        "outcome": outcome,
        "output": authorization["output"],
        "build": build,
        "isolation_report_digest": isolation_digest,
        "termination": termination,
        "reason": reason,
        "claims": copy.deepcopy(CLAIMS),
    }
    receipt["digest"] = planning.digest(receipt)
    return receipt


def apply(
    journal_path: Path,
    ledger: Path,
    documents: Mapping[str, Any],
    state_path: Path,
    request: Request,
    *,
    authorization: Any,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> Doc:
    with ExitStack() as leases:
        return _apply(
            journal_path,
            ledger,
            documents,
            state_path,
            request,
            authorization=authorization,
            lock_timeout=lock_timeout,
            leases=leases,
        )


def _apply(
    journal_path: Path,
    ledger: Path,
    documents: Mapping[str, Any],
    state_path: Path,
    request: Request,
    *,
    authorization: Any,
    lock_timeout: float,
    leases: ExitStack,
) -> Doc:
    """Reserve and run one exact reviewed ordinal; repeats never create another attempt."""
    require(
        isinstance(authorization, dict) and isinstance(authorization.get("storage"), dict),
        "separate reviewed executor authorization is required",
    )
    # Immutable replay intentionally precedes current model, state and binding checks.
    with executor_store.opened(journal_path, write=False, deadline=execution_store.Deadline(lock_timeout)) as journal:
        require(
            authorization.get("storage", {}).get("journal") == str(journal_path.resolve())
            and authorization.get("storage", {}).get("journal_id") == journal.journal_id
            and authorization.get("storage", {}).get("ledger") == str(ledger.resolve()) == journal.ledger,
            "executor authorization selects another canonical journal instance or binding ledger",
        )
        prior = _checked_get(journal, authorization.get("execution_id", ""))
        if prior is not None:
            replay = _retained(prior, authorization)
            if replay is not None:
                return replay
    chosen = validate_policy(request.executor_policy)
    storage = chosen["storage"]
    require(
        str(journal_path.resolve()) == storage["journal"]
        and str(ledger.resolve()) == storage["ledger"]
        and str(state_path.resolve()) == storage["state"],
        "executor paths differ from reviewed policy",
    )
    deadline = execution_store.Deadline(lock_timeout)
    with (
        operation_locks.directories((journal_path.parent, ledger.parent, state_path.parent), deadline=deadline),
        executor_store.opened(journal_path, write=True, deadline=deadline, already_locked=True) as journal,
    ):
        snapshot = state_writer.read(state_path)
        identity, intent, inputs, work, expected = _selection(documents, snapshot, request, journal, chosen, deadline)
        if work is not None:
            replay = _retained(work, authorization)
            if replay is not None:
                return replay
        require(authorization == expected, "executor authorization differs from current attempt, history or state")
        _output_folder(Path(expected["output"]))
        require(
            leases.enter_context(_lease(expected["output"], create=True)) is not None,
            "this executor attempt already has an active owner",
        )
        if work is None:
            work = journal.prepare(intent)
        else:
            intent = work["intent"]
        work = journal.reserve(
            identity,
            {"kind": "reservation", "authorization": authorization, "owner": executor_runtime.process_identity()},
        )
        number = work["attempts"][-1]["number"]

    def is_cancelled() -> bool:
        # Runtime supervision must never wait behind a writer while a candidate is executing.
        try:
            with executor_store.opened(journal_path, write=False, deadline=execution_store.Deadline(0)) as source:
                selected = _checked_get(source, identity)
                require(selected is not None, "executor attempt disappeared")
                assert selected is not None
                return _cancelled(selected["attempts"][number - 1])
        except execution_store.LockTimeoutError:
            return False

    def on_worker(process: Doc) -> None:
        with executor_store.opened(journal_path, write=True, deadline=execution_store.Deadline(lock_timeout)) as target:
            target.append(identity, number, "running", {"kind": "worker", "identity": process})

    def on_namespace(process: Doc) -> None:
        budget = execution_store.Deadline(lock_timeout)
        with (
            operation_locks.directories((journal_path.parent, ledger.parent, state_path.parent), deadline=budget),
            executor_store.opened(journal_path, write=True, deadline=budget, already_locked=True) as target,
        ):
            selected = _checked_get(target, identity)
            assert selected is not None
            attempt = selected["attempts"][number - 1]
            # Retain the known sandbox even when a changed precondition refuses its release.
            target.append(identity, number, "running", {"kind": "namespace", "identity": process})
            require(not _cancelled(attempt), "executor cancellation was requested before candidate release")
            current = state_writer.read(state_path)
            require(
                current.digest == authorization["state_digest"],
                "current state changed before candidate release; review another attempt",
            )
            _current(documents, current, request, ledger, budget)

    try:
        result = executor_runtime.run(
            isolation.parse_policy(chosen["runtime"]),
            chosen["argv"],
            inputs,
            Path(expected["output"]),
            timeout=chosen["timeout_seconds"],
            on_worker=on_worker,
            on_namespace=on_namespace,
            is_cancelled=is_cancelled,
        )
    except (ValueError, OSError, RuntimeError) as error:
        # A callback can fail after the runtime's cleanup. Establish our own retained-process
        # proof before releasing capacity; if proof fails, the per-invocation lease is released
        # on return so explicit recovery can retry even in this same long-lived API process.
        with executor_store.opened(journal_path, write=True, deadline=execution_store.Deadline(lock_timeout)) as target:
            selected = _checked_get(target, identity)
            assert selected is not None
            attempt = selected["attempts"][number - 1]
            processes = [
                event["identity"] for kind in ("worker", "namespace") if (event := _event(attempt, kind)) is not None
            ]
            try:
                termination = executor_runtime.terminate(processes)
            except (OSError, ValueError) as cleanup_error:
                return _finish(
                    _view("apply", selected),
                    "interrupted",
                    "Attempt needs explicit recovery after unverified termination: " + str(cleanup_error),
                )
            receipt = _terminal(
                intent,
                attempt,
                "interrupted",
                "The supervisor stopped: " + str(error),
                termination,
                inspect_output=True,
            )
            selected = target.append(identity, number, receipt["outcome"], {"kind": "receipt", "receipt": receipt})
            return _view("apply", selected)
    with executor_store.opened(journal_path, write=True, deadline=execution_store.Deadline(lock_timeout)) as target:
        selected = _checked_get(target, identity)
        assert selected is not None
        attempt = selected["attempts"][number - 1]
        if attempt["phase"] in TERMINAL:
            return _view("apply", selected)
        status = str(result["status"])
        outcome = {"completed": "output_invalid", "candidate_failed": "failed", "runtime_failed": "failed"}.get(
            status, status
        )
        receipt = _terminal(
            intent,
            attempt,
            outcome,
            result.get("reason") or "The executor attempt ended without an identified successful build.",
            result["termination"],
            inspect_output=True,
        )
        selected = target.append(identity, number, receipt["outcome"], {"kind": "receipt", "receipt": receipt})
        return _view("apply", selected)


def show(journal: Path, execution_id: str, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Inspect immutable work without requiring its old model or any execution authority."""
    with executor_store.opened(journal, write=False, deadline=execution_store.Deadline(lock_timeout)) as source:
        work = _checked_get(source, execution_id)
        require(work is not None, "executor work does not exist")
        assert work is not None
        return _view("show", work)


def _recover(journal_path: Path, execution_id: str, *, cancel_requested: bool, lock_timeout: float) -> Doc:
    with executor_store.opened(journal_path, write=True, deadline=execution_store.Deadline(lock_timeout)) as journal:
        work = _checked_get(journal, execution_id)
        require(work is not None, "executor work does not exist")
        assert work is not None
        if not work["attempts"] or work["attempts"][-1]["phase"] in TERMINAL:
            return _view("cancel" if cancel_requested else "recover", work)
        attempt = work["attempts"][-1]
        if cancel_requested and not _cancelled(attempt):
            work = journal.append(execution_id, attempt["number"], attempt["phase"], {"kind": "cancellation_requested"})
            attempt = work["attempts"][-1]
        output = attempt["events"][0]["data"]["authorization"]["output"]
        with _lease(output, create=False) as lease:
            if lease is None:
                report = _view("cancel" if cancel_requested else "recover", work)
                return _finish(
                    report,
                    "cancellation_requested" if _cancelled(attempt) else "running",
                    "The active supervisor will observe cancellation."
                    if _cancelled(attempt)
                    else "This attempt still has an active supervisor; no recovery launch is permitted.",
                )
            identities = [
                event["identity"] for kind in ("worker", "namespace") if (event := _event(attempt, kind)) is not None
            ]
            # The old invocation no longer holds its lease and cannot launch another worker.
            # A sandbox identity is persisted before candidate release.
            termination = executor_runtime.terminate(identities)
            receipt = _terminal(
                work["intent"],
                attempt,
                "interrupted",
                "The interrupted supervisor's retained processes are terminated; this attempt remains consumed.",
                termination,
                inspect_output=True,
            )
            work = journal.append(
                execution_id, attempt["number"], receipt["outcome"], {"kind": "receipt", "receipt": receipt}
            )
            return _view("cancel" if cancel_requested else "recover", work)


def recover(journal: Path, execution_id: str, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Reconcile or terminate an old attempt; recovery never starts another candidate."""
    return _recover(journal, execution_id, cancel_requested=False, lock_timeout=lock_timeout)


def cancel(journal: Path, execution_id: str, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Persist cancellation for a live supervisor or terminate an abandoned attempt."""
    return _recover(journal, execution_id, cancel_requested=True, lock_timeout=lock_timeout)


def recover_journal(
    journal: Path, *, authorize: bool = False, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT
) -> Doc:
    """Repair an interrupted journal after protected backup, with no model or launch effects."""
    require(authorize is True, "executor journal recovery requires explicit authorization")
    with executor_store.opened(
        journal, write=True, recover=True, deadline=execution_store.Deadline(lock_timeout)
    ) as source:
        report = _report("recover-journal", "")
        report.update(
            journal_id=source.journal_id,
            recovery_backup=str(source.recovery_backup) if source.recovery_backup else None,
        )
        return _finish(report, "recovered", "The executor journal is usable; no candidate was started or retried.")


def as_text(report: Doc) -> str:
    return "\n".join(
        (
            f"Executor: {report['status']}.",
            f"Execution: {report['execution_id']}.",
            report["reason"],
            "Outputs remain untrusted; evidence admission and completion are independent.",
        )
    )

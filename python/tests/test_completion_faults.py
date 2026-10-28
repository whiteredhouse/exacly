"""Completion remains recoverable across real process death and cooperating writers."""

from __future__ import annotations

import copy
import datetime as dt
import json
import multiprocessing
import os
import signal
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pytest
from exacly import admission, completion, completion_store, execution_maintenance, executions, state_writer
from exacly.refresh import Run, refresh
from test_admission import ATTEMPT, READINESS, RUN_ID, Source
from test_applicability import WHEN, A, B
from test_executions import OPENED, TASK, run
from test_reconciliation import make_plan, selected, update_model
from test_refresh import config, requirement, valid_state

Doc = dict[str, Any]
OTHER_TASK = "issues/unrelated-delivery"
NIGHTLY_CASE = "nightly::another_requirement"


class NoNetwork:
    def get_json(self, path: str) -> Doc:
        raise AssertionError("historical receipt replay must not fetch GitHub metadata")

    def download_artifact(self, repository: str, artifact_id: int) -> bytes:
        raise AssertionError("historical receipt replay must not download evidence")


@dataclass
class FaultScenario:
    source: Source
    plan: Doc
    state_path: Path
    ledger: Path
    binding: Doc

    def complete(self, *, source: admission.Source | None = None, **changes: Any) -> Doc:
        fields: Doc = {
            "ledger": self.ledger,
            "binding_id": self.binding["id"],
            "state_path": self.state_path,
        }
        return completion.complete(
            self.source if source is None else source,
            self.plan,
            self.plan["actions"][0]["id"],
            READINESS,
            self.source.policy,
            selected(B),
            RUN_ID,
            ATTEMPT,
            WHEN,
            **{**fields, **changes},
        )

    def registration(self, **changes: Any) -> executions.Registration:
        fields: Doc = {
            "plan": self.plan,
            "action_id": self.plan["actions"][0]["id"],
            "state": json.loads(self.state_path.read_bytes()),
            "task": TASK,
            "opened_at": OPENED,
            "readiness_policy": READINESS,
            "trust_policy": self.source.policy,
            "target": selected(B),
            "date": WHEN,
        }
        return executions.Registration(**{**fields, **changes})

    def recover(self, completion_id: str) -> Doc:
        return completion.recover(self.ledger, completion_id, state_path=self.state_path, lock_timeout=1.0)


def prepare_case(tmp_path: Path, *, two_observers: bool = False, large_receipt: bool = False) -> FaultScenario:
    source = Source()
    if large_receipt:
        # A valid, large policy descriptor makes the receipt transaction spill actual database pages.
        source.policy["name"] += "-" + "a" * (512 * 1024)
    if two_observers:
        first = next(record for record in source.config["records"] if record["id"] == "REQ-1")
        second = requirement("REQ-2", {"workflow": source.workflow_path, "job": "nightly", "cases": [NIGHTLY_CASE]})
        update_model(source, config(copy.deepcopy(first), second))
    documents = {source.model_file: source.config}
    initial = refresh(documents, {}, run(source))
    if two_observers:
        initial = refresh(
            documents,
            {NIGHTLY_CASE: "failed"},
            Run(
                revision=B,
                model_revision=A,
                context="main",
                observed_at=dt.datetime.combine(WHEN, dt.time(9), tzinfo=dt.UTC),
                workflow=source.workflow_path,
                job="nightly",
                job_status="failure",
            ),
            previous=initial,
        )
    initial["pending"] = [
        {"record": "REQ-1", "task": TASK, "opened_at": OPENED, "dimension": "verification"},
        {"record": "REQ-1", "task": OTHER_TASK, "opened_at": OPENED, "dimension": "delivery"},
    ]
    initial["records"]["REQ-1"]["evidence"] = [
        {
            "locator": "observations/retained-runtime.log",
            "kind": "runtime_observation",
            "claim": "An unrelated observation must survive completion",
            "observed_at": WHEN.isoformat(),
            "applies_to": A,
        }
    ]
    state_directory = tmp_path / "observations"
    state_directory.mkdir()
    state_path = state_directory / "main.json"
    # The initial state may have been observed by more than one source already.
    state_path.write_text(json.dumps(initial) + "\n")
    private = tmp_path / "original-operator"
    private.mkdir(mode=0o700)
    original_ledger = private / "ledger.sqlite3"
    case = FaultScenario(source, make_plan(source), state_path, original_ledger, {})
    case.binding = executions.register(original_ledger, case.registration())
    destination = tmp_path / "upgraded-operator"
    execution_maintenance.upgrade(original_ledger, destination)
    case.ledger = destination / "upgraded" / "ledger.sqlite3"
    assert case.ledger.is_file()
    return case


def _pause(control: Connection, completion_id: str) -> None:
    control.send(("paused", completion_id))
    control.recv()


def interrupted_completion(case: FaultScenario, phase: str, control: Connection) -> None:
    """The parent kills only after a real durability boundary has been reached."""
    original_prepare = completion_store.Journal.prepare
    original_finalize = completion_store.Journal.finalize
    original_replace = os.replace
    prepared_id: str | None = None

    def prepare(*args: Any, **kwargs: Any) -> Any:
        nonlocal prepared_id
        value = original_prepare(*args, **kwargs)
        prepared_id = value["intent"]["id"]
        assert prepared_id is not None
        if phase == "after_prepare":
            _pause(control, prepared_id)
        return value

    def finalize(*args: Any, **kwargs: Any) -> Any:
        assert prepared_id is not None
        if phase == "before_finalize":
            _pause(control, prepared_id)
        value = original_finalize(*args, **kwargs)
        if phase == "after_finalize":
            _pause(control, prepared_id)
        return value

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        original_replace(source, destination, *args, **kwargs)
        if phase == "after_replace" and os.fspath(destination) in (case.state_path.name, str(case.state_path)):
            assert prepared_id is not None
            _pause(control, prepared_id)

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(completion_store.Journal, "prepare", prepare)
            patched.setattr(completion_store.Journal, "finalize", finalize)
            patched.setattr(os, "replace", replace)
            control.send(("unexpected-result", case.complete()))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@contextmanager
def paused_at(case: FaultScenario, phase: str) -> Iterator[tuple[BaseProcess, str]]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_completion, args=(case, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), f"completion did not reach {phase}"
        message = parent.recv()
        assert message[0] == "paused", message
        yield process, message[1]
    finally:
        if process.is_alive():
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        parent.close()
        assert process.exitcode == -signal.SIGKILL


def kill_at(case: FaultScenario, phase: str) -> str:
    with paused_at(case, phase) as (_, identity):
        return identity


def remaining_work(state: Doc, previous: Doc) -> None:
    assert state["pending"] == [entry for entry in previous["pending"] if entry["task"] == OTHER_TASK]
    assert state["records"]["REQ-1"]["evidence"] == previous["records"]["REQ-1"]["evidence"]
    assert state["lineage"] == previous["lineage"]


def test_killed_after_preparation_aborts_without_publication_then_requires_fresh_admission(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    before_bytes = case.state_path.read_bytes()
    before = json.loads(before_bytes)
    identity = kill_at(case, "after_prepare")
    assert case.state_path.read_bytes() == before_bytes
    # The dead writer released its locks, but the prepared operation still prevents supersession.
    with pytest.raises(ValueError):
        executions.register(case.ledger, case.registration(supersedes=case.binding["id"]), lock_timeout=0.0)
    report = case.recover(identity)
    assert report["status"] == "aborted", report
    assert case.state_path.read_bytes() == before_bytes
    assert case.source.calls == [] and case.source.downloads == []
    completed = case.complete()
    assert completed["status"] == "completed", completed
    assert len(case.source.downloads) == 1, "an aborted intent cannot substitute for a new live admission"
    after = valid_state(json.loads(case.state_path.read_bytes()))
    assert after["serial"] == before["serial"] + 1
    remaining_work(after, before)


@pytest.mark.parametrize("phase", ["after_replace", "before_finalize"])
def test_killed_after_publication_recovers_the_original_intent_without_reapplying(tmp_path: Path, phase: str) -> None:
    case = prepare_case(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    identity = kill_at(case, phase)
    published_bytes = case.state_path.read_bytes()
    published = valid_state(json.loads(published_bytes))
    assert published["serial"] == before["serial"] + 1
    remaining_work(published, before)
    recovered = case.recover(identity)
    assert recovered["status"] == "completed", recovered
    assert case.state_path.read_bytes() == published_bytes
    assert case.source.calls == [] and case.source.downloads == []
    repeated = case.complete(source=NoNetwork())
    assert repeated["status"] == "completed", repeated
    assert repeated["receipt"] == recovered["receipt"]
    assert case.recover(identity)["receipt"] == recovered["receipt"]
    assert case.state_path.read_bytes() == published_bytes


def test_killed_after_final_receipt_replays_before_http_or_pending_matching(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    identity = kill_at(case, "after_finalize")
    published_bytes = case.state_path.read_bytes()
    report = case.complete(source=NoNetwork())
    assert report["status"] == "completed", report
    assert report["completion_id"] == identity
    assert case.state_path.read_bytes() == published_bytes
    remaining_work(valid_state(json.loads(published_bytes)), before)
    with pytest.raises(ValueError):
        executions.register(case.ledger, case.registration(state=before), lock_timeout=0.0)


def interrupted_receipt_commit(case: FaultScenario, control: Connection) -> None:
    """Pause with receipt, phase and watermark updates visible only inside the uncommitted transaction."""
    original_connect = sqlite3.connect

    class PausedReceipt(sqlite3.Connection):
        def commit(self) -> None:
            row = self.execute(
                "SELECT i.id FROM completion_intents i JOIN completion_receipts r ON r.intent_id=i.id "
                "WHERE i.phase='committed'"
            ).fetchone()
            if row is not None:
                control.send(("ready", row[0]))
                control.recv()
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs, factory=PausedReceipt)
        connection.execute("PRAGMA cache_size=1")
        connection.execute("PRAGMA cache_spill=1")
        return connection

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(sqlite3, "connect", connect)
            control.send(("unexpected-result", case.complete()))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            control.send(("error", repr(error)))
        raise
    finally:
        control.close()


def test_sigkill_during_receipt_transaction_recovers_copy_then_finalizes_published_state(tmp_path: Path) -> None:
    case = prepare_case(tmp_path, large_receipt=True)
    before = json.loads(case.state_path.read_bytes())
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_receipt_commit, args=(case, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), "receipt finalization did not reach its uncommitted SQLite boundary"
        message = parent.recv()
        assert message[0] == "ready", message
        identity = message[1]
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()

    published_bytes = case.state_path.read_bytes()
    published = valid_state(json.loads(published_bytes))
    assert published["serial"] == before["serial"] + 1
    remaining_work(published, before)
    original_ledger = case.ledger
    journal = Path(str(original_ledger) + "-journal")
    assert journal.read_bytes().startswith(bytes.fromhex("d9d505f920a163d7")), "a real hot rollback journal is required"
    residue = {entry.name: entry.read_bytes() for entry in original_ledger.parent.iterdir() if entry.is_file()}
    with pytest.raises(ValueError, match=r"journal|sidecars"):
        completion.show(original_ledger, identity)
    assert execution_maintenance.inspect(original_ledger)["status"] == "recovery_required"

    destination = tmp_path / "receipt-recovery"
    copied = execution_maintenance.recover(original_ledger, destination)
    assert copied["status"] == "recovered", copied
    assert copied["store"]["user_version"] == 2
    assert (destination / "raw" / "ledger.sqlite3").read_bytes() == residue[original_ledger.name]
    assert (destination / "raw" / "ledger.sqlite3-journal").read_bytes() == residue[journal.name]

    # The operator explicitly cuts over to the recovered copy. SQLite rolls back only its unfinished
    # receipt transaction, retaining the original prepared proof for the state already published.
    case.ledger = destination / "recovered" / "ledger.sqlite3"
    prepared = completion.show(case.ledger, identity)
    assert prepared["status"] == "prepared" and prepared["receipt"] is None, prepared
    assert execution_maintenance.inspect(case.ledger)["status"] == "clean"
    recovered = case.recover(identity)
    assert recovered["status"] == "completed", recovered
    assert recovered["completion_id"] == identity
    assert case.source.calls == [] and case.source.downloads == []
    repeated = case.complete(source=NoNetwork())
    assert repeated["status"] == "completed" and repeated["receipt"] == recovered["receipt"]
    assert case.recover(identity)["receipt"] == recovered["receipt"]
    assert case.state_path.read_bytes() == published_bytes
    assert {entry.name: entry.read_bytes() for entry in original_ledger.parent.iterdir() if entry.is_file()} == residue


def test_recovery_of_a_third_state_preserves_the_conflict_and_prepared_intent(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    identity = kill_at(case, "after_prepare")
    snapshot = state_writer.read(case.state_path)
    changed = snapshot.document
    assert changed is not None
    changed["serial"] += 1
    changed["x-unrelated-observation"] = "new state observed after the interrupted operation"
    state_writer.publish(snapshot, changed)
    conflict_bytes = case.state_path.read_bytes()
    first = case.recover(identity)
    assert first["status"] == "conflict", first
    assert case.state_path.read_bytes() == conflict_bytes
    assert case.recover(identity)["status"] == "conflict"
    assert case.state_path.read_bytes() == conflict_bytes
    with pytest.raises(ValueError):
        executions.register(case.ledger, case.registration(supersedes=case.binding["id"]), lock_timeout=0.0)


def test_completion_lock_timeout_is_bounded_while_a_prepared_writer_is_alive(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    before = case.state_path.read_bytes()
    with paused_at(case, "after_prepare"):
        started = time.monotonic()
        with pytest.raises(ValueError, match="timed out"):
            case.complete(lock_timeout=0.0)
        assert time.monotonic() - started < 1.0
        assert case.state_path.read_bytes() == before


def admission_paused_completion(case: FaultScenario, control: Connection) -> None:
    original_download = case.source.download_artifact

    def download(repository: str, artifact_id: int) -> bytes:
        content = original_download(repository, artifact_id)
        control.send("downloaded")
        assert control.recv() == "resume"
        return content

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(case.source, "download_artifact", download)
            control.send(("result", case.complete()))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@contextmanager
def waiting_for_admission(case: FaultScenario) -> Iterator[tuple[BaseProcess, Connection]]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=admission_paused_completion, args=(case, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "completion did not reach fresh evidence download"
        assert parent.recv() == "downloaded"
        yield process, parent
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)
        parent.close()


def finished(process: BaseProcess, control: Connection) -> Doc:
    assert control.poll(15), "completion did not finish after evidence admission resumed"
    message = control.recv()
    assert message[0] == "result", message
    process.join(timeout=10)
    assert process.exitcode == 0
    result: Doc = message[1]
    return result


def test_concurrent_completion_callers_share_one_receipt_and_one_state_advance(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    with waiting_for_admission(case) as (first, first_control), waiting_for_admission(case) as (second, second_control):
        first_control.send("resume")
        second_control.send("resume")
        first_result = finished(first, first_control)
        second_result = finished(second, second_control)
    assert first_result["status"] == second_result["status"] == "completed"
    assert first_result["receipt"] == second_result["receipt"]
    after = valid_state(json.loads(case.state_path.read_bytes()))
    assert after["serial"] == before["serial"] + 1
    remaining_work(after, before)


def test_supersession_during_admission_prevents_publication_of_the_old_binding(tmp_path: Path) -> None:
    case = prepare_case(tmp_path)
    before = case.state_path.read_bytes()
    with waiting_for_admission(case) as (process, control):
        replacement = executions.register(case.ledger, case.registration(supersedes=case.binding["id"]))
        control.send("resume")
        result = finished(process, control)
    assert result["status"] == "stale", result
    assert case.state_path.read_bytes() == before
    assert executions.get(case.ledger, replacement["id"])["active"] is True
    assert executions.get(case.ledger, case.binding["id"])["active"] is False


def test_refresh_during_admission_is_retained_by_the_completion_merge(tmp_path: Path) -> None:
    case = prepare_case(tmp_path, two_observers=True)
    with waiting_for_admission(case) as (process, control):
        snapshot = state_writer.read(case.state_path)
        assert snapshot.document is not None
        observed = refresh(
            {case.source.model_file: case.source.config},
            {NIGHTLY_CASE: "passed"},
            Run(
                revision=B,
                model_revision=A,
                context="main",
                observed_at=dt.datetime.combine(WHEN, dt.time(9, 10), tzinfo=dt.UTC),
                workflow=case.source.workflow_path,
                job="nightly",
                job_status="success",
            ),
            previous=snapshot.document,
        )
        state_writer.publish(snapshot, observed)
        control.send("resume")
        result = finished(process, control)
    assert result["status"] == "completed", result
    final = valid_state(json.loads(case.state_path.read_bytes()))
    assert final["serial"] == observed["serial"] + 1
    assert final["records"]["REQ-2"] == observed["records"]["REQ-2"]
    remaining_work(final, observed)

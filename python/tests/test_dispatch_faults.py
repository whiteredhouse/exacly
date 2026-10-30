"""Real dispatcher process death cannot turn uncertain external effects into another POST."""

from __future__ import annotations

import copy
import fcntl
import json
import multiprocessing
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import (
    dispatch,
    dispatch_store,
    execution_maintenance,
    execution_store,
    executions,
    history_maintenance,
    planning,
    state_writer,
)
from ledger_store_fixtures import initialize_legacy_ledger

Doc = dict[str, Any]


@dataclass
class DurableRemote:
    """A controlled external service whose task survives death of the requesting process."""

    path: Path
    pause_after_create: Connection | None = None

    @property
    def tasks(self) -> list[Doc]:
        return list(json.loads(self.path.read_text())["tasks"])

    @property
    def creates(self) -> int:
        return int(json.loads(self.path.read_text())["creates"])

    def repository(self, repository: Doc) -> Doc:
        return copy.deepcopy(repository)

    def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]:
        return [task for task in self.tasks if marker in task["body"]]

    def create(self, repository: Doc, effect: Doc) -> Doc:
        # The coordinator is responsible for serializing concurrent create requests. The remote
        # fixture deliberately offers no idempotency key or duplicate prevention of its own.
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            task = self._record_create(repository, effect)
        finally:
            os.close(descriptor)
        if self.pause_after_create is not None:
            pause(self.pause_after_create, "after_remote_create")
        return task

    def _record_create(self, repository: Doc, effect: Doc) -> Doc:
        # Serialize the fake service's numbering so two erroneous POSTs cannot be hidden by a
        # lost update to its storage. Every call still creates a distinct task.
        data = json.loads(self.path.read_text())
        number = len(data["tasks"]) + 1
        task = {
            "repository": copy.deepcopy(repository),
            "id": 10_000 + number,
            "number": number,
            "url": f"https://github.com/{repository['full_name']}/issues/{number}",
            "created_at": "2026-10-30T08:30:00Z",
            "title": effect["title"],
            "body": effect["body"],
        }
        data["tasks"].append(task)
        data["creates"] += 1
        temporary = self.path.with_suffix(".new")
        with temporary.open("w") as stream:
            stream.write(json.dumps(data))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return task


def remote_for(tmp_path: Path) -> DurableRemote:
    path = tmp_path / "external-tasks.json"
    path.write_text(json.dumps({"creates": 0, "tasks": []}))
    return DurableRemote(path)


def prepare_fault_case(case: Scenario, tmp_path: Path, version: int = 2) -> Scenario:
    snapshot = state_writer.read(case.state_path)
    before = snapshot.document
    assert before is not None
    before["serial"] += 1
    before.setdefault("pending", []).append(
        {
            "record": "REQ-1",
            "task": "https://github.com/acme/demo/issues/other-delivery",
            "opened_at": "2026-10-26T08:00:00Z",
            "dimension": "delivery",
        }
    )
    before["x-other-work"] = {"observation": "preserve this unrelated operator progress"}
    state_writer.publish(snapshot, before)
    if version == 4:
        # Exercise the supported operational indexed ledger without depending on an implicit
        # migration or changing the separate journal's format.
        if not case.ledger.exists():
            initialize_legacy_ledger(case.ledger)
            upgraded = tmp_path / "completion-ready"
            execution_maintenance.upgrade(case.ledger, upgraded)
            case.ledger = upgraded / "upgraded" / "ledger.sqlite3"
        indexed = tmp_path / "indexed-operator"
        history_maintenance.activate(case.ledger, indexed)
        case.ledger = indexed / "operational" / "ledger.sqlite3"
        # The test operator explicitly selects the new canonical copy before approving dispatch.
        case.policy["storage"]["ledger"] = str(case.ledger)
        case.request = replace(case.request, dispatch_policy=copy.deepcopy(case.policy))
    return case


def apply(case: Scenario, client: DurableRemote, **changes: Any) -> Doc:
    return dispatch.apply(
        case.journal,
        case.ledger,
        case.documents,
        case.state_path,
        case.request,
        authorization=case.authorization,
        client=client,
        **changes,
    )


def pause(control: Connection, phase: str) -> None:
    control.send(("paused", phase))
    control.recv()


def interrupted_dispatch(case: Scenario, client: DurableRemote, phase: str, control: Connection) -> None:
    append = dispatch_store.Journal.append
    publish = state_writer.publish_locked
    put = execution_store.put_locked
    transaction = dispatch_store.Journal._transaction

    @contextmanager
    def transaction_before_commit(journal: dispatch_store.Journal) -> Iterator[None]:
        if phase == "during_final_receipt":
            journal.connection.execute("PRAGMA cache_size=1")
            journal.connection.execute("PRAGMA cache_spill=1")
        with transaction(journal):
            yield
            current = journal.connection.execute(
                "SELECT phase FROM dispatch_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if phase == "during_final_receipt" and current == ("registered",):
                pause(control, phase)

    def append_then_pause(*args: Any, **kwargs: Any) -> Any:
        value = append(*args, **kwargs)
        stage = args[2] if len(args) > 2 else kwargs.get("phase")
        if phase == f"after_{stage}":
            pause(control, phase)
        return value

    def publish_then_pause(*args: Any, **kwargs: Any) -> None:
        publish(*args, **kwargs)
        if phase == "after_state_publish":
            pause(control, phase)

    def put_then_pause(*args: Any, **kwargs: Any) -> Any:
        value = put(*args, **kwargs)
        if phase == "after_binding_commit":
            pause(control, phase)
        return value

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(dispatch_store.Journal, "append", append_then_pause)
            patched.setattr(dispatch_store.Journal, "_transaction", transaction_before_commit)
            patched.setattr(state_writer, "publish_locked", publish_then_pause)
            patched.setattr(execution_store, "put_locked", put_then_pause)
            if phase == "after_remote_create":
                client.pause_after_create = control
            control.send(("unexpected-result", apply(case, client)))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@contextmanager
def paused_at(case: Scenario, client: DurableRemote, phase: str) -> Iterator[BaseProcess]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_dispatch, args=(case, client, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), f"dispatcher did not reach {phase}"
        message = parent.recv()
        assert message == ("paused", phase), message
        yield process
    finally:
        if process.is_alive():
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        parent.close()
        assert process.exitcode == -signal.SIGKILL


def assert_registered(case: Scenario, report: Doc, remote: DurableRemote, before: Doc) -> None:
    assert report["status"] == "registered", report
    assert remote.creates == 1 and len(remote.tasks) == 1
    after = json.loads(case.state_path.read_bytes())
    pending = [entry for entry in after.get("pending", []) if entry["task"] == remote.tasks[0]["url"]]
    assert len(pending) == 1 and pending[0]["dimension"] == "verification"
    assert after["records"] == before["records"]
    assert after["lineage"] == before["lineage"]
    assert after["serial"] == before["serial"] + 1
    unrelated = [entry for entry in after.get("pending", []) if entry["task"] != remote.tasks[0]["url"]]
    assert unrelated == before.get("pending", [])
    assert after["x-other-work"] == before["x-other-work"]
    receipt = report["receipt"]
    envelope = executions.get(case.ledger, receipt["binding_id"])
    binding = envelope["binding"]
    assert envelope["active"] is True and envelope["superseded_by"] is None
    assert binding["contract"]["pending"]["task"] == remote.tasks[0]["url"]
    assert binding["contract"]["pending"]["entry_digest"] == planning.digest(pending[0])
    assert binding["state_snapshot"]["serial"] == after["serial"]
    assert binding["state_snapshot"]["digest"] == planning.digest(after)
    assert receipt["state"]["digest"] == state_writer.read(case.state_path).digest
    assert receipt["claims"]["task_completion_authorized"] is False
    assert receipt["claims"]["verification_established"] is False


@pytest.mark.parametrize(
    "phase",
    [
        "after_remote_create",
        "after_remote_confirmed",
        "after_registration_prepared",
        "after_state_publish",
        "after_binding_commit",
        "after_registered",
    ],
)
@pytest.mark.parametrize("version", [2, 4])
def test_sigkill_after_external_or_local_durability_recovers_without_duplicate_create(
    dispatch_scenario: Scenario, tmp_path: Path, phase: str, version: int
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path, version)
    remote = remote_for(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    with paused_at(case, remote, phase):
        pass
    first = apply(case, remote)
    assert_registered(case, first, remote, before)
    after = case.state_path.read_bytes()
    second = apply(case, remote)
    assert second["status"] == "registered", second
    assert second["receipt"] == first["receipt"]
    assert case.state_path.read_bytes() == after
    assert remote.creates == 1


def test_sigkill_after_create_authorization_blocks_retry_when_discovery_finds_nothing(
    dispatch_scenario: Scenario, tmp_path: Path
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path)
    remote = remote_for(tmp_path)
    before = case.state_path.read_bytes()
    with paused_at(case, remote, "after_creating"):
        pass
    assert remote.creates == 0
    for _ in range(2):
        report = apply(case, remote)
        assert report["status"] in {"ambiguous", "blocked"}, report
        assert remote.creates == 0
        assert case.state_path.read_bytes() == before


def test_sigkill_during_final_receipt_requires_explicit_journal_recovery(
    dispatch_scenario: Scenario, tmp_path: Path
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path)
    remote = remote_for(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    with paused_at(case, remote, "during_final_receipt"):
        pass
    published = case.state_path.read_bytes()
    rollback = Path(str(case.journal) + "-journal")
    assert rollback.read_bytes().startswith(bytes.fromhex("d9d505f920a163d7")), (
        "a real hot rollback journal is required"
    )
    raw_database, raw_rollback = case.journal.read_bytes(), rollback.read_bytes()
    with pytest.raises(ValueError, match=r"recovery|sidecar|rollback"):
        apply(case, remote)
    assert (case.journal.read_bytes(), rollback.read_bytes()) == (raw_database, raw_rollback)
    recovered = apply(case, remote, recover=True)
    assert_registered(case, recovered, remote, before)
    assert case.state_path.read_bytes() == published
    backups = list(case.journal.parent.glob(".dispatch-recovery-*"))
    assert len(backups) == 1
    assert (backups[0] / "database.sqlite3").read_bytes() == raw_database
    assert (backups[0] / "database.sqlite3-journal").read_bytes() == raw_rollback
    assert (backups[0] / "receipt.json").is_file()
    assert not rollback.exists()
    assert apply(case, remote)["receipt"] == recovered["receipt"]
    assert remote.creates == 1


@pytest.mark.parametrize("phase", ["after_state_publish", "after_binding_commit"])
def test_recovery_preserves_unrelated_state_advancement_and_binding_history(
    dispatch_scenario: Scenario, tmp_path: Path, phase: str
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path)
    remote = remote_for(tmp_path)
    identity = case.authorization["dispatch_id"]
    with paused_at(case, remote, phase):
        pass
    retained = dispatch.show(case.journal, identity)["record"]["events"]
    snapshot = state_writer.read(case.state_path)
    changed = snapshot.document
    assert changed is not None
    changed["serial"] += 1
    changed["x-observer-progress"] = {"observed": "unrelated work completed after dispatch publication"}
    state_writer.publish(snapshot, changed)
    advanced = case.state_path.read_bytes()

    report = apply(case, remote)
    assert report["status"] == "registered", report
    assert case.state_path.read_bytes() == advanced
    assert remote.creates == 1 and len(remote.tasks) == 1
    assert len([entry for entry in changed["pending"] if entry["task"] == remote.tasks[0]["url"]]) == 1
    envelope = executions.get(case.ledger, report["receipt"]["binding_id"])
    assert envelope["active"] is True
    assert envelope["state_watermark"] == {"serial": changed["serial"], "digest": planning.digest(changed)}
    assert report["receipt"]["state"]["digest"] == state_writer.read(case.state_path).digest
    recovered = dispatch.show(case.journal, identity)["record"]["events"]
    assert recovered[: len(retained)] == retained, (
        "recovery must append history rather than replacing the original attempt"
    )
    assert len(recovered) > len(retained)


def concurrent_dispatch(case: Scenario, remote: DurableRemote, ready: Connection) -> None:
    try:
        ready.send("ready")
        ready.recv()
        ready.send(("result", apply(case, remote)))
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError):
            ready.send(("error", repr(error)))
        raise
    finally:
        ready.close()


def test_concurrent_workers_recover_one_task_one_pending_entry_and_one_binding(
    dispatch_scenario: Scenario, tmp_path: Path
) -> None:
    case = prepare_fault_case(dispatch_scenario, tmp_path)
    remote = remote_for(tmp_path)
    before = json.loads(case.state_path.read_bytes())
    context = multiprocessing.get_context("fork")
    workers: list[tuple[BaseProcess, Connection]] = []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process: BaseProcess = context.Process(target=concurrent_dispatch, args=(case, remote, child))
            process.start()
            child.close()
            workers.append((process, parent))
        for _, control in workers:
            assert control.poll(15) and control.recv() == "ready"
        for _, control in workers:
            control.send("start")
        reports = []
        for process, control in workers:
            assert control.poll(15), "dispatcher did not finish"
            message = control.recv()
            assert message[0] == "result", message
            reports.append(message[1])
            process.join(timeout=10)
            assert process.exitcode == 0
        assert_registered(case, reports[0], remote, before)
        assert reports[1]["status"] == "registered", reports[1]
        assert reports[0]["receipt"] == reports[1]["receipt"]
    finally:
        for process, control in workers:
            if process.is_alive():
                process.kill()
            process.join(timeout=10)
            control.close()

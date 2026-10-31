"""Failure and recovery against controlled transports and actual local durable journals."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import remote_publication as remote
from handoff_fixtures import handoff_case as handoff_case
from test_remote_publication import Case, ControlledTransport
from test_remote_publication import remote_case as remote_case

Doc = dict[str, Any]


@pytest.mark.parametrize("failure", ["before", "after"])
def test_ambiguous_send_never_retries_even_when_discovery_reports_old_or_absent_ref(
    remote_case: Case, failure: str
) -> None:
    case = remote_case
    case.transport.failure = failure
    result = case.apply()
    assert result["status"] == "ambiguous"
    case.transport.failure = None
    for _ in range(3):
        assert case.apply()["status"] == "ambiguous"
        assert case.recover()["status"] == ("published" if failure == "after" else "ambiguous")
        if failure == "after":
            break
    assert len(case.transport.sends) == 1


def test_failed_preflight_retains_prepared_intent_and_explicit_apply_rechecks_before_first_send(
    remote_case: Case,
) -> None:
    case = remote_case
    case.transport.failure = "inspect"
    assert case.apply()["status"] == "prepared"
    case.transport.failure = None
    assert case.recover()["status"] == "prepared"
    assert case.transport.sends == []
    assert case.apply()["status"] == "published"
    assert len(case.transport.sends) == 1


def test_invalid_inspection_is_not_salvaged_as_publication(remote_case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    case = remote_case
    case.transport.failure = "after"
    assert case.apply()["status"] == "ambiguous"
    original = case.transport.inspect

    def wrong(operation: Doc, *, timeout: float) -> Doc:
        value = original(operation, timeout=timeout)
        value["repository_id"] = "999"
        return value

    monkeypatch.setattr(case.transport, "inspect", wrong)
    result = case.recover()
    assert result["status"] == "ambiguous"
    assert result["claims"]["remote_published"] is False
    assert len(case.transport.sends) == 1


def test_conflict_remains_inspectable_when_remote_inspection_is_unavailable(remote_case: Case) -> None:
    case = remote_case
    case.transport.revision = "d" * 40
    assert case.apply()["status"] == "conflict"
    case.transport.failure = "inspect"
    assert case.recover()["status"] == "conflict"
    assert (
        remote.show(case.request.journal, case.request.journal_id, case.authorization["operation_id"])["status"]
        == "conflict"
    )


def test_preflight_conflict_cannot_be_salvaged_into_publication_without_any_send(remote_case: Case) -> None:
    case = remote_case
    case.transport.revision = "d" * 40
    assert case.apply()["status"] == "conflict"
    case.transport.revision = case.authorization["commit"]
    case.transport.commit_present = True
    case.transport.operation_id = case.authorization["operation_id"]
    assert case.recover()["status"] == "conflict"
    assert case.transport.sends == []


def test_sent_conflict_remains_when_another_actor_returns_ref_to_expected_old_value(remote_case: Case) -> None:
    case = remote_case
    case.transport.failure = "after"
    assert case.apply()["status"] == "ambiguous"
    case.transport.failure = None
    case.transport.revision = "d" * 40
    assert case.recover()["status"] == "conflict"
    case.transport.revision = case.request.expected_revision
    assert case.recover()["status"] == "conflict"
    assert len(case.transport.sends) == 1


@pytest.mark.parametrize("damage", ["config", "replacement"])
def test_source_repository_drift_blocks_adapter_send(remote_case: Case, tmp_path: Path, damage: str) -> None:
    case = remote_case
    if damage == "config":
        (case.handoff.repository / "config").write_text("[include]\npath=/unapproved\n")
    else:
        old = tmp_path / "previous-repository"
        case.handoff.repository.rename(old)
        from exacly import handoff_git

        handoff_git.initialize_repository(case.handoff.repository)
    with pytest.raises(ValueError, match=r"(config changed|repository changed)"):
        case.apply()
    assert case.transport.sends == []


def _crash_send(request: remote.Request, authorization: Doc, remote_state: Path) -> None:
    class CrashingTransport(ControlledTransport):
        def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
            value = {
                "repository_id": operation["request"]["repository_id"],
                "ref": operation["request"]["ref"],
                "revision": operation["authorization"]["commit"],
                "commit_present": True,
                "operation_id": operation["id"],
            }
            with remote_state.open("w") as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
            os._exit(73)

    remote.apply(request, authorization=authorization, transport=CrashingTransport())


def test_actual_process_loss_after_remote_acceptance_recovers_by_discovery_only(
    remote_case: Case, tmp_path: Path
) -> None:
    case = remote_case
    state = tmp_path / "controlled-remote.json"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_send, args=(case.request, case.authorization, state)
    )
    process.start()
    process.join(10)
    assert process.exitcode == 73
    assert (
        remote.show(case.request.journal, case.request.journal_id, case.authorization["operation_id"])["status"]
        == "sent"
    )

    class DiscoveringTransport(ControlledTransport):
        def inspect(self, operation: Doc, *, timeout: float) -> Doc:
            value: Doc = json.loads(state.read_text())
            return value

        def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
            pytest.fail("recovery must never send a second effect")

    result = remote.recover(
        case.request.journal,
        case.request.journal_id,
        case.authorization["operation_id"],
        transport=DiscoveringTransport(),
    )
    assert result["status"] == "published"
    assert [event["phase"] for event in result["history"]] == ["prepared", "sent", "published"]


def _concurrent_apply(request: remote.Request, authorization: Doc, state: Path, barrier: Any, replies: Any) -> None:
    class SharedTransport(ControlledTransport):
        def inspect(self, operation: Doc, *, timeout: float) -> Doc:
            if state.exists():
                value: Doc = json.loads(state.read_text())
                return value
            return super().inspect(operation, timeout=timeout)

        def publish(self, operation: Doc, repository: Path, *, timeout: float) -> None:
            # Only the durable local coordination lock permits entry; a second send is a test failure.
            with state.open("x") as output:
                json.dump(
                    {
                        "repository_id": operation["request"]["repository_id"],
                        "ref": operation["request"]["ref"],
                        "revision": operation["authorization"]["commit"],
                        "commit_present": True,
                        "operation_id": operation["id"],
                    },
                    output,
                )
                output.flush()
                os.fsync(output.fileno())

    barrier.wait(timeout=10)
    result = remote.apply(request, authorization=authorization, transport=SharedTransport())
    replies.put(result["status"])


def test_concurrent_duplicate_events_coalesce_to_one_send_and_one_retained_intent(
    remote_case: Case, tmp_path: Path
) -> None:
    case = remote_case
    context = multiprocessing.get_context("fork")
    barrier, replies = context.Barrier(3), context.Queue()
    state = tmp_path / "one-controlled-send.json"
    processes = [
        context.Process(target=_concurrent_apply, args=(case.request, case.authorization, state, barrier, replies))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert [replies.get(timeout=3) for _ in processes] == ["published"] * 3
    retained = remote.show(case.request.journal, case.request.journal_id, case.authorization["operation_id"])
    assert [event["phase"] for event in retained["history"]] == ["prepared", "sent", "published"]


@pytest.mark.parametrize("damage", ["schema", "application", "digest", "sidecar", "mode", "symlink"])
def test_changed_storage_fails_closed_without_transport_effect(remote_case: Case, damage: str, tmp_path: Path) -> None:
    case = remote_case
    assert case.apply()["status"] == "published"
    path = case.request.journal
    if damage in ("schema", "application", "digest"):
        connection = sqlite3.connect(path)
        try:
            if damage == "schema":
                connection.execute("CREATE TABLE extra (value TEXT)")
            elif damage == "application":
                connection.execute("PRAGMA application_id=7")
            else:
                connection.execute("UPDATE operations SET digest='changed'")
            connection.commit()
        finally:
            connection.close()
    elif damage == "sidecar":
        Path(str(path) + "-journal").write_bytes(b"ambiguous rollback")
    elif damage == "mode":
        path.chmod(0o644)
    else:
        other = tmp_path / "moved.sqlite3"
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        case.apply()
    assert len(case.transport.sends) == 1


def test_initialize_never_replaces_existing_journal(remote_case: Case) -> None:
    before = remote_case.request.journal.read_bytes()
    with pytest.raises(FileExistsError):
        remote.initialize(remote_case.request.journal)
    assert remote_case.request.journal.read_bytes() == before


def _retained_phase(case: Case) -> str | None:
    connection = sqlite3.connect(case.request.journal.as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT document FROM operations WHERE id=?", (case.authorization["operation_id"],)
        ).fetchone()
        if row is None:
            return None
        value: Doc = json.loads(row[0])
        phase: str = value["events"][-1]["phase"]
        return phase
    finally:
        connection.close()


@pytest.mark.parametrize("phase", ["prepared", "sent", "published"])
def test_directory_sync_failure_blocks_effect_or_durable_success_and_requires_replay_barrier(
    remote_case: Case, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    case = remote_case
    original = os.fsync
    parent = case.request.journal.parent.stat()

    def failing_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino) and _retained_phase(case) == phase:
            raise OSError("injected remote parent directory sync failure")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failing_sync)
        with pytest.raises(OSError, match="directory sync failure"):
            case.apply()
        assert _retained_phase(case) == phase
        assert len(case.transport.sends) == (1 if phase == "published" else 0)
        # A committed row is insufficient when its directory barrier has not succeeded. All public
        # historical paths must retry that barrier before exposing retained progress or success.
        with pytest.raises(OSError, match="directory sync failure"):
            case.apply()
        with pytest.raises(OSError, match="directory sync failure"):
            case.recover()
        with pytest.raises(OSError, match="directory sync failure"):
            remote.show(case.request.journal, case.request.journal_id, case.authorization["operation_id"])
        assert len(case.transport.sends) == (1 if phase == "published" else 0)

    result = case.apply()
    if phase == "prepared":
        assert result["status"] == "published" and len(case.transport.sends) == 1
    elif phase == "sent":
        assert result["status"] == "sent" and len(case.transport.sends) == 0
        assert case.recover()["status"] == "ambiguous"
        assert len(case.transport.sends) == 0
    else:
        assert result["status"] == "published" and len(case.transport.sends) == 1


def test_directory_sync_precedes_transport_and_receipt_release_and_historical_replay(
    remote_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = remote_case
    original = os.fsync
    parent = case.request.journal.parent.stat()
    synchronized: list[str | None] = []

    def record_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        original(descriptor)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            synchronized.append(_retained_phase(case))

    def before_send(operation: Doc) -> None:
        assert synchronized[-1] == "sent"

    monkeypatch.setattr(os, "fsync", record_sync)
    case.transport.before_send = before_send
    assert case.apply()["status"] == "published"
    assert synchronized == [None, None, "prepared", "sent", "published"]
    synchronized.clear()
    assert case.apply()["status"] == "published"
    assert synchronized == ["published"]
    synchronized.clear()
    assert case.recover()["status"] == "published"
    assert synchronized == ["published"]
    assert len(case.transport.sends) == 1

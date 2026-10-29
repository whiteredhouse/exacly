"""Completion coordination retains shared locks and publishes the exact retained state bytes."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store as store
from exacly import operation_locks, state_writer
from test_state_writer import state


def paths(tmp_path: Path, *, same_parent: bool = False) -> tuple[Path, Path]:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    state_parent = private if same_parent else tmp_path / "state"
    state_parent.mkdir(exist_ok=True)
    destination = state_parent / "main.json"
    destination.write_text(json.dumps(state()))
    return private / "ledger.sqlite3", destination


@pytest.mark.parametrize("same_parent", [False, True])
def test_joint_locks_are_globally_ordered_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_parent: bool
) -> None:
    ledger, destination = paths(tmp_path, same_parent=same_parent)
    snapshot = state_writer.read(destination)
    acquired: list[tuple[int, int]] = []
    actual = fcntl.flock

    def record(descriptor: int, mode: int) -> None:
        info = os.fstat(descriptor)
        acquired.append((info.st_dev, info.st_ino))
        actual(descriptor, mode)

    monkeypatch.setattr(fcntl, "flock", record)
    with operation_locks.locked(ledger, snapshot, deadline=store.Deadline(0)) as (absolute, parent_fd):
        assert absolute == ledger
        assert os.fstat(parent_fd).st_ino == destination.parent.stat().st_ino
    expected = {(p.stat().st_dev, p.stat().st_ino) for p in (ledger.parent, destination.parent)}
    assert acquired == sorted(expected)


def test_exact_retained_bytes_publish_without_reacquiring_and_observer_stays_blocked(tmp_path: Path) -> None:
    ledger, destination = paths(tmp_path)
    snapshot = state_writer.read(destination)
    # Deliberately use a representation different from serialize() to detect accidental reserialization.
    retained = (json.dumps(state(serial=2), separators=(",", ":")) + "\n\n").encode()
    with operation_locks.locked(ledger, snapshot, deadline=store.Deadline(0)) as (_, parent_fd):
        state_writer.publish_locked(snapshot, retained, parent_fd)
        assert destination.read_bytes() == retained
        fresh = state_writer.read_locked(snapshot, parent_fd)
        assert fresh.content == retained
        with pytest.raises(state_writer.StateLockTimeoutError):
            state_writer.publish(fresh, state(serial=3), lock_timeout=0)
    state_writer.publish(state_writer.read(destination), state(serial=3), lock_timeout=0)
    assert json.loads(destination.read_bytes())["serial"] == 3


def test_locked_read_observes_a_newer_observer_without_weakening_the_old_precondition(tmp_path: Path) -> None:
    ledger, destination = paths(tmp_path)
    captured_before_admission = state_writer.read(destination)
    state_writer.publish(captured_before_admission, state(serial=2, **{"x-peer": "retained"}))
    newer_bytes = destination.read_bytes()
    with operation_locks.locked(ledger, captured_before_admission) as (_, parent_fd):
        current = state_writer.read_locked(captured_before_admission, parent_fd)
        assert current.document == state(serial=2, **{"x-peer": "retained"})
        with pytest.raises(state_writer.StateConflictError):
            state_writer.publish_locked(captured_before_admission, state_writer.serialize(state(serial=2)), parent_fd)
        assert destination.read_bytes() == newer_bytes
        state_writer.publish_locked(
            current, state_writer.serialize(state(serial=3, **{"x-peer": "retained"})), parent_fd
        )
    assert json.loads(destination.read_bytes())["x-peer"] == "retained"


def test_after_state_recovery_syncs_file_then_directory_under_the_held_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    calls: list[str] = []
    actual = os.fsync

    def sync(descriptor: int) -> None:
        calls.append("file" if stat.S_ISREG(os.fstat(descriptor).st_mode) else "directory")
        actual(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with operation_locks.locked(ledger, captured) as (_, parent_fd):
        state_writer.sync_locked(captured, parent_fd)
    assert calls == ["file", "directory"]


def test_failed_recovery_sync_keeps_the_visible_bytes_and_reports_uncertain_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    before = destination.read_bytes()

    def failed(descriptor: int) -> None:
        raise OSError("injected recovery synchronization failure")

    monkeypatch.setattr(os, "fsync", failed)
    with operation_locks.locked(ledger, captured) as (_, parent_fd):
        with pytest.raises(state_writer.PublicationDurabilityError) as error:
            state_writer.sync_locked(captured, parent_fd)
        assert error.value.published
    assert destination.read_bytes() == before


def test_recovery_sync_refuses_a_different_state_before_synchronizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    state_writer.publish(captured, state(serial=2))
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", calls.append)
    with operation_locks.locked(ledger, captured) as (_, parent_fd), pytest.raises(state_writer.StateConflictError):
        state_writer.sync_locked(captured, parent_fd)
    assert not calls


@pytest.mark.parametrize("sidecar", ["", "-journal", "-wal", "-shm"])
def test_state_cannot_alias_the_ledger_or_its_sidecars(tmp_path: Path, sidecar: str) -> None:
    ledger, _ = paths(tmp_path, same_parent=True)
    destination = Path(str(ledger) + sidecar)
    destination.write_text(json.dumps(state()))
    snapshot = state_writer.read(destination)
    with pytest.raises(store.StoreError, match="overlaps"), operation_locks.locked(ledger, snapshot):
        pytest.fail("aliased state must not acquire completion authority")


def test_directory_replacement_after_snapshot_cannot_redirect_publication(tmp_path: Path) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    destination.parent.rename(tmp_path / "old-state")
    destination.parent.mkdir()
    replacement = destination.parent / destination.name
    replacement.write_text(json.dumps(state(lineage="replacement")))
    before = replacement.read_bytes()
    with pytest.raises(state_writer.StateConflictError), operation_locks.locked(ledger, captured):
        pytest.fail("replaced directory must conflict")
    assert replacement.read_bytes() == before


def test_retained_bytes_require_valid_schema_and_a_single_successor(tmp_path: Path) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    before = destination.read_bytes()
    with operation_locks.locked(ledger, captured) as (_, parent_fd):
        for invalid in (b"null", b"{broken", json.dumps(state(serial=3)).encode()):
            with pytest.raises(state_writer.StateWriteError):
                state_writer.publish_locked(captured, invalid, parent_fd)
            assert destination.read_bytes() == before


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_failure_inside_joint_operation_releases_every_directory_lock(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    ledger, destination = paths(tmp_path)
    captured = state_writer.read(destination)
    failure = error_type("coordinator failed")
    with pytest.raises(error_type, match="coordinator failed") as caught, operation_locks.locked(ledger, captured):
        raise failure
    assert caught.value is failure
    with operation_locks.locked(ledger, captured, deadline=store.Deadline(0)) as (_, parent_fd):
        state_writer.publish_locked(captured, state_writer.serialize(state(serial=2)), parent_fd)


def test_serialization_retains_only_finite_json_state() -> None:
    valid = state(**{"x-message": "εvidence"})
    encoded = state_writer.serialize(valid)
    assert state_writer.decode(encoded) == valid
    invalid: Any = {"recursive": None}
    invalid["recursive"] = invalid
    with pytest.raises(state_writer.StateWriteError):
        state_writer.serialize(state(**{"x-recursive": invalid}))

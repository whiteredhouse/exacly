"""The local publication contract: frozen snapshots, generation checks and failure outcomes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from exacly import state_writer

Doc = dict[str, Any]


def state(**changes: Any) -> Doc:
    return {
        "exacly": "0.2.0",
        "namespace": "example",
        "context": "main",
        "config_revision": "rev-1",
        "serial": 1,
        "lineage": "lineage-1",
        "written_at": "2026-10-28T10:00:00Z",
        "records": {},
        **changes,
    }


def existing(path: Path, **changes: Any) -> state_writer.Snapshot:
    path.write_text(json.dumps(state(**changes)) + "\n", encoding="utf-8")
    return state_writer.read(path)


def test_capture_absent_nested_state_has_no_filesystem_side_effects_and_publishes_once(tmp_path: Path) -> None:
    path = tmp_path / "state" / "contexts" / "main.json"
    captured = state_writer.read(path)
    assert captured.document is None and captured.digest is None
    assert list(tmp_path.iterdir()) == []
    state_writer.publish(captured, state(), lock_timeout=0)
    assert json.loads(path.read_bytes()) == state()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(state_writer.StateConflictError):
        state_writer.publish(captured, state())
    assert json.loads(path.read_bytes())["serial"] == 1


def test_capture_freezes_exact_bytes_header_and_independent_documents(tmp_path: Path) -> None:
    path = tmp_path / "main.json"
    captured = existing(path, **{"x-note": {"message": "original"}})
    before = path.read_bytes()
    assert captured.digest == "sha256:" + hashlib.sha256(before).hexdigest()
    previous = captured.document
    assert previous is not None
    previous["serial"] = 999
    previous["lineage"] = "different"
    previous["x-note"]["message"] = "changed"
    assert captured.document == state(**{"x-note": {"message": "original"}})
    with pytest.raises(dataclasses.FrozenInstanceError):
        captured.path = tmp_path / "other.json"  # type: ignore[misc]
    state_writer.publish(captured, state(serial=2))
    assert json.loads(path.read_bytes()) == state(serial=2)


def test_publication_preserves_existing_permissions_and_replaces_inode(tmp_path: Path) -> None:
    path = tmp_path / "main.json"
    existing(path)
    path.chmod(0o640)
    captured = state_writer.read(path)
    inode = path.stat().st_ino
    state_writer.publish(captured, state(serial=2, config_revision="rev-2"))
    assert path.stat().st_ino != inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert json.loads(path.read_bytes()) == state(serial=2, config_revision="rev-2")


@pytest.mark.parametrize(
    "changes",
    [{"serial": 1}, {"serial": 3}, {"namespace": "other"}, {"context": "other"}, {"lineage": "other"}],
)
def test_invalid_successor_leaves_existing_state_unchanged(tmp_path: Path, changes: Doc) -> None:
    path = tmp_path / "main.json"
    captured = existing(path)
    before = path.read_bytes()
    with pytest.raises(state_writer.StateWriteError):
        state_writer.publish(captured, state(serial=2) | changes)
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]


def test_initial_state_cannot_skip_serials(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "main.json"
    with pytest.raises(state_writer.StateWriteError, match="serial 1"):
        state_writer.publish(state_writer.read(path), state(serial=2))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["remove", "format", "replace-with-invalid"])
def test_any_change_to_expected_bytes_conflicts(tmp_path: Path, change: str) -> None:
    path = tmp_path / "main.json"
    captured = existing(path)
    if change == "remove":
        path.unlink()
    elif change == "format":
        path.write_text(json.dumps(state(), indent=2), encoding="utf-8")
    else:
        path.write_bytes(b"invalid")
    expected = path.read_bytes() if path.exists() else None
    with pytest.raises(state_writer.StateConflictError):
        state_writer.publish(captured, state(serial=2))
    assert (path.read_bytes() if path.exists() else None) == expected


@pytest.mark.parametrize(
    "content",
    [
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"serial": 1, "serial": 2}',
        json.dumps(state(serial=True)).encode(),
        json.dumps(state(serial=0)).encode(),
        json.dumps(state(written_at="not-a-timestamp")).encode(),
        json.dumps(state(exacly="0.1.0")).encode(),
        json.dumps(state(**{"x-number": math.nan})).encode(),
        json.dumps(state(**{"x-number": math.inf})).encode(),
        json.dumps(state(**{"x-number": "placeholder"})).replace('"placeholder"', "1e999").encode(),
    ],
)
def test_invalid_json_or_state_schema_is_refused_without_writes(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "main.json"
    path.write_bytes(content)
    with pytest.raises(state_writer.StateWriteError):
        state_writer.read(path)
    assert path.read_bytes() == content


@pytest.mark.parametrize("invalid", [math.nan, math.inf, {1: "integer-key"}, {"not-json"}])
def test_new_state_requires_finite_json_values_and_string_keys(tmp_path: Path, invalid: Any) -> None:
    path = tmp_path / "missing" / "main.json"
    with pytest.raises(state_writer.StateWriteError):
        state_writer.publish(state_writer.read(path), state(**{"x-invalid": invalid}))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_nonregular_or_aliased_state_is_refused_without_touching_it(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "main.json"
    other = tmp_path / "other.json"
    other.write_text(json.dumps(state()), encoding="utf-8")
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(state_writer.StateWriteError):
        state_writer.read(path)
    assert path.lstat() == before
    assert json.loads(other.read_bytes()) == state()


def test_parent_aliases_are_normalized_to_one_lock_destination(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    captured = state_writer.read(alias / "main.json")
    assert captured.path == real / "main.json"
    state_writer.publish(captured, state())
    assert json.loads((real / "main.json").read_bytes()) == state()


@pytest.mark.parametrize("nested", [False, True])
def test_replacing_captured_parent_directory_conflicts(tmp_path: Path, nested: bool) -> None:
    parent = tmp_path / "state"
    parent.mkdir()
    path = parent / "missing" / "main.json" if nested else parent / "main.json"
    captured = state_writer.read(path)
    parent.rename(tmp_path / "old-state")
    parent.mkdir()
    with pytest.raises(state_writer.StateConflictError):
        state_writer.publish(captured, state())
    assert not path.exists()
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("timeout", [-1.0, math.nan, math.inf, True, 10**1000])
def test_invalid_lock_timeout_has_no_filesystem_side_effect(tmp_path: Path, timeout: Any) -> None:
    path = tmp_path / "missing" / "main.json"
    with pytest.raises(state_writer.StateWriteError, match="lock timeout"):
        state_writer.publish(state_writer.read(path), state(), lock_timeout=timeout)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", ["stage-fsync", "replace"])
def test_failure_before_replacement_preserves_old_bytes_and_removes_own_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path = tmp_path / "main.json"
    captured = existing(path)
    before = path.read_bytes()
    real_fsync = os.fsync

    def sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("simulated stage sync failure")
        real_fsync(descriptor)

    def replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated rename failure")

    if phase == "stage-fsync":
        monkeypatch.setattr(os, "fsync", sync)
    else:
        monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(state_writer.StateWriteError):
        state_writer.publish(captured, state(serial=2))
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]


def test_failure_after_replacement_reports_already_published_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "main.json"
    captured = existing(path)
    real_fsync = os.fsync

    def sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory sync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(state_writer.PublicationDurabilityError, match="inspect current state") as error:
        state_writer.publish(captured, state(serial=2))
    assert error.value.published is True
    assert json.loads(path.read_bytes()) == state(serial=2)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["main.json"]


def test_competing_creator_directory_entries_are_synced_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "contexts" / "main.json"
    captured = state_writer.read(path)
    real_fsync = os.fsync

    def failed_sync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == tmp_path.stat().st_ino:
            raise OSError("creator interrupted before directory entry was synced")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(state_writer.StateWriteError):
        state_writer.publish(captured, state())
    assert path.parent.parent.exists()
    assert not path.exists()
    synced: list[int] = []

    def sync(descriptor: int) -> None:
        synced.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    state_writer.publish(captured, state())
    assert synced[:3] == [tmp_path.parent.stat().st_ino, tmp_path.stat().st_ino, path.parent.parent.stat().st_ino]
    assert synced[-1] == path.parent.stat().st_ino
    assert json.loads(path.read_bytes()) == state()


def test_fresh_snapshot_finishes_a_previous_creators_unsynced_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "main.json"
    first_capture = state_writer.read(path)
    real_fsync = os.fsync

    def failed_sync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == tmp_path.stat().st_ino:
            raise OSError("creator interrupted after mkdir, before parent sync")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(state_writer.StateWriteError):
        state_writer.publish(first_capture, state())
    assert path.parent.exists() and not path.exists()
    # This new snapshot has zero missing components; the existing anchor itself still needs syncing.
    second_capture = state_writer.read(path)
    synced: list[int] = []

    def sync(descriptor: int) -> None:
        synced.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    state_writer.publish(second_capture, state())
    assert synced[0] == tmp_path.stat().st_ino
    assert synced[-1] == path.parent.stat().st_ino
    assert json.loads(path.read_bytes()) == state()


def test_missing_lock_capability_only_rejects_writer_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_writer, "fcntl", None)
    with pytest.raises(state_writer.StateWriteError, match="POSIX"):
        state_writer.read(tmp_path / "main.json")
    assert list(tmp_path.iterdir()) == []

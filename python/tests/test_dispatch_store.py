from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from exacly import dispatch_store, execution_store, planning

Doc = dict[str, Any]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    return private / "dispatch.sqlite3"


def intent(name: str = "first") -> Doc:
    identity = {
        "schema_version": "0.1.0",
        "namespace": "sample",
        "action_id": planning.digest(name),
        "adapter": "github-issue-v1",
        "repository": {"full_name": "sample/project", "id": 123},
    }
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(identity),
        "identity": identity,
        "contract": {"literal": "'; DROP TABLE dispatch_events; --"},
        "paths": {"journal": "operator/dispatch.sqlite3"},
        "created_at": "2026-10-30T09:00:00Z",
        "authorization": {"effect": "create-github-issue"},
        "effect": {"title": "Reviewed work"},
    }


def prepare(path: Path, proposed: Doc | None = None) -> Doc:
    dispatch_store.initialize(path)
    with dispatch_store.opened(path, write=True, already_locked=False) as journal:
        return journal.prepare(proposed or intent())


def read(path: Path, identity: str | None = None) -> Doc | None:
    with dispatch_store.opened(path, write=False, already_locked=False) as journal:
        return journal.get(identity or intent()["id"])


def snapshot(path: Path) -> dict[str, bytes | str]:
    return {
        item.name: os.readlink(item) if item.is_symlink() else item.read_bytes() if item.is_file() else "directory"
        for item in path.parent.iterdir()
    }


def test_initialization_is_explicit_private_and_never_replaces_existing_storage(path: Path) -> None:
    result = dispatch_store.initialize(path)
    assert result["status"] == "initialized"
    assert result["journal"] == str(path)
    assert uuid.UUID(hex=result["journal_id"]).version == 4
    assert uuid.UUID(hex=result["journal_id"]).hex == result["journal_id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError, match="new destination"):
        dispatch_store.initialize(path)
    assert snapshot(path) == before
    assert read(path) is None
    with dispatch_store.opened(path, write=False) as journal:
        assert journal.journal_id == result["journal_id"]
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (dispatch_store.APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (dispatch_store.USER_VERSION,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT key, value FROM dispatch_journal").fetchall() == [
            ("journal_id", result["journal_id"])
        ]


def test_each_initialized_outbox_has_a_distinct_persistent_instance_identity(path: Path) -> None:
    first = dispatch_store.initialize(path)
    second_path = path.with_name("another-dispatch.sqlite3")
    second = dispatch_store.initialize(second_path)
    assert first["journal_id"] != second["journal_id"]
    for current, report in ((path, first), (second_path, second)):
        before = current.read_bytes()
        with dispatch_store.opened(current, write=False) as journal:
            assert journal.journal_id == report["journal_id"]
        assert current.read_bytes() == before


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM dispatch_journal",
        "INSERT INTO dispatch_journal VALUES ('another', 'entry')",
        "UPDATE dispatch_journal SET key='unknown'",
        "UPDATE dispatch_journal SET value='not-a-uuid'",
        "UPDATE dispatch_journal SET value='00000000000010008000000000000000'",
        "UPDATE dispatch_journal SET value='AAAAAAAAAAAA4AAA8AAAAAAAAAAAAAAA'",
    ],
)
def test_missing_extra_or_malformed_instance_metadata_fails_closed(path: Path, statement: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = snapshot(path)
    for write in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="identity"),
            dispatch_store.opened(path, write=write),
        ):
            pytest.fail("invalid instance metadata was accepted")
        assert snapshot(path) == before


@pytest.mark.parametrize("write", [False, True])
def test_opening_missing_journal_never_initializes_it(path: Path, write: bool) -> None:
    with (
        pytest.raises(execution_store.StoreError, match="cannot open"),
        dispatch_store.opened(path, write=write, already_locked=False),
    ):
        pytest.fail("missing journal was opened")
    assert not path.exists()
    assert not list(path.parent.iterdir())


def test_event_history_keeps_intent_and_payloads_immutable_across_retries(path: Path) -> None:
    proposed = intent()
    original = copy.deepcopy(proposed)
    initial = prepare(path, proposed)
    assert initial["phase"] == "prepared"
    before = snapshot(path)
    with dispatch_store.opened(path, write=True, already_locked=False) as journal:
        assert journal.prepare(proposed) == initial
        assert snapshot(path) == before
        phases = ("creating", "ambiguous", "remote_confirmed", "registration_prepared", "registered")
        for number, phase in enumerate(phases, 1):
            payload = {"kind": phase, "sequence": number}
            result = journal.append(proposed["id"], phase, payload)
            payload["sequence"] = 999
            assert result["events"][-1]["data"]["sequence"] == number
    assert proposed == original
    stored = read(path)
    assert stored is not None
    assert stored["intent"] == original
    assert [event["phase"] for event in stored["events"]] == ["prepared", *phases]
    assert stored["events"][0]["previous"] == planning.digest(original)
    for previous, current in zip(stored["events"], stored["events"][1:], strict=False):
        assert current["previous"] == previous["digest"]
    stored["intent"]["effect"]["title"] = "caller mutation"
    assert read(path)["intent"] == original  # type: ignore[index]
    assert set(snapshot(path)) == {path.name}


def test_existing_dispatch_identity_cannot_be_reassociated_with_changed_intent(path: Path) -> None:
    original = prepare(path)
    changed = intent()
    changed["effect"]["title"] = "different work"
    before = snapshot(path)
    with (
        dispatch_store.opened(path, write=True, already_locked=False) as journal,
        pytest.raises(execution_store.StoreError, match="another intent"),
    ):
        journal.prepare(changed)
    assert snapshot(path) == before
    assert read(path) == original


@pytest.mark.parametrize("damage", ["identity", "extra", "version", "timestamp", "nan", "not_json"])
def test_invalid_intents_cannot_be_committed(path: Path, damage: str) -> None:
    dispatch_store.initialize(path)
    proposed = intent()
    if damage == "identity":
        proposed["id"] = planning.digest("wrong")
    elif damage == "extra":
        proposed["unreviewed"] = True
    elif damage == "version":
        proposed["schema_version"] = "2.0.0"
    elif damage == "timestamp":
        proposed["created_at"] = "2026-10-30T09:00:00"
    elif damage == "nan":
        proposed["effect"]["value"] = float("nan")
    else:
        proposed["effect"]["value"] = object()
    before = snapshot(path)
    with (
        dispatch_store.opened(path, write=True, already_locked=False) as journal,
        pytest.raises(execution_store.StoreError),
    ):
        journal.prepare(proposed)
    assert snapshot(path) == before


@pytest.mark.parametrize("phase", ["creating", "prepared", "remote_confirmed", "unknown"])
def test_registered_history_cannot_return_to_creation(path: Path, phase: str) -> None:
    prepare(path)
    with dispatch_store.opened(path, write=True, already_locked=False) as journal:
        for current in ("remote_confirmed", "registration_prepared", "registered"):
            journal.append(intent()["id"], current, {"kind": current})
        before = snapshot(path)
        with pytest.raises(execution_store.StoreError, match="phase transition"):
            journal.append(intent()["id"], phase, {})
        assert snapshot(path) == before


def test_supersession_retains_history_and_is_terminal(path: Path) -> None:
    prepare(path)
    with dispatch_store.opened(path, write=True, already_locked=False) as journal:
        result = journal.append(intent()["id"], "superseded", {"reason": "new contract"})
        assert len(result["events"]) == 2
        with pytest.raises(execution_store.StoreError, match="phase transition"):
            journal.append(intent()["id"], "creating", {})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE dispatch_intents SET intent_digest='sha256:wrong'",
        "UPDATE dispatch_intents SET head_digest='sha256:wrong'",
        "UPDATE dispatch_intents SET sequence=2",
        "DELETE FROM dispatch_events",
        'UPDATE dispatch_events SET data_json=\'{"kind":"changed"}\'',
        'UPDATE dispatch_events SET data_json=\'{"kind":1,"kind":2}\'',
        "UPDATE dispatch_events SET data_json='{\"kind\":NaN}'",
        "UPDATE dispatch_events SET phase='registered'",
        "UPDATE dispatch_events SET at='invalid'",
        "UPDATE dispatch_events SET previous='sha256:wrong'",
        "UPDATE dispatch_events SET digest='sha256:wrong'",
    ],
)
def test_corruption_of_selected_history_fails_closed_without_rewriting_it(path: Path, statement: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError):
        read(path)
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE extra (id INTEGER)",
        "CREATE VIEW extra AS SELECT id FROM dispatch_intents",
        "CREATE INDEX extra ON dispatch_events(phase)",
        "CREATE TRIGGER extra AFTER INSERT ON dispatch_intents BEGIN DELETE FROM dispatch_events; END",
        "PRAGMA application_id=1",
        "PRAGMA user_version=99",
    ],
)
def test_unknown_schema_or_format_is_not_migrated_or_repaired(path: Path, statement: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = snapshot(path)
    with (
        pytest.raises(execution_store.StoreError),
        dispatch_store.opened(path, write=True, already_locked=False, recover=True),
    ):
        pytest.fail("unknown journal was opened")
    assert snapshot(path) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode"])
def test_unsafe_journal_file_is_rejected(path: Path, damage: str) -> None:
    prepare(path)
    if damage == "symlink":
        target = path.with_name("original")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        os.link(path, path.with_name("other"))
    else:
        path.chmod(0o644)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError):
        read(path)
    assert snapshot(path) == before


def test_unsafe_parent_and_git_checkout_are_rejected_before_initialization(path: Path) -> None:
    path.parent.chmod(0o755)
    with pytest.raises(execution_store.StoreError, match="0700"):
        dispatch_store.initialize(path)
    path.parent.chmod(0o700)
    (path.parent / ".git").write_text("gitdir: controlled\n")
    with pytest.raises(execution_store.StoreError, match="Git working trees"):
        dispatch_store.initialize(path)
    assert not path.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_wal_sidecars_are_never_opened_for_implicit_or_explicit_recovery(path: Path, suffix: str) -> None:
    prepare(path)
    Path(str(path) + suffix).write_bytes(b"unsupported")
    before = snapshot(path)
    for recover in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="WAL"),
            dispatch_store.opened(path, write=True, already_locked=False, recover=recover),
        ):
            pytest.fail("WAL sidecar was accepted")
        assert snapshot(path) == before


def test_read_only_journal_rejects_mutations(path: Path) -> None:
    prepare(path)
    before = snapshot(path)
    with dispatch_store.opened(path, write=False, already_locked=False) as journal:
        with pytest.raises(execution_store.StoreError, match="read-only"):
            journal.append(intent()["id"], "creating", {})
        with pytest.raises(execution_store.StoreError, match="read-only"):
            journal.prepare(intent())
    assert snapshot(path) == before


def interrupted_writer(path: Path) -> None:
    script = """
import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE dispatch_events SET data_json=?',('{"kind":"uncommitted"}',))
os._exit(73)
"""
    completed = subprocess.run([sys.executable, "-I", "-c", script, str(path)], check=False, timeout=10)
    assert completed.returncode == 73
    assert Path(str(path) + "-journal").exists()


def test_interrupted_writer_requires_explicit_recovery_and_preserves_exact_inputs_first(path: Path) -> None:
    original = prepare(path)
    with dispatch_store.opened(path, write=False) as journal:
        journal_id = journal.journal_id
    interrupted_writer(path)
    before = snapshot(path)
    for write in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="explicit dispatch recovery"),
            dispatch_store.opened(path, write=write, already_locked=False),
        ):
            pytest.fail("ordinary open recovered a journal")
        assert snapshot(path) == before
    with dispatch_store.opened(path, write=True, recover=True, already_locked=False) as journal:
        assert journal.journal_id == journal_id
        assert journal.get(intent()["id"]) == original
        backup = journal.recovery_backup
        assert backup is not None
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert (backup / "database.sqlite3").read_bytes() == before[path.name]
        assert (backup / "database.sqlite3-journal").read_bytes() == before[path.name + "-journal"]
        receipt = json.loads((backup / "receipt.json").read_text())
        for name, expected in receipt["files"].items():
            assert expected == "sha256:" + hashlib.sha256((backup / name).read_bytes()).hexdigest()
    assert not Path(str(path) + "-journal").exists()
    assert read(path) == original
    with dispatch_store.opened(path, write=False) as journal:
        assert journal.journal_id == journal_id


def test_failed_backup_prevents_sqlite_recovery(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(path)
    interrupted_writer(path)
    before = snapshot(path)

    def failed_backup(*args: Any, **kwargs: Any) -> Path:
        raise OSError("injected backup failure")

    monkeypatch.setattr(dispatch_store, "_backup", failed_backup)
    with (
        pytest.raises(execution_store.StoreError, match="cannot open"),
        dispatch_store.opened(path, write=True, recover=True, already_locked=False),
    ):
        pytest.fail("backup failure allowed recovery")
    assert snapshot(path) == before


def test_unrecognized_or_unsafe_rollback_sidecar_is_not_recovered(path: Path) -> None:
    prepare(path)
    sidecar = Path(str(path) + "-journal")
    sidecar.write_bytes(b"not a journal")
    sidecar.chmod(0o600)
    before = snapshot(path)
    with (
        pytest.raises(execution_store.StoreError, match="unrecognized"),
        dispatch_store.opened(path, write=True, recover=True, already_locked=False),
    ):
        pytest.fail("unknown sidecar accepted")
    assert snapshot(path) == before
    sidecar.unlink()
    sidecar.symlink_to(path)
    before = snapshot(path)
    with (
        pytest.raises(execution_store.StoreError),
        dispatch_store.opened(path, write=True, recover=True, already_locked=False),
    ):
        pytest.fail("symlink sidecar accepted")
    assert snapshot(path) == before


def test_history_limit_stops_without_losing_earlier_events(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_store, "MAX_EVENTS", 3)
    prepare(path)
    with dispatch_store.opened(path, write=True, already_locked=False) as journal:
        journal.append(intent()["id"], "prepared", {"kind": "discovery"})
        previous = journal.append(intent()["id"], "prepared", {"kind": "discovery"})
        before = snapshot(path)
        with pytest.raises(execution_store.StoreError, match="history limit"):
            journal.append(intent()["id"], "creating", {})
        assert journal.get(intent()["id"]) == previous
    assert snapshot(path) == before


def test_lock_timeout_is_bounded_and_leaves_storage_unchanged(path: Path) -> None:
    prepare(path)
    script = """
import fcntl,os,sys
descriptor=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)
fcntl.flock(descriptor,fcntl.LOCK_EX)
print('locked',flush=True)
sys.stdin.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(path.parent)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        before = snapshot(path)
        with (
            pytest.raises(execution_store.LockTimeoutError, match="timed out"),
            dispatch_store.opened(path, write=True, deadline=execution_store.Deadline(0), already_locked=False),
        ):
            pytest.fail("competing operation acquired the lock")
        assert snapshot(path) == before
    finally:
        process.communicate("x", timeout=5)


def test_commit_survives_process_exit_and_reading_has_no_writes(path: Path) -> None:
    prepare(path)
    script = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import dispatch_store
with dispatch_store.opened(Path(sys.argv[2]),write=True,already_locked=False) as journal:
    journal.append(sys.argv[3],'creating',{'kind':'create_started'})
    os._exit(73)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path(dispatch_store.__file__).parent.parent),
            str(path),
            intent()["id"],
        ],
        check=False,
        timeout=10,
    )
    assert completed.returncode == 73
    before = snapshot(path)
    result = read(path)
    assert result is not None and result["phase"] == "creating"
    assert snapshot(path) == before

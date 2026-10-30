from __future__ import annotations

import base64
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
from exacly import execution_store, handoff_store, planning

Doc = dict[str, Any]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    return private / "handoff.sqlite3"


def repository() -> Doc:
    return {"path": "/explicit/repository.git", "repository_id": "a" * 32, "format": "sha1"}


def intent(name: str = "first") -> Doc:
    identity = {"schema_version": "0.1.0", "execution": planning.digest(name), "repository": repository()}
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(identity),
        "identity": identity,
        "authorization": {"ref": "refs/heads/reviewed", "expected": "1" * 40},
        "contract": {"literal": "'; DROP TABLE handoffs; --"},
        "created_at": "2026-10-30T09:00:00Z",
    }


def prepared() -> Doc:
    payload = b"retained candidate bytes\x00\xff"
    oid = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
    return {
        "base": "1" * 40,
        "tree": "2" * 40,
        "commit": "3" * 40,
        "changes": [{"path": "src/main.py", "oid": oid}],
        "objects": [{"type": "blob", "oid": oid, "data_base64": base64.b64encode(payload).decode()}],
    }


def initialize(path: Path) -> Doc:
    return handoff_store.initialize(path, repository=repository())


def prepare(path: Path) -> Doc:
    initialize(path)
    with handoff_store.opened(path, write=True) as journal:
        return journal.prepare(intent(), prepared())


def read(path: Path) -> Doc | None:
    with handoff_store.opened(path, write=False) as journal:
        return journal.get(intent()["id"])


def snapshot(path: Path) -> dict[str, bytes | str]:
    return {
        item.name: os.readlink(item) if item.is_symlink() else item.read_bytes() if item.is_file() else "directory"
        for item in path.parent.iterdir()
    }


def test_explicit_initialization_pins_one_private_instance_and_repository_without_replacement(path: Path) -> None:
    supplied = repository()
    initialized = handoff_store.initialize(path, repository=supplied)
    assert initialized["status"] == "initialized" and initialized["repository"] == supplied
    assert uuid.UUID(hex=initialized["journal_id"]).version == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    supplied.clear()
    before = snapshot(path)
    with handoff_store.opened(path, write=False) as journal:
        assert journal.journal_id == initialized["journal_id"]
        assert journal.repository == repository()
        copied = journal.repository
        copied.clear()
        assert journal.repository == repository()
        assert journal.get(intent()["id"]) is None
    with pytest.raises(execution_store.StoreError, match="new destination"):
        initialize(path)
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (handoff_store.APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


@pytest.mark.parametrize("write", [False, True])
def test_ordinary_open_never_creates_a_missing_journal(path: Path, write: bool) -> None:
    with pytest.raises(execution_store.StoreError, match="cannot open"), handoff_store.opened(path, write=write):
        pytest.fail("missing storage was initialized implicitly")
    assert not path.exists() and not list(path.parent.iterdir())


def test_initialization_cannot_leave_a_journal_inside_its_pinned_repository(path: Path) -> None:
    bare = path.parent / "destination.git"
    bare.mkdir(mode=0o700)
    (bare / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    before = {item.name: item.read_bytes() for item in bare.iterdir()}
    selected = {**repository(), "path": str(bare)}
    journal = bare / "handoff.sqlite3"
    with pytest.raises(execution_store.StoreError, match="overlaps"):
        handoff_store.initialize(journal, repository=selected)
    assert not journal.exists()
    assert {item.name: item.read_bytes() for item in bare.iterdir()} == before


def test_full_preparation_and_receipt_are_immutable_detached_and_idempotent(path: Path) -> None:
    initialize(path)
    proposed, objects = intent(), prepared()
    original = copy.deepcopy((proposed, objects))
    with handoff_store.opened(path, write=True) as journal:
        initial = journal.prepare(proposed, objects)
        assert initial["phase"] == "prepared" and initial["receipt"] is None
        assert journal.prepare(proposed, objects) == initial
        proposed["contract"]["literal"] = "changed after prepare"
        objects["objects"][0]["data_base64"] = ""
        retained = journal.get(original[0]["id"])
        assert retained is not None
        assert (retained["intent"], retained["prepared"]) == original
        retained["prepared"]["objects"].clear()
        assert journal.get(original[0]["id"]) == initial
        receipt = {"commit": original[1]["commit"], "published": True}
        terminal = journal.finish(original[0]["id"], receipt)
        assert terminal["phase"] == "published"
        assert journal.finish(original[0]["id"], receipt) == terminal
        assert journal.prepare(*original) == terminal
        with pytest.raises(execution_store.StoreError, match="immutable"):
            journal.finish(original[0]["id"], {"commit": "4" * 40})
        receipt.clear()
        assert journal.get(original[0]["id"]) == terminal
    assert read(path) == terminal
    assert set(snapshot(path)) == {path.name}


@pytest.mark.parametrize("changed", ["intent", "objects"])
def test_existing_identity_cannot_be_rebound_to_different_authority_or_object_bytes(path: Path, changed: str) -> None:
    initial = prepare(path)
    proposed, objects = intent(), prepared()
    if changed == "intent":
        proposed["authorization"]["ref"] = "refs/heads/unreviewed"
    else:
        objects["objects"][0]["data_base64"] = "dW5yZXZpZXdlZA=="
    before = snapshot(path)
    with (
        handoff_store.opened(path, write=True) as journal,
        pytest.raises(execution_store.StoreError, match="another intent or object package"),
    ):
        journal.prepare(proposed, objects)
    assert read(path) == initial and snapshot(path) == before


@pytest.mark.parametrize("damage", ["identity", "extra", "version", "timestamp", "nan", "not_json", "keys"])
def test_invalid_authorization_intents_cannot_change_storage(path: Path, damage: str) -> None:
    initialize(path)
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
        proposed["contract"]["value"] = float("nan")
    elif damage == "not_json":
        proposed["contract"]["value"] = object()
    else:
        proposed["contract"][1] = "nonstring key"
    before = snapshot(path)
    with handoff_store.opened(path, write=True) as journal, pytest.raises(execution_store.StoreError):
        journal.prepare(proposed, prepared())
    assert snapshot(path) == before


def test_git_payloads_larger_than_metadata_limit_survive_a_fresh_connection(path: Path) -> None:
    initialize(path)
    objects = prepared()
    raw = b"\x00\xffbinary" * ((handoff_store.JSON_LIMIT * 3 // 4) // 8 + 1)
    objects["objects"][0]["data_base64"] = base64.b64encode(raw).decode()
    assert len(objects["objects"][0]["data_base64"]) > handoff_store.JSON_LIMIT
    with handoff_store.opened(path, write=True) as journal:
        retained = journal.prepare(intent(), objects)
    reopened = read(path)
    assert reopened == retained
    assert reopened is not None
    assert base64.b64decode(reopened["prepared"]["objects"][0]["data_base64"]) == raw


def test_complete_source_contract_above_eight_mebibytes_survives_a_fresh_connection(path: Path) -> None:
    initialize(path)
    proposed = intent()
    source = "x" * (9 * 1024 * 1024)
    proposed["contract"]["source"] = {"contract": {"reviewed": source}}
    assert 8 * 1024 * 1024 < len(planning.canonical(proposed).encode()) < handoff_store.JSON_LIMIT
    with handoff_store.opened(path, write=True) as journal:
        retained = journal.prepare(proposed, prepared())
    reopened = read(path)
    assert reopened == retained
    assert reopened is not None
    assert reopened["intent"]["contract"]["source"]["contract"]["reviewed"] == source


@pytest.mark.parametrize("section", ["intent", "prepared", "receipt"])
def test_each_payload_class_has_its_own_bound_before_writes(
    path: Path, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    prepare(path)
    before = snapshot(path)
    with handoff_store.opened(path, write=True) as journal:
        if section == "intent":
            proposed = intent("different")
            proposed["contract"]["oversized"] = "x" * (handoff_store.JSON_LIMIT + 1)
            with pytest.raises(execution_store.StoreError):
                journal.prepare(proposed, prepared())
        elif section == "prepared":
            monkeypatch.setattr(handoff_store, "PREPARED_LIMIT", 64)
            with pytest.raises(execution_store.StoreError):
                journal.prepare(intent("different"), prepared())
        else:
            with pytest.raises(execution_store.StoreError):
                journal.finish(intent()["id"], {"oversized": "x" * (handoff_store.JSON_LIMIT + 1)})
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE handoffs SET intent_digest='sha256:wrong'",
        "UPDATE handoffs SET prepared_digest='sha256:wrong'",
        "UPDATE handoffs SET head_digest='sha256:wrong'",
        "UPDATE handoffs SET phase='unknown'",
        "UPDATE handoffs SET receipt_json='{}'",
        "UPDATE handoffs SET prepared_json='{\"objects\":[]}'",
        'UPDATE handoffs SET intent_json=\'{"a":1,"a":2}\'',
    ],
)
def test_corrupt_retained_facts_fail_closed_without_rewriting(path: Path, statement: str) -> None:
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
        "CREATE VIEW extra AS SELECT id FROM handoffs",
        "CREATE INDEX extra ON handoffs(phase)",
        "CREATE TRIGGER extra AFTER INSERT ON handoffs BEGIN DELETE FROM handoffs; END",
        "PRAGMA application_id=1",
        "PRAGMA user_version=99",
        "DELETE FROM handoff_journal",
        "INSERT INTO handoff_journal VALUES ('unreviewed','value')",
        "UPDATE handoff_journal SET value='invalid' WHERE key='journal_id'",
        "UPDATE handoff_journal SET value='{}' WHERE key='repository'",
    ],
)
def test_unrecognized_formats_or_instance_metadata_are_never_repaired(path: Path, statement: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError), handoff_store.opened(path, write=True, recover=True):
        pytest.fail("unrecognized format was opened")
    assert snapshot(path) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode"])
def test_unsafe_database_files_are_rejected(path: Path, damage: str) -> None:
    prepare(path)
    if damage == "symlink":
        original = path.with_name("original.sqlite3")
        path.rename(original)
        path.symlink_to(original)
    elif damage == "hardlink":
        os.link(path, path.with_name("another.sqlite3"))
    else:
        path.chmod(0o644)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError):
        read(path)
    assert snapshot(path) == before


def test_initialization_rejects_public_parents_and_git_working_trees(path: Path) -> None:
    path.parent.chmod(0o755)
    with pytest.raises(execution_store.StoreError, match="0700"):
        initialize(path)
    path.parent.chmod(0o700)
    (path.parent / ".git").write_text("gitdir: controlled\n")
    with pytest.raises(execution_store.StoreError, match="Git working trees"):
        initialize(path)
    assert not path.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_wal_files_are_not_recovered(path: Path, suffix: str) -> None:
    prepare(path)
    Path(str(path) + suffix).write_bytes(b"unrecognized")
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError, match="WAL"), handoff_store.opened(path, write=True, recover=True):
        pytest.fail("WAL was accepted")
    assert snapshot(path) == before


def test_read_only_and_expired_sessions_cannot_mutate(path: Path) -> None:
    prepare(path)
    before = snapshot(path)
    with handoff_store.opened(path, write=False) as journal:
        with pytest.raises(execution_store.StoreError, match="read-only"):
            journal.prepare(intent(), prepared())
        with pytest.raises(execution_store.StoreError, match="read-only"):
            journal.finish(intent()["id"], {})
    with pytest.raises(execution_store.StoreError, match="closed"):
        journal.get(intent()["id"])
    assert snapshot(path) == before


def interrupted_writer(path: Path) -> None:
    script = """
import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=10')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE handoffs SET prepared_json=?',('{"objects":[],"padding":"'+'x'*(1024*1024)+'"}',))
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(path)], check=False, timeout=10)
    assert result.returncode == 73
    assert Path(str(path) + "-journal").exists()
    assert Path(str(path) + "-journal").read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"


def test_interrupted_sqlite_repair_retains_exact_backup_and_original_objects(path: Path) -> None:
    original = prepare(path)
    interrupted_writer(path)
    before = snapshot(path)
    for write in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="explicit handoff journal recovery"),
            handoff_store.opened(path, write=write),
        ):
            pytest.fail("ordinary open recovered an interrupted write")
        assert snapshot(path) == before
    with handoff_store.opened(path, write=True, recover=True) as journal:
        assert journal.get(intent()["id"]) == original
        backup = journal.recovery_backup
        assert backup is not None and stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert (backup / "database.sqlite3").read_bytes() == before[path.name]
        assert (backup / "database.sqlite3-journal").read_bytes() == before[path.name + "-journal"]
        receipt = json.loads((backup / "receipt.json").read_text())
        for name, expected in receipt["files"].items():
            assert stat.S_IMODE((backup / name).stat().st_mode) == 0o600
            assert "sha256:" + hashlib.sha256((backup / name).read_bytes()).hexdigest() == expected
    assert read(path) == original and not Path(str(path) + "-journal").exists()


def test_failed_backup_prevents_recovery(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(path)
    interrupted_writer(path)
    before = snapshot(path)

    def failure(*args: Any, **kwargs: Any) -> Path:
        raise OSError("controlled backup failure")

    monkeypatch.setattr(handoff_store, "_backup", failure)
    with (
        pytest.raises(execution_store.StoreError, match="cannot open"),
        handoff_store.opened(path, write=True, recover=True),
    ):
        pytest.fail("SQLite recovery proceeded after backup failure")
    assert snapshot(path) == before


@pytest.mark.parametrize("damage", ["schema", "history"])
def test_unrecognized_recovered_inputs_do_not_roll_back_the_original_files(path: Path, damage: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        if damage == "schema":
            connection.execute("CREATE TABLE unrecognized (id INTEGER)")
        else:
            connection.execute("UPDATE handoffs SET head_digest='sha256:changed'")
    interrupted_writer(path)
    rollback = Path(str(path) + "-journal")
    before = path.read_bytes(), rollback.read_bytes()
    with pytest.raises(execution_store.StoreError), handoff_store.opened(path, write=True, recover=True):
        pytest.fail("unrecognized recovered facts were accepted")
    assert (path.read_bytes(), rollback.read_bytes()) == before
    backups = list(path.parent.glob(".handoff-recovery-*"))
    assert len(backups) == 1
    assert (backups[0] / "database.sqlite3").read_bytes() == before[0]
    assert (backups[0] / "database.sqlite3-journal").read_bytes() == before[1]


def test_unrecognized_rollback_bytes_are_not_recovered(path: Path) -> None:
    prepare(path)
    rollback = Path(str(path) + "-journal")
    rollback.write_bytes(b"unrecognized")
    rollback.chmod(0o600)
    before = snapshot(path)
    with (
        pytest.raises(execution_store.StoreError, match="unrecognized"),
        handoff_store.opened(path, write=True, recover=True),
    ):
        pytest.fail("unrecognized rollback sidecar was accepted")
    assert snapshot(path) == before


def test_competing_process_lock_wait_is_bounded_without_writes(path: Path) -> None:
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
        assert process.stdout is not None and process.stdout.readline().strip() == "locked"
        before = snapshot(path)
        with (
            pytest.raises(execution_store.LockTimeoutError),
            handoff_store.opened(path, write=True, deadline=execution_store.Deadline(0)),
        ):
            pytest.fail("competing process acquired held lock")
        assert snapshot(path) == before
    finally:
        process.communicate("x", timeout=5)


@pytest.mark.parametrize("finish", [False, True])
def test_committed_preparation_and_publication_survive_immediate_process_exit(path: Path, finish: bool) -> None:
    prepare(path)
    script = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import handoff_store
with handoff_store.opened(Path(sys.argv[2]),write=True) as journal:
    if sys.argv[4]=='finish':
        journal.finish(sys.argv[3],{'commit':'3'*40,'published':True})
    else:
        retained=journal.get(sys.argv[3])
        journal.prepare(retained['intent'],retained['prepared'])
    os._exit(73)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path(handoff_store.__file__).parent.parent),
            str(path),
            intent()["id"],
            "finish" if finish else "prepare",
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 73
    before = snapshot(path)
    retained = read(path)
    assert retained is not None
    assert retained["phase"] == ("published" if finish else "prepared")
    assert retained["prepared"] == prepared()
    assert snapshot(path) == before

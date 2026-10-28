"""Maintenance diagnoses interrupted writes and recovers copies without changing the source."""

from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
import stat
import tempfile
from functools import cache
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from exacly import completion_store, executions, operation_locks, planning
from exacly import execution_maintenance as maintenance
from exacly import execution_store as store
from exacly.refresh import refresh
from test_admission import READINESS, Source
from test_applicability import WHEN
from test_executions import run
from test_reconciliation import make_plan, selected

Doc = dict[str, Any]
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")


@cache
def _registered(name: str, serial: int, state_name: str, task: str) -> Doc:
    source = Source()
    if name == "first":
        # A large, valid descriptor makes cache spills deterministic without adding fields to the contract.
        source.policy["name"] += "-" + "a" * (512 * 1024)
    state = refresh({source.model_file: source.config}, {}, run(source))
    state.update(lineage="maintenance-fixture-lineage", serial=serial)
    state["x-snapshot"] = state_name
    state["pending"] = [
        {"record": "REQ-1", "task": task, "opened_at": "2026-10-26T08:00:00Z", "dimension": "verification"}
    ]
    plan = make_plan(source)
    request = executions.Registration(
        plan,
        plan["actions"][0]["id"],
        state,
        task,
        "2026-10-26T08:00:00Z",
        READINESS,
        source.policy,
        selected(hashlib.sha1(name.encode()).hexdigest()),
        WHEN,
    )
    with tempfile.TemporaryDirectory(prefix="exacly-maintenance-binding-") as directory:
        return executions.register(Path(directory) / "prototype.sqlite3", request)


def binding(
    name: str = "first",
    *,
    serial: int = 1,
    state: str = "state1",
    supersedes: str | None = None,
    task: str = "issues/1",
) -> Doc:
    result = copy.deepcopy(_registered(name, serial, state, task))
    result["supersedes"] = supersedes
    result["id"] = executions.record_identity(result)
    return executions.validate_record(result)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    return private / "executions.sqlite3"


@pytest.fixture
def output(tmp_path: Path) -> Path:
    private = tmp_path / "exports"
    private.mkdir(mode=0o700)
    return private / "copy"


def snapshot(directory: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in directory.iterdir() if entry.is_file()}


def private_tree(directory: Path) -> None:
    for entry in (directory, *directory.rglob("*")):
        assert stat.S_IMODE(entry.stat().st_mode) == (0o700 if entry.is_dir() else 0o600)


def valid_report(report: Doc, output: Path | None = None) -> None:
    jsonschema.validate(report, maintenance.SCHEMA)
    assert report["digest"] == planning.digest({key: value for key, value in report.items() if key != "digest"})
    assert report["claims"] and all(value is False for value in report["claims"].values())
    if output is not None:
        for item in report["files"]:
            content = (output / item["path"]).read_bytes()
            assert item["digest"] == "sha256:" + hashlib.sha256(content).hexdigest()
            assert item["size"] == len(content)


def committed_history(path: Path) -> tuple[Doc, Doc]:
    first = binding()
    second = binding("second", serial=3, state="state3", supersedes=first["id"])
    store.put(path, first)
    store.put(path, second)
    store.put(path, binding("second", serial=5, state="state5", supersedes=first["id"]))
    return first, second


def interrupted_writer(path: Path, proposed: Doc, phase: str, pipe: Connection) -> None:
    """Pause at a real SQLite commit boundary; the parent kills this process through the pipe protocol."""
    original_connect = sqlite3.connect

    class PausedCommit(sqlite3.Connection):
        def commit(self) -> None:
            if phase == "before_commit":
                # Rewriting then restoring a large history row forces real cache spills even when
                # this store operation only advances a one-page watermark. The final row is unchanged.
                identity, text = self.execute(
                    "SELECT id, record_json FROM bindings ORDER BY length(record_json) DESC LIMIT 1"
                ).fetchone()
                self.execute("UPDATE bindings SET record_json=? WHERE id=?", (text + " ", identity))
                self.execute("UPDATE bindings SET record_json=? WHERE id=?", (text, identity))
                pipe.send("ready")
                pipe.recv()
            super().commit()
            if phase == "after_commit":
                pipe.send("ready")
                pipe.recv()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs, factory=PausedCommit)
        connection.execute("PRAGMA cache_size=1")
        connection.execute("PRAGMA cache_spill=1")
        return connection

    try:
        if phase == "initialization":
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            connection = original_connect(path, isolation_level=None)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA cache_size=1")
            connection.execute("PRAGMA cache_spill=1")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE interrupted_initialization (payload BLOB)")
            connection.execute("INSERT INTO interrupted_initialization VALUES (zeroblob(524288))")
            pipe.send("ready")
            pipe.recv()
        else:
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(sqlite3, "connect", connect)
                store.put(path, proposed)
    except BaseException as error:
        pipe.send({"error": repr(error)})
        raise


def kill_writer(path: Path, proposed: Doc, phase: str = "before_commit") -> None:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_writer, args=(path, proposed, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "writer never reached the selected transaction boundary"
        assert parent.recv() == "ready"
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()


def hot_journal(path: Path, case: str = "supersession") -> tuple[Doc, Doc, Doc]:
    first, second = committed_history(path)
    if case == "registration":
        proposed = binding("third", task="issues/other")
    elif case == "watermark":
        proposed = binding("second", serial=6, state="state6", supersedes=first["id"])
    else:
        proposed = binding("third", serial=6, state="state6", supersedes=second["id"])
    kill_writer(path, proposed)
    journal = Path(str(path) + "-journal")
    assert journal.read_bytes().startswith(JOURNAL_MAGIC), "test must leave a real hot rollback journal"
    return first, second, proposed


def test_inspect_and_backup_a_clean_store_are_read_only_and_preserve_history(ledger: Path, output: Path) -> None:
    first, second = committed_history(ledger)
    before = snapshot(ledger.parent)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "clean"
    valid_report(inspected)
    report = maintenance.backup(ledger, output)
    valid_report(report, output)
    assert snapshot(ledger.parent) == before
    assert (output / "manifest.json").is_file()
    assert json.loads((output / "manifest.json").read_text()) == report
    copied = output / "ledger.sqlite3"
    assert maintenance.inspect(copied)["status"] == "clean"
    assert executions.get(copied, first["id"])["superseded_by"] == second["id"]
    assert executions.get(copied, second["id"])["state_watermark"] == {
        "serial": 5,
        "digest": binding("second", serial=5, state="state5", supersedes=first["id"])["state_snapshot"]["digest"],
    }
    private_tree(output)


@pytest.mark.parametrize("case", ["registration", "supersession", "watermark"])
def test_killed_uncommitted_store_writes_recover_only_to_a_new_copy(
    ledger: Path, output: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, proposed = hot_journal(ledger, case)
    before = snapshot(ledger.parent)
    with monkeypatch.context() as guarded:
        guarded.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("inspection must not open hot SQLite"))
        assert maintenance.inspect(ledger)["status"] == "recovery_required"
    assert snapshot(ledger.parent) == before
    with pytest.raises(ValueError):
        maintenance.backup(ledger, output)
    assert not output.exists()
    report = maintenance.recover(ledger, output)
    valid_report(report, output)
    assert snapshot(ledger.parent) == before
    assert (output / "raw/ledger.sqlite3").read_bytes() == before[ledger.name]
    assert (output / "raw/ledger.sqlite3-journal").read_bytes() == before[ledger.name + "-journal"]
    assert json.loads((output / "manifest.json").read_text()) == report
    recovered = output / "recovered/ledger.sqlite3"
    assert maintenance.inspect(recovered)["status"] == "clean"
    assert executions.get(recovered, first["id"])["superseded_by"] == second["id"]
    old = executions.get(recovered, second["id"])
    assert old["active"] is True
    assert old["state_watermark"] == {
        "serial": 5,
        "digest": binding("second", serial=5, state="state5", supersedes=first["id"])["state_snapshot"]["digest"],
    }
    if case != "watermark":
        with pytest.raises(store.StoreError):
            store.get(recovered, proposed["id"])
    assert store.put(recovered, proposed) == (second if case == "watermark" else proposed)
    assert maintenance.inspect(recovered)["status"] == "clean"
    assert snapshot(ledger.parent) == before
    private_tree(output)


def test_kill_after_commit_before_response_is_clean_and_replays_once(ledger: Path, output: Path) -> None:
    proposed = binding()
    kill_writer(ledger, proposed, "after_commit")
    before = snapshot(ledger.parent)
    assert set(before) == {ledger.name}
    assert maintenance.inspect(ledger)["status"] == "clean"
    assert store.put(ledger, proposed) == proposed
    assert store.get(ledger, proposed["id"])["binding"] == proposed
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT count(*) FROM bindings").fetchone() == (1,)
    maintenance.backup(ledger, output)
    assert store.get(output / "ledger.sqlite3", proposed["id"])["active"] is True


def test_killed_initialization_is_preserved_and_cannot_be_assumed_to_be_a_ledger(ledger: Path, output: Path) -> None:
    kill_writer(ledger, binding(), "initialization")
    before = snapshot(ledger.parent)
    assert len(before[ledger.name]) > 0  # SQLite spilled a partial database before its first commit.
    assert maintenance.inspect(ledger)["status"] == "unsupported"
    with pytest.raises(ValueError):
        maintenance.recover(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()


@pytest.mark.parametrize("case", ["unknown_header", "newer_version"])
def test_unsupported_headers_are_diagnosed_without_sqlite_or_source_changes(
    ledger: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case == "unknown_header":
        ledger.write_bytes(b"unrecognized interrupted content")
        ledger.chmod(0o600)
    else:
        store.put(ledger, binding())
        raw = bytearray(ledger.read_bytes())
        raw[60:64] = (store.MAX_USER_VERSION + 1).to_bytes(4, "big")
        ledger.write_bytes(raw)
    before = snapshot(ledger.parent)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("unknown header must not open SQLite"))
    assert maintenance.inspect(ledger)["status"] == "unsupported"
    assert snapshot(ledger.parent) == before


def test_inspection_validates_stored_records_in_a_known_clean_database(ledger: Path) -> None:
    store.put(ledger, binding())
    with sqlite3.connect(ledger) as connection:
        connection.execute("UPDATE bindings SET record_json='{}'")
    before = snapshot(ledger.parent)
    assert maintenance.inspect(ledger)["status"] == "corrupt"
    assert snapshot(ledger.parent) == before


def tamper_target(path: Path, identity: str, revision: Any) -> None:
    with sqlite3.connect(path) as connection:
        document = json.loads(
            connection.execute("SELECT record_json FROM bindings WHERE id=?", (identity,)).fetchone()[0]
        )
        document["contract"]["target"]["revision"] = revision
        connection.execute("UPDATE bindings SET record_json=? WHERE id=?", (store.canonical(document), identity))


@pytest.mark.parametrize("revision", ["c" * 40, 123], ids=["content_identity_mismatch", "schema_mismatch"])
def test_semantically_invalid_binding_contracts_cannot_be_inspected_or_backed_up_as_clean(
    ledger: Path, output: Path, revision: Any
) -> None:
    original = binding()
    store.put(ledger, original)
    tamper_target(ledger, original["id"], revision)
    assert store.get(ledger, original["id"])["binding"]["contract"]["target"]["revision"] == revision
    before = snapshot(ledger.parent)
    assert maintenance.inspect(ledger)["status"] == "corrupt"
    with pytest.raises(ValueError):
        maintenance.backup(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()


def test_recovery_refuses_a_semantically_invalid_committed_binding(ledger: Path, output: Path) -> None:
    _, current = committed_history(ledger)
    tamper_target(ledger, current["id"], "c" * 40)
    proposed = binding("third", serial=6, state="state6", supersedes=current["id"])
    kill_writer(ledger, proposed)
    assert Path(str(ledger) + "-journal").read_bytes().startswith(JOURNAL_MAGIC)
    before = snapshot(ledger.parent)
    with pytest.raises(ValueError):
        maintenance.recover(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()


@pytest.mark.parametrize("case", ["wal", "shm", "magic", "sector", "page", "superjournal"])
def test_unsupported_recovery_residue_is_preserved_without_guessing(ledger: Path, output: Path, case: str) -> None:
    hot_journal(ledger)
    journal = Path(str(ledger) + "-journal")
    if case in {"wal", "shm"}:
        residue = Path(str(ledger) + "-" + case)
        residue.write_bytes(b"unsupported sidecar")
        residue.chmod(0o600)
    else:
        raw = bytearray(journal.read_bytes())
        if case == "magic":
            raw[:8] = b"\0" * 8
        elif case == "sector":
            raw[20:24] = (3).to_bytes(4, "big")
        elif case == "page":
            raw[24:28] = (3).to_bytes(4, "big")
        else:
            master = b"/operator/other-superjournal"
            raw.extend(master + len(master).to_bytes(4, "big") + sum(master).to_bytes(4, "big") + JOURNAL_MAGIC)
        journal.write_bytes(raw)
    before = snapshot(ledger.parent)
    with pytest.raises(ValueError):
        maintenance.recover(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()


@pytest.mark.parametrize("operation", ["backup", "recover"])
def test_outputs_are_never_overwritten_or_aliased_to_the_source(ledger: Path, output: Path, operation: str) -> None:
    if operation == "recover":
        hot_journal(ledger)
    else:
        store.put(ledger, binding())
    output.mkdir(mode=0o700)
    marker = output / "keep.txt"
    marker.write_bytes(b"existing operator content")
    marker.chmod(0o600)
    before = snapshot(ledger.parent)
    action = maintenance.recover if operation == "recover" else maintenance.backup
    with pytest.raises(ValueError):
        action(ledger, output)
    assert marker.read_bytes() == b"existing operator content"
    with pytest.raises(ValueError):
        action(ledger, ledger.parent)
    assert snapshot(ledger.parent) == before


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("nested", [False, True])
def test_backup_output_cannot_occupy_any_source_sqlite_namespace(ledger: Path, suffix: str, nested: bool) -> None:
    original = binding()
    store.put(ledger, original)
    destination = Path(str(ledger) + suffix)
    if nested:
        destination /= "copy"
    before = snapshot(ledger.parent)
    with pytest.raises(ValueError):
        maintenance.backup(ledger, destination)
    assert snapshot(ledger.parent) == before
    assert store.get(ledger, original["id"])["binding"] == original
    if suffix or nested:
        assert not destination.exists()


@pytest.mark.parametrize("operation", ["backup", "recover"])
def test_final_parent_fsync_failure_preserves_the_complete_published_copy(
    ledger: Path, output: Path, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if operation == "recover":
        _, current, _ = hot_journal(ledger)
    else:
        current = binding()
        store.put(ledger, current)
    before = snapshot(ledger.parent)
    original_fsync = os.fsync
    failures = 0

    def fail_only_after_publication(fd: int) -> None:
        nonlocal failures
        if output.exists():
            failures += 1
            assert (output / "manifest.json").is_file()
            raise OSError("injected final parent fsync failure")
        original_fsync(fd)

    with monkeypatch.context() as patched:
        patched.setattr(os, "fsync", fail_only_after_publication)
        action = maintenance.recover if operation == "recover" else maintenance.backup
        with pytest.raises(store.StoreError, match="copy was published but durability could not be confirmed"):
            action(ledger, output)
    assert failures == 1
    assert snapshot(ledger.parent) == before
    manifest = json.loads((output / "manifest.json").read_text())
    valid_report(manifest, output)
    copied = output / ("recovered/ledger.sqlite3" if operation == "recover" else "ledger.sqlite3")
    assert maintenance.inspect(copied)["status"] == "clean"
    assert store.get(copied, current["id"])["active"] is True
    private_tree(output)


def test_validation_failure_does_not_publish_an_output(ledger: Path, output: Path) -> None:
    store.put(ledger, binding())
    with sqlite3.connect(ledger) as connection:
        connection.execute("UPDATE bindings SET record_json='{}'")
    before = snapshot(ledger.parent)
    with pytest.raises(store.StoreError):
        maintenance.backup(ledger, output)
    assert not output.exists()
    assert list(output.parent.iterdir()) == []
    assert snapshot(ledger.parent) == before


def upgrade_fixture_in_place(path: Path) -> None:
    """Only fixtures use the connection helper directly; the public maintenance operation copies."""
    with store.database(path, create=True) as (connection, _):
        completion_store.upgrade(connection, deadline=store.Deadline())


def test_upgrade_publishes_exact_v1_backup_and_valid_private_v2_copy(ledger: Path, output: Path) -> None:
    first, current = committed_history(ledger)
    before = snapshot(ledger.parent)
    report = maintenance.upgrade(ledger, output)
    valid_report(report, output)
    assert report["operation"] == "upgrade" and report["status"] == "upgraded"
    assert report["observed_store"]["user_version"] == store.USER_VERSION
    assert report["store"]["user_version"] == store.COMPLETION_VERSION
    assert report["validation"] == {"status": "passed", "bindings": 2, "active": 1}
    assert {item["path"] for item in report["files"]} == {"raw/ledger.sqlite3", "upgraded/ledger.sqlite3"}
    assert snapshot(ledger.parent) == before
    assert (output / "raw/ledger.sqlite3").read_bytes() == before[ledger.name]
    upgraded = output / "upgraded/ledger.sqlite3"
    assert int.from_bytes(upgraded.read_bytes()[60:64], "big") == store.COMPLETION_VERSION
    inspected = maintenance.inspect(upgraded)
    assert inspected["status"] == "clean" and inspected["store"]["user_version"] == store.COMPLETION_VERSION
    assert executions.get(upgraded, first["id"])["superseded_by"] == current["id"]
    assert executions.get(upgraded, current["id"]) == executions.get(ledger, current["id"])
    assert json.loads((output / "manifest.json").read_text()) == report
    assert snapshot(ledger.parent) == before
    private_tree(output)


def test_inspect_and_backup_preserve_version_2(ledger: Path, output: Path) -> None:
    proposed = binding()
    store.put(ledger, proposed)
    upgrade_fixture_in_place(ledger)
    before = snapshot(ledger.parent)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "clean"
    assert inspected["store"]["user_version"] == inspected["observed_store"]["user_version"] == store.COMPLETION_VERSION
    valid_report(inspected)
    report = maintenance.backup(ledger, output)
    valid_report(report, output)
    assert report["store"]["user_version"] == report["observed_store"]["user_version"] == store.COMPLETION_VERSION
    copied = output / "ledger.sqlite3"
    assert maintenance.inspect(copied)["status"] == "clean"
    assert executions.get(copied, proposed["id"])["binding"] == proposed
    assert snapshot(ledger.parent) == before
    private_tree(output)


def test_interrupted_version_2_write_recovers_only_a_version_2_copy(ledger: Path, output: Path) -> None:
    first, current = committed_history(ledger)
    upgrade_fixture_in_place(ledger)
    proposed = binding("third", serial=6, state="state6", supersedes=current["id"])
    kill_writer(ledger, proposed)
    before = snapshot(ledger.parent)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "recovery_required"
    assert inspected["store"]["user_version"] == store.COMPLETION_VERSION
    report = maintenance.recover(ledger, output)
    valid_report(report, output)
    assert report["store"]["user_version"] == store.COMPLETION_VERSION
    recovered = output / "recovered/ledger.sqlite3"
    assert executions.get(recovered, first["id"])["superseded_by"] == current["id"]
    assert executions.get(recovered, current["id"])["active"] is True
    with pytest.raises(store.StoreError):
        store.get(recovered, proposed["id"])
    assert snapshot(ledger.parent) == before
    assert (output / "raw/ledger.sqlite3").read_bytes() == before[ledger.name]
    private_tree(output)


def interrupted_completion_prepare(case: Any, control: Connection) -> None:
    """Pause with a real v2 completion intent inserted but its SQLite transaction uncommitted."""
    original_connect = sqlite3.connect

    class PausedPrepare(sqlite3.Connection):
        def commit(self) -> None:
            row = self.execute("SELECT id FROM completion_intents WHERE phase='prepared'").fetchone()
            if row is not None:
                control.send(("ready", row[0]))
                control.recv()
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs, factory=PausedPrepare)
        connection.execute("PRAGMA cache_size=1")
        connection.execute("PRAGMA cache_spill=1")
        return connection

    try:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(sqlite3, "connect", connect)
            control.send(("unexpected_result", case.complete()))
    except BaseException as error:
        control.send(("error", repr(error)))
        raise
    finally:
        control.close()


def test_sigkill_during_v2_completion_prepare_recovers_only_a_clean_copy(tmp_path: Path, output: Path) -> None:
    from test_completion_faults import prepare_case

    case = prepare_case(tmp_path)
    # Keep the execution contract but advance current state and retain a large legal extension so the
    # prepared before/after-state BLOBs force a real spill and hot rollback journal before COMMIT.
    before = json.loads(case.state_path.read_bytes())
    before["serial"] += 1
    before["x-maintenance-payload"] = "a" * (512 * 1024)
    case.state_path.write_text(json.dumps(before) + "\n")
    state_bytes = case.state_path.read_bytes()
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_completion_prepare, args=(case, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "completion prepare never reached its uncommitted SQLite boundary"
        message = parent.recv()
        assert message[0] == "ready", message
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()
    ledger = case.ledger
    journal = Path(str(ledger) + "-journal")
    assert journal.read_bytes().startswith(JOURNAL_MAGIC)
    residue = snapshot(ledger.parent)
    assert case.state_path.read_bytes() == state_bytes
    assert maintenance.inspect(ledger)["status"] == "recovery_required"
    report = maintenance.recover(ledger, output)
    valid_report(report, output)
    assert report["store"]["user_version"] == store.COMPLETION_VERSION
    recovered = output / "recovered/ledger.sqlite3"
    assert maintenance.inspect(recovered)["status"] == "clean"
    with sqlite3.connect(recovered) as connection:
        assert connection.execute("SELECT count(*) FROM completion_intents").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM completion_receipts").fetchone() == (0,)
    assert executions.get(recovered, case.binding["id"])["active"] is True
    assert snapshot(ledger.parent) == residue
    assert case.state_path.read_bytes() == state_bytes
    assert (output / "raw/ledger.sqlite3").read_bytes() == residue[ledger.name]
    assert (output / "raw/ledger.sqlite3-journal").read_bytes() == residue[journal.name]
    private_tree(output)


def test_backup_retains_and_validates_committed_v2_completion_proof(tmp_path: Path, output: Path) -> None:
    from test_completion_faults import prepare_case

    case = prepare_case(tmp_path)
    completed = case.complete()
    assert completed["status"] == "completed", completed
    before = snapshot(case.ledger.parent)
    report = maintenance.backup(case.ledger, output)
    valid_report(report, output)
    assert report["store"]["user_version"] == store.COMPLETION_VERSION
    copied = output / "ledger.sqlite3"
    with (
        store.database(case.ledger, create=False) as (source, _),
        store.database(copied, create=False) as (destination, _created),
    ):
        original = completion_store.get(source, completed["completion_id"])
        retained = completion_store.get(destination, completed["completion_id"])
        assert retained == original and retained["phase"] == "committed"
        assert retained["receipt"] == completed["receipt"]
    assert maintenance.inspect(copied)["status"] == "clean"
    assert snapshot(case.ledger.parent) == before
    private_tree(output)


def test_corrupt_v2_completion_proof_is_never_inspected_or_backed_up_as_clean(tmp_path: Path, output: Path) -> None:
    from test_completion_faults import prepare_case

    case = prepare_case(tmp_path)
    completed = case.complete()
    assert completed["status"] == "completed", completed
    with sqlite3.connect(case.ledger) as connection:
        connection.execute("UPDATE completion_intents SET report=?", (b"forged report bytes",))
    before = snapshot(case.ledger.parent)
    inspected = maintenance.inspect(case.ledger)
    assert inspected["status"] == "corrupt" and inspected["validation"]["status"] == "failed"
    assert inspected["store"]["user_version"] == store.COMPLETION_VERSION
    with pytest.raises(store.StoreError):
        maintenance.backup(case.ledger, output)
    assert snapshot(case.ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


@pytest.mark.parametrize("case", ["hot_journal", "version_2", "invalid_contract"])
def test_upgrade_rejects_unsafe_sources_without_output_or_source_changes(ledger: Path, output: Path, case: str) -> None:
    if case == "hot_journal":
        hot_journal(ledger)
    else:
        original = binding()
        store.put(ledger, original)
        if case == "version_2":
            upgrade_fixture_in_place(ledger)
        else:
            tamper_target(ledger, original["id"], "c" * 40)
    before = snapshot(ledger.parent)
    with pytest.raises(store.StoreError):
        maintenance.upgrade(ledger, output)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_upgrade_failure_does_not_publish_or_modify_the_v1_source(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put(ledger, binding())
    before = snapshot(ledger.parent)

    def fail_after_schema_upgrade(connection: sqlite3.Connection, *, deadline: store.Deadline) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE interrupted_upgrade (value TEXT)")
        raise store.StoreError("injected upgrade failure")

    monkeypatch.setattr(completion_store, "upgrade", fail_after_schema_upgrade)
    with pytest.raises(store.StoreError, match="injected upgrade failure"):
        maintenance.upgrade(ledger, output)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_upgrade_requires_the_copy_to_reach_version_2(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put(ledger, binding())
    before = snapshot(ledger.parent)
    monkeypatch.setattr(completion_store, "upgrade", lambda connection, *, deadline: None)
    with pytest.raises(store.StoreError, match="does not have the completion schema"):
        maintenance.upgrade(ledger, output)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_maintenance_uses_shared_ordered_directory_locks(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposed = binding()
    store.put(ledger, proposed)
    original = operation_locks.directories
    selected: list[tuple[set[Path], bool]] = []

    def recorded(paths: Any, *, write: bool, deadline: store.Deadline) -> Any:
        values = list(paths)
        selected.append((set(values), write))
        return original(values, write=write, deadline=deadline)

    monkeypatch.setattr(operation_locks, "directories", recorded)
    assert maintenance.inspect(ledger)["status"] == "clean"
    assert maintenance.backup(ledger, output)["status"] == "backed_up"
    assert selected == [({ledger.parent}, False), ({ledger.parent, output.parent}, True)]

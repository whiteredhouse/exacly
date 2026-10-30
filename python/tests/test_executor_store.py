from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store, executor_store, planning

Doc = dict[str, Any]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    return private / "attempts.sqlite3"


def intent(name: str = "first", *, attempts: int = 2) -> Doc:
    identity = {
        "schema_version": "0.1.0",
        "task": name,
        "binding": planning.digest(name),
        "adapter": "local-command-v1",
    }
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(identity),
        "identity": identity,
        "contract": {"criteria": "reviewed", "literal": "'; DROP TABLE executor_events; --"},
        "created_at": "2026-10-30T09:00:00Z",
        "max_attempts": attempts,
    }


def prepare(path: Path, proposed: Doc | None = None, *, concurrency: int = 1) -> Doc:
    executor_store.initialize(path, ledger=path.with_name("bindings.sqlite3"), max_concurrency=concurrency)
    with executor_store.opened(path, write=True) as journal:
        return journal.prepare(proposed or intent())


def read(path: Path, identity: str | None = None) -> Doc | None:
    with executor_store.opened(path, write=False) as journal:
        return journal.get(identity or intent()["id"])


def snapshot(path: Path) -> dict[str, bytes | str]:
    return {
        item.name: os.readlink(item) if item.is_symlink() else item.read_bytes() if item.is_file() else "directory"
        for item in path.parent.iterdir()
    }


def test_explicit_initialization_pins_independent_instance_ledger_and_fixed_concurrency(path: Path) -> None:
    ledger = path.with_name("bindings.sqlite3")
    ledger.write_bytes(b"existing operator ledger untouched")
    result = executor_store.initialize(path, ledger=ledger, max_concurrency=2)
    assert result["status"] == "initialized"
    assert result["ledger"] == str(ledger)
    assert result["max_concurrency"] == 2
    assert uuid.UUID(hex=result["journal_id"]).version == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ledger.read_bytes() == b"existing operator ledger untouched"
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError, match="new destination"):
        executor_store.initialize(path, ledger=ledger, max_concurrency=16)
    with executor_store.opened(path, write=False) as journal:
        assert journal.journal_id == result["journal_id"]
        assert journal.ledger == str(ledger)
        assert journal.max_concurrency == 2
    assert snapshot(path) == before
    another = path.with_name("another.sqlite3")
    assert executor_store.initialize(another, ledger=ledger)["journal_id"] != result["journal_id"]


@pytest.mark.parametrize("limit", [True, 0, -1, 17, 1.0, "1"])
def test_initialization_rejects_invalid_global_limits_without_creating_storage(path: Path, limit: Any) -> None:
    with pytest.raises(execution_store.StoreError, match="max_concurrency"):
        executor_store.initialize(path, ledger=path.with_name("ledger"), max_concurrency=limit)
    assert snapshot(path) == {}


@pytest.mark.parametrize("write", [False, True])
def test_ordinary_open_does_not_initialize_storage(path: Path, write: bool) -> None:
    with pytest.raises(execution_store.StoreError), executor_store.opened(path, write=write):
        pytest.fail("missing store was opened")
    assert snapshot(path) == {}


@pytest.mark.parametrize("change", ["limit", "contract", "identity", "extra", "naive_time"])
def test_changed_or_invalid_contract_cannot_reset_budget(path: Path, change: str) -> None:
    original = prepare(path)
    proposed = intent()
    if change == "limit":
        proposed["max_attempts"] = 10
    elif change == "contract":
        proposed["contract"]["criteria"] = "new criteria"
    elif change == "identity":
        proposed["id"] = planning.digest("wrong")
    elif change == "extra":
        proposed["unreviewed"] = True
    else:
        proposed["created_at"] = "2026-10-30T09:00:00"
    before = snapshot(path)
    with executor_store.opened(path, write=True) as journal, pytest.raises(execution_store.StoreError):
        journal.prepare(proposed)
    assert read(path) == original
    assert snapshot(path) == before


@pytest.mark.parametrize("limit", [True, 0, -1, 11, "2", 2.0])
def test_work_attempt_limits_are_strict_bounded_integers(path: Path, limit: Any) -> None:
    executor_store.initialize(path, ledger=path.with_name("ledger"))
    proposed = intent()
    proposed["max_attempts"] = limit
    with (
        executor_store.opened(path, write=True) as journal,
        pytest.raises(execution_store.StoreError, match="max_attempts"),
    ):
        journal.prepare(proposed)
    assert read(path) is None


def test_reservation_consumes_budget_before_launch_and_unresolved_slot_has_no_expiry(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
        reserved = journal.reserve(intent()["id"], {"owner": {"pid": 1}, "expires_at": "2000-01-01T00:00:00Z"})
        assert reserved["phase"] == "reserved"
        assert len(reserved["attempts"]) == 1
        before = snapshot(path)
        with pytest.raises(execution_store.StoreError, match="unresolved"):
            journal.reserve(intent()["id"], {})
        with pytest.raises(execution_store.StoreError, match="concurrency"):
            journal.reserve(intent("second")["id"], {})
        assert snapshot(path) == before
    assert read(path) == reserved


def test_failure_consumes_one_attempt_retry_preserves_history_and_budget_exhaustion_stops(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        initial = journal.reserve(intent()["id"], {"approved_attempt": 1})
        journal.append(intent()["id"], 1, "running", {"namespace": {"pid": 42}})
        first = journal.append(intent()["id"], 1, "failed", {"reason": "command failed"})
        second = journal.reserve(intent()["id"], {"approved_attempt": 2})
        assert second["attempts"][0] == first["attempts"][0]
        assert second["attempts"][1]["events"][0]["previous"] == first["digest"]
        journal.append(intent()["id"], 2, "timed_out", {"reason": "deadline"})
        before = snapshot(path)
        with pytest.raises(execution_store.StoreError, match="budget exhausted"):
            journal.reserve(intent()["id"], {"approved_attempt": 3})
        assert snapshot(path) == before
        assert initial["attempts"][0]["events"][0]["previous"] == planning.digest(intent())


@pytest.mark.parametrize("terminal", sorted(executor_store.TERMINAL))
def test_terminal_receipt_is_immutable_and_exact_replay_has_no_write(path: Path, terminal: str) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.reserve(intent()["id"], {})
        receipt = {"outcome": terminal, "processes_stopped": True}
        result = journal.append(intent()["id"], 1, terminal, receipt)
        before = snapshot(path)
        assert journal.append(intent()["id"], 1, terminal, receipt) == result
        with pytest.raises(execution_store.StoreError, match="immutable"):
            journal.append(intent()["id"], 1, terminal, {"outcome": "changed"})
        with pytest.raises(execution_store.StoreError, match="immutable"):
            journal.append(intent()["id"], 1, "running", {})
        assert snapshot(path) == before


def test_success_cannot_be_repeated_and_releases_capacity_for_another_work_item(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
        journal.reserve(intent()["id"], {})
        journal.append(intent()["id"], 1, "succeeded", {"build": planning.digest("output")})
        with pytest.raises(execution_store.StoreError, match="successful"):
            journal.reserve(intent()["id"], {})
        second = journal.reserve(intent("second")["id"], {})
        assert second["phase"] == "reserved"


def test_reserving_unrelated_work_checks_terminal_metadata_without_decoding_its_output(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
        journal.reserve(intent()["id"], {})
        journal.append(intent()["id"], 1, "succeeded", {"large_terminal_output": "retained"})
    decode = executor_store._decoded

    def selected_only(text: Any) -> Doc:
        assert "large_terminal_output" not in str(text), "unrelated terminal output was decoded"
        return decode(text)

    monkeypatch.setattr(executor_store, "_decoded", selected_only)
    with executor_store.opened(path, write=True) as journal:
        assert journal.reserve(intent("second")["id"], {})["phase"] == "reserved"


def test_unrelated_terminal_phase_metadata_corruption_still_blocks_reservation(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
        journal.reserve(intent()["id"], {})
        journal.append(intent()["id"], 1, "succeeded", {"build": "retained"})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE executor_attempts SET phase='failed'")
    before = snapshot(path)
    with (
        executor_store.opened(path, write=True) as journal,
        pytest.raises(execution_store.StoreError, match="metadata"),
    ):
        journal.reserve(intent("second")["id"], {})
    assert snapshot(path) == before


def test_deleted_unrelated_reservation_cannot_free_an_occupied_slot(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
        journal.reserve(intent()["id"], {"worker": "still possibly running"})
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM executor_attempts WHERE execution_id=?", (intent()["id"],))
    before = snapshot(path)
    with (
        executor_store.opened(path, write=True) as journal,
        pytest.raises(execution_store.StoreError, match="membership"),
    ):
        journal.reserve(intent("second")["id"], {})
    assert snapshot(path) == before


def test_event_limit_reserves_a_terminal_slot_without_losing_prior_facts(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.reserve(intent()["id"], {})
        for count in range(executor_store.MAX_EVENTS - 2):
            journal.append(intent()["id"], 1, "reserved", {"fact": count})
        before = snapshot(path)
        with pytest.raises(execution_store.StoreError, match="history limit"):
            journal.append(intent()["id"], 1, "running", {})
        assert snapshot(path) == before
        terminal = journal.append(intent()["id"], 1, "interrupted", {"reason": "fact limit"})
        assert len(terminal["attempts"][0]["events"]) == executor_store.MAX_EVENTS


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE executor_intents SET intent_digest='sha256:wrong'",
        "UPDATE executor_intents SET attempt_count=0",
        "UPDATE executor_intents SET head_digest='sha256:wrong'",
        "DELETE FROM executor_attempts",
        "UPDATE executor_attempts SET head_digest='sha256:wrong'",
        "UPDATE executor_attempts SET phase='succeeded'",
        "UPDATE executor_attempts SET sequence=2",
        "DELETE FROM executor_events",
        "UPDATE executor_events SET data_json='{\"owner\":2}'",
        'UPDATE executor_events SET data_json=\'{"owner":1,"owner":2}\'',
        "UPDATE executor_events SET phase='running'",
        "UPDATE executor_events SET at='invalid'",
        "UPDATE executor_events SET previous='sha256:wrong'",
        "UPDATE executor_events SET digest='sha256:wrong'",
    ],
)
def test_selected_history_corruption_is_not_rewritten(path: Path, statement: str) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.reserve(intent()["id"], {})
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
        "CREATE VIEW extra AS SELECT id FROM executor_intents",
        "CREATE INDEX extra ON executor_attempts(phase)",
        "PRAGMA application_id=1",
        "PRAGMA user_version=99",
        "DELETE FROM executor_journal WHERE key='journal_id'",
        "UPDATE executor_journal SET value='17' WHERE key='max_concurrency'",
        "UPDATE executor_journal SET value='01' WHERE key='max_concurrency'",
        "UPDATE executor_journal SET value='relative/path' WHERE key='ledger'",
    ],
)
def test_unknown_schema_or_metadata_is_rejected_without_repair(path: Path, statement: str) -> None:
    prepare(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError), executor_store.opened(path, write=True, recover=True):
        pytest.fail("unknown format accepted")
    assert snapshot(path) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode", "parent"])
def test_unsafe_storage_is_rejected(path: Path, damage: str) -> None:
    prepare(path)
    if damage == "symlink":
        target = path.with_name("original")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        os.link(path, path.with_name("other"))
    elif damage == "mode":
        path.chmod(0o644)
    else:
        path.parent.chmod(0o755)
    before = snapshot(path)
    with pytest.raises(execution_store.StoreError):
        read(path)
    assert snapshot(path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_unsupported_wal_sidecars_are_never_recovered(path: Path, suffix: str) -> None:
    prepare(path)
    Path(str(path) + suffix).write_bytes(b"unsupported")
    before = snapshot(path)
    for recover in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="WAL"),
            executor_store.opened(path, write=True, recover=recover),
        ):
            pytest.fail("unsupported sidecar accepted")
    assert snapshot(path) == before


def test_read_only_inspection_and_caller_mutation_cannot_change_history(path: Path) -> None:
    original = intent()
    prepare(path, original)
    with executor_store.opened(path, write=True) as journal:
        result = journal.reserve(original["id"], {"owner": {"pid": 42}})
    expected = copy.deepcopy(result)
    result["attempts"][0]["events"][0]["data"]["owner"]["pid"] = 99
    original["contract"]["criteria"] = "mutated"
    before = snapshot(path)
    with executor_store.opened(path, write=False) as journal:
        assert journal.get(intent()["id"]) == expected
        actions: tuple[Callable[[], Doc], ...] = (
            lambda: journal.prepare(intent()),
            lambda: journal.reserve(intent()["id"], {}),
            lambda: journal.append(intent()["id"], 1, "failed", {}),
        )
        for action in actions:
            with pytest.raises(execution_store.StoreError, match="read-only"):
                action()
    assert snapshot(path) == before


def test_interrupted_sqlite_writer_requires_explicit_exact_backup_before_recovery(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        expected = journal.reserve(intent()["id"], {"owner": "retained"})
    script = """
import sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE executor_events SET data_json=?',('{"owner":"uncommitted"}',))
connection.execute('UPDATE executor_intents SET intent_json=?',('uncommitted'*100000,))
print('written',flush=True)
sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "written"
        child.kill()
        _, error = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL and not error
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    sidecar = Path(str(path) + "-journal")
    assert sidecar.exists()
    assert sidecar.read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
    before = snapshot(path)
    for write in (False, True):
        with (
            pytest.raises(execution_store.StoreError, match="explicit executor recovery"),
            executor_store.opened(path, write=write),
        ):
            pytest.fail("ordinary open recovered interrupted transaction")
        assert snapshot(path) == before
    with executor_store.opened(path, write=True, recover=True) as journal:
        assert journal.get(intent()["id"]) == expected
        backup = journal.recovery_backup
        assert backup is not None
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert (backup / "database.sqlite3").read_bytes() == before[path.name]
        assert (backup / "database.sqlite3-journal").read_bytes() == before[sidecar.name]
        receipt = json.loads((backup / "receipt.json").read_text())
        for name, digest in receipt["files"].items():
            assert digest == "sha256:" + hashlib.sha256((backup / name).read_bytes()).hexdigest()
    assert not sidecar.exists()
    assert read(path) == expected


def test_independent_processes_cannot_overbook_fixed_coordinator_capacity(path: Path) -> None:
    prepare(path)
    with executor_store.opened(path, write=True) as journal:
        journal.prepare(intent("second"))
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from exacly import executor_store,execution_store
sys.stdin.read(1)
try:
 with executor_store.opened(Path(sys.argv[2]),write=True) as journal:
  result=journal.reserve(sys.argv[3],{'owner':sys.argv[3]})
 print('reserved',flush=True)
except execution_store.StoreError as error:
 print(str(error),flush=True)
"""
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(Path(executor_store.__file__).parent.parent),
                str(path),
                proposed["id"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for proposed in (intent(), intent("second"))
    ]
    try:
        for child in children:
            assert child.stdin is not None
            child.stdin.write("x")
            child.stdin.flush()
        results = [child.communicate(timeout=10) for child in children]
        assert sorted(output.strip() for output, _ in results) == ["executor concurrency limit reached", "reserved"]
        assert all(child.returncode == 0 and not error for child, (_, error) in zip(children, results, strict=True))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    first, second = read(path), read(path, intent("second")["id"])
    assert first is not None and second is not None
    assert len(first["attempts"]) + len(second["attempts"]) == 1

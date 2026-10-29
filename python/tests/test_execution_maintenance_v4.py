"""Operational indexed maintenance always audits bytes and proofs afresh on preserved copies."""

from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from exacly import execution_maintenance as maintenance
from exacly import execution_store as store
from exacly import planning
from ledger_history_fixtures import build
from test_execution_maintenance import JOURNAL_MAGIC, private_tree, snapshot

Doc = dict[str, Any]


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    from exacly import execution_index

    root = tmp_path / "history"
    root.mkdir(mode=0o700)
    path = build(root, 1, cases=2).ledger
    with store.database(path, create=True) as (connection, _):
        store.configure(connection)
        connection.execute("BEGIN IMMEDIATE")
        execution_index.initialize(connection)
        connection.commit()
    return path


@pytest.fixture
def output(tmp_path: Path) -> Path:
    parent = tmp_path / "exports"
    parent.mkdir(mode=0o700)
    return parent / "copy"


def valid(report: Doc, output: Path | None = None) -> None:
    jsonschema.validate(report, maintenance.SCHEMA_V4)
    assert report["schema_version"] == "0.3.0" and report["store"]["user_version"] == 4
    assert report["digest"] == planning.digest({key: value for key, value in report.items() if key != "digest"})
    assert report["claims"] and all(value is False for value in report["claims"].values())
    validation = report["validation"]
    assert validation["all_retained_bytes_checked"] is True
    assert validation["integrity_baseline_checked"] is True
    assert validation["semantics"] == {"bindings": 1, "fresh": 1, "reused": 0, "prepared": 0}
    assert validation["profile_status"] in {"current", "known_stale"}
    assert validation["facts"]["profile"] and validation["facts"]["retained_profile"]
    if output is not None:
        assert json.loads((output / "manifest.json").read_bytes()) == report
        for item in report["files"]:
            content = (output / item["path"]).read_bytes()
            assert item["digest"] == "sha256:" + hashlib.sha256(content).hexdigest()
            assert item["size"] == len(content)


def test_v4_inspection_and_backup_use_fresh_audit_and_keep_source_bytes(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exacly import execution_index

    original = execution_index.audit
    calls: list[bool] = []

    def audited(connection: sqlite3.Connection, **options: Any) -> Doc:
        calls.append(connection.in_transaction)
        return original(connection, **options)

    monkeypatch.setattr(execution_index, "audit", audited)
    before = snapshot(ledger.parent)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "clean"
    valid(inspected)
    report = maintenance.backup(ledger, output)
    assert report["status"] == "backed_up"
    valid(report, output)
    assert calls == [True, True, True], "inspection and both backup sides must audit coherent fresh snapshots"
    assert snapshot(ledger.parent) == before
    assert maintenance.inspect(output / "ledger.sqlite3")["status"] == "clean"
    private_tree(output)


def test_known_stale_v4_profile_is_audited_fresh_and_copied_without_refresh(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exacly import execution_index

    installed = copy.deepcopy(execution_index.profile())
    installed["runtime"]["python"] += ".maintenance-profile-change"
    installed["digest"] = planning.digest({key: value for key, value in installed.items() if key != "digest"})
    before = snapshot(ledger.parent)
    monkeypatch.setattr(execution_index, "profile", lambda: installed)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "clean" and inspected["validation"]["profile_status"] == "known_stale"
    valid(inspected)
    report = maintenance.backup(ledger, output)
    valid(report, output)
    assert report["validation"]["profile_status"] == "known_stale"
    assert report["validation"]["facts"]["profile"] == installed
    assert report["validation"]["facts"]["retained_profile"] != installed
    with (
        sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as source,
        sqlite3.connect((output / "ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as copied,
    ):
        query = "SELECT profile_json FROM history_profile"
        assert copied.execute(query).fetchall() == source.execute(query).fetchall()
    assert snapshot(ledger.parent) == before


def test_unknown_v4_profile_is_unsupported_and_not_repaired(ledger: Path, output: Path) -> None:
    with sqlite3.connect(ledger) as connection:
        profile = json.loads(connection.execute("SELECT profile_json FROM history_profile").fetchone()[0])
        profile["family"] = "unsupported-future-family"
        profile["digest"] = planning.digest({key: value for key, value in profile.items() if key != "digest"})
        connection.execute("UPDATE history_profile SET profile_json=?", (store.canonical(profile),))
    before = snapshot(ledger.parent)
    report = maintenance.inspect(ledger)
    assert report["status"] == "unsupported" and report["validation"]["profile_status"] == "unsupported"
    jsonschema.validate(report, maintenance.SCHEMA_V4)
    with pytest.raises(store.StoreError):
        maintenance.backup(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()


def interrupted_write(path: Path, control: Connection) -> None:
    try:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA synchronous=EXTRA")
        connection.execute("PRAGMA cache_size=1")
        connection.execute("PRAGMA cache_spill=1")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE completion_intents SET report=?", (b"uncommitted proof" * (64 * 1024),))
        control.send("ready")
        control.recv()
    except BaseException as error:
        control.send(("error", repr(error)))
        raise


def leave_hot_journal(path: Path) -> None:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=interrupted_write, args=(path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "writer did not reach its real spilled transaction"
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
    assert Path(str(path) + "-journal").read_bytes().startswith(JOURNAL_MAGIC)


@pytest.mark.parametrize("baseline_mismatch", [False, True])
def test_v4_recovery_checks_the_actual_durable_baseline_without_resealing(
    ledger: Path, output: Path, baseline_mismatch: bool
) -> None:
    if baseline_mismatch:
        with sqlite3.connect(ledger) as connection:
            connection.execute("UPDATE completion_intents SET report=?", (b"committed changed proof",))
    leave_hot_journal(ledger)
    before = snapshot(ledger.parent)
    inspected = maintenance.inspect(ledger)
    assert inspected["status"] == "recovery_required"
    assert inspected["validation"]["all_retained_bytes_checked"] is False
    if baseline_mismatch:
        with pytest.raises(store.StoreError):
            maintenance.recover(ledger, output)
        assert not output.exists() and list(output.parent.iterdir()) == []
    else:
        report = maintenance.recover(ledger, output)
        assert report["status"] == "recovered"
        valid(report, output)
        assert maintenance.inspect(output / "recovered/ledger.sqlite3")["status"] == "clean"
        assert (output / "raw/ledger.sqlite3").read_bytes() == before[ledger.name]
        assert (output / "raw/ledger.sqlite3-journal").read_bytes() == before[ledger.name + "-journal"]
        private_tree(output)
    assert snapshot(ledger.parent) == before


def test_ordinary_maintenance_keeps_v3_audit_only_without_opening_sqlite(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = bytearray(ledger.read_bytes())
    raw[60:64] = (3).to_bytes(4, "big")
    ledger.write_bytes(raw)
    before = snapshot(ledger.parent)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **options: pytest.fail("v3 maintenance must not open SQLite"))
    report = maintenance.inspect(ledger)
    assert report["status"] == "unsupported" and report["schema_version"] == "0.2.0"
    jsonschema.validate(report, maintenance.LEGACY_SCHEMA)
    with pytest.raises(store.StoreError):
        maintenance.backup(ledger, output)
    assert snapshot(ledger.parent) == before and not output.exists()
